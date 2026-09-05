import hashlib
import json
from pathlib import Path
import unittest
from contextlib import ExitStack
from unittest.mock import patch
import test_native
import test_pve_drive
import pve_drive as p


class RecoveryTests(unittest.TestCase):
    def prepare(self, native=True):
        case = test_native.NativeTests() if native else test_pve_drive.LifecycleTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        stack = ExitStack()
        self.addCleanup(stack.close)
        if native:
            stack.enter_context(case.harness())
            case.fail_check = True
        else:
            stack.enter_context(patch.object(p, 'run', case.command))
            stack.enter_context(patch.object(case.m, 'api', case.api))
            case.failure = 'check'
        calls = []
        original = case.rc
        def remote(*args, **kwargs):
            a = list(map(str, args)); calls.append(a)
            if a[0] == 'copy':
                for f in Path(a[1]).iterdir():
                    case.remote[a[2] + '/' + f.name] = f.read_bytes()
                return ''
            if a[0] == 'copyto':
                case.remote[a[2]] = Path(a[1]).read_bytes()
                return ''
            if a[0] == 'cat':
                return case.remote[a[1]].decode()
            if a[0] == 'lsjson' and '--hash' in a:
                return json.dumps([{'Path': k[len(a[1])+1:], 'Size': len(data),
                                    'Hashes': {'md5': hashlib.md5(data).hexdigest()}}
                                   for k, data in case.remote.items() if k.startswith(a[1] + '/')])
            return original(*args, **kwargs)
        stack.enter_context(patch.object(case.m, 'rc', remote))
        with self.assertRaises((ValueError, RuntimeError)):
            case.m.archive()
        stage = next(Path(case.args.work_dir).rglob('manifest.json')).parent.parent
        case.args.resume = str(stage)
        case.args.deep_verify = False
        case.args.cleanup_local = False
        calls.clear(); case.commands.clear()
        return case, stage, calls

    def test_recovers_both_formats_without_reupload_or_deletion(self):
        for native in (True, False):
            with self.subTest(native=native):
                case, stage, calls = self.prepare(native)
                case.m.recover()
                self.assertTrue(stage.exists())
                self.assertTrue(any(k.endswith('/COMPLETE') for k in case.remote))
                self.assertEqual([a[0] for a in calls], ['lsjson', 'copyto', 'cat'])
                self.assertTrue(calls[1][1].endswith('COMPLETE'))
                self.assertFalse(any(c[:2] == ['qm', 'destroy'] for c in case.commands))
                self.assertIn(['qm', 'unlock', '100'], case.commands)

    def test_missing_cloud_file_keeps_vm_locked(self):
        case, stage, calls = self.prepare()
        del case.remote[next(k for k in case.remote if k.endswith('.qcow2'))]
        with self.assertRaisesRegex(ValueError, 'listing differs'):
            case.m.recover()
        self.assertFalse(any(k.endswith('/COMPLETE') for k in case.remote))
        self.assertNotIn(['qm', 'unlock', '100'], case.commands)

    def test_corrupt_local_disk_fails_before_cloud(self):
        case, stage, calls = self.prepare()
        (stage / 'payload/scsi0.qcow2').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'SHA-256/size mismatch'):
            case.m.recover()
        self.assertEqual(calls, [])

    def test_changed_snapshot_config_is_rejected(self):
        case, stage, calls = self.prepare()
        conf = case.file('100')
        conf.write_text(conf.read_text().replace('memory: 24576', 'memory: 8192'))
        with self.assertRaises(ValueError):
            case.m.recover()
        self.assertEqual(calls, [])

    def test_existing_valid_marker_can_be_retried(self):
        case, stage, calls = self.prepare()
        key = next(k for k in case.remote if k.endswith('/manifest.json'))
        case.remote[key.rsplit('/', 1)[0] + '/COMPLETE'] = (p.sha256(stage / 'payload/manifest.json') + '\n').encode()
        case.m.recover()
        self.assertIn(['qm', 'unlock', '100'], case.commands)

    def test_wrong_existing_marker_is_rejected(self):
        case, stage, calls = self.prepare()
        key = next(k for k in case.remote if k.endswith('/manifest.json'))
        case.remote[key.rsplit('/', 1)[0] + '/COMPLETE'] = b'wrong\n'
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            case.m.recover()
        self.assertNotIn(['qm', 'unlock', '100'], case.commands)

    def test_marker_upload_failure_retains_lock_and_staging(self):
        case, stage, calls = self.prepare()
        original = case.m.rc
        def fail(*args, **kwargs):
            if args[0] == 'copyto':
                raise RuntimeError('marker upload failed')
            return original(*args, **kwargs)
        with patch.object(case.m, 'rc', fail), self.assertRaisesRegex(RuntimeError, 'marker upload'):
            case.m.recover()
        self.assertTrue(stage.exists())
        self.assertNotIn(['qm', 'unlock', '100'], case.commands)

    def test_wrong_source_and_wrong_node_rejected(self):
        for field in ('source', 'source_node'):
            case, stage, calls = self.prepare()
            path = stage / 'payload/manifest.json'
            manifest = json.loads(path.read_text()); manifest[field] = 'another-node'
            path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                case.m.recover()
            self.assertEqual(calls, [])

    def test_help_defaults_preserve_vm_and_remove_local_files(self):
        args = p.parser().parse_args(['--remote', 'gdrive:archive', '--source', 'example',
                                      'recover', '100', '--resume', '/example'])
        self.assertTrue(args.cleanup_local)
        self.assertFalse(hasattr(args, 'delete_vm'))

    def test_success_removes_staging_when_cleanup_enabled(self):
        case, stage, calls = self.prepare()
        case.args.cleanup_local = True
        case.m.recover()
        self.assertFalse(stage.exists())
        self.assertTrue(case.file('100').exists())
        self.assertTrue(any(k.endswith('/COMPLETE') for k in case.remote))

    def test_failure_preserves_data_with_cleanup_enabled(self):
        case, stage, calls = self.prepare()
        case.args.cleanup_local = True
        del case.remote[next(k for k in case.remote if k.endswith('.qcow2'))]
        with self.assertRaises(ValueError):
            case.m.recover()
        self.assertTrue((stage / 'payload/scsi0.qcow2').exists())
        self.assertNotIn(['qm', 'unlock', '100'], case.commands)

    def test_keep_local_and_old_cleanup_option(self):
        base = ['--remote', 'gdrive:archive', '--source', 'example',
                'recover', '100', '--resume', '/example']
        self.assertFalse(p.parser().parse_args(base + ['--keep-local']).cleanup_local)
        self.assertTrue(p.parser().parse_args(base + ['--cleanup-local']).cleanup_local)

    def test_space_failure_removes_only_empty_staging(self):
        from types import SimpleNamespace
        case, stage, calls = self.prepare()
        empty = stage.parent / 'empty-staging'
        empty.mkdir()
        with patch.object(p.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaises(ValueError):
                case.m.require_staging_space(empty, 1024, 'No space')
            self.assertFalse(empty.exists())
            with self.assertRaises(ValueError):
                case.m.require_staging_space(stage, 1024, 'No space')
            self.assertTrue((stage / 'payload/scsi0.qcow2').exists())
