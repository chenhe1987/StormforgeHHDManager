"""热插拔即时发现的回归测试（全部离线，不接触硬件）。

修复的两个缺陷：
  D1 只有 5 分钟兜底轮询、没有设备事件触发 → 插入后最长 300 秒才被发现；
  D2 侧栏完整列表只在 target_disks=None 的全量路径生成，定时路径对非白名单盘
     直接 continue + _merge_ui_data 只从 updates 追加 → 新盘永远不显示。
"""
import sys
import threading
import time
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.device_manager import DiskInfo                    # noqa: E402
from src.core.monitor_service import MonitorService             # noqa: E402
from src.core.disk_whitelist import disk_id                     # noqa: E402
from src.ui.main_window import MainWindow                       # noqa: E402

NEW_PNP = "SCSI\\DISK&VEN_STORMFOR&PROD_D4_DAS\\7&11111111&0&000000"
NEW_SN = "3740BBBBAAAA"
OLD_SN = "VRJ3VBHK"


def new_disk(index=3, serial=NEW_SN, pnp=NEW_PNP, model="Stormfor D4 DAS SCSI Disk Device"):
    return DiskInfo(device_id="\\\\.\\PHYSICALDRIVE%d" % index, model=model,
                    serial_number=serial, index=index, interface_type="SCSI",
                    is_removable=True, pnp_id=pnp)


def old_disk():
    return DiskInfo(device_id="\\\\.\\PHYSICALDRIVE0", model="HGST HUS728T8TALE6L4",
                    serial_number=OLD_SN, index=0, interface_type="IDE",
                    is_removable=False,
                    pnp_id="SCSI\\DISK&VEN_HGST&PROD_HUS728T8TALE6L4\\4&2EE479D7&0&070000")


class FakeConfig:
    def __init__(self, whitelist=(), sleeping=(), interval=120):
        self.config = {"eject_quarantine": []}
        self._whitelist = set(whitelist)
        self._sleeping = list(sleeping)
        self.interval = interval
        self.saved = 0

    def get_managed_disk_whitelist(self):
        return set(self._whitelist)

    def get_disk_interval(self, serial):
        return 86400

    def get_sleeping_disks(self):
        return list(self._sleeping)

    def set_sleeping_disks(self, serials):
        self._sleeping = list(serials)

    def add_sleeping_disk(self, serial):
        self._sleeping.append(serial)

    def remove_sleeping_disk(self, serial):
        if serial in self._sleeping:
            self._sleeping.remove(serial)

    def get_eject_dismount_without_lock(self):
        return True

    def get_device_inventory_interval(self, default=120):
        return self.interval

    def save_config(self):
        self.saved += 1


def make_monitor(disks, whitelist=(), sleeping=(), config=None):
    m = MonitorService.__new__(MonitorService)
    m.running = True
    m.shutdown_mode = False
    m.removal_pending = threading.Event()
    m.scan_lock = threading.RLock()
    m.sleeping_disks = set(sleeping)
    m.ejected_disks = set()
    m.eject_quarantine = set()
    m.config_manager = config or FakeConfig(whitelist, sleeping)
    m.cached_disks = list(disks)
    m.last_ui_data = []
    m.last_inventory_scan_time = 0
    m.device_inventory_interval = 120
    m.disk_health_status = {}
    m.disk_last_check_times = {}
    m._idle_tracker = {}
    m.callback_notify = None
    m.pushed = []
    m.callback_update_ui = m.pushed.append
    m.enumerations = []
    m.next_disks = None
    m._device_fingerprints = {}
    m._pending_bay_changes = set()

    def fake_get(force=False):
        m.enumerations.append(bool(force))
        if m.next_disks is not None:
            m.cached_disks = list(m.next_disks)
        m._sync_device_fingerprints()      # 真实现里在枚举后调用（见 _get_cached_or_scan_disks_locked）
        return m._filter_excluded_disks(m.cached_disks)

    m._get_cached_or_scan_disks = fake_get
    return m


def baseline_probe(m):
    """复现原缺陷：定时路径不推送新盘。用于对照，证明修复方向正确。"""
    m._check_all_smart_impl(force=False, target_disks=[new_disk()])
    return m.pushed[-1] if m.pushed else []


