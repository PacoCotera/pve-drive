import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pve_drive as p


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stage = self.root / 'stream-100-example'
        self.stage.mkdir()
        self.a = p.parser().parse_args(['--remote', 'gdrive:archive', '--source', 'test',
            '--work-dir', str(self.root), 'cleanup', '--stage', str(self.stage)])
        self.m = p.Manager(self.a)
        self.bid = '100/20260905T010000Z-' + 'a' * 32
        self.dest = self.m.base + '/' + self.bid
        (self.stage / 'attempt.json').write_text(json.dumps({'backup_id': self.bid, 'destination': self.dest}))
        self.rows = [{'Path': self.bid + '/archive.vma.zst'}]
        self.purges = []

    def rc(self, *args, **kwargs):
        if args[0] == 'purge':
            self.purges.append(args[1])
            self.rows = []
            return ''
        return json.dumps(self.rows)

    def execute(self):
        with patch.object(self.m, 'rc', self.rc):
            self.m.cleanup()

    def test_preview_preserves_local_and_remote(self):
        self.execute()
        self.assertTrue(self.stage.exists())
        self.assertEqual(self.purges, [])

    def test_apply_removes_only_selected_attempt(self):
        other = self.root / 'stream-200-other'
        other.mkdir()
        self.a.apply = True
        self.execute()
        self.assertFalse(self.stage.exists())
        self.assertTrue(other.exists())
        self.assertEqual(self.purges, [self.dest])

    def test_any_completion_marker_protects_cloud_archive(self):
        self.rows.append({'Path': self.bid + '/COMPLETE'})
        self.a.apply = True
        self.execute()
        self.assertFalse(self.stage.exists())
        self.assertEqual(self.purges, [])

    def test_cloud_completion_between_checks_is_preserved(self):
        self.a.apply = True
        with patch.object(self.m, 'rc', side_effect=[json.dumps(self.rows),
                json.dumps(self.rows + [{'Path': self.bid + '/COMPLETE'}])]) as rc:
            self.m.cleanup()
        self.assertTrue(all(call.args[0] == 'lsjson' for call in rc.call_args_list))

    def test_listing_or_purge_failure_retains_local_recovery(self):
        self.a.apply = True
        for outputs in ([RuntimeError('offline')], [json.dumps(self.rows), json.dumps(self.rows), RuntimeError('purge failed')]):
            with self.subTest(outputs=outputs), patch.object(self.m, 'rc', side_effect=outputs), self.assertRaises(RuntimeError):
                self.m.cleanup()
            self.assertTrue(self.stage.exists())

    def test_wrong_source_or_vmid_refused(self):
        self.a.apply = True
        (self.stage / 'attempt.json').write_text(json.dumps({'backup_id': self.bid, 'destination': 'other:remote/' + self.bid}))
        with self.assertRaises(ValueError):
            self.execute()
        self.assertTrue(self.stage.exists())

    def test_parent_or_unrecognized_directory_refused(self):
        for value in [str(self.root), str(self.root / 'unknown')]:
            self.a.stage = value
            with self.subTest(path=value), self.assertRaises(ValueError):
                self.execute()

    def test_restore_files_cleanup_never_touches_cloud_or_vm(self):
        restore = self.root / 'restore-example'
        restore.mkdir()
        (restore / 'restore-state.json').write_text('{"vmid":200,"allocated":["pool:disk"]}')
        self.a.stage = str(restore)
        self.a.apply = True
        with patch.object(self.m, 'rc') as rc, patch.object(p, 'run') as command:
            self.m.cleanup()
        rc.assert_not_called()
        command.assert_not_called()
        self.assertFalse(restore.exists())

    def test_apply_requires_exact_stage(self):
        self.a.stage = None
        self.a.apply = True
        with self.assertRaises(ValueError):
            self.execute()
