"""Windows-only integration regression tests; all hardware calls are mocked."""
import ctypes
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.hal.removal_notifications import SafeRemovalPatcher, BCAST_QUERY_DENY
from src.hal.win32_api import DEV_BROADCAST_HANDLE
from src.core import eject_service
from src.core.monitor_service import MonitorService
from src.ui.main_window import MainWindow
from test_eject_sleep_protocol import FakeBackend


class NativeTests(unittest.TestCase):
    def setUp(self):
        SafeRemovalPatcher._instance = None
        self.p = SafeRemovalPatcher()
        self.p._handle_disk_map = {123: 4, 456: 3}
        self.p._handle_notify_map = {789: 123, 999: 456}
        self.p._identity = {4: dict(index=4, serial="bridge-E", pnp_id="USB-E", model="test")}
        self.requests, self.released = [], []
        self.p.on_native_request = self.requests.append
        self.p._release_handle_for_disk = self.released.append
        self.packet = DEV_BROADCAST_HANDLE()
        self.packet.dbch_size = ctypes.sizeof(self.packet)
        self.packet.dbch_devicetype = 6
        self.packet.dbch_handle = 123
        self.packet.dbch_hdevnotify = 789

    def event(self, code):
        return self.p.handle_wm_devicechange(code, ctypes.addressof(self.packet))

    def test_native_query_deferred_once_then_failed_event_marks_ready(self):
        self.assertEqual(self.event(0x8001), BCAST_QUERY_DENY)
        self.assertEqual(self.event(0x8001), BCAST_QUERY_DENY)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.p.native_ready(4))
        self.event(0x8002)
        self.assertTrue(self.p.native_ready(4))
        self.assertEqual(self.released, [])  # Keep notification for QUERYREMOVEFAILED.

    def test_untargeted_tree_and_removal_never_send_io(self):
        self.assertEqual(self.event(7), 1)
        self.packet.dbch_devicetype = 5
        self.assertEqual(self.event(0x8004), 1)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.released, [])

    def test_managed_request_is_not_intercepted_recursively(self):
        self.p.managed.add(4)
        self.assertEqual(self.event(0x8001), 1)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.released, [4])

    def test_shutdown_only_releases_target_handle(self):
        self.p.shutdown_in_progress = True
        self.assertEqual(self.event(0x8001), 1)
        self.assertEqual(self.released, [4])
        self.assertEqual(self.requests, [])

    def test_unknown_handle_never_vetoes_or_starts_transaction(self):
        self.packet.dbch_handle = 222
        self.packet.dbch_hdevnotify = 333
        self.assertEqual(self.event(0x8001), 1)
        self.assertEqual(self.requests, [])

    def test_removal_cleans_only_target(self):
        self.assertEqual(self.event(0x8004), 1)
        self.assertEqual(self.released, [4])


class ServiceTests(unittest.TestCase):
    def monitor(self):
        m = SimpleNamespace(scan_lock=threading.RLock(), shutdown_mode=False,
                            eject_quarantine=set(), mark_disk_ejected=lambda *a: None,
                            config_manager=SimpleNamespace(config={}, save_config=lambda: None))
        m.quarantine_eject = lambda pnp: m.eject_quarantine.add(pnp.upper())
        return m

    def execute(self, m, backend):
        identity = dict(index=4, pnp_id="USB-E", serial="bridge-E")
        with patch.object(eject_service, "inventory", return_value={}), \
             patch.object(eject_service, "WindowsBackend", return_value=backend):
            return eject_service.execute_eject(m, identity)

    def test_failed_sleep_remains_quarantined(self):
        m = self.monitor()
        r = self.execute(m, FakeBackend("sleep"))
        self.assertEqual(r["state"], "sleep_unknown_offline")
        self.assertIn("USB-E", m.eject_quarantine)

    def test_success_clears_quarantine_and_releases_lock(self):
        m = self.monitor()
        r = self.execute(m, FakeBackend())
        self.assertTrue(r["ejected"])
        self.assertEqual(m.eject_quarantine, set())
        acquired = []
        def check():
            got = m.scan_lock.acquire(timeout=0.1)
            acquired.append(got)
            if got:
                m.scan_lock.release()
        t = threading.Thread(target=check)
        t.start()
        t.join()
        self.assertEqual(acquired, [True])

    def test_quarantine_rejects_new_inventory(self):
        m = self.monitor()
        m.eject_quarantine.add("USB-E")
        with patch.object(eject_service, "inventory") as inventory:
            r = eject_service.execute_eject(m, dict(index=4, pnp_id="USB-E", serial="bridge-E"))
        inventory.assert_not_called()
        self.assertFalse(r["sleep_attempted"])

    def test_scan_and_idle_timer_do_nothing_when_removal_pending(self):
        m = MonitorService.__new__(MonitorService)
        m.removal_pending = threading.Event()
        m.removal_pending.set()
        with patch.object(m, "_check_all_smart_impl") as scan, \
             patch.object(m, "_check_idle_timers_locked") as idle:
            m.check_all_smart(force=True)
            m._check_idle_timers()
        scan.assert_not_called()
        idle.assert_not_called()


class GuiTests(unittest.TestCase):
    def test_completion_does_not_resume_quarantined_disk(self):
        pending = threading.Event()
        pending.set()
        class Button:
            def setText(self, text): self.text = text
            def setEnabled(self, enabled): self.enabled = enabled
        notices = []
        ui = SimpleNamespace(
            spindown_patcher=SimpleNamespace(finish_managed=lambda idx: None),
            monitor_service=SimpleNamespace(removal_pending=pending),
            eject_button=Button(), spin_down_button=Button(), refresh_btn=Button(),
            status_label=Button(), show_notification=lambda *args: notices.append(args))
        MainWindow._on_eject_complete(ui, dict(index=3, ejected=False,
            sleep_attempted=True, error="veto", source="windows_tray"))
        self.assertFalse(pending.is_set())
        self.assertFalse(ui.eject_button.enabled)
        self.assertFalse(ui.spin_down_button.enabled)
        self.assertIn("veto", notices[0][1])

    def test_gui_uses_cached_bridge_identity_not_smart_serial(self):
        from PySide6.QtWidgets import QMessageBox
        requests = []
        ui = SimpleNamespace(_eject_active=None, current_disk_index=3,
            current_disk_serial="ATA-REAL-SERIAL",
            monitor_service=SimpleNamespace(cached_disks=[SimpleNamespace(index=3,
                model="ASMT", serial_number="BRIDGE-SERIAL", pnp_id="USB-PNP")]),
            _start_eject=requests.append)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            MainWindow.on_eject_clicked(ui)
        self.assertEqual(requests[0]["serial"], "BRIDGE-SERIAL")
        self.assertEqual(requests[0]["ui_serial"], "ATA-REAL-SERIAL")
        self.assertEqual(requests[0]["pnp_id"], "USB-PNP")


if __name__ == "__main__":
    unittest.main(verbosity=2)
