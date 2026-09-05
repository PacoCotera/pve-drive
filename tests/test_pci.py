import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pve_drive as p
import test_pve_drive
from test_native import CONFIG


class SysfsFixture:
    """Map Linux PCI names to filenames usable on Windows test hosts."""
    def __init__(self, path):
        self.path = Path(path)

    def __truediv__(self, address):
        return self.path / address.replace(':', '_')

    def glob(self, pattern):
        return self.path.glob(pattern.replace(':', '_'))


class PciTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = SysfsFixture(self.tmp.name)

    def device(self, address, code):
        path = self.root / address
        path.mkdir()
        (path / 'class').write_text(code)

    def test_display_and_audio_all_functions(self):
        self.device('0000:01:00.0', '0x030000')
        self.device('0000:01:00.1', '0x040300')
        cfg = {'hostpci0': 'host=01:00,pcie=1'}
        p.safe_config(cfg)
        p.validate_pci(cfg, self.root)

    def test_storage_function_in_same_slot_rejected(self):
        self.device('0000:01:00.0', '0x030000')
        self.device('0000:01:00.1', '0x010802')
        with self.assertRaisesRegex(ValueError, 'not a display'):
            p.validate_pci({'hostpci0': '0000:01:00'}, self.root)

    def test_explicit_function_does_not_include_other_functions(self):
        self.device('0000:01:00.0', '0x030000')
        self.device('0000:01:00.1', '0x010802')
        p.validate_pci({'hostpci0': '0000:01:00.0'}, self.root)

    def test_missing_device_fails(self):
        for address in ['0000:01:00', '0000:01:00.0']:
            with self.subTest(address=address), self.assertRaises(ValueError):
                p.validate_pci({'hostpci0': address}, self.root)

    def test_second_device_checked(self):
        self.device('0000:01:00.0', '0x030000')
        self.device('0000:02:00.0', '0x010000')
        with self.assertRaises(ValueError):
            p.validate_pci({'hostpci0': '01:00.0;02:00.0'}, self.root)

    def test_unknown_class_and_mapping_rejected(self):
        self.device('0000:01:00.0', 'invalid')
        with self.assertRaises(ValueError):
            p.validate_pci({'hostpci0': '01:00.0'}, self.root)
        for value in ['mapping=gpu', '01:00.0,mdev=test', '../../etc', '01:00.0,host=02:00.0']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                p.safe_config({'hostpci0': value})

    def test_native_snapshot_config_preserved_without_destination_hardware(self):
        raw = CONFIG.replace('agent: 1', 'agent: 1\nhostpci0: 0000:01:00')
        sections = p.parse_native_config(raw)
        restored = p.remap_native_config(sections, {'local:100/vm-100-disk-0.qcow2':
                                                   'dest:200/vm-200-disk-0.qcow2'}, True)
        self.assertEqual(restored.count('hostpci0: 0000:01:00'), 2)

    def test_vma_manifest_retains_gpu(self):
        case = test_pve_drive.LifecycleTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.cfg['hostpci0'] = '0000:01:00'
        self.device('0000:01:00.0', '0x030000')
        validator = p.validate_pci
        with patch.object(p, 'validate_pci', side_effect=lambda cfg: validator(cfg, self.root)):
            case.execute()
        manifest = json.loads(next(v for k, v in case.remote.items() if k.endswith('/manifest.json')))
        self.assertEqual(manifest['config']['hostpci0'], '0000:01:00')

    def test_storage_rejection_precedes_shutdown(self):
        case = test_pve_drive.LifecycleTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.cfg['hostpci0'] = '0000:01:00'
        self.device('0000:01:00.0', '0x010000')
        validator = p.validate_pci
        with patch.object(p, 'validate_pci', side_effect=lambda cfg: validator(cfg, self.root)):
            with self.assertRaises(ValueError):
                case.execute()
        self.assertEqual(case.commands, [])
