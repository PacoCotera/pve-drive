import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pve_drive as p
ORIGINAL_RUN = p.run


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dirs = {name: self.root / name for name in ('local', 'hdd')}
        for directory in self.dirs.values():
            directory.mkdir()
        self.name = 'vzdump-qemu-100-2026_09_06-12_00_00.vma.zst'
        self.path = self.dirs['local'] / self.name
        self.data = bytes(range(256)) * 5
        self.path.write_bytes(self.data)
        Path(str(self.path) + '.notes').write_bytes('Permanent lab copy — keep'.encode())
        self.args = p.parser().parse_args(['--remote', 'gdrive:archive', '--source', 'site',
                                         '--work-dir', str(self.root / 'work'), 'backups', 'upload', self.name])
        self.args.part_size, self.args.transfers, self.args.quota_retries = 300, 2, 0
        self.lib = p.BackupLibrary(self.args)
        self.remote, self.commands = {}, []
        self.active = False
        self.fail = None
        self.max_spool = 0
        self.extra_stores = []
        self.disabled = set()
        self.addCleanup(patch.stopall)
        patch.object(p, 'run', self.fake_run).start()
        patch.object(self.lib, 'api', self.api).start()
        patch.object(self.lib, 'rc', self.rc).start()
        patch.object(self.lib, 'part_rc', lambda cancel, *a, **kw: self.rc(*a, **kw)).start()

    def api(self, route):
        if route.endswith('/storage'):
            return [dict(storage=k, content='backup', type='dir', active=1, enabled=k not in self.disabled,
                         avail=100 * 1024 ** 3) for k in self.dirs] + self.extra_stores
        store = route.split('/')[-2]
        return [dict(volid=f'{store}:backup/{x.name}', content='backup', size=x.stat().st_size,
                     ctime=1780000000, protected=int(Path(str(x) + '.protected').exists()))
                for x in self.dirs[store].iterdir() if x.is_file() and x.name.endswith(('.vma.zst', '.tar.gz'))]

    def fake_run(self, *args, **kwargs):
        self.commands.append(tuple(map(str, args)))
        if args[:2] == ('pvesm', 'path'):
            store, name = args[2].split(':backup/')
            return str(self.dirs[store] / name) + '\n'
        if args[:2] == ('pvesh', 'get'):
            return json.dumps([{'type': 'vzdump'}] if self.active else [])
        if args[:2] == ('pvesm', 'free'):
            store, name = args[2].split(':backup/')
            path = self.dirs[store] / name
            path.unlink()
            Path(str(path) + '.notes').unlink(missing_ok=True)
            return ''
        raise AssertionError(args)

    def rc(self, *args, capture=False):
        a = list(map(str, args))
        self.commands.append(tuple(a))
        if self.fail and self.fail(a):
            raise RuntimeError('injected quota uploadLimitExceeded')
        if a[0] == 'mkdir':
            return ''
        if a[0] == 'cat':
            return self.remote[a[1]].decode()
        if a[0] == 'copyto':
            if a[1].startswith('gdrive:'):
                if a[1] not in self.remote:
                    raise RuntimeError('missing remote part')
                Path(a[2]).write_bytes(self.remote[a[1]])
            else:
                data = Path(a[1]).read_bytes()
                if a[2] in self.remote and self.remote[a[2]] != data:
                    raise RuntimeError('immutable conflict')
                self.remote[a[2]] = data
                if '/spool/' in Path(a[1]).as_posix():
                    self.max_spool = max(self.max_spool, len(list(Path(a[1]).parent.iterdir())))
            return ''
        if a[0] == 'lsjson':
            if a[1] in self.remote:
                selected = [(a[1].rsplit('/', 1)[-1], self.remote[a[1]])]
            else:
                prefix = a[1] + '/'
                selected = [(k[len(prefix):], v) for k, v in list(self.remote.items()) if k.startswith(prefix)]
            return json.dumps([{'Path': name, 'Size': len(data), 'Hashes': {'md5': hashlib.md5(data).hexdigest()}}
                               for name, data in selected])
        if a[0] == 'purge':
            for name in list(self.remote):
                if name.startswith(a[1] + '/'):
                    del self.remote[name]
            return ''
        raise AssertionError(a)

    def upload(self):
        return self.lib.upload_file(self.name)

    def test_discovery_all_stores_custom_paths_and_pbs_skip(self):
        (self.dirs['hdd'] / self.name).write_bytes(b'other')
        self.extra_stores = [dict(storage='pbs', type='pbs', active=1, enabled=1, content='backup')]
        stores = self.lib.stores()
        self.assertFalse(next(x for x in stores if x['storage'] == 'pbs')['suitable'])
        self.assertEqual({x['storage'] for x in self.lib.local()}, {'local', 'hdd'})
        with self.assertRaisesRegex(ValueError, 'unambiguous'):
            self.upload()

    def test_copy_default_parts_order_metadata_and_bounded_spool(self):
        m = self.upload()
        self.assertTrue(self.path.exists())
        self.assertEqual(m['storage'], 'local')
        self.assertEqual(m['source'], 'site')
        self.assertEqual(m['original_volume'], 'local:backup/' + self.name)
        self.assertEqual(m['sha256'], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(b''.join(self.remote[self.lib.base + '/' + m['backup_id'] + '/' + part['filename']]
                                  for part in m['parts']), self.data)
        self.assertLessEqual(self.max_spool, self.args.transfers)
        self.assertEqual(self.lib.cloud(), [m])
        self.assertFalse(any(c[0] in ('qm', 'qmrestore', 'vzdump') for c in self.commands))

    def test_inventory_marks_both_as_unverified_name_size_match(self):
        self.upload()
        with patch('sys.stdout', io.StringIO()) as output:
            self.lib.listing_files()
        self.assertIn('both*', output.getvalue())
        self.assertIn('not checksum-verified', output.getvalue())

    def test_tar_backup_file_uses_same_transport(self):
        name = 'vzdump-lxc-101-2026_09_06-12_00_00.tar.gz'
        (self.dirs['local'] / name).write_bytes(b'existing-container-backup')
        m = self.lib.upload_file(name)
        self.assertEqual(m['vmid'], 101)
        self.args.storage = 'hdd'
        self.assertEqual(self.lib.download_file(m['backup_id']).read_bytes(), b'existing-container-backup')

    def test_move_deletes_only_after_complete_readback(self):
        self.args.delete_local = True
        m = self.upload()
        self.assertFalse(self.path.exists())
        deletion = next(i for i, c in enumerate(self.commands) if c[:2] == ('pvesm', 'free'))
        marker = max(i for i, c in enumerate(self.commands) if c[0] == 'cat' and c[1].endswith('/COMPLETE'))
        self.assertLess(marker, deletion)
        self.assertTrue(self.lib.file_manifest(m['backup_id']))

    def test_protected_backup_copy_download_and_move_refusal(self):
        Path(str(self.path) + '.protected').touch()
        self.args.delete_local = True
        with self.assertRaisesRegex(ValueError, 'Protected'):
            self.upload()
        self.args.delete_local = False
        m = self.upload()
        self.args.storage = 'hdd'
        target = self.lib.download_file(m['backup_id'])
        self.assertTrue(Path(str(target) + '.protected').exists())
        self.assertEqual(Path(str(target) + '.notes').read_bytes(), Path(str(self.path) + '.notes').read_bytes())

    def test_active_job_blocks_upload_before_staging(self):
        self.active = True
        with self.assertRaisesRegex(ValueError, 'active'):
            self.upload()
        self.assertFalse(self.remote)
        self.assertTrue(self.path.exists())

    def test_quota_failure_resume_reuses_verified_parts(self):
        self.fail = lambda a: a[0] == 'copyto' and a[2].endswith('part-000003')
        with self.assertRaises(RuntimeError):
            self.upload()
        self.assertFalse(any(k.endswith('/COMPLETE') for k in self.remote))
        self.assertTrue(self.path.exists())
        completed = {k for k in self.remote if '/part-' in k}
        self.fail = None
        self.commands.clear()
        m = self.upload()
        resent = {c[2] for c in self.commands if c[0] == 'copyto'}
        self.assertFalse(completed & resent)
        self.assertTrue(self.lib.file_manifest(m['backup_id']))

    def test_manifest_or_marker_failure_never_deletes(self):
        self.args.delete_local = True
        self.fail = lambda a: a[0] == 'copyto' and a[2].endswith('/COMPLETE')
        with self.assertRaises(RuntimeError):
            self.upload()
        self.assertTrue(self.path.exists())
        self.assertEqual(self.lib.cloud(), [])

    def test_changed_source_resume_refused(self):
        self.fail = lambda a: a[0] == 'copyto'
        with self.assertRaises(RuntimeError):
            self.upload()
        self.path.write_bytes(b'changed')
        self.fail = None
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.upload()

    def test_download_exact_bytes_to_selected_store_without_vm_creation(self):
        m = self.upload()
        self.args.storage = 'hdd'
        target = self.lib.download_file(m['backup_id'])
        self.assertEqual(target, self.dirs['hdd'] / self.name)
        self.assertEqual(target.read_bytes(), self.data)
        self.assertTrue(self.lib.file_manifest(m['backup_id']))
        self.assertFalse(list(self.dirs['hdd'].glob('.pve-drive-*')))

    def test_store_auto_original_only_fallback_and_ambiguity(self):
        m = self.upload()
        self.assertEqual(self.lib.select_store(m)['storage'], 'local')
        self.args.interactive = True
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='2'), patch('sys.stdout', io.StringIO()):
            self.assertEqual(self.lib.select_store(m)['storage'], 'hdd')
        self.args.interactive = False
        m['storage'] = 'old-store'
        self.args.storage = None
        with self.assertRaisesRegex(ValueError, 'Several'):
            self.lib.select_store(m)
        self.args.storage = 'auto'
        self.assertIn(self.lib.select_store(m)['storage'], self.dirs)
        self.disabled = {'local'}
        self.args.storage = None
        self.assertEqual(self.lib.select_store(m)['storage'], 'hdd')

    def test_insufficient_space_or_disabled_explicit_store(self):
        m = self.upload()
        self.args.storage = 'local'
        self.disabled = {'local'}
        with self.assertRaisesRegex(ValueError, 'Selected store'):
            self.lib.select_store(m)
        with patch.object(self.lib, 'stores', return_value=[dict(storage='local', suitable=True, writable=True,
                                                              directory=str(self.dirs['local']), free=1)]):
            with self.assertRaises(ValueError):
                self.lib.select_store(m)

    def test_missing_or_corrupted_part_download_never_publishes(self):
        m = self.upload()
        self.args.storage = 'hdd'
        key = self.lib.base + '/' + m['backup_id'] + '/part-000000'
        original = self.remote.pop(key)
        with self.assertRaises(RuntimeError):
            self.lib.download_file(m['backup_id'])
        self.remote[key] = bytes([original[0] ^ 1]) + original[1:]
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.lib.download_file(m['backup_id'])
        self.assertFalse((self.dirs['hdd'] / self.name).exists())
        self.remote[key] = original
        self.assertEqual(self.lib.download_file(m['backup_id']).read_bytes(), self.data)

    def test_conflicting_target_never_overwritten(self):
        m = self.upload()
        self.args.storage = 'local'
        with self.assertRaisesRegex(ValueError, 'exists'):
            self.lib.download_file(m['backup_id'])
        self.assertEqual(self.path.read_bytes(), self.data)

    def test_manifest_rejects_traversal_reordering_wrong_sizes_and_whole_hash(self):
        m = self.upload()
        for bad in [dict(m, filename='../bad'), dict(m, parts=list(reversed(m['parts']))),
                    dict(m, size=m['size'] + 1), dict(m, sidecars={'../notes': ''})]:
            with self.assertRaises(ValueError):
                self.lib.validate_file_manifest(bad, m['backup_id'])
        bad = dict(m, sha256='f' * 64)
        raw = (json.dumps(bad) + '\n').encode()
        prefix = self.lib.base + '/' + m['backup_id']
        self.remote[prefix + '/manifest.json'] = raw
        self.remote[prefix + '/COMPLETE'] = (hashlib.sha256(raw).hexdigest() + '\n').encode()
        self.args.storage = 'hdd'
        with self.assertRaisesRegex(ValueError, 'Whole-file'):
            self.lib.download_file(m['backup_id'])
        self.assertFalse((self.dirs['hdd'] / self.name).exists())

    def test_cleanup_incomplete_attempt_preserves_original(self):
        self.fail = lambda a: a[0] == 'copyto' and a[2].endswith('part-000003')
        with self.assertRaises(RuntimeError):
            self.upload()
        stage = next(self.lib.work.glob('backup-file-*'))
        self.args.stage, self.args.apply, self.fail = str(stage), False, None
        self.lib.cleanup_files()
        self.assertTrue(stage.exists())
        self.args.apply = True
        self.lib.cleanup_files()
        self.assertFalse(stage.exists())
        self.assertFalse(self.remote)
        self.assertTrue(self.path.exists())

    def test_cleanup_complete_attempt_keeps_cloud(self):
        self.args.cleanup_local = False
        m = self.upload()
        self.args.stage = str(next(self.lib.work.glob('backup-file-*')))
        self.args.apply = True
        self.lib.cleanup_files()
        self.assertTrue(self.lib.file_manifest(m['backup_id']))

    def test_source_mutation_during_upload_never_completes_or_deletes(self):
        original = self.lib.upload_vma_part
        mutated = False
        def change(*args):
            nonlocal mutated
            original(*args)
            if not mutated:
                mutated = True
                self.path.write_bytes(self.data[:-1] + b'!')
        self.args.delete_local = True
        with patch.object(self.lib, 'upload_vma_part', side_effect=change), self.assertRaises(ValueError):
            self.upload()
        self.assertTrue(self.path.exists())
        self.assertFalse(any(k.endswith('/COMPLETE') for k in self.remote))

    def test_interrupted_sidecar_publication_resumes_without_overwrite(self):
        m = self.upload()
        self.args.storage = 'hdd'
        link = os.link
        def fail_backup(source, target):
            if Path(target).name == self.name:
                raise OSError('interrupted publication')
            return link(source, target)
        with patch.object(p.os, 'link', side_effect=fail_backup), self.assertRaisesRegex(OSError, 'interrupted'):
            self.lib.download_file(m['backup_id'])
        self.assertTrue((self.dirs['hdd'] / (self.name + '.notes')).exists())
        self.assertFalse((self.dirs['hdd'] / self.name).exists())
        self.assertEqual(self.lib.download_file(m['backup_id']).read_bytes(), self.data)

    def test_interrupted_after_backup_publication_only_finishes_cleanup(self):
        m = self.upload()
        self.args.storage = 'hdd'
        with patch.object(p.shutil, 'rmtree', side_effect=OSError('cleanup failed')), self.assertRaises(OSError):
            self.lib.download_file(m['backup_id'])
        self.assertEqual((self.dirs['hdd'] / self.name).read_bytes(), self.data)
        self.assertEqual(self.lib.download_file(m['backup_id']).read_bytes(), self.data)

    def test_download_resume_credits_partial_reconstruction_space(self):
        m = self.upload()
        self.args.storage = 'hdd'
        key = self.lib.base + '/' + m['backup_id'] + '/part-000000'
        original = self.remote.pop(key)
        with self.assertRaises(RuntimeError):
            self.lib.download_file(m['backup_id'])
        stage = next(self.dirs['hdd'].glob('.pve-drive-*'))
        # Simulate an interrupted reconstruction with all parts already present.
        for part in m['parts']:
            data = original if part['filename'] == 'part-000000' else self.remote[self.lib.base + '/' + m['backup_id'] + '/' + part['filename']]
            (stage / 'parts' / part['filename']).write_bytes(data)
        partial_size = m['size'] // 2
        (stage / (m['filename'] + '.partial')).write_bytes(self.data[:partial_size])
        free = m['size'] - partial_size + 1024 ** 3
        with patch.object(self.lib, 'stores', return_value=[dict(storage='hdd', suitable=True, writable=True,
                                                              directory=str(self.dirs['hdd']), free=free)]):
            self.assertEqual(self.lib.select_store(m)['storage'], 'hdd')

    def test_cleanup_failed_remote_delete_preserves_recovery(self):
        self.fail = lambda a: a[0] == 'copyto' and a[2].endswith('/COMPLETE')
        with self.assertRaises(RuntimeError):
            self.upload()
        stage = next(self.lib.work.glob('backup-file-*'))
        self.args.stage, self.args.apply = str(stage), True
        self.fail = lambda a: a[0] == 'purge'
        with self.assertRaises(RuntimeError):
            self.lib.cleanup_files()
        self.assertTrue(stage.exists())

    def test_completed_download_staging_does_not_require_another_full_copy(self):
        m = self.upload()
        self.args.storage = 'hdd'
        with patch.object(p.os, 'link', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.lib.download_file(m['backup_id'])
        with patch.object(p, 'check_parts', side_effect=AssertionError('must reuse verified reconstruction')):
            self.assertEqual(self.lib.download_file(m['backup_id']).read_bytes(), self.data)

    def test_download_cleanup_retains_original_and_cloud(self):
        m = self.upload()
        self.args.storage = 'hdd'
        key = self.lib.base + '/' + m['backup_id'] + '/part-000000'
        del self.remote[key]
        with self.assertRaises(RuntimeError):
            self.lib.download_file(m['backup_id'])
        stage = next(self.dirs['hdd'].glob('.pve-drive-*'))
        self.args.stage, self.args.apply = str(stage), True
        self.lib.cleanup_files()
        self.assertFalse(stage.exists())
        self.assertTrue(self.path.exists())
        self.assertTrue(self.lib.file_manifest(m['backup_id']))

    @unittest.skipUnless(sys.platform == 'linux', 'Linux symlink semantics')
    def test_symlink_target_and_staging_rejected(self):
        m = self.upload()
        self.args.storage = 'hdd'
        target = self.dirs['hdd'] / self.name
        target.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, 'exists'):
            self.lib.download_file(m['backup_id'])
        target.unlink()
        key = hashlib.sha256((self.lib.base + '/' + m['backup_id']).encode()).hexdigest()[:24]
        (self.dirs['hdd'] / ('.pve-drive-' + key)).symlink_to(self.dirs['local'], target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.lib.download_file(m['backup_id'])

    @unittest.skipUnless(shutil.which('rclone'), 'real rclone required')
    def test_real_parallel_rclone_roundtrip(self):
        # Restore the real transport but simulate only PVE inventory/commands.
        # The module-level original is saved below before patches are installed.
        def dispatch(*args, **kwargs):
            return ORIGINAL_RUN(*args, **kwargs) if args[0] == 'rclone' else self.fake_run(*args, **kwargs)
        config = self.root / 'rclone.conf'
        config.write_text('[gdrive]\ntype = local\n')
        self.args.rclone_config = str(config)
        self.args.remote = 'gdrive:' + str(self.root / 'remote')
        self.lib.base = self.args.remote + '/backup-files/' + self.args.source
        self.args.part_size = 400
        with patch.object(p, 'run', side_effect=dispatch), \
                patch.object(self.lib, 'rc', p.Manager.rc.__get__(self.lib)), \
                patch.object(self.lib, 'part_rc', p.Manager.part_rc.__get__(self.lib)):
            m = self.upload()
            self.args.storage = 'hdd'
            target = self.lib.download_file(m['backup_id'])
        self.assertEqual(target.read_bytes(), self.data)


class MenuTests(unittest.TestCase):
    def args(self):
        return p.parser().parse_args(['--remote', 'gdrive:archive', '--source', 'site', 'interactive'])

    def test_archive_menu_defaults_to_retention_and_batch_commands(self):
        rows = [{'vmid': 100}, {'vmid': 101}]
        with patch.object(p.Manager, 'api', return_value=rows), patch.object(p, 'choose_rows', side_effect=[rows, 'Retain VMs stopped']):
            commands, destructive = p.interactive_actions(self.args(), 'Archive a VM')
        self.assertEqual(commands, [['upload', '100'], ['upload', '101']])
        self.assertFalse(destructive)

    def test_local_backup_menu_builds_copy_without_deletion(self):
        rows = [{'volid': 'local:backup/example', 'protected': False}]
        with patch.object(p.BackupLibrary, 'local', return_value=rows), patch.object(p, 'choose_rows', side_effect=[rows, 'Copy; retain local files']):
            commands, destructive = p.interactive_actions(self.args(), 'Local backup files')
        self.assertEqual(commands, [['backups', 'upload', 'local:backup/example']])
        self.assertFalse(destructive)

    def test_cloud_download_menu_resolves_store_before_execution(self):
        bid = 'old/100/20260906T000000Z-' + 'a' * 32
        rows = [{'backup_id': bid, 'size': 20}]
        with patch.object(p.BackupLibrary, 'cloud', return_value=rows), patch.object(p, 'choose_rows', return_value=rows), \
                patch.object(p.BackupLibrary, 'select_store', return_value={'storage': 'hdd'}), patch('sys.stdout', io.StringIO()):
            commands, destructive = p.interactive_actions(self.args(), 'Cloud backup files')
        self.assertEqual(commands, [['backups', 'download', bid, '--storage', 'hdd']])
        self.assertFalse(destructive)

    def test_vm_restore_menu_uses_existing_restore_implementation(self):
        bid = '100/20260906T000000Z-' + 'a' * 32
        m = {'backup_id': bid, 'vmid': 100, 'size': 20}
        store = dict(storage='local', active=1, content='images', avail=5000)
        with patch.object(p.Manager, 'rc', return_value=json.dumps([{'Path': bid + '/COMPLETE'}])), \
                patch.object(p.Manager, 'manifest', return_value=('remote', m)), \
                patch.object(p.Manager, 'api', side_effect=[200, [store]]), \
                patch.object(p, 'choose_rows', side_effect=[m, store]), patch('builtins.input', return_value=''), patch('sys.stdout', io.StringIO()):
            commands, destructive = p.interactive_actions(self.args(), 'Cloud VM archives / restore')
        self.assertEqual(commands, [['restore', bid, '200', '--storage', 'local', '--unique']])
        self.assertFalse(destructive)

    def test_noninteractive_refuses_to_prompt(self):
        with patch('sys.stdin.isatty', return_value=False), patch('builtins.input') as ask:
            with self.assertRaisesRegex(ValueError, 'terminal'):
                p.choose_rows('Select', [1], str)
        ask.assert_not_called()

    def test_multiselect_invalid_retry_dedup_and_cancel(self):
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=['99', '2,1,2']), patch('sys.stdout', io.StringIO()):
            self.assertEqual(p.choose_rows('Select', ['a', 'b'], str, multiple=True), ['b', 'a'])
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value=''), patch('sys.stdout', io.StringIO()):
            self.assertIsNone(p.choose_rows('Select', ['a'], str))
            self.assertEqual(p.choose_rows('Select', ['a'], str, default=1), 'a')

    def test_direct_commands_have_no_interactive_flag(self):
        base = ['--remote', 'gdrive:archive', '--source', 'site']
        args = p.parser().parse_args(base + ['backups', 'upload', 'example'])
        self.assertFalse(args.delete_local)
        self.assertFalse(getattr(args, 'interactive', False))

    def test_cancel_menu_summary_never_executes(self):
        args = p.parser().parse_args(['--remote', 'gdrive:archive', '--source', 'site', 'interactive'])
        with patch('sys.stdin.isatty', return_value=True), patch.object(p, 'choose_rows', side_effect=['Archive a VM', 'Exit']), \
                patch.object(p, 'interactive_actions', return_value=([['upload', '100']], False)), \
                patch('builtins.input', return_value=''), patch.object(p, 'execute_command') as execute, patch('sys.stdout', io.StringIO()):
            p.interactive_menu(args)
        execute.assert_not_called()

    def test_deletion_requires_exact_delete_confirmation(self):
        args = p.parser().parse_args(['--remote', 'gdrive:archive', '--source', 'site', 'interactive'])
        with patch('sys.stdin.isatty', return_value=True), patch.object(p, 'choose_rows', side_effect=['Archive a VM', 'Exit']), \
                patch.object(p, 'interactive_actions', return_value=([['upload', '100', '--delete-vm']], True)), \
                patch('builtins.input', return_value='yes'), patch.object(p, 'execute_command') as execute, patch('sys.stdout', io.StringIO()):
            p.interactive_menu(args)
        execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
