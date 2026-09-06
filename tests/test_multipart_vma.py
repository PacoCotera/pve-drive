import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

import pve_drive as p
import test_pve_drive


class BoundedVmaPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stage = Path(self.tmp.name)
        self.data = bytes(range(256)) * 4096 + b'last'

    def command(self, script):
        return [sys.executable, '-c', script]

    def producer(self):
        return self.command('import sys; sys.stdout.buffer.write(bytes(range(256))*4096+b"last")')

    def checker(self):
        return self.command('import sys; assert sys.stdin.buffer.read()==bytes(range(256))*4096+b"last"')

    def test_bounded_spool_parallel_order_hashes_and_releases_verified_parts(self):
        remote, metrics = {}, {'active': 0, 'peak_active': 0, 'peak_bytes': 0}
        guard = threading.Lock()
        barrier = threading.Barrier(2)
        def upload(path, part, cancel):
            with guard:
                metrics['active'] += 1
                metrics['peak_active'] = max(metrics['peak_active'], metrics['active'])
                metrics['peak_bytes'] = max(metrics['peak_bytes'], sum(x.stat().st_size for x in path.parent.iterdir()))
            if int(part['filename'].rsplit('-', 1)[1]) < 2:
                barrier.wait(timeout=5)
            time.sleep(.03)
            data = path.read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), part['sha256'])
            self.assertEqual(hashlib.md5(data).hexdigest(), part['md5'])
            remote[part['filename']] = data
            with guard:
                metrics['active'] -= 1
        receipts = []
        result = p.multipart_stream_pipeline(self.producer(), self.checker(), self.stage,
                                            'test.vma.zst', 65536, 2, upload, receipts.append)
        self.assertEqual(metrics['peak_active'], 2)
        self.assertLessEqual(metrics['peak_bytes'], 3 * 65536)
        self.assertEqual(b''.join(remote[x['filename']] for x in result['parts']), self.data)
        self.assertEqual(result['sha256'], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(result['size'], len(self.data))
        self.assertEqual(receipts, [result])
        self.assertEqual(list((self.stage / 'spool').iterdir()), [])

    def test_quota_pause_applies_backpressure_without_growing_disk(self):
        gate, waiting = threading.Event(), threading.Event()
        failures, receipts = [], []
        def upload(path, part, cancel):
            waiting.set()
            while not gate.wait(.02):
                if cancel.is_set():
                    raise RuntimeError('cancelled')
        def task():
            try:
                p.multipart_stream_pipeline(self.producer(), self.checker(), self.stage,
                                            'test.vma.zst', 65536, 2, upload, receipts.append)
            except BaseException as exc:
                failures.append(exc)
        thread = threading.Thread(target=task)
        thread.start()
        try:
            self.assertTrue(waiting.wait(5))
            time.sleep(.3)
            self.assertLessEqual(sum(x.stat().st_size for x in (self.stage / 'spool').iterdir()), 3 * 65536)
            self.assertTrue(thread.is_alive())
            self.assertEqual(receipts, [])
        finally:
            gate.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(receipts), 1)

    def test_cancellation_interrupts_waiting_upload_workers(self):
        waiting = threading.Event()
        def upload(path, part, cancel):
            waiting.set()
            cancel.wait(30)
            raise RuntimeError('cancelled')
        def update(*args, **kwargs):
            if waiting.is_set():
                raise KeyboardInterrupt()
        started = time.monotonic()
        with patch.object(p.console, 'update', update), self.assertRaises(KeyboardInterrupt):
            p.multipart_stream_pipeline(self.producer(), self.checker(), self.stage,
                                        'test.vma.zst', 65536, 2, upload, lambda r: None)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(list((self.stage / 'spool').iterdir()))

    def test_failed_producer_or_checker_never_certifies(self):
        good = self.producer()
        consume = self.command('import sys; sys.stdin.buffer.read()')
        fail = self.command('import sys; print("injected failure",file=sys.stderr);sys.exit(7)')
        for index, (producer, checker) in enumerate([(fail, consume), (good, fail)]):
            stage = self.stage / str(index)
            stage.mkdir()
            certified = []
            with self.assertRaisesRegex(RuntimeError, 'injected failure'):
                p.multipart_stream_pipeline(producer, checker, stage, 'a.vma.zst', 65536, 2,
                                            lambda *args: None, certified.append)
            self.assertEqual(certified, [])

    def test_upload_failure_stops_production_and_retains_spool(self):
        def fail(*args):
            raise RuntimeError('injected upload failure')
        certified = []
        with self.assertRaisesRegex(RuntimeError, 'injected upload failure'):
            p.multipart_stream_pipeline(self.producer(), self.checker(), self.stage,
                                        'a.vma.zst', 65536, 1, fail, certified.append)
        self.assertEqual(certified, [])
        self.assertTrue(list((self.stage / 'spool').iterdir()))

    @unittest.skipUnless(shutil.which('zstd'), 'zstd unavailable')
    def test_real_compressed_stream_is_byte_exact_after_reconstruction(self):
        source = self.stage / 'source'
        source.write_bytes(os.urandom(2 * 1024 ** 2))
        remote = self.stage / 'remote'
        remote.mkdir()
        def upload(path, part, cancel):
            shutil.copyfile(path, remote / path.name)
        result = p.multipart_stream_pipeline(['zstd', '-q', '-c', source], ['zstd', '--test', '-'],
                                            self.stage, 'test.vma.zst', 65536, 4, upload, lambda r: None)
        rebuilt = self.stage / 'rebuilt.zst'
        p.check_parts(remote, result, rebuilt)
        self.assertEqual(subprocess.check_output(['zstd', '-q', '-d', '-c', rebuilt]), source.read_bytes())


class MultipartVmaLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.c = test_pve_drive.LifecycleTests()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.a, self.m = self.c.args, self.c.m
        self.a.command = 'upload'
        self.a.format = 'auto'
        self.a.cleanup_local = False
        self.a.deep_verify = False
        self.a.part_size = 7
        self.a.transfers = 2
        self.a.quota_retries = 0
        self.a.delete_vm = False
        self.data = b'compressed-fixture-data-across-multiple-parts'
        self.remote, self.calls, self.sent = {}, [], []
        self.failure = None
        self.production_count = 0

    def command(self, *args, **kwargs):
        if list(map(str, args[:2])) == ['qm', 'unlock']:
            self.c.cfg.pop('lock', None)
        return self.c.command(*args, **kwargs)

    def rc(self, *args, capture=False):
        a = list(map(str, args))
        self.calls.append(a)
        if a[0] == 'backend':
            return json.dumps({'Hashes': ['MD5'], 'Features': {}})
        if a[0] == 'copyto':
            if self.failure == 'marker' and a[2].endswith('/COMPLETE'):
                raise RuntimeError('marker failed')
            if self.failure == 'part' and '.part-000001' in a[2]:
                raise RuntimeError('upload limit')
            if Path(a[1]).is_file():
                data = Path(a[1]).read_bytes()
                if a[2] in self.remote and self.remote[a[2]] != data:
                    raise RuntimeError('immutable conflict')
                if a[2] not in self.remote:
                    self.sent.append(a[2])
                self.remote[a[2]] = data
            else:
                Path(a[2]).write_bytes(self.remote[a[1]])
        if a[0] == 'copy':
            for name in Path(a[a.index('--files-from-raw') + 1]).read_text().splitlines():
                (Path(a[2]) / name).write_bytes(self.remote[a[1] + '/' + name])
        if a[0] == 'lsjson':
            return json.dumps([{'Path': k.rsplit('/', 1)[1] if k == a[1] else k[len(a[1])+1:],
                                'Size': len(v), 'Hashes': {'md5': hashlib.md5(v).hexdigest()}}
                               for k, v in self.remote.items() if k == a[1] or k.startswith(a[1] + '/')])
        if a[0] == 'cat':
            return self.remote[a[1]].decode()
        if a[0] == 'purge':
            self.remote = {k: v for k, v in self.remote.items() if not k.startswith(a[1] + '/')}
        return ''

    def pipeline(self, producer, checker, stage, filename, size, transfers, upload, completed, **kwargs):
        self.production_count += 1
        spool = stage / 'spool'
        spool.mkdir()
        (stage / 'vzdump.log').write_text("INFO: include disk 'scsi0' 'local-lvm:vm-100-disk-0' 32G\n")
        records = []
        for index, offset in enumerate(range(0, len(self.data), size)):
            data = self.data[offset:offset + size]
            name = f'{filename}.part-{index:06d}'
            (spool / name).write_bytes(data)
            records.append({'filename': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                            'md5': hashlib.md5(data).hexdigest()})
        result = {'size': len(self.data), 'sha256': hashlib.sha256(self.data).hexdigest(),
                  'part_size': size, 'parts': records}
        if self.failure == 'producer':
            raise RuntimeError('producer interrupted')
        # Simulate successful EOF before the last concurrent transfers finish.
        completed(result)
        for part in records:
            path = spool / part['filename']
            upload(path, part, threading.Event())
            path.unlink()
        return result

    def harness(self):
        stack = ExitStack()
        stack.enter_context(patch.object(p, 'run', self.command))
        stack.enter_context(patch.object(self.m, 'api', self.c.api))
        stack.enter_context(patch.object(self.m, 'rc', self.rc))
        stack.enter_context(patch.object(self.m, 'part_rc', lambda cancel, *args, **kw: self.rc(*args, **kw)))
        stack.enter_context(patch.object(p, 'multipart_stream_pipeline', self.pipeline))
        return stack

    def stage(self):
        return next(Path(self.a.work_dir).glob('stream-100-*'))

    def manifest(self):
        return json.loads((self.stage() / 'payload/manifest.json').read_text())

    def test_normal_upload_uses_compressed_multipart_without_native_copy(self):
        with self.harness():
            self.m.move_to_cloud()
        m = self.manifest()
        self.assertEqual(m['schema'], 5)
        self.assertEqual(m['sha256'], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(b''.join(self.remote[self.m.base + '/' + m['backup_id'] + '/' + x['filename']]
                                  for x in m['parts']), self.data)
        self.assertFalse(self.c.deleted)
        self.assertNotIn('lock', self.c.cfg)
        self.assertEqual(list((self.stage() / 'spool').iterdir()), [])
        self.assertTrue(any(k.endswith('/COMPLETE') for k in self.remote))
        self.assertEqual(self.calls[-1][1].split('/')[-1], 'COMPLETE')

    def test_default_spool_budget_is_small_and_independent_of_vm_capacity(self):
        self.a.part_size = None
        self.a.transfers = 8
        with self.harness(), patch.object(self.m, 'require_staging_space') as space:
            self.m.move_to_cloud()
        self.assertEqual(space.call_args.args[1], 9 * 256 * 1024 ** 2 + 1024 ** 3)

    def test_delete_requires_all_parts_manifest_and_marker(self):
        self.a.delete_vm = True
        with self.harness():
            self.m.move_to_cloud()
        self.assertTrue(self.c.deleted)
        self.assertTrue(any(k.endswith('/COMPLETE') for k in self.remote))

    def test_completed_production_resumes_after_quota_without_reproducing(self):
        self.failure = 'part'
        with self.harness(), self.assertRaisesRegex(RuntimeError, 'quota still blocked'):
            self.m.move_to_cloud()
        self.assertFalse(any(k.endswith('/COMPLETE') for k in self.remote))
        self.assertTrue((self.stage() / 'stream-complete.json').exists())
        first = next(k for k in self.remote if '.part-' in k)
        self.failure = None
        with self.harness():
            self.m.move_to_cloud()
        self.assertEqual(self.production_count, 1)
        self.assertEqual(self.sent.count(first), 1)
        self.assertTrue(any(k.endswith('/COMPLETE') for k in self.remote))
        self.assertFalse(self.c.deleted)

    def test_interrupted_production_restarts_new_attempt_and_keeps_old_for_cleanup(self):
        self.failure = 'producer'
        with self.harness(), self.assertRaisesRegex(RuntimeError, 'producer interrupted'):
            self.m.move_to_cloud()
        old = self.stage()
        self.assertFalse((old / 'stream-complete.json').exists())
        self.failure = None
        with self.harness():
            self.m.move_to_cloud()
        self.assertTrue((old / 'SUPERSEDED').exists())
        self.assertEqual(self.production_count, 2)
        self.assertEqual(len(list(Path(self.a.work_dir).glob('stream-100-*'))), 2)

    def test_interrupted_marker_can_be_recovered_without_reupload(self):
        self.failure = 'marker'
        with self.harness(), self.assertRaisesRegex(RuntimeError, 'marker failed'):
            self.m.move_to_cloud()
        self.failure = None
        self.a.resume = str(self.stage())
        self.calls.clear()
        with self.harness():
            self.m.recover()
        self.assertFalse(any(a[0] == 'copyto' and '.part-' in a[2] for a in self.calls))
        self.assertFalse(self.c.deleted)

    def test_corrupt_part_missing_part_bad_order_whole_hash_restore(self):
        with self.harness():
            self.m.move_to_cloud()
            original = self.manifest()
            self.a.backup_id = original['backup_id']
            self.a.vmid = '200'
            self.a.storage = None
            self.a.unique = False
            stage, archive = self.m.download()
            self.assertEqual(archive.read_bytes(), self.data)
            destination = self.m.base + '/' + original['backup_id']
            key = destination + '/' + original['parts'][0]['filename']
            data = self.remote[key]
            self.remote[key] = b'X' * len(data)
            with self.assertRaisesRegex(ValueError, 'Part checksum'):
                self.m.download()
            del self.remote[key]
            with self.assertRaises(KeyError):
                self.m.download()
            self.remote[key] = data
            changed = copy.deepcopy(original)
            changed['sha256'] = '0' * 64
            with self.assertRaisesRegex(ValueError, 'Whole-file'):
                self.m.download_vma_multipart(destination, changed)
            changed = copy.deepcopy(original)
            changed['parts'].reverse()
            with self.assertRaisesRegex(ValueError, 'ordering'):
                self.m.validate_manifest(changed, changed['backup_id'])
        self.assertFalse(any(c[0] == 'qmrestore' for c in self.c.commands))

    def test_cleanup_incomplete_vma_removes_only_recorded_attempt(self):
        self.failure = 'part'
        with self.harness(), self.assertRaises(RuntimeError):
            self.m.move_to_cloud()
        self.a.stage = None
        self.a.apply = True
        with self.harness():
            self.m.cleanup()
        self.assertFalse(self.remote)
        self.assertEqual(list(Path(self.a.work_dir).glob('stream-100-*')), [])
        self.assertFalse(self.c.deleted)

    def test_remote_corruption_before_publication_blocks_deletion(self):
        self.a.delete_vm = True
        original = self.rc
        def corrupt(*args, **kwargs):
            if args[0] == 'copyto' and str(args[2]).endswith('/manifest.json'):
                key = next(k for k in self.remote if '.part-' in k)
                self.remote[key] = b'X' * len(self.remote[key])
            return original(*args, **kwargs)
        with self.harness(), patch.object(self.m, 'rc', corrupt), self.assertRaisesRegex(ValueError, 'MD5 mismatch'):
            self.m.move_to_cloud()
        self.assertFalse(self.c.deleted)
        self.assertFalse(any(k.endswith('/COMPLETE') for k in self.remote))

    @unittest.skipUnless(shutil.which('rclone') and shutil.which('zstd'), 'rclone/zstd unavailable')
    def test_real_parallel_compressed_upload_and_download(self):
        root = Path(self.a.work_dir)
        config = root / 'rclone.conf'
        config.write_text('[gdrive]\ntype = local\n')
        self.a.rclone_config = str(config)
        self.a.remote = 'gdrive:' + str(root / 'cloud')
        self.a.part_size = 65536
        self.a.transfers = 4
        self.m = p.Manager(self.a)
        source = root / 'data'
        source.write_bytes(os.urandom(2 * 1024 ** 2))
        actual_run, actual_pipeline = p.run, p.multipart_stream_pipeline
        producer = [sys.executable, '-c',
                    'import sys,subprocess; print("INFO: include disk \'scsi0\' \'local-lvm:vm-100-disk-0\' 32G",file=sys.stderr); '
                    'sys.exit(subprocess.call(["zstd","-q","-c",sys.argv[1]]))', str(source)]
        def pipeline(command, *args, **kwargs):
            return actual_pipeline(producer, *args, **kwargs)
        def run(*args, **kwargs):
            if args[0] in ('rclone', 'zstd'):
                return actual_run(*args, **kwargs)
            return self.command(*args, **kwargs)
        with patch.object(p, 'run', run), patch.object(self.m, 'api', self.c.api), \
                patch.object(p, 'multipart_stream_pipeline', pipeline):
            self.m.move_to_cloud()
            m = self.manifest()
            self.a.backup_id = m['backup_id']
            self.a.vmid = '200'
            self.a.resume = None
            stage, archive = self.m.download()
            self.assertEqual(subprocess.check_output(['zstd', '-q', '-d', '-c', archive]), source.read_bytes())
            self.assertEqual(p.sha256(archive), m['sha256'])
            self.assertEqual(len(list((self.stage() / 'spool').iterdir())), 0)
            # A repeated download resumes verified parts, reconstructs and checks.
            self.a.resume = str(stage)
            self.m.download()

    def test_all_three_configured_disks_must_be_included(self):
        self.c.cfg.update(scsi1='pool:disk-one,size=240G', scsi2='pool:disk-two,size=1T')
        with self.harness(), self.assertRaisesRegex(ValueError, 'disk inventory'):
            self.m.move_to_cloud()
        self.assertFalse(self.c.deleted)
        self.assertFalse(any(k.endswith('/COMPLETE') for k in self.remote))
        # Retry production with an inventory covering all three disks.
        original = self.pipeline
        def three_disks(producer, checker, stage, filename, size, transfers, upload, completed, **kwargs):
            def certify(result):
                with (stage / 'vzdump.log').open('a') as log:
                    log.write("INFO: include disk 'scsi1' 'pool:disk-one' 240G\n")
                    log.write("INFO: include disk 'scsi2' 'pool:disk-two' 1T\n")
                completed(result)
            return original(producer, checker, stage, filename, size, transfers, upload, certify, **kwargs)
        with self.harness(), patch.object(p, 'multipart_stream_pipeline', three_disks):
            self.m.move_to_cloud()
        manifest = json.loads(next(v for k, v in self.remote.items() if k.endswith('/manifest.json')))
        self.assertEqual(set(manifest['included_disks']), {'scsi0', 'scsi1', 'scsi2'})

    def test_interrupted_download_reuses_verified_compressed_parts(self):
        with self.harness():
            self.m.move_to_cloud()
            m = self.manifest()
            self.a.backup_id = m['backup_id']
            original = self.rc
            first = []
            def interrupted(*args, **kwargs):
                if args[0] == 'copy':
                    a = list(map(str, args))
                    name = Path(a[a.index('--files-from-raw') + 1]).read_text().splitlines()[0]
                    first.append(name)
                    (Path(a[2]) / name).write_bytes(self.remote[a[1] + '/' + name])
                    raise RuntimeError('download interrupted')
                return original(*args, **kwargs)
            with patch.object(self.m, 'rc', interrupted), self.assertRaisesRegex(RuntimeError, 'download interrupted'):
                self.m.download()
            self.a.resume = str(next(Path(self.a.work_dir).glob('restore-*')))
            stage, archive = self.m.download()
            selection = (stage / 'download-files.txt').read_text().splitlines()
        self.assertNotIn(first[0], selection)
        self.assertEqual(archive.read_bytes(), self.data)

    def test_snapshot_vm_still_selects_native(self):
        original = self.c.api
        def api(path):
            if path.endswith('/snapshot'):
                return [{'name': 'current'}, {'name': 'before'}]
            return original(path)
        with self.harness(), patch.object(self.m, 'api', api), patch.object(self.m, 'archive_native') as native:
            self.m.move_to_cloud()
        native.assert_called_once()
        self.assertEqual(self.production_count, 0)

    def test_cancelled_quota_wait_returns_immediately(self):
        cancel = threading.Event()
        cancel.set()
        self.a.quota_retries = 24
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.m.quota_retry(lambda: (_ for _ in ()).throw(RuntimeError('upload limit')), cancel_event=cancel)
