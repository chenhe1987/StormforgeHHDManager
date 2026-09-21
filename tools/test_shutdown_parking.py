"""Regression coverage for shutdown isolation and best-effort SLEEP."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core import shutdown_service as svc
from src.core.disk_whitelist import disk_id
from src.hal.win32_api import Win32API


class ParkingTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.disk = dict(index=5, pnp_id='USB\\MOCK', serial_number='MOCK',
                         model='mock', is_removable=True)
        snap = dict(disks=[self.disk, dict(self.disk, index=6, serial_number='OTHER')],
                    managed_disk_whitelist=[disk_id(self.disk)], sleeping=[])
        self.cmd = Mock(handle=101)
        self.cmd.flush_cache.side_effect = lambda **kw: self.events.append('flush') or True
        self.cmd.sleep_only.side_effect = lambda: self.events.append('sleep') or True
        self.factory = MagicMock()
        self.factory.return_value.__enter__.return_value = self.cmd
        self.offline = Mock(side_effect=lambda h, flag: (
            self.events.append('offline' if flag else 'online') or (True, 0)))
        patches = [
            patch.object(svc, '_load_shared_disk_snapshot', return_value=snap),
            patch.object(svc, '_validate_target_handle'),
            patch.object(svc, '_set_offline_handle', self.offline),
            patch('src.hal.asm_commander.ASMCommander', self.factory),
            patch.object(Win32API, 'get_volume_disk_mapping', return_value={}),
        ]
        self.validators = patches[1].start()
        self.addCleanup(patches[1].stop)
        for p in patches[:1] + patches[2:]:
            p.start()
            self.addCleanup(p.stop)

    def test_flush_offline_sleep_and_no_access_after_sleep(self):
        self.assertEqual(svc._park_all_disks(), 1)
        self.assertEqual(self.events, ['flush', 'offline', 'sleep'])
        self.factory.assert_called_once()

    def test_offline_failure_still_sleeps(self):
        self.offline.side_effect = lambda *a: (False, 5)
        self.assertEqual(svc._park_all_disks(), 1)
        self.cmd.sleep_only.assert_called_once()

    def test_flush_exception_still_attempts_offline_sleep(self):
        self.cmd.flush_cache.side_effect = OSError('unavailable')
        self.assertEqual(svc._park_all_disks(), 1)
        self.assertEqual(self.events, ['offline', 'sleep'])

    def test_identity_mismatch_never_offlines_or_sleeps(self):
        self.validators.side_effect = RuntimeError('different disk')
        self.assertEqual(svc._park_all_disks(), 0)
        self.offline.assert_not_called()
        self.cmd.sleep_only.assert_not_called()

    def test_failed_offline_passthrough_restores_then_retries_once(self):
        self.cmd.sleep_only.side_effect = [False, True]
        self.assertEqual(svc._park_all_disks(), 1)
        self.assertEqual(self.cmd.sleep_only.call_count, 2)
        self.assertEqual(self.events, ['flush', 'offline', 'online'])

    def test_protected_system_disk_is_skipped(self):
        with patch.object(Win32API, 'get_volume_disk_mapping', return_value={'C': 5}), \
                patch.dict('os.environ', {'SystemDrive': 'C:'}):
            self.assertEqual(svc._park_all_disks(), 0)
        self.factory.assert_not_called()

    def test_empty_whitelist_never_touches_devices(self):
        with patch.object(svc, '_load_shared_disk_snapshot', return_value={}):
            self.assertEqual(svc._park_all_disks(), 0)
        self.factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
