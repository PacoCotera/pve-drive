import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pve_drive as p


class ProgressTests(unittest.TestCase):
    def display(self, tty=False):
        stream = io.StringIO()
        stream.isatty = lambda: tty
        c = p.Console(stream=stream, log=io.StringIO())
        c.enabled = True
        return c, stream

    def test_redirected_progress_has_speed_eta_and_no_escapes(self):
        c, stream = self.display()
        c.stage('Uploading archive')
        c.update(512, 1024, speed=128, elapsed=4, force=True)
        self.assertIn('50.0%', stream.getvalue())
        self.assertIn('128.0 B/s', stream.getvalue())
        self.assertIn('ETA 00:00:04', stream.getvalue())
        self.assertNotIn('\033', stream.getvalue())

    def test_terminal_redraw_and_clear(self):
        c, stream = self.display(tty=True)
        c.stage('Copying')
        c.update(1, 2, speed=0, force=True)
        self.assertIn('ETA --:--:--', stream.getvalue())
        self.assertTrue(c.live)
        c.note('Done')
        self.assertFalse(c.live)

    def test_estimated_total_and_eta_are_explicit(self):
        c, stream = self.display()
        c.update(512, 1024, speed=128, elapsed=4, estimated=True, force=True)
        self.assertIn('est. max', stream.getvalue())
        self.assertIn('50.0% of est. max', stream.getvalue())
        self.assertIn('ETA (est. max) 00:00:04', stream.getvalue())

    def test_exceeded_estimate_does_not_claim_completion(self):
        c, stream = self.display()
        c.update(2048, 1024, estimated=True, force=True)
        self.assertIn('estimate exceeded; ETA unknown', stream.getvalue())
        self.assertNotIn('100%', stream.getvalue())

    def test_virtual_capacity_estimate_includes_overhead_and_excludes_cdrom(self):
        cfg = {'scsi0': 'local:disk,size=32G', 'scsi1': 'pool:disk,size=240G',
               'scsi2': 'pool:large,size=1T', 'ide2': 'none,media=cdrom'}
        size = 1296 * 1024 ** 3
        self.assertEqual(p.stream_size_estimate(cfg), (size * 102 + 99) // 100 + 64 * 1024 ** 2)

    def test_incomplete_sizes_disable_estimate(self):
        for value in ['pool:disk', 'pool:disk,size=unknown', 'pool:disk,size=0G']:
            self.assertIsNone(p.stream_size_estimate({'scsi0': 'pool:disk,size=1G', 'scsi1': value}))

    def test_small_and_fractional_disk_sizes(self):
        size = int(1.5 * 1024 ** 3) + 4 * 1024 ** 2
        self.assertEqual(p.stream_size_estimate({'scsi0': 'pool:disk,size=1.5G',
                                                'efidisk0': 'pool:efi,size=4M'}),
                         (size * 102 + 99) // 100 + 64 * 1024 ** 2)

    def test_rclone_stats_and_warning(self):
        c, stream = self.display()
        c.stage('Uploading')
        c.last = float('-inf')  # Independent of host/WSL uptime.
        c.output(json.dumps({'stats': {'bytes': 512, 'totalBytes': 1024,
                                     'speed': 128, 'elapsedTime': 4}}), rclone=True)
        c.output('{"level":"warning","msg":"Retrying transfer"}', rclone=True)
        self.assertIn('50.0%', stream.getvalue())
        self.assertIn('Retrying transfer', stream.getvalue())
        self.assertNotIn('"stats"', stream.getvalue())
        self.assertIn('"stats"', c.log.getvalue())

    def test_commands_hidden_but_logged(self):
        c, stream = self.display()
        c.command(['qm', 'status', '100'])
        self.assertEqual(stream.getvalue(), '')
        self.assertIn('qm status 100', c.log.getvalue())
        c.verbose = True
        c.command(['qm', 'status', '100'])
        self.assertIn('qm status 100', stream.getvalue())

    def test_capture_separates_stdout_and_stderr(self):
        c, stream = self.display()
        with patch.object(p, 'console', c):
            result = p.run(sys.executable, '-c',
                           'import sys, json; print(json.dumps(dict(ok=True))); print("diagnostic", file=sys.stderr)', capture=True)
        self.assertTrue(json.loads(result)['ok'])
        self.assertEqual(stream.getvalue(), '')
        self.assertIn('diagnostic', c.log.getvalue())

    def test_failed_command_retains_error_detail(self):
        c, _ = self.display()
        with patch.object(p, 'console', c):
            with self.assertRaisesRegex(RuntimeError, 'exit 7.*disk error'):
                p.run(sys.executable, '-c', 'import sys; print("disk error", file=sys.stderr); sys.exit(7)')

    def test_verbose_help_option(self):
        args = p.parser().parse_args(['--verbose', '--remote', 'gdrive:archive', '--source', 'test', 'list'])
        self.assertTrue(args.verbose)


if __name__ == '__main__':
    unittest.main()
