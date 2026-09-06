import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pve_drive as p


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.args = p.parser().parse_args([
            '--remote', 'gdrive:pve-archive', '--source', 'pve-site-b', '--work-dir', self.tmp.name,
            'archive', '100', '--delete-vm', '--deep-verify'])
        self.m = p.Manager(self.args)
        self.cfg = {'digest': 'abc', 'name': 'test', 'scsi0': 'local-lvm:vm-100-disk-0'}
        self.commands = []
        self.remote = {}
        self.failure = None
        self.deleted = False

    def command(self, *args, capture=False):
        args = list(map(str, args))
        self.commands.append(args)
        if args[:2] == ['qm', 'status']:
            return 'status: stopped\n'
        if args[0] == 'vzdump':
            stage = Path(args[args.index('--dumpdir') + 1])
            (stage / 'vzdump-qemu-100-2026_09_05-01_00_00.vma.zst').write_bytes(b'backup')
        if args[:2] == ['qm', 'set']:
            self.cfg['lock'] = 'backup'
        if args[:2] == ['qm', 'destroy']:
            self.deleted = True
        return ''

    def rc(self, *args, capture=False):
        args = list(map(str, args))
        self.commands.append(['rclone'] + args)
        if args[0] == self.failure:
            raise RuntimeError('injected failure')
        if args[0] == 'copy':
            for path in Path(args[1]).iterdir():
                self.remote[args[2] + '/' + path.name] = path.read_text()
        if args[0] == 'copyto':
            self.remote[args[2]] = Path(args[1]).read_text()
        if args[0] == 'cat':
            return self.remote[args[1]]
        return '[]'

    def api(self, path):
        if path.endswith('/config'):
            return self.cfg.copy()
        return []

    def execute(self):
        with patch.object(p, 'run', self.command), patch.object(self.m, 'rc', self.rc), \
                patch.object(self.m, 'api', self.api):
            self.m.archive()

    def test_destroy_only_after_readback_and_marker(self):
        self.execute()
        check = next(i for i, c in enumerate(self.commands) if c[:2] == ['rclone', 'check'])
        marker = next(i for i, c in enumerate(self.commands) if c[:2] == ['rclone', 'cat'])
        destroy = next(i for i, c in enumerate(self.commands) if c[:2] == ['qm', 'destroy'])
        self.assertLess(check, marker)
        self.assertLess(marker, destroy)
        self.assertTrue(self.deleted)

    def test_upload_failure_preserves_vm_and_local_files(self):
        self.failure = 'copy'
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertFalse(self.deleted)
        self.assertTrue(list(Path(self.tmp.name).rglob('*.vma.zst')))

    def test_readback_failure_preserves_vm(self):
        self.failure = 'check'
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertFalse(self.deleted)
        self.assertFalse(any(k.endswith('/COMPLETE') for k in self.remote))

    def test_marker_failure_preserves_vm(self):
        self.failure = 'cat'
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertFalse(self.deleted)

    def test_config_change_during_upload_preserves_vm(self):
        original = self.rc
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0] == 'check':
                self.cfg['scsi0'] = 'local-lvm:vm-100-disk-1'
            return result
        with patch.object(p, 'run', self.command), patch.object(self.m, 'rc', changed), \
                patch.object(self.m, 'api', self.api), self.assertRaises(ValueError):
            self.m.archive()
        self.assertFalse(self.deleted)

    def test_keep_vm_does_not_destroy(self):
        self.args.delete_vm = False
        self.execute()
        self.assertFalse(self.deleted)
        self.assertIn(['qm', 'unlock', '100'], self.commands)

    def test_restore_existing_id_never_downloads(self):
        with patch.object(self.m, 'api', return_value=[{'vmid': 100}]), \
                patch.object(self.m, 'download') as download, self.assertRaises(ValueError):
            self.m.restore()
        download.assert_not_called()

    def test_corrupt_download_never_restores(self):
        self.args.backup_id = '100/20260905T010000Z-' + 'a' * 32
        self.args.storage = 'local-lvm'
        manifest = {'filename': 'backup.vma.zst', 'size': 6, 'sha256': hashlib.sha256(b'backup').hexdigest()}
        def corrupt(*args, **kwargs):
            Path(args[2]).write_bytes(b'broken')
        with patch.object(self.m, 'api', return_value=[]), \
                patch.object(self.m, 'manifest', return_value=('remote', manifest)), \
                patch.object(self.m, 'rc', corrupt), patch.object(p, 'run') as command, \
                self.assertRaises(ValueError):
            self.m.restore()
        command.assert_not_called()

    def test_manifest_path_traversal_rejected(self):
        bid = '100/20260905T010000Z-' + 'a' * 32
        raw = json.dumps({'schema': 2, 'source': 'pve-site-b', 'backup_id': bid, 'vmid': 100, 'filename': '../bad'})
        with patch.object(self.m, 'rc', side_effect=[hashlib.sha256(raw.encode()).hexdigest(), raw]), \
                self.assertRaises(ValueError):
            self.m.manifest(bid)

    def test_unbacked_resources_rejected(self):
        for config in [{'scsi0': 'pool:disk,backup=0'}, {'unused0': 'pool:disk'},
                       {'hostpci0': 'mapping=gpu'}, {'args': '-drive secret'},
                       {'ide2': '/dev/cdrom,media=cdrom'}, {'scsi0': '/dev/sda'}]:
            with self.subTest(config=config), self.assertRaises(ValueError):
                p.safe_config(config)

    def test_iso_is_recorded_without_changing_vm(self):
        self.cfg['ide2'] = 'local:iso/installer.iso,media=cdrom,backup=0'
        self.execute()
        manifest = json.loads(next(v for k, v in self.remote.items() if k.endswith('/manifest.json')))
        self.assertEqual(manifest['external_media'], {'ide2': 'local:iso/installer.iso'})
        self.assertEqual(self.cfg['ide2'], 'local:iso/installer.iso,media=cdrom,backup=0')
        self.assertTrue(self.deleted)

    def test_empty_and_cloudinit_media_are_not_external_isos(self):
        self.assertEqual(p.safe_config({'ide2': 'none,media=cdrom'}), {})
        self.assertEqual(p.safe_config({'ide2': 'local-lvm:vm-100-cloudinit,media=cdrom'}), {})

    def with_snapshots(self):
        original = self.api
        def snapshot_api(path):
            if path.endswith('/snapshot'):
                return [{'name': 'current'}, {'name': 'before-upgrade'}]
            return original(path)
        return snapshot_api

    def test_keep_vm_with_snapshots_records_exclusion(self):
        self.args.delete_vm = False
        with patch.object(self, 'api', self.with_snapshots()):
            self.execute()
        self.assertFalse(self.deleted)
        manifest = json.loads(next(v for k, v in self.remote.items() if k.endswith('/manifest.json')))
        self.assertEqual(manifest['excluded_snapshots'], ['before-upgrade'])

    def test_delete_snapshots_requires_explicit_flag_before_shutdown(self):
        with patch.object(self, 'api', self.with_snapshots()), \
                self.assertRaisesRegex(ValueError, '--allow-snapshot-loss'):
            self.execute()
        self.assertFalse(self.deleted)
        self.assertFalse(any(c[:2] == ['qm', 'shutdown'] for c in self.commands))

    def test_delete_snapshots_with_explicit_flag(self):
        self.args.allow_snapshot_loss = True
        with patch.object(self, 'api', self.with_snapshots()):
            self.execute()
        self.assertTrue(self.deleted)

    def test_sources_separate_same_vmid(self):
        first = self.m.base
        self.args.source = 'lab-pve1'
        second = p.Manager(self.args).base
        self.assertEqual(first, 'gdrive:pve-archive/sources/pve-site-b')
        self.assertEqual(second, 'gdrive:pve-archive/sources/lab-pve1')
        self.assertNotEqual(first + '/100', second + '/100')

    def test_source_mismatch_rejected(self):
        bid = '100/20260905T010000Z-' + 'a' * 32
        raw = json.dumps({'schema': 2, 'source': 'other-server', 'backup_id': bid, 'vmid': 100})
        with patch.object(self.m, 'rc', side_effect=[hashlib.sha256(raw.encode()).hexdigest(), raw]), \
                self.assertRaisesRegex(ValueError, 'source does not match'):
            self.m.manifest(bid)

    def test_unsafe_source_rejected(self):
        for source in ['../node', 'node/path', '', '.', 'x' * 65]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                p.source_name(source)

    def test_legacy_archive_rejected(self):
        self.args.source = None
        with self.assertRaisesRegex(ValueError, 'legacy layout is read-only'):
            p.Manager(self.args)

    def test_legacy_manifest_readable(self):
        self.args.source = None
        self.args.command = 'verify'
        manager = p.Manager(self.args)
        bid = '100/20260905T010000Z-' + 'a' * 32
        raw = json.dumps({'schema': 1, 'backup_id': bid, 'vmid': 100,
                          'filename': 'vzdump-qemu-100-test.vma.zst', 'size': 6, 'sha256': 'a' * 64})
        with patch.object(manager, 'rc', side_effect=[hashlib.sha256(raw.encode()).hexdigest(), raw]):
            destination, _ = manager.manifest(bid)
        self.assertEqual(destination, 'gdrive:pve-archive/' + bid)


if __name__ == '__main__':
    unittest.main()
