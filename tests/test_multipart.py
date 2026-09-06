"""Multipart transport fault tests; tiny parts exercise the same streaming paths."""
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import pve_drive as p
import test_native


class MultipartTests(unittest.TestCase):
    def setUp(self):
        self.case = test_native.NativeTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.c = self.case
        self.m = self.c.m
        self.a = self.c.args
        self.a.single_file = False
        self.a.part_size = 7
        self.a.quota_retries = 0
        self.a.cleanup_local = False
        self.a.deep_verify = False
        self.calls = []
        self.fail = None
        self.download_count = 0
        self.uploaded = []

    def rc(self, *args, capture=False):
        a = list(map(str, args))
        self.calls.append(a)
        if a[0] == 'copy':
            if a[1].startswith(self.m.base):
                selection = Path(a[a.index('--files-from-raw') + 1]).read_text().splitlines()
                for name in selection:
                    if self.fail == 'download' and self.download_count == 1:
                        raise RuntimeError('interrupted restore')
                    key = a[1] + '/' + name
                    if key not in self.c.remote:
                        raise RuntimeError('missing remote part')
                    (Path(a[2]) / name).write_bytes(self.c.remote[key])
                    self.download_count += 1
            else:
                for path in sorted(Path(a[1]).iterdir()):
                    if path.name == 'manifest.json' and '--exclude' in a:
                        continue
                    if self.fail == 'upload' and len(self.uploaded) == 1:
                        raise RuntimeError('interrupted upload')
                    key = a[2] + '/' + path.name
                    data = path.read_bytes()
                    if key in self.c.remote:
                        if self.c.remote[key] != data:
                            raise RuntimeError('immutable conflict')
                        continue
                    self.c.remote[key] = data
                    self.uploaded.append(key)
            return ''
        if a[0] == 'copyto':
            if self.fail == 'marker' and a[2].endswith('/COMPLETE'):
                raise RuntimeError('interrupted publication')
            if a[1] in self.c.remote:
                Path(a[2]).write_bytes(self.c.remote[a[1]])
            else:
                self.c.remote[a[2]] = Path(a[1]).read_bytes()
            return ''
        if a[0] == 'lsjson':
            return json.dumps([{'Path': k[len(a[1]) + 1:], 'Size': len(v),
                                'Hashes': {'md5': hashlib.md5(v).hexdigest()}}
                               for k, v in self.c.remote.items() if k.startswith(a[1] + '/')])
        if a[0] == 'purge':
            self.c.remote = {k: v for k, v in self.c.remote.items() if not k.startswith(a[1] + '/')}
            return ''
        if a[0] == 'cat':
            return self.c.remote[a[1]].decode()
        return self.c.rc(*args, capture=capture)

    def harness(self):
        stack = self.c.harness()
        stack.enter_context(patch.object(self.m, 'rc', self.rc))
        return stack

    def archive(self, keep=True):
        self.a.delete_vm = not keep
        self.a.delete_vm = not keep
        self.m.archive()
        return json.loads(next(v for k, v in self.c.remote.items() if k.endswith('/manifest.json')))

    def stage(self):
        return next(Path(self.a.work_dir).glob('native-100-*'))

    def prepare_restore(self, manifest):
        self.a.command = 'restore'
        self.a.resume = None
        self.a.vmid = '200'
        self.a.backup_id = manifest['backup_id']
        self.a.storage = 'local'
        self.a.unique = True

    def test_creation_order_sizes_hashes_and_publication(self):
        with self.harness():
            m = self.archive(keep=False)
        self.assertEqual(m['schema'], 4)
        self.assertEqual(m['transport'], {'format': 'pve-drive-parts', 'version': 1})
        d = m['disks'][0]
        self.assertEqual(d['original_filename'], self.c.disk.name)
        expected = self.c.disk.read_bytes()
        self.assertEqual(d['sha256'], hashlib.sha256(expected).hexdigest())
        parts = [self.c.remote[self.m.base + '/' + m['backup_id'] + '/' + x['filename']] for x in d['parts']]
        self.assertEqual(b''.join(parts), expected)
        for index, (data, part) in enumerate(zip(parts, d['parts'])):
            self.assertEqual(part['filename'], f'scsi0.qcow2.part-{index:06d}')
            self.assertEqual(part['size'], len(data))
            self.assertEqual(part['sha256'], hashlib.sha256(data).hexdigest())
        transfer = next(a for a in self.calls if a[0] == 'copy')
        self.assertEqual(transfer[transfer.index('--transfers') + 1], '8')
        self.assertEqual(transfer[transfer.index('--drive-chunk-size') + 1], '128M')
        self.assertIn('--drive-stop-on-upload-limit', transfer)
        self.assertFalse(self.c.file('100').exists())
        self.assertEqual(self.calls[-1][1].split('/')[-1], 'COMPLETE')

    def test_preupload_staging_is_verified_once_with_both_hashes(self):
        with self.harness(), patch.object(p, 'check_parts', wraps=p.check_parts) as checks:
            self.archive()
        self.assertEqual(checks.call_count, 1)
        self.assertTrue(checks.call_args.kwargs['collect_md5'])

    def test_corrupt_staging_after_split_never_uploads_or_deletes(self):
        original = p.split_native
        def corrupt(*args, **kwargs):
            record = original(*args, **kwargs)
            path = args[1] / record['parts'][0]['filename']
            data = path.read_bytes()
            path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
            return record
        with self.harness(), patch.object(p, 'split_native', side_effect=corrupt), self.assertRaisesRegex(ValueError, 'checksum'):
            self.archive(keep=False)
        self.assertFalse(any(c[0] == 'copy' for c in self.calls))
        self.assertTrue(self.c.file('100').exists())

    @unittest.skipUnless(shutil.which('rclone'), 'rclone unavailable')
    def test_real_rclone_multipart_lifecycle(self):
        self.a.remote = 'gdrive:' + str(self.c.root / 'remote')
        config = self.c.root / 'rclone.conf'
        config.write_text('[gdrive]\ntype = local\n')
        self.a.rclone_config = str(config)
        self.m = p.Manager(self.a)
        actual_run = p.run
        def run(*args, **kwargs):
            if args[0] == 'rclone':
                return actual_run(*args, **kwargs)
            return self.c.fake_run(*args, **kwargs)
        with patch.object(p, 'run', run), patch.object(self.m, 'api', self.c.api), \
                patch.object(self.m, 'config_file', self.c.file):
            self.a.delete_vm = False
            self.m.archive()
            manifest = json.loads((self.stage() / 'payload/manifest.json').read_text())
            # Retry after publication with a pre-existing remote marker and a
            # still-locked source, using the real download-based verifier.
            self.c.fake_run('qm', 'set', '100', '--lock', 'backup')
            self.a.resume = str(self.stage())
            self.a.deep_verify = True
            self.m.archive()
            self.prepare_restore(manifest)
            self.m.restore()
        self.assertEqual((self.c.root / 'vm-200-disk-0.qcow2').read_bytes(), self.c.disk.read_bytes())

    def test_manifest_rejects_bad_order_missing_duplicate_size_and_checksum(self):
        with self.harness():
            original = self.archive()
        mutations = [lambda d: d['parts'].reverse(), lambda d: d['parts'].pop(),
                     lambda d: d['parts'].__setitem__(1, d['parts'][0]),
                     lambda d: d['parts'][0].update(size=6),
                     lambda d: d['parts'][0].update(sha256='bad'),
                     lambda d: d['parts'][0].update(filename='../escape')]
        for mutate in mutations:
            m = copy.deepcopy(original)
            mutate(m['disks'][0])
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.m.validate_manifest(m, m['backup_id'])
        for schema in (5,):
            m = dict(original, schema=schema)
            with self.assertRaises(ValueError):
                self.m.validate_manifest(m, m['backup_id'])
        m = dict(original, transport={'format': 'pve-drive-parts', 'version': 2})
        with self.assertRaises(ValueError):
            self.m.validate_manifest(m, m['backup_id'])

    def test_reconstruction_and_snapshot_configuration_restore(self):
        with self.harness():
            m = self.archive()
            self.prepare_restore(m)
            self.m.restore()
        self.assertEqual((self.c.root / 'vm-200-disk-0.qcow2').read_bytes(), self.c.disk.read_bytes())
        sections = p.parse_native_config(self.c.file('200').read_text())
        self.assertEqual(set(sections), {'current', 'before-change'})
        self.assertEqual(sections['current']['parent'], 'before-change')
        self.assertEqual(sections['before-change']['scsi0'], 'local:200/vm-200-disk-0.qcow2,size=64G')
        self.assertFalse(any('convert' in c for c in self.c.commands))
        download = next(a for a in self.calls if a[0] == 'copy' and a[1].startswith(self.m.base))
        self.assertEqual(download[download.index('--transfers') + 1], '8')

    def test_corrupted_part_fails_before_vm_creation(self):
        with self.harness():
            m = self.archive()
            key = next(k for k in self.c.remote if '.part-' in k)
            self.c.remote[key] = b'X' * len(self.c.remote[key])
            self.prepare_restore(m)
            with self.assertRaisesRegex(ValueError, 'Part checksum'):
                self.m.restore()
        self.assertFalse(self.c.file('200').exists())

    def test_missing_part_fails_before_vm_creation(self):
        with self.harness():
            m = self.archive()
            del self.c.remote[next(k for k in self.c.remote if '.part-' in k)]
            self.prepare_restore(m)
            with self.assertRaisesRegex(RuntimeError, 'missing remote part'):
                self.m.restore()
        self.assertFalse(self.c.file('200').exists())

    def test_whole_checksum_failure_with_valid_parts(self):
        with self.harness():
            m = self.archive()
            self.prepare_restore(m)
            m['disks'][0]['sha256'] = '0' * 64
            with self.assertRaisesRegex(ValueError, 'Whole-file'):
                self.m.download_native(self.m.base + '/' + m['backup_id'], m)
        self.assertFalse(self.c.file('200').exists())

    def test_interrupted_upload_resumes_same_command_without_resending_completed_parts(self):
        self.fail = 'upload'
        with self.harness():
            with self.assertRaisesRegex(RuntimeError, 'interrupted upload'):
                self.archive()
            first = self.uploaded[0]
            self.assertFalse(any(k.endswith('/COMPLETE') for k in self.c.remote))
            self.assertTrue(self.c.file('100').exists())
            self.fail = None
            m = self.archive()
        self.assertEqual(self.uploaded.count(first), 1)
        self.assertEqual(len(list(Path(self.a.work_dir).glob('native-100-*'))), 1)
        self.assertEqual(self.stage().joinpath('UPLOAD_DONE').read_text().strip(), m['backup_id'])

    def test_interrupted_marker_resumes_even_with_local_complete(self):
        self.fail = 'marker'
        with self.harness():
            with self.assertRaises(RuntimeError):
                self.archive()
            self.assertTrue((self.stage() / 'COMPLETE').exists())
            self.fail = None
            self.archive()
        self.assertTrue(any(k.endswith('/COMPLETE') for k in self.c.remote))

    def test_quota_wait_and_retry_then_success(self):
        self.a.quota_retries = 2
        self.a.quota_retry_delay = 3600
        count = 0
        original = self.rc
        def limited(*args, **kwargs):
            nonlocal count
            if args[0] == 'copy' and count < 2:
                count += 1
                raise RuntimeError('googleapi userRateLimitExceeded: upload limit')
            return original(*args, **kwargs)
        with self.harness(), patch.object(self.m, 'rc', limited), patch.object(p.time, 'sleep') as sleep:
            self.archive()
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(sleep.call_args.args, (3600,))

    def test_quota_exhaustion_and_interrupt_preserve_source_and_attempt(self):
        self.a.quota_retries = 1
        original = self.rc
        def limited(*args, **kwargs):
            if args[0] == 'copy':
                raise RuntimeError('upload limit')
            return original(*args, **kwargs)
        with self.harness(), patch.object(self.m, 'rc', limited), patch.object(p.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'quota still blocked'):
                self.archive(keep=False)
        self.assertTrue(self.c.file('100').exists())
        self.assertTrue((self.stage() / 'payload/manifest.json').exists())
        self.assertFalse(any(k.endswith('/COMPLETE') for k in self.c.remote))
        with patch.object(p.time, 'sleep', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.m.quota_retry(lambda: (_ for _ in ()).throw(RuntimeError('upload limit')))

    def test_source_changes_while_quota_waiting_prevent_publication(self):
        self.a.quota_retries = 1
        calls = 0
        original = self.rc
        def quota(*args, **kwargs):
            nonlocal calls
            if args[0] == 'copy' and calls == 0:
                calls += 1
                raise RuntimeError('upload limit')
            return original(*args, **kwargs)
        def changed(delay):
            self.c.disk.write_bytes(b'changed while blocked')
        with self.harness(), patch.object(self.m, 'rc', quota), patch.object(p.time, 'sleep', changed):
            with self.assertRaisesRegex(ValueError, 'Original disk changed'):
                self.archive(keep=False)
        self.assertTrue(self.c.file('100').exists())
        self.assertFalse(any(k.endswith('/COMPLETE') for k in self.c.remote))

    def test_resume_rejects_changed_source_before_upload(self):
        self.fail = 'upload'
        with self.harness():
            with self.assertRaises(RuntimeError):
                self.archive()
            self.c.disk.write_bytes(b'changed')
            self.fail = None
            self.calls.clear()
            with self.assertRaisesRegex(ValueError, 'Original disk changed'):
                self.archive()
        self.assertFalse(any(a[0] == 'copy' for a in self.calls))

    def test_corrupt_downloaded_part_is_replaced_on_resume(self):
        with self.harness():
            m = self.archive()
            self.prepare_restore(m)
            self.fail = 'download'
            with self.assertRaises(RuntimeError):
                self.m.restore()
            stage = next(Path(self.a.work_dir).glob('native-download-*'))
            path = next((stage / 'parts').iterdir())
            path.write_bytes(b'X' * path.stat().st_size)
            self.a.resume = str(stage)
            self.fail = None
            self.m.restore()
        self.assertEqual(self.download_count, len(m['disks'][0]['parts']) + 1)

    def test_multipart_deep_verify_with_existing_marker(self):
        # Simulated check is covered here; real-rclone coverage is above.
        self.a.deep_verify = True
        self.fail = 'marker'
        with self.harness():
            with self.assertRaises(RuntimeError):
                self.archive()
            m = json.loads((self.stage() / 'payload/manifest.json').read_text())
            self.c.remote[self.m.base + '/' + m['backup_id'] + '/COMPLETE'] = (self.stage() / 'COMPLETE').read_bytes()
            self.fail = None
            self.archive()
        self.assertTrue(any(a[0] == 'check' for a in self.calls))

    def test_interrupted_download_reuses_sha_verified_parts(self):
        with self.harness():
            m = self.archive()
            self.prepare_restore(m)
            self.fail = 'download'
            with self.assertRaisesRegex(RuntimeError, 'interrupted restore'):
                self.m.restore()
            self.assertFalse(self.c.file('200').exists())
            stage = next(Path(self.a.work_dir).glob('native-download-*'))
            self.a.resume = str(stage)
            self.fail = None
            self.m.restore()
        self.assertEqual(self.download_count, len(m['disks'][0]['parts']))
        self.assertEqual((self.c.root / 'vm-200-disk-0.qcow2').read_bytes(), self.c.disk.read_bytes())

    def test_interrupted_reconstruction_reuses_downloads(self):
        with self.harness():
            m = self.archive()
            self.prepare_restore(m)
            original = p.check_parts
            def interrupt(directory, disk, target=None):
                if target:
                    target.with_name(target.name + '.partial').write_bytes(b'partial')
                    raise KeyboardInterrupt()
                return original(directory, disk, target)
            with patch.object(p, 'check_parts', interrupt), self.assertRaises(KeyboardInterrupt):
                self.m.restore()
            stage = next(Path(self.a.work_dir).glob('native-download-*'))
            self.a.resume = str(stage)
            self.m.restore()
        self.assertEqual(self.download_count, len(m['disks'][0]['parts']))
        self.assertFalse((stage / 'scsi0.qcow2.partial').exists())

    def test_cleanup_incomplete_attempt_by_vmid_and_protect_complete(self):
        self.fail = 'upload'
        with self.harness():
            with self.assertRaises(RuntimeError):
                self.archive()
            self.a.vmid = '100'
            self.a.stage = None
            self.a.apply = False
            self.m.cleanup()
            self.assertTrue(self.stage().exists())
            self.a.apply = True
            self.m.cleanup()
        self.assertFalse(self.c.remote)
        self.assertEqual(list(Path(self.a.work_dir).glob('native-100-*')), [])
        self.assertTrue(self.c.file('100').exists())

    def test_completed_cleanup_retains_remote_parts(self):
        with self.harness():
            self.archive()
            remote = self.c.remote.copy()
            self.a.stage = str(self.stage())
            self.a.vmid = None
            self.a.apply = True
            self.m.cleanup()
        self.assertEqual(self.c.remote, remote)

    def test_recover_finalizes_verified_multipart_without_reupload(self):
        self.fail = 'marker'
        with self.harness():
            with self.assertRaises(RuntimeError):
                self.archive()
            self.fail = None
            self.a.resume = str(self.stage())
            self.calls.clear()
            self.m.recover()
        self.assertFalse(any(a[0] == 'copy' for a in self.calls))
        self.assertTrue(any(k.endswith('/COMPLETE') for k in self.c.remote))
        self.assertTrue(self.c.file('100').exists())

    def test_remote_part_verification_failure_never_deletes_or_completes(self):
        original = self.rc
        def corrupt(*args, **kwargs):
            if args[0] == 'lsjson' and '--hash' in args:
                key = next(k for k in self.c.remote if '.part-' in k)
                self.c.remote[key] = b'X' * len(self.c.remote[key])
            return original(*args, **kwargs)
        with self.harness(), patch.object(self.m, 'rc', corrupt), self.assertRaisesRegex(ValueError, 'MD5 mismatch'):
            self.archive(keep=False)
        self.assertTrue(self.c.file('100').exists())
        self.assertFalse(any(k.endswith('/COMPLETE') for k in self.c.remote))

    def test_local_part_changes_after_sha_validation_cannot_be_blessed_by_md5(self):
        original = self.rc
        def changed(*args, **kwargs):
            if args[0] == 'copy' and not str(args[1]).startswith(self.m.base):
                path = next(Path(args[1]).glob('*.part-*'))
                path.write_bytes(b'X' * path.stat().st_size)
            return original(*args, **kwargs)
        with self.harness(), patch.object(self.m, 'rc', changed), self.assertRaisesRegex(ValueError, 'MD5 mismatch'):
            self.archive(keep=False)
        self.assertTrue(self.c.file('100').exists())
        self.assertFalse(any(k.endswith('/COMPLETE') for k in self.c.remote))

    def test_manifest_readback_failure_never_deletes_or_completes(self):
        original = self.rc
        def corrupt(*args, **kwargs):
            if args[0] == 'cat' and str(args[1]).endswith('/manifest.json'):
                return '{}'
            return original(*args, **kwargs)
        with self.harness(), patch.object(self.m, 'rc', corrupt), self.assertRaisesRegex(ValueError, 'manifest read-back'):
            self.archive(keep=False)
        self.assertTrue(self.c.file('100').exists())
        self.assertFalse(any(k.endswith('/COMPLETE') for k in self.c.remote))

    def test_list_shows_one_backup_and_ignores_partial_attempt(self):
        with self.harness():
            self.archive()
            self.c.remote[self.m.base + '/100/20260906T000000Z-' + 'a' * 32 + '/scsi0.qcow2.part-000000'] = b'incomplete'
            output = io.StringIO()
            with redirect_stdout(output):
                self.m.listing()
        self.assertEqual(output.getvalue().count('native-qcow2'), 1)
        self.assertNotIn('.part-', output.getvalue())


class PartPrimitivesTests(unittest.TestCase):
    def test_empty_missing_corrupt_whole_and_reconstruction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / 'source'
            src.write_bytes(bytes(range(256)) * 3 + b'end')
            d = p.split_native(src, root, 'scsi0.qcow2', 64)
            target = root / 'rebuilt.qcow2'
            p.check_parts(root, d, target)
            self.assertEqual(target.read_bytes(), src.read_bytes())
            path = root / d['parts'][0]['filename']
            data = path.read_bytes()
            path.unlink()
            with self.assertRaisesRegex(ValueError, 'Missing part'):
                p.check_parts(root, d)
            path.write_bytes(b'X' * len(data))
            with self.assertRaisesRegex(ValueError, 'Part checksum'):
                p.check_parts(root, d)
            path.write_bytes(data)
            d['sha256'] = '0' * 64
            with self.assertRaisesRegex(ValueError, 'Whole-file'):
                p.check_parts(root, d)

    def test_cli_defaults_and_invalid_tuning(self):
        base = ['--remote', 'gdrive:pve-archive', '--source', 'pve-site-a']
        a = p.parser().parse_args(base + ['upload', '100'])
        self.assertIsNone(a.part_size)
        self.assertEqual(p.PART_SIZE, 4 * 1024 ** 3)
        self.assertEqual(a.transfers, 8)
        self.assertEqual(a.quota_retries, 24)
        self.assertFalse(a.single_file)
        self.assertEqual(p.part_size('512M'), 512 * 1024 ** 2)
        for value in ('0G', '4', '-1G', '2T'):
            with self.assertRaises(Exception):
                p.part_size(value)
        for value in ('0', '33'):
            with self.assertRaises(Exception):
                p.transfer_count(value)

    @unittest.skipUnless(shutil.which('qemu-img') and shutil.which('qemu-io'), 'qemu tools unavailable')
    def test_real_qcow2_internal_snapshot_bytes_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.qcow2'
            restored = root / 'restored.qcow2'
            def run(*args):
                return subprocess.check_output(list(map(str, args)), stderr=subprocess.STDOUT)
            run('qemu-img', 'create', '-f', 'qcow2', source, '16M')
            run('qemu-io', '-f', 'qcow2', '-c', 'write -P 0x11 0 4096', source)
            run('qemu-img', 'snapshot', '-c', 'before-change', source)
            run('qemu-io', '-f', 'qcow2', '-c', 'write -P 0x22 0 4096', source)
            record = p.split_native(source, root, 'scsi0.qcow2', 65536)
            p.check_parts(root, record, restored)
            self.assertEqual(source.read_bytes(), restored.read_bytes())
            info = json.loads(run('qemu-img', 'info', '--output=json', restored))
            self.assertEqual([s['name'] for s in info['snapshots']], ['before-change'])
            run('qemu-img', 'check', restored)
            run('qemu-io', '-f', 'qcow2', '-c', 'read -P 0x22 0 4096', restored)
            run('qemu-img', 'snapshot', '-a', 'before-change', restored)
            run('qemu-io', '-f', 'qcow2', '-c', 'read -P 0x11 0 4096', restored)
