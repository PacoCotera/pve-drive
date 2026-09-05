import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pve_drive as p
import test_native
import test_pve_drive


class CloudVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.payload = Path(self.tmp.name)
        (self.payload / 'disk.qcow2').write_bytes(b'archive')
        self.args = p.parser().parse_args(['--remote', 'gdrive:archive', '--source', 'test', 'upload', '100'])
        self.manager = p.Manager(self.args)
        self.row = {'Path': 'disk.qcow2', 'Size': 7,
                    'Hashes': {'MD5': hashlib.md5(b'archive').hexdigest()}}

    def verify(self, rows):
        with patch.object(self.manager, 'rc', return_value=json.dumps(rows)) as rc:
            self.manager.verify_upload(self.payload, 'gdrive:archive/backup')
        return rc

    def test_default_uses_metadata_without_downloading(self):
        self.assertFalse(self.args.deep_verify)
        rc = self.verify([self.row])
        rc.assert_called_once_with('lsjson', 'gdrive:archive/backup', '--recursive', '--files-only', '--hash', capture=True)

    def test_rejects_missing_or_invalid_md5(self):
        for hashes in ({}, {'md5': ''}, {'sha1': 'a' * 40}, {'md5': 'invalid'}):
            with self.subTest(hashes=hashes), self.assertRaisesRegex(ValueError, 'MD5 unavailable'):
                self.verify([dict(self.row, Hashes=hashes)])

    def test_rejects_same_size_corruption(self):
        with self.assertRaisesRegex(ValueError, 'MD5 mismatch'):
            self.verify([dict(self.row, Hashes={'md5': '0' * 32})])

    def test_rejects_wrong_size(self):
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            self.verify([dict(self.row, Size=8)])

    def test_rejects_missing_extra_and_duplicate_files(self):
        for rows in ([], [self.row, dict(self.row, Path='extra')], [self.row, self.row]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.verify(rows)

    def test_deep_verification_uses_readback(self):
        self.args.deep_verify = True
        with patch.object(self.manager, 'rc') as rc:
            self.manager.verify_upload(self.payload, 'gdrive:archive/backup')
        rc.assert_called_once_with('check', self.payload, 'gdrive:archive/backup', '--download')

    def test_fast_failure_blocks_deletion_for_both_formats(self):
        for case_type in (test_native.NativeTests, test_pve_drive.LifecycleTests):
            case = case_type()
            case.setUp()
            try:
                case.args.deep_verify = False
                with self.subTest(format=case_type.__name__), self.assertRaisesRegex(ValueError, 'listing differs'):
                    if isinstance(case, test_native.NativeTests):
                        with case.harness():
                            case.archive()
                    else:
                        case.execute()
                self.assertFalse(any(k.endswith('/COMPLETE') for k in case.remote))
                if isinstance(case, test_native.NativeTests):
                    self.assertTrue(case.file('100').exists())
                else:
                    self.assertFalse(case.deleted)
            finally:
                case.doCleanups()

    def test_fast_success_completes_both_formats_without_readback(self):
        for case_type in (test_native.NativeTests, test_pve_drive.LifecycleTests):
            case = case_type()
            case.setUp()
            try:
                case.args.deep_verify = False
                original = case.rc
                calls = []
                uploaded = {}
                def metadata(*args, **kwargs):
                    calls.append(args)
                    if args[0] == 'copy':
                        uploaded.update({str(args[2]) + '/' + f.name: f.read_bytes()
                                         for f in Path(args[1]).iterdir()})
                    if args[0] == 'lsjson' and '--hash' in args:
                        rows = []
                        for key, data in case.remote.items():
                            if key.startswith(str(args[1]) + '/'):
                                data = uploaded[key]
                                rows.append({'Path': key[len(str(args[1])) + 1:], 'Size': len(data),
                                             'Hashes': {'md5': hashlib.md5(data).hexdigest()}})
                        return json.dumps(rows)
                    return original(*args, **kwargs)
                with self.subTest(format=case_type.__name__), patch.object(case, 'rc', metadata):
                    if isinstance(case, test_native.NativeTests):
                        with case.harness():
                            case.archive()
                        self.assertFalse(case.file('100').exists())
                    else:
                        case.execute()
                        self.assertTrue(case.deleted)
                self.assertTrue(any(k.endswith('/COMPLETE') for k in case.remote))
                self.assertFalse(any(c[0] == 'check' for c in calls))
            finally:
                case.doCleanups()
