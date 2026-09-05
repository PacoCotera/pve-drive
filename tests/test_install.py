from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'pve_drive.py'
        self.source.write_bytes(b'#!/usr/bin/env python3\nprint("installed")\n')
        self.target = self.root / 'sbin' / 'pve-drive'

    def test_fresh_install_and_update(self):
        self.assertIsNone(install.install_to(self.source, self.target))
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        self.source.write_bytes(b'print("updated")\n')
        install.install_to(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        self.assertEqual(list(self.target.parent.glob('.pve-drive-install-*')), [])

    def test_preserves_old_directory_including_source_inside_it(self):
        self.target.mkdir(parents=True)
        original = self.target / 'pve_drive.py'
        original.write_bytes(self.source.read_bytes())
        (self.target / 'notes.txt').write_text('retain me')
        backup = install.install_to(original, self.target)
        self.assertTrue(self.target.is_file())
        self.assertEqual((backup / 'notes.txt').read_text(), 'retain me')
        self.assertEqual((backup / 'pve_drive.py').read_bytes(), self.target.read_bytes())

    def test_invalid_source_leaves_existing_installation_untouched(self):
        self.target.mkdir(parents=True)
        self.source.write_text('invalid Python !!!')
        with self.assertRaises(SyntaxError):
            install.install_to(self.source, self.target)
        self.assertTrue(self.target.is_dir())

    def test_failed_replace_restores_original_directory(self):
        self.target.mkdir(parents=True)
        (self.target / 'notes.txt').write_text('retain me')
        with patch.object(install.os, 'replace', side_effect=OSError('injected failure')), self.assertRaises(OSError):
            install.install_to(self.source, self.target)
        self.assertEqual((self.target / 'notes.txt').read_text(), 'retain me')
        self.assertEqual(list(self.target.parent.glob('pve-drive.previous-*')), [])
        self.assertEqual(list(self.target.parent.glob('.pve-drive-install-*')), [])


if __name__ == '__main__':
    unittest.main()
