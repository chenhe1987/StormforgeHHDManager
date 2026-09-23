import sys
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core.disk_whitelist import disk_id, is_external_disk
from src.core.monitor_service import MonitorService
from src.ui.main_window import MainWindow, QMessageBox
from src.utils.log_reporter import load_report_server_config


class RegressionTests(unittest.TestCase):
    def disk(self):
        return SimpleNamespace(index=5, model='UASP disk', serial_number='MOCK',
                               pnp_id='SCSI\\DISK&VEN_MOCK\\5',
                               is_removable=True, interface_type='SCSI')

    def test_gui_preserves_usb_evidence_for_scsi_device(self):
        disk = self.disk()
        ui = SimpleNamespace(_eject_active=None, current_disk_index=5,
                             current_disk_managed=True, current_disk_serial='MOCK',
                             monitor_service=SimpleNamespace(cached_disks=[disk]),
                             _start_eject=Mock())
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            MainWindow.on_eject_clicked(ui)
        identity = ui._start_eject.call_args.args[0]
        self.assertTrue(is_external_disk(identity))
        self.assertEqual(disk_id(identity), disk_id(disk))

    def test_sleeping_ui_retains_external_flag_without_smart_probe(self):
        disk = self.disk()
        monitor = SimpleNamespace(shutdown_mode=False,
            _get_cached_or_scan_disks=Mock(return_value=[disk]),
            config_manager=SimpleNamespace(get_managed_disk_whitelist=lambda: {disk_id(disk)}),
            removal_pending=threading.Event(), is_eject_blocked=lambda d: False,
            ejected_disks=set(), sleeping_disks={'MOCK'}, callback_update_ui=Mock())
        with patch('src.hal.asm_commander.ASMCommander') as commander:
            MonitorService._check_all_smart_impl(monitor)
            commander.assert_not_called()
        row = monitor.callback_update_ui.call_args.args[0][0]
        self.assertEqual(row['status'], 'Sleeping')
        self.assertTrue(row['is_removable'])
        self.assertTrue(is_external_disk(row))

    def test_internal_scsi_disk_is_not_promoted_to_usb(self):
        self.assertFalse(is_external_disk({'pnp_id': 'SCSI\\DISK&VEN_INTERNAL',
                                           'is_removable': False}))

    def test_missing_external_config_uses_bundled_upload_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'report_client.json'
            path.write_text(json.dumps({'upload_url': 'https://example.invalid/upload',
                                        'token': 'synthetic-upload-only'}))
            with patch('src.utils.log_reporter.get_base_path', return_value=folder), \
                 patch('src.utils.log_reporter.get_resource_path', return_value=str(path)):
                cfg = load_report_server_config()
            self.assertEqual(cfg['token'], 'synthetic-upload-only')


if __name__ == '__main__':
    unittest.main()
