import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pve_drive as p


CONFIG = '''agent: 1
cpu: host
ide2: none,media=cdrom
memory: 24576
name: VM100-EXAMPLE
net0: virtio=02:00:00:00:01:00,bridge=vmbr0,queues=1
parent: before-change
scsi0: local:100/vm-100-disk-0.qcow2,size=64G

[before-change]
agent: 1
memory: 24576
name: VM100-EXAMPLE
net0: virtio=02:00:00:00:01:00,bridge=vmbr0,queues=1
scsi0: local:100/vm-100-disk-0.qcow2,size=64G
snaptime: 1700000000
'''


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.args = p.parser().parse_args([
            '--remote', 'gdrive:pve-archive', '--source', 'pve-site-a',
            '--work-dir', str(self.root / 'stage'), 'archive', '100',
            '--format', 'native-qcow2', '--delete-vm'])
        self.m = p.Manager(self.args)
        self.file('100').write_text(CONFIG)
        self.disk = self.root / 'source.qcow2'
        self.disk.write_bytes(b'fixture-for-qcow2-content-with-internal-snapshot')
        self.remote = {}
        self.commands = []
        self.fail_check = False
        self.info = [{'format': 'qcow2', 'virtual-size': 68719476736,
                      'snapshots': [{'name': 'before-change', 'vm-state-size': 0}],
                      'format-specific': {'data': {}}}]

    def file(self, ident):
        return self.root / (str(ident) + '.conf')

    def fake_run(self, *args, capture=False):
        a = list(map(str, args))
        self.commands.append(a)
        if a[:2] == ['qm', 'status']:
            return 'status: stopped\n'
        if a[:2] == ['qm', 'set']:
            path = self.file(a[2])
            path.write_text('lock: backup\n' + path.read_text())
        if a[:2] == ['qm', 'unlock']:
            path = self.file(a[2])
            path.write_text(re.sub(r'^lock: .*\n', '', path.read_text(), flags=re.M))
        if a[:2] == ['qm', 'create']:
            path = self.file(a[2])
            if path.exists():
                raise ValueError('ID taken')
            path.write_text('lock: create\n')
        if a[:2] == ['qm', 'destroy']:
            self.file(a[2]).unlink()
        if a[:2] == ['pvesm', 'path']:
            if a[2].startswith('local:100/'):
                return str(self.disk)
            return str(self.root / a[2].split('/')[-1])
        if a[:2] == ['pvesm', 'alloc']:
            (self.root / a[4]).write_bytes(b'allocated')
        if a[0] == 'cp':
            shutil.copyfile(a[-2], a[-1])
        if a[:2] == ['qemu-img', 'info']:
            return json.dumps(self.info)
        return ''

    def api(self, path):
        if path.startswith('/storage/'):
            return {'type': 'dir', 'path': str(self.root)}
        match = re.search(r'/qemu/(\d+)/config$', path)
        if match:
            raw = self.file(match[1]).read_text().split('[before-change]')[0]
            return dict([line.split(': ', 1) for line in raw.splitlines() if ': ' in line], digest='test-digest')
        if path.endswith('/snapshot'):
            return [{'name': 'current'}, {'name': 'before-change'}]
        return []

    def rc(self, *args, capture=False):
        a = list(map(str, args))
        if a[0] == 'copy':
            for f in Path(a[1]).iterdir():
                self.remote[a[2] + '/' + f.name] = f.read_bytes()
        if a[0] == 'copyto':
            if a[1] in self.remote:
                Path(a[2]).write_bytes(self.remote[a[1]])
            else:
                self.remote[a[2]] = Path(a[1]).read_bytes()
        if a[0] == 'cat':
            return self.remote[a[1]].decode()
        if a[0] == 'check' and self.fail_check:
            raise ValueError('remote mismatch')
        return '[]'

    def harness(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(p, 'run', self.fake_run))
        stack.enter_context(patch.object(self.m, 'api', self.api))
        stack.enter_context(patch.object(self.m, 'rc', self.rc))
        stack.enter_context(patch.object(self.m, 'config_file', self.file))
        return stack

    def archive(self):
        self.m.archive()
        key = next(k for k in self.remote if k.endswith('/manifest.json'))
        return json.loads(self.remote[key])

    def test_native_archive_preserves_bytes_and_snapshot_configuration(self):
        with self.harness():
            m = self.archive()
        self.assertEqual(m['snapshots'], ['before-change'])
        self.assertEqual(next(v for k, v in self.remote.items() if k.endswith('/vm.conf')).decode(), CONFIG)
        self.assertEqual(next(v for k, v in self.remote.items() if k.endswith('/scsi0.qcow2')), self.disk.read_bytes())
        self.assertFalse(self.file('100').exists())
        self.assertFalse(any(c[0] == 'vzdump' or 'convert' in c for c in self.commands))

    def test_failed_native_upload_check_prevents_deletion(self):
        self.fail_check = True
        with self.harness(), self.assertRaisesRegex(ValueError, 'remote mismatch'):
            self.archive()
        self.assertTrue(self.file('100').exists())
        self.assertIn('[before-change]', self.file('100').read_text())
        self.assertFalse(any(c[:2] == ['qm', 'destroy'] for c in self.commands))

    def test_native_keep_vm_preserves_original_snapshot_configuration(self):
        self.args.delete_vm = False
        self.args.keep_vm = True
        with self.harness():
            self.archive()
        self.assertEqual(self.file('100').read_text(), CONFIG)
        self.assertFalse(any(c[:2] == ['qm', 'destroy'] for c in self.commands))

    def test_native_insufficient_space_fails_before_shutdown(self):
        usage = shutil._ntuple_diskusage(100, 100, 0)
        with self.harness(), patch.object(shutil, 'disk_usage', return_value=usage), \
                self.assertRaisesRegex(ValueError, 'staging needs'):
            self.archive()
        self.assertEqual(self.file('100').read_text(), CONFIG)
        self.assertFalse(any(c[:2] == ['qm', 'shutdown'] for c in self.commands))

    def test_source_disk_change_during_upload_prevents_deletion(self):
        original = self.rc
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0] == 'check':
                self.disk.write_bytes(b'changed source')
            return result
        with self.harness(), patch.object(self.m, 'rc', changed), \
                self.assertRaisesRegex(ValueError, 'Original disk changed'):
            self.archive()
        self.assertTrue(self.file('100').exists())
        self.assertFalse(any(c[:2] == ['qm', 'destroy'] for c in self.commands))

    def test_bad_local_copy_retains_hash_evidence_and_never_uploads(self):
        def corrupt(source, destination):
            Path(destination).write_bytes(b'bad copy')
        with self.harness(), patch.object(p, 'stream_copy', corrupt), \
                self.assertRaisesRegex(ValueError, 'Copied bytes differ'):
            self.archive()
        report = json.loads(next(self.root.rglob('copy-mismatch.json')).read_text())
        self.assertEqual(report['source_sha256_before'], report['source_sha256_after'])
        self.assertNotEqual(report['source_sha256_before'], report['destination_sha256'])
        self.assertFalse(self.remote)
        self.assertTrue(self.file('100').exists())

    def test_source_change_during_copy_is_reported(self):
        destination = self.root / 'copy.qcow2'
        report_path = self.root / 'copy-mismatch.json'
        def change_then_copy(*args, **kwargs):
            self.disk.write_bytes(b'new source data')
            shutil.copyfile(self.disk, destination)
        with patch.object(p, 'stream_copy', change_then_copy), \
                self.assertRaisesRegex(ValueError, 'Source changed'):
            p.verified_native_copy(self.disk, destination, report_path)
        report = json.loads(report_path.read_text())
        self.assertEqual(report['source_sha256_after'], report['destination_sha256'])

    def test_stream_copy_preserves_sparse_content_and_truncates_old_tail(self):
        with self.disk.open('wb') as f:
            f.seek(8 * 1024 * 1024)
            f.write(b'data near a block boundary')
            f.seek(17 * 1024 * 1024)
            f.write(b'final nonzero data')
            f.truncate(24 * 1024 * 1024 + 19)
        destination = self.root / 'old-copy.qcow2'
        with destination.open('wb') as f:
            f.write(b'old bytes')
            f.truncate(30 * 1024 * 1024)
        p.stream_copy(self.disk, destination)
        self.assertEqual(self.disk.stat().st_size, destination.stat().st_size)
        self.assertEqual(p.sha256(self.disk), p.sha256(destination))

    def test_stream_copy_refuses_same_source(self):
        before = self.disk.read_bytes()
        with self.assertRaisesRegex(ValueError, 'source disk'):
            p.stream_copy(self.disk, self.disk)
        self.assertEqual(self.disk.read_bytes(), before)

    def failed_copy_stage(self):
        def corrupt(source, destination):
            Path(destination).write_bytes(b'failed copy')
        with self.harness(), patch.object(p, 'stream_copy', corrupt), self.assertRaises(ValueError):
            self.archive()
        return next((self.root / 'stage').glob('native-100-*'))

    def test_resume_reuses_failed_copy_and_keeps_vm_when_requested(self):
        stage = self.failed_copy_stage()
        self.args.resume = str(stage)
        self.args.keep_vm = True
        self.args.delete_vm = False
        self.args.cleanup_local = False
        with self.harness():
            self.archive()
        self.assertEqual(len(list((self.root / 'stage').iterdir())), 1)
        self.assertEqual((stage / 'payload/scsi0.qcow2').read_bytes(), self.disk.read_bytes())
        self.assertEqual(self.file('100').read_text(), CONFIG)
        self.assertEqual(sum(c[:2] == ['qm', 'shutdown'] for c in self.commands), 1)

    def test_resume_rejects_changed_configuration_without_rewriting_copy(self):
        stage = self.failed_copy_stage()
        self.args.resume = str(stage)
        self.file('100').write_text(self.file('100').read_text().replace('memory: 24576', 'memory: 8192'))
        with self.harness(), self.assertRaisesRegex(ValueError, 'differs from the failed operation'):
            self.archive()
        self.assertEqual((stage / 'payload/scsi0.qcow2').read_bytes(), b'failed copy')

    def test_resume_rejects_wrong_directory(self):
        self.failed_copy_stage()
        self.args.resume = str(self.root)
        with self.harness(), self.assertRaisesRegex(ValueError, 'directly under'):
            self.archive()

    def test_native_cross_id_restore_preserves_snapshot_disk_and_remaps_config(self):
        with self.harness():
            m = self.archive()
            self.args.command = 'restore'
            self.args.vmid = '200'
            self.args.backup_id = m['backup_id']
            self.args.storage = 'local'
            self.args.unique = True
            self.m.restore()
        restored = p.parse_native_config(self.file('200').read_text())
        self.assertEqual(set(restored), {'current', 'before-change'})
        for cfg in restored.values():
            self.assertEqual(cfg['scsi0'], 'local:200/vm-200-disk-0.qcow2,size=64G')
        self.assertEqual(restored['current']['parent'], 'before-change')
        self.assertEqual(restored['current']['onboot'], '0')
        self.assertNotIn('lock', restored['current'])
        self.assertEqual(restored['current']['net0'], restored['before-change']['net0'])
        self.assertNotIn('02:00:00:00:01:00', restored['current']['net0'])
        self.assertEqual((self.root / 'vm-200-disk-0.qcow2').read_bytes(), self.disk.read_bytes())
        self.assertTrue(self.remote)

    def test_corrupt_native_download_never_creates_vm(self):
        with self.harness():
            m = self.archive()
            key = next(k for k in self.remote if k.endswith('/scsi0.qcow2'))
            self.remote[key] = b'corrupt'
            self.args.vmid = '200'
            self.args.backup_id = m['backup_id']
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                self.m.restore()
        self.assertFalse(any(c[:2] == ['qm', 'create'] for c in self.commands))

    def test_missing_internal_snapshot_prevents_deletion(self):
        self.info[0]['snapshots'] = []
        with self.harness(), self.assertRaisesRegex(ValueError, 'snapshots do not match'):
            self.archive()
        self.assertTrue(self.file('100').exists())

    def test_backing_chain_and_external_data_rejected(self):
        for info in [[dict(self.info[0], **{'backing-filename': '/base.qcow2'})],
                     self.info * 2,
                     [dict(self.info[0], **{'format-specific': {'data': {'data-file': 'data.raw'}}})]]:
            self.info = info
            with self.harness(), self.assertRaises(ValueError):
                self.m.qcow_info(self.disk, ['before-change'])

    def test_saved_ram_and_different_topology_rejected(self):
        with self.assertRaisesRegex(ValueError, 'vmstate'):
            p.parse_native_config(CONFIG + 'vmstate: local:100/vm-100-state-before-change.raw\n')
        sections = p.parse_native_config(CONFIG)
        sections['before-change']['scsi0'] = 'local:100/vm-100-disk-1.qcow2'
        with self.assertRaisesRegex(ValueError, 'same disk attachments'):
            p.native_volumes(sections, '100')

    def test_manifest_traversal_rejected(self):
        with self.harness():
            m = self.archive()
        m['disks'][0]['filename'] = '../disk.qcow2'
        with self.assertRaisesRegex(ValueError, 'Unsafe disk'):
            self.m.validate_native_manifest(m)

    def test_upload_automatically_preserves_snapshots(self):
        self.args.format = 'vzdump'
        with self.harness():
            self.m.move_to_cloud()
        manifest = json.loads(next(v for k, v in self.remote.items() if k.endswith('/manifest.json')))
        self.assertEqual(manifest['format'], 'native-qcow2')
        self.assertFalse(self.file('100').exists())

    def test_restore_by_vmid_selects_latest_complete_backup_and_original_storage(self):
        self.args.vmid = '100'
        self.args.storage = None
        old = '20260101T000000Z-' + 'a' * 32
        new = '20260905T000000Z-' + 'b' * 32
        rows = [{'Path': old + '/COMPLETE'}, {'Path': new + '/COMPLETE'},
                {'Path': '20260906T000000Z-' + 'c' * 32 + '/scsi0.qcow2'}]
        manifest = {'schema': 3, 'disks': [{'volume': 'local:100/vm-100-disk-0.qcow2'}]}
        with patch.object(self.m, 'rc', return_value=json.dumps(rows)), \
                patch.object(self.m, 'manifest', return_value=('remote', manifest)) as read, \
                patch.object(self.m, 'restore') as restore:
            self.m.move_from_cloud()
        read.assert_called_once_with('100/' + new)
        restore.assert_called_once()
        self.assertEqual(self.args.vmid, '100')
        self.assertEqual(self.args.storage, 'local')

    def test_restore_does_not_fall_back_when_latest_manifest_is_bad(self):
        rows = [{'Path': '20260905T000000Z-' + 'a' * 32 + '/COMPLETE'}]
        with patch.object(self.m, 'rc', return_value=json.dumps(rows)), \
                patch.object(self.m, 'manifest', side_effect=ValueError('checksum mismatch')), \
                patch.object(self.m, 'restore') as restore, self.assertRaises(ValueError):
            self.m.move_from_cloud()
        restore.assert_not_called()

    def test_simple_command_arguments(self):
        base = ['--remote', 'gdrive:pve-archive', '--source', 'pve-site-a']
        upload = p.parser().parse_args(base + ['upload', '100'])
        self.assertEqual(upload.vmid, '100')
        self.assertTrue(upload.cleanup_local)
        self.assertFalse(upload.keep_vm)
        restore = p.parser().parse_args(base + ['restore', '100'])
        self.assertEqual(restore.backup_id, '100')
        self.assertIsNone(restore.vmid)
        self.assertIsNone(restore.storage)

    def test_restore_alternate_id_keeps_original_source_lookup(self):
        self.args.vmid = '100'
        self.args.target_vmid = '200'
        self.args.storage = 'destination-dir'
        version = '20260905T000000Z-' + 'a' * 32
        with patch.object(self.m, 'rc', return_value=json.dumps([{'Path': version + '/COMPLETE'}])) as rc, \
                patch.object(self.m, 'manifest', return_value=('remote', {'schema': 3})) as manifest, \
                patch.object(self.m, 'restore') as restore:
            self.m.move_from_cloud()
        self.assertIn('gdrive:pve-archive/sources/pve-site-a/100', rc.call_args.args)
        manifest.assert_called_once_with('100/' + version)
        self.assertEqual(self.args.vmid, '200')
        self.assertEqual(self.args.storage, 'destination-dir')
        restore.assert_called_once()


if __name__ == '__main__':
    unittest.main()