class ScheduledPathBaselineTests(unittest.TestCase):
    def test_scheduled_path_alone_never_adds_new_unmanaged_disk(self):
        m = make_monitor([old_disk(), new_disk()])
        rows = baseline_probe(m)
        self.assertFalse(any(r.get("serial") == NEW_SN for r in rows),
                         "定时路径本来就不会带出新盘（这就是缺陷 D2 的对照组）")
        self.assertEqual(rows, [])


class DebounceTests(unittest.TestCase):
    def test_only_arrival_removal_and_tree_change_trigger_rescan(self):
        self.assertTrue(MainWindow.device_change_needs_rescan(0x8000))   # ARRIVAL
        self.assertTrue(MainWindow.device_change_needs_rescan(0x8004))   # REMOVECOMPLETE
        self.assertTrue(MainWindow.device_change_needs_rescan(0x0007))   # DEVNODES_CHANGED
        for other in (0x8001, 0x8002, 0x8003, 0, None):
            self.assertFalse(MainWindow.device_change_needs_rescan(other),
                             "QUERYREMOVE 系列不能触发重扫，返回值语义不允许改动")

    def test_event_burst_restarts_one_timer_and_runs_one_rescan(self):
        class FakeTimer:
            def __init__(self):
                self.starts = 0
            def start(self):
                self.starts += 1

        timer = FakeTimer()
        calls = []
        ui = SimpleNamespace(_device_change_timer=timer, _pending_device_change=0,
                             monitor_service=SimpleNamespace(
                                 rescan_devices=lambda reason: calls.append(reason)))
        for wparam in (0x0007, 0x0007, 0x8000, 0x0007):      # 硬盘柜上电的事件风暴
            MainWindow._schedule_device_rescan(ui, wparam)
        self.assertEqual(timer.starts, 4)                    # 每次都重新计时 = 去抖
        self.assertEqual(ui._pending_device_change, 0x0007)  # 只保留最后一次
        with patch("src.ui.main_window.threading.Thread",
                   lambda target, daemon: SimpleNamespace(start=target)):
            MainWindow._on_device_change_settled(ui)
        self.assertEqual(len(calls), 1)
        self.assertIn("0x0007", calls[0])


