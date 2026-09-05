import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pve_drive as p
import test_pve_drive


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stage = Path(self.tmp.name)

    def command(self, source):
        return [sys.executable, '-c', source]

    def test_binary_fanout_and_hashes_without_archive_spooling(self):
        data = bytes(range(256)) * 8192
        producer = self.command('import sys; sys.stdout.buffer.write(bytes(range(256))*8192)')
        consumer = self.command('import sys; d=sys.stdin.buffer.read(); assert d==bytes(range(256))*8192')
        result = p.stream_pipeline(producer, consumer, consumer, self.stage)
        self.assertEqual(result, {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                                  'md5': hashlib.md5(data).hexdigest()})
        self.assertTrue(all(path.suffix == '.log' for path in self.stage.iterdir()))
        self.assertTrue(all(path.stat().st_size < 100 for path in self.stage.iterdir()))

    def test_every_process_failure_propagates(self):
        producer = self.command('import sys; sys.stdout.buffer.write(b"x"*1048576)')
        consumer = self.command('import sys; sys.stdin.buffer.read()')
        fail = self.command('import sys; sys.exit(7)')
        for index in range(3):
            commands = [producer, consumer, consumer]
            commands[index] = fail
            with self.subTest(process=index), self.assertRaises(RuntimeError):
                p.stream_pipeline(*commands, self.stage)

    def test_empty_stream_rejected(self):
        producer = self.command('pass')
        consumer = self.command('import sys; sys.stdin.buffer.read()')
        with self.assertRaises(RuntimeError):
            p.stream_pipeline(producer, consumer, consumer, self.stage)

    def test_size_estimate_never_limits_upload_data(self):
        producer = self.command('import sys; sys.stdout.buffer.write(b"archive")')
        consumer = self.command('import sys; assert sys.stdin.buffer.read()==b"archive"')
        result = p.stream_pipeline(producer, consumer, consumer, self.stage, estimated_total=1)
        self.assertEqual(result['size'], 7)

    def test_producer_diagnostic_is_reported_with_downstream_failure(self):
        producer = self.command('import sys; print("backup worker startup failed", file=sys.stderr); sys.exit(25)')
        consumer = self.command('import sys; sys.stdin.buffer.read(); sys.exit(1)')
        with self.assertRaisesRegex(RuntimeError, 'backup worker startup failed'):
            p.stream_pipeline(producer, consumer, consumer, self.stage)

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX terminal semantics')
    def test_pipeline_launched_from_terminal_does_not_pass_terminal_to_producer(self):
        import pty
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        # Emulate the Proxmox branch: an inherited tty in a new session causes
        # tcsetpgrp to fail. Parent stdin is a tty; producer stdin must not be.
        producer = self.command('import os,sys; '
            'os.tcsetpgrp(0, os.getpgrp()) if os.isatty(0) else None; '
            'sys.stdout.buffer.write(b"archive")')
        consumer = self.command('import sys; assert sys.stdin.buffer.read()==b"archive"')
        script = ('import sys; sys.path.insert(0, ' + repr(str(Path(p.__file__).parent)) + '); '
                  'import pve_drive as p; '
                  f'r=p.stream_pipeline({producer!r}, {consumer!r}, {consumer!r}, {str(self.stage)!r}); '
                  'assert r["size"]==7')
        result = subprocess.run(self.command(script), stdin=slave, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_restore_pipeline_hashes_input_and_delivers_transformed_output(self):
        producer = self.command('import sys; sys.stdout.buffer.write(b"compressed")')
        transform = self.command('import sys; assert sys.stdin.buffer.read()==b"compressed"; sys.stdout.buffer.write(b"disk")')
        restore = self.command('import sys; assert sys.stdin.buffer.read()==b"disk"; print("restored")')
        result = p.stream_pipeline(producer, transform, None, self.stage, downstream_args=restore,
                                   producer_name='rclone-download', total=10)
        self.assertEqual(result['sha256'], hashlib.sha256(b'compressed').hexdigest())
        self.assertIn('restored', (self.stage / 'qmrestore-output.log').read_text())

    def test_restore_consumer_failure_propagates(self):
        producer = self.command('import sys; sys.stdout.buffer.write(b"data")')
        transform = self.command('import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())')
        restore = self.command('import sys; sys.stdin.buffer.read(); sys.exit(9)')
        with self.assertRaises(RuntimeError):
            p.stream_pipeline(producer, transform, None, self.stage, downstream_args=restore)

    def test_interrupt_stops_children(self):
        producer = self.command('import time; time.sleep(60)')
        consumer = self.command('import sys; sys.stdin.buffer.read()')
        with patch.object(p.console, 'update', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            p.stream_pipeline(producer, consumer, consumer, self.stage)

    @unittest.skipUnless(shutil.which('zstd'), 'requires zstd')
    def test_real_zstd_roundtrip(self):
        source = self.stage / 'source.bin'
        source.write_bytes(bytes(range(256)) * 4096)
        sink = self.command('import sys; assert sys.stdin.buffer.read()==bytes(range(256))*4096')
        result = p.stream_pipeline(['zstd', '-q', '-c', str(source)],
                                   ['zstd', '-q', '-d', '-c'], None, self.stage,
                                   downstream_args=sink)
        self.assertGreater(result['size'], 0)

    def test_restore_oversized_stream_rejected(self):
        producer = self.command('import sys; sys.stdout.buffer.write(b"too long")')
        consumer = self.command('import sys; sys.stdin.buffer.read()')
        with self.assertRaises(RuntimeError):
            p.stream_pipeline(producer, consumer, None, self.stage, total=3)


class StreamLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.case = test_pve_drive.LifecycleTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.m = self.case.m
        self.a = self.case.args
        self.a.stream = True
        self.a.deep_verify = False
        self.a.cleanup_local = False
        self.features = {'Features': {'PutStream': True}, 'Hashes': ['MD5']}
        self.bad_hash = False
        self.bad_inventory = False
        self.stream_failure = False

    def rc(self, *args, capture=False):
        if args[0] == 'backend':
            return json.dumps(self.features)
        if args[0] == 'lsjson' and '--files-only' in args:
            rows = []
            prefix = str(args[1]) + '/'
            for key, value in self.case.remote.items():
                if key.startswith(prefix):
                    data = value.encode()
                    rows.append({'Path': key[len(prefix):], 'Size': len(data),
                                 'Hashes': {'md5': '0' * 32 if self.bad_hash else hashlib.md5(data).hexdigest()}})
            return json.dumps(rows)
        return self.case.rc(*args, capture=capture)

    def pipeline(self, producer, uploader, checker, stage, **kwargs):
        if self.stream_failure:
            raise RuntimeError('injected stream failure')
        destination = uploader[uploader.index('rcat') + 1]
        self.case.remote[destination] = 'archive-data'
        (stage / 'vzdump.log').write_text('' if self.bad_inventory else
            "INFO: include disk 'scsi0' 'local-lvm:vm-100-disk-0' 32G\n")
        data = b'archive-data'
        return {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                'md5': hashlib.md5(data).hexdigest()}

    def execute(self):
        with patch.object(self.m, 'rc', self.rc), patch.object(self.m, 'api', self.case.api), \
                patch.object(p, 'run', self.case.command), patch.object(p, 'stream_pipeline', self.pipeline):
            self.m.archive()

    def stage(self):
        return next(Path(self.case.tmp.name).glob('stream-100-*'))

    def test_success_cloud_verified_before_destroy_and_no_local_archive(self):
        self.execute()
        self.assertTrue(self.case.deleted)
        self.assertEqual([path.name for path in (self.stage() / 'payload').iterdir()], ['manifest.json'])
        self.assertTrue((self.stage() / 'stream-complete.json').is_file())
        commands = self.case.commands
        marker = next(i for i, c in enumerate(commands) if c[:2] == ['rclone', 'cat'])
        destroy = next(i for i, c in enumerate(commands) if c[:2] == ['qm', 'destroy'])
        self.assertLess(marker, destroy)

    def test_keep_vm(self):
        self.a.delete_vm = False
        self.a.keep_vm = True
        self.execute()
        self.assertFalse(self.case.deleted)
        self.assertIn(['qm', 'unlock', '100'], self.case.commands)

    def test_unsupported_remote_fails_before_shutdown(self):
        for features in [{'Features': {'PutStream': False}, 'Hashes': ['MD5']},
                         {'Features': {'PutStream': True}, 'Hashes': []}]:
            self.features = features
            with self.subTest(features=features), self.assertRaises(ValueError):
                self.execute()
        self.assertEqual(self.case.commands, [])

    def test_snapshots_and_incompatible_flags_rejected(self):
        self.a.deep_verify = True
        with self.assertRaises(ValueError):
            self.execute()
        self.a.deep_verify = False
        with patch.object(self.case, 'api', self.case.with_snapshots()), self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.case.commands, [])

    def test_producer_failure_and_missing_disk_never_publish_or_delete(self):
        for failure in ['stream_failure', 'bad_inventory']:
            setattr(self, failure, True)
            with self.subTest(failure=failure), self.assertRaises((RuntimeError, ValueError)):
                self.execute()
            setattr(self, failure, False)
        self.assertFalse(self.case.deleted)
        self.assertFalse(any(path.endswith('/COMPLETE') for path in self.case.remote))
        self.assertFalse(list(Path(self.case.tmp.name).rglob('stream-complete.json')))

    def test_hash_mismatch_blocks_deletion_then_recovery_retains_vm(self):
        self.bad_hash = True
        with self.assertRaises(ValueError):
            self.execute()
        self.assertFalse(self.case.deleted)
        self.assertFalse(any(path.endswith('/COMPLETE') for path in self.case.remote))
        self.bad_hash = False
        self.a.resume = str(self.stage())
        with patch.object(self.m, 'rc', self.rc), patch.object(self.m, 'api', self.case.api), \
                patch.object(p, 'run', self.case.command), patch.object(p, 'stream_pipeline') as pipeline:
            self.m.recover()
        pipeline.assert_not_called()
        self.assertFalse(self.case.deleted)
        self.assertTrue(any(path.endswith('/COMPLETE') for path in self.case.remote))

    def test_recovery_without_receipt_refused(self):
        self.stream_failure = True
        with self.assertRaises(RuntimeError):
            self.execute()
        self.a.resume = str(self.stage())
        with self.assertRaisesRegex(ValueError, 'receipt'):
            self.m.recover()

    def test_marker_readback_failure_prevents_deletion(self):
        self.case.failure = 'cat'
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertFalse(self.case.deleted)

    def test_changed_config_prevents_deletion(self):
        original = self.m.verify_upload
        def verify(*args, **kwargs):
            original(*args, **kwargs)
            self.case.cfg['name'] = 'changed'
        with patch.object(self.m, 'verify_upload', side_effect=verify), self.assertRaises(ValueError):
            self.execute()
        self.assertFalse(self.case.deleted)

    def test_recovery_rejects_altered_receipt(self):
        self.bad_hash = True
        with self.assertRaises(ValueError):
            self.execute()
        self.a.resume = str(self.stage())
        (self.stage() / 'stream-complete.json').write_text('{"manifest_sha256":"invalid"}')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.m.recover()


class StreamRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.args = p.parser().parse_args(['--remote', 'gdrive:pve', '--source', 'test',
            '--work-dir', self.tmp.name, 'restore', '100', '--target-vmid', '200', '--stream'])
        self.args.vmid = '200'
        self.args.backup_id = '100/20260905T010000Z-' + 'a' * 32
        self.m = p.Manager(self.args)
        self.manifest = {'schema': 2, 'filename': 'backup.vma.zst', 'size': 4,
                         'sha256': hashlib.sha256(b'data').hexdigest()}
        self.rows = [{'Path': 'backup.vma.zst', 'Size': 4}]
        self.commands = []

    def command(self, *args, **kwargs):
        self.commands.append(list(args))
        if args[:2] == ('qm', 'status'):
            return 'status: stopped'

    def execute(self, result=None):
        result = result or {'size': 4, 'sha256': self.manifest['sha256']}
        with patch.object(self.m, 'manifest', return_value=('gdrive:pve/archive', self.manifest)), \
                patch.object(self.m, 'api', return_value=[]), \
                patch.object(self.m, 'rc', return_value=json.dumps(self.rows)), \
                patch.object(p, 'stream_pipeline', return_value=result) as pipeline, \
                patch.object(p, 'run', self.command):
            self.m.restore()
        return pipeline

    def test_legacy_vma_stream_restore_and_target_options(self):
        self.args.storage = 'destination'
        self.args.unique = True
        pipeline = self.execute()
        self.assertEqual(pipeline.call_args.kwargs['downstream_args'],
                         ['qmrestore', '-', '200', '--start', '0', '--storage', 'destination', '--unique', '1'])
        self.assertIn(['qm', 'set', '200', '--onboot', '0', '--lock', 'backup'], self.commands)
        self.assertIn(['qm', 'unlock', '200'], self.commands)
        self.assertFalse(list(Path(self.tmp.name).iterdir()))

    def test_bad_sha_keeps_target_locked_and_recovery_files(self):
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.execute({'size': 4, 'sha256': '0' * 64})
        self.assertNotIn(['qm', 'unlock', '200'], self.commands)
        state = json.loads(next(Path(self.tmp.name).rglob('restore-state.json')).read_text())
        self.assertEqual(state['status'], 'failed-unverified')

    def test_native_archive_and_wrong_size_rejected_before_pipeline(self):
        self.manifest['schema'] = 3
        with self.assertRaisesRegex(ValueError, 'VMA'):
            self.execute()
        self.manifest['schema'] = 2
        self.rows[0]['Size'] = 5
        with self.assertRaisesRegex(ValueError, 'wrong size'):
            self.execute()
        self.assertEqual(self.commands, [])

    def test_existing_target_never_fetches_archive(self):
        with patch.object(self.m, 'api', return_value=[{'vmid': 200}]), \
                patch.object(self.m, 'manifest') as manifest, self.assertRaises(ValueError):
            self.m.restore()
        manifest.assert_not_called()

    def test_streamed_archive_requires_matching_remote_md5(self):
        self.manifest['md5'] = hashlib.md5(b'data').hexdigest()
        with self.assertRaisesRegex(ValueError, 'MD5'):
            self.execute()
        self.rows[0]['Hashes'] = {'MD5': self.manifest['md5']}
        self.execute()