class RescanTests(unittest.TestCase):
    def test_rescan_rebuilds_full_list_including_unmanaged_new_disk(self):
        m = make_monitor([old_disk()])
        m.next_disks = [old_disk(), new_disk()]
        result = m.rescan_devices(reason="WM_DEVICECHANGE:0x8000")
        self.assertEqual(result["added"], 1)
        rows = m.pushed[-1]
        serials = [r.get("serial") for r in rows]
        self.assertIn(NEW_SN, serials)
        new_row = next(r for r in rows if r.get("serial") == NEW_SN)
        self.assertEqual(new_row["status"], "未加入白名单（不管理）")
        self.assertTrue(new_row["is_removable"])

    def test_rescan_drops_removed_disk(self):
        m = make_monitor([old_disk(), new_disk()])
        m.next_disks = [new_disk()]
        result = m.rescan_devices(reason="WM_DEVICECHANGE:0x8004")
        self.assertEqual(result["removed"], 1)
        serials = [r.get("serial") for r in m.pushed[-1]]
        self.assertNotIn(OLD_SN, serials)
        self.assertIn(NEW_SN, serials)

    def test_rescan_skips_during_eject_transaction_and_shutdown(self):
        m = make_monitor([old_disk()])
        m.removal_pending.set()
        self.assertEqual(m.rescan_devices()["skipped"], "removal_pending")
        m.removal_pending.clear()
        m.shutdown_mode = True
        self.assertEqual(m.rescan_devices()["skipped"], "shutdown_mode")
        m.shutdown_mode = False
        m.running = False
        self.assertEqual(m.rescan_devices()["skipped"], "not_running")
        self.assertEqual(m.enumerations, [], "跳过时不能去枚举设备")

    def test_reinserted_disk_is_unblocked_from_ejected_and_quarantine(self):
        config = FakeConfig()
        m = make_monitor([], config=config)
        m.ejected_disks.add(NEW_SN)
        m.eject_quarantine.add(NEW_PNP.upper())
        config.config["eject_quarantine"] = [NEW_PNP.upper()]
        m.next_disks = [new_disk()]
        result = m.rescan_devices(reason="WM_DEVICECHANGE:0x8000")
        self.assertEqual(len(result["healed"]), 2)
        self.assertEqual(m.ejected_disks, set())
        self.assertEqual(m.eject_quarantine, set())
        self.assertEqual(config.config["eject_quarantine"], [])
        self.assertGreaterEqual(config.saved, 1)
        self.assertIn(NEW_SN, [r.get("serial") for r in m.pushed[-1]])

    def test_disk_present_but_quarantined_stays_hidden_without_reinsertion(self):
        """没有重新插入（枚举里没有这块盘）时，不能擅自解除隔离。"""
        m = make_monitor([old_disk()])
        m.eject_quarantine.add(NEW_PNP.upper())
        m.next_disks = [old_disk()]
        result = m.rescan_devices(reason="WM_DEVICECHANGE:0x0007")
        self.assertEqual(result["healed"], [])
        self.assertIn(NEW_PNP.upper(), m.eject_quarantine)

    def test_sleeping_whitelisted_disk_is_shown_without_being_probed(self):
        target = new_disk()
        config = FakeConfig(whitelist={disk_id(target)}, sleeping=[NEW_SN])
        m = make_monitor([target], whitelist={disk_id(target)}, sleeping=[NEW_SN],
                         config=config)
        with patch("src.core.monitor_service.ASMCommander") as commander:
            m.rescan_devices(reason="WM_DEVICECHANGE:0x0007")
            commander.assert_not_called()          # 绝不发 IDENTIFY/TUR 唤醒休眠盘
        row = next(r for r in m.pushed[-1] if r.get("serial") == NEW_SN)
        self.assertEqual(row["status"], "Sleeping")

    def test_rescan_never_probes_any_disk(self):
        """事件路径只重建列表、零磁盘访问；白名单盘交给按盘调度去检测。

        0x0007 任何设备变化都会发，若每次事件都跑 SMART，会把空闲停转但未标记
        休眠的盘反复唤醒。
        """
        managed = new_disk()
        m = make_monitor([old_disk()], whitelist={disk_id(managed)},
                         config=FakeConfig(whitelist={disk_id(managed)}))
        m.next_disks = [old_disk(), managed]
        with patch("src.core.monitor_service.ASMCommander") as commander:
            m.rescan_devices(reason="WM_DEVICECHANGE:0x0007")
            commander.assert_not_called()
        rows = m.pushed[-1]
        fresh = next(r for r in rows if r.get("serial") == NEW_SN)
        self.assertEqual(fresh["status"], "待检测")      # 下一轮调度会立刻补上真实数据
        self.assertIn(OLD_SN, [r.get("serial") for r in rows])   # 已知盘沿用旧行

    def test_known_disk_row_is_reused_verbatim(self):
        """已知盘沿用上次的行（保留温度/健康数据），不产生空壳行。"""
        target = old_disk()
        m = make_monitor([target])
        m.last_ui_data = [{"index": 0, "serial": OLD_SN, "model": "HGST",
                           "status": "Warning", "temp": "41C", "pending": "3064",
                           "reallocated": "0", "is_removable": False,
                           "pnp_id": "", "managed_id": "", "interface": "USB (SATA)",
                           "attributes": [1, 2, 3]}]
        m.next_disks = [target]
        m.rescan_devices(reason="WM_DEVICECHANGE:0x0007")
        row = next(r for r in m.pushed[-1] if r.get("serial") == OLD_SN)
        self.assertEqual(row["temp"], "41C")
        self.assertEqual(row["status"], "Warning")
        self.assertEqual(row["attributes"], [1, 2, 3])

    def test_smart_row_survives_when_ui_serial_is_ata_serial(self):
        """回归（v1.3.89 的 bug）：UI 行的 serial 是 SATA IDENTIFY 的真实序列号，
        设备缓存里是桥接序列号 —— 用 (index, serial) 精确匹配会失配，
        整表被占位行替换，已管理盘的 SMART 数据被冲成"待检测"。"""
        target = new_disk()                      # 缓存 serial = 桥接序列号 3740BBBBAAAA
        m = make_monitor([target], whitelist={disk_id(target)},
                         config=FakeConfig(whitelist={disk_id(target)}))
        m.last_ui_data = [{"index": 3, "serial": "Z1F0REALATASERIAL",   # 真实序列号
                           "model": "HGST HUS728T8TALE6L4", "status": "Healthy",
                           "temp": "38C", "pending": "0", "reallocated": "0",
                           "pnp_id": target.pnp_id, "managed_id": disk_id(target),
                           "is_removable": True, "interface": "SCSI",
                           "attributes": [9, 9, 9]}]
        m.next_disks = [target]
        m.rescan_devices(reason="WM_DEVICECHANGE:0x0007")
        row = next(r for r in m.pushed[-1] if r.get("index") == 3)
        self.assertEqual(row["status"], "Healthy")     # 不能变成"待检测"
        self.assertEqual(row["temp"], "38C")
        self.assertEqual(row["attributes"], [9, 9, 9])

    def test_bay_swap_is_detected_by_model_fingerprint(self):
        """同一盘位换盘：序列号按盘位固定（不变），只能靠型号指纹识别。"""
        empty = new_disk(index=5, serial="1740BBBBAAAA",
                         model="Stormfor D4 DAS USB Device")
        inserted = new_disk(index=5, serial="1740BBBBAAAA",
                            model="HGST HUS728T8TALE6L4")
        m = make_monitor([empty])
        m.next_disks = [empty]
        m.rescan_devices(reason="baseline")
        self.assertEqual(next(r for r in m.pushed[-1] if r.get("index") == 5)["model"],
                         "Stormfor D4 DAS USB Device")

        m.disk_last_check_times["1740BBBBAAAA"] = time.time()   # 上一块盘的检测时间
        m.next_disks = [inserted]
        result = m.rescan_devices(reason="WM_DEVICECHANGE:0x0007")
        self.assertEqual(result["changed_bays"], [5])
        # 盘位序列号不变 → 按索引/序列号的 diff 是空的，必须靠指纹才能发现
        self.assertEqual((result["added"], result["removed"]), (0, 0))
        self.assertNotIn("1740BBBBAAAA", m.disk_last_check_times,
                         "换盘后必须清掉检测时间，否则新盘沿用旧盘的到期时间不被检测")
        self.assertEqual(next(r for r in m.pushed[-1] if r.get("index") == 5)["model"],
                         "HGST HUS728T8TALE6L4")

    def test_unchanged_bay_keeps_its_row(self):
        """没有换盘的盘位不能被指纹逻辑误伤。"""
        target = new_disk()
        m = make_monitor([target])
        m.next_disks = [target]
        m.rescan_devices(reason="baseline")
        m.last_ui_data[0]["status"] = "Healthy"
        m.last_ui_data[0]["temp"] = "40C"
        m.disk_last_check_times[NEW_SN] = 1234567890.0
        m.next_disks = [target]
        result = m.rescan_devices(reason="WM_DEVICECHANGE:0x0007")
        self.assertEqual(result["changed_bays"], [])
        self.assertEqual(m.last_ui_data[0]["temp"], "40C")
        self.assertEqual(m.last_ui_data[0]["status"], "Healthy")
        self.assertEqual(m.disk_last_check_times.get(NEW_SN), 1234567890.0)


class IntervalConfigTests(unittest.TestCase):
    def test_zero_interval_means_events_only_not_enumerate_every_time(self):
        from src.core.config_manager import ConfigManager
        config = ConfigManager.__new__(ConfigManager)
        config.config = {"device_inventory_interval_seconds": 0}
        self.assertEqual(config.get_device_inventory_interval(), 0)
        config.config = {"device_inventory_interval_seconds": 240}
        self.assertEqual(config.get_device_inventory_interval(), 240)
        config.config = {"device_inventory_interval_seconds": "bad"}
        self.assertEqual(config.get_device_inventory_interval(), 120)   # 非法值回退默认

    def test_monitor_honours_zero_interval_without_rescanning_every_call(self):
        m = make_monitor([old_disk()])
        m.device_inventory_interval = 0
        m.cached_disks = [old_disk()]
        m.last_inventory_scan_time = 0
        real_get = MonitorService._get_cached_or_scan_disks_locked
        calls = []
        with patch("src.core.monitor_service.DeviceManager.get_physical_disks",
                   side_effect=lambda allowlist=None: calls.append(1) or [old_disk()]):
            real_get(m, force=False)
            real_get(m, force=False)
        self.assertEqual(calls, [], "间隔为 0 时不应因为 now-last>=0 而每次重新枚举")


if __name__ == "__main__":
    unittest.main(verbosity=2)
