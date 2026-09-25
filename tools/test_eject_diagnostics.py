"""弹出失败诊断与提示文案的回归测试（全部离线，不接触硬件）。"""
import ctypes
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core import eject_service                     # noqa: E402
from src.core.eject_protocol import Outcome, run       # noqa: E402
from src.hal.eject_backend import WindowsBackend       # noqa: E402
from src.utils import volume_diag                      # noqa: E402

VOLUME_GUID = "\\\\?\\Volume{187d5a39-4976-40b1-a67d-7e72d4504f13}"


def make_snapshot(filesystem="ReFS", letter="E"):
    return {
        "disk": {"Number": 4, "FriendlyName": "ASMT ASMT105x",
                 "SerialNumber": "B000BBBBAAAA", "BusType": "USB", "IsBoot": False,
                 "IsSystem": False, "IsOffline": False, "Size": 16000900661248},
        "volumes": [{"UniqueId": VOLUME_GUID + "\\", "Path": VOLUME_GUID + "\\",
                     "DriveLetter": letter, "FileSystemType": filesystem}],
        "physical": [{"Index": 4, "PNPDeviceID": "SCSI\\DISK&VEN_ASMT&PROD_ASMT105X\\7&8747CBD&0&000000",
                      "SerialNumber": "B000BBBBAAAA", "Size": 16000900661248}],
        "managers": [], "pagefiles": [],
    }


EMPTY_DIAGNOSTIC = {
    "label": "E:", "drive_letter": "E:", "filesystem": "REFS", "disk_index": 4,
    "occupiers": [], "self_handles": [], "other_handles": [],
    "unresolved": 0, "timed_out": False,
}


def make_backend(snapshot=None, lock_error=5, dismount_error=None,
                 filesystem=None, letter=None):
    if snapshot is None:
        snapshot = make_snapshot(filesystem or "ReFS", letter or "E")
    events = []
    backend = WindowsBackend(snapshot, "B000BBBBAAAA",
                             lambda event, data: events.append((event, data)))
    backend.index = 4

    def ioctl(handle, code, source=None, destination=None):
        if code == 0x90018 and lock_error:
            raise ctypes.WinError(lock_error)
        if code == 0x90020 and dismount_error:
            raise ctypes.WinError(dismount_error)
        return 0

    backend.ioctl = ioctl
    backend._diagnose = lambda handle: dict(EMPTY_DIAGNOSTIC)
    return backend, events, snapshot


class WinErrorTextTests(unittest.TestCase):
    def test_error_code_is_translated_not_bare(self):
        text = volume_diag.describe_winerror(5)
        self.assertIn("拒绝访问", text)
        self.assertIn("5", text)
        self.assertNotEqual(text.strip(), "[WinError 5] 拒绝访问。")

    def test_unknown_code_is_still_readable(self):
        self.assertIn("WinError 9999", volume_diag.describe_winerror(9999))

    def test_filesystem_lock_support_matrix(self):
        self.assertFalse(volume_diag.filesystem_supports_lock("ReFS"))
        self.assertFalse(volume_diag.filesystem_supports_lock("exfat"))
        self.assertTrue(volume_diag.filesystem_supports_lock("NTFS"))
        self.assertTrue(volume_diag.filesystem_supports_lock(""))


class MessageTests(unittest.TestCase):
    def test_lock_failure_message_names_volume_filesystem_and_occupiers(self):
        diagnostic = dict(EMPTY_DIAGNOSTIC, occupiers=["explorer.exe"],
                          self_handles=["\\Device\\HarddiskVolume15"])
        message = volume_diag.format_lock_failure(diagnostic, 5, None, "ReFS")
        self.assertIn("E:", message)
        self.assertIn("ReFS", message)
        self.assertIn("拒绝访问", message)
        self.assertIn("explorer.exe", message)
        self.assertIn("本程序自身持有", message)
        self.assertIn("建议", message)

    def test_message_says_when_nothing_occupies_the_volume(self):
        message = volume_diag.format_lock_failure(dict(EMPTY_DIAGNOSTIC), 5, None, "ReFS")
        self.assertIn("未发现其他程序", message)
        self.assertIn("0 个", message)

    def test_double_failure_reports_both_errors(self):
        message = volume_diag.format_lock_failure(dict(EMPTY_DIAGNOSTIC), 5, 32, "NTFS")
        self.assertIn("锁定卷失败", message)
        self.assertIn("直接卸载也失败", message)
        self.assertIn("共享冲突", message)

    def test_stage_failure_message(self):
        message = volume_diag.format_stage_failure(
            "打开物理盘 \\\\.\\PhysicalDrive4 句柄", "", 5, hint="确认管理员权限。")
        self.assertIn("PhysicalDrive4", message)
        self.assertIn("拒绝访问", message)
        self.assertIn("确认管理员权限", message)


class LockFallbackTests(unittest.TestCase):
    def test_refs_lock_error5_dismounts_directly_and_skips_later_volume_io(self):
        backend, events, _ = make_backend(filesystem="ReFS")
        handle = 1234
        backend._handle_volume[handle] = VOLUME_GUID
        backend.lock(handle)                      # 不应抛异常
        self.assertIn(handle, backend._predismounted)
        backend.flush_volume(handle)              # 已卸载：不应再 FlushFileBuffers
        backend.dismount(handle)                  # 已卸载：不应重复 DISMOUNT
        names = [event for event, _ in events]
        self.assertIn("volume_dismounted_without_lock", names)
        self.assertNotIn("volume_locked", names)
        self.assertEqual(names.count("volume_dismount_skipped"), 1)

    def test_ntfs_lock_error5_still_fails_closed(self):
        backend, _, _ = make_backend(filesystem="NTFS")
        handle = 4321
        backend._handle_volume[handle] = VOLUME_GUID
        with self.assertRaises(RuntimeError) as raised:
            backend.lock(handle)
        self.assertIn("拒绝访问", str(raised.exception))
        self.assertNotIn(handle, backend._predismounted)

    def test_refs_dismount_failure_is_reported_with_both_errors(self):
        backend, events, _ = make_backend(filesystem="ReFS", dismount_error=32)
        handle = 999
        backend._handle_volume[handle] = VOLUME_GUID
        with self.assertRaises(RuntimeError) as raised:
            backend.lock(handle)
        message = str(raised.exception)
        self.assertIn("无法隔离卷 E:", message)
        self.assertIn("共享冲突", message)
        self.assertNotIn(handle, backend._predismounted)
        self.assertIn("volume_isolation_failed",
                      [event for event, _ in events])

    def test_fallback_can_be_disabled_by_config(self):
        backend, _, _ = make_backend(filesystem="ReFS")
        backend.allow_dismount_without_lock = False
        handle = 55
        backend._handle_volume[handle] = VOLUME_GUID
        with self.assertRaises(RuntimeError) as raised:
            backend.lock(handle)
        self.assertIn("eject_dismount_without_lock", str(raised.exception))
        self.assertNotIn(handle, backend._predismounted)

    def test_volume_metadata_read_from_inventory(self):
        backend, _, _ = make_backend(filesystem="ReFS", letter="E")
        self.assertEqual(backend.volume_meta[VOLUME_GUID]["filesystem"], "REFS")
        self.assertEqual(backend.volume_meta[VOLUME_GUID]["drive_letter"], "E")


class FakeIsolationBackend:
    """最小后端：验证协议把可读文案原样交给上层。"""
    volumes = [VOLUME_GUID]

    def __init__(self):
        self.calls = []
        self.handles = set()

    def validate(self): self.calls.append("validate")
    def open_disk(self): self.handles.add("disk"); return "disk"
    def open_volume(self, name): self.handles.add(name); return name

    def lock(self, handle):
        self.calls.append("lock")
        raise RuntimeError(volume_diag.format_lock_failure(dict(EMPTY_DIAGNOSTIC), 5, None, "ReFS"))

    def flush_volume(self, handle): self.calls.append("flush")
    def dismount(self, handle): self.calls.append("dismount")
    def offline(self, disk, value): self.calls.append("offline")
    def verify_offline(self, disk): self.calls.append("verify_offline")
    def flush_disk(self, disk): self.calls.append("flush_disk")
    def sleep(self, disk): self.calls.append("sleep")
    def eject(self): self.calls.append("eject")
    def close(self, handle): self.handles.discard(handle)
    def record(self, event, result): self.calls.append("record:" + event)


class ProtocolSurfaceTests(unittest.TestCase):
    def test_protocol_surfaces_readable_message_and_never_sleeps(self):
        backend = FakeIsolationBackend()
        result = run(backend)
        self.assertEqual(result.state, "unchanged")
        self.assertIn("无法隔离卷", result.error)
        self.assertIn("拒绝访问", result.error)
        self.assertNotIn("sleep", backend.calls)
        self.assertNotIn("offline", backend.calls)
        self.assertFalse(backend.handles)


class ServiceWiringTests(unittest.TestCase):
    def monitor(self, allow=True):
        class Config:
            config = {}

            def get_managed_disk_whitelist(self):
                return {"SCSI\\DISK&VEN_ASMT&PROD_ASMT105X\\7&8747CBD&0&000000|B000BBBBAAAA"}

            def get_eject_dismount_without_lock(self):
                return allow

            def save_config(self):
                pass

        import threading
        from types import SimpleNamespace
        m = SimpleNamespace(scan_lock=threading.RLock(), shutdown_mode=False,
                            eject_quarantine=set(),
                            mark_disk_ejected=lambda *a: None,
                            config_manager=Config())
        m.quarantine_eject = lambda pnp: m.eject_quarantine.add(pnp.upper())
        return m

    def test_service_injects_config_flag_into_backend(self):
        captured = {}

        class Backend:
            def __init__(self, *args, **kwargs):
                captured["flag"] = None

            @property
            def allow_dismount_without_lock(self):
                return captured["flag"]

            @allow_dismount_without_lock.setter
            def allow_dismount_without_lock(self, value):
                captured["flag"] = value

        identity = dict(index=4, is_removable=True,
                        pnp_id="SCSI\\DISK&VEN_ASMT&PROD_ASMT105X\\7&8747CBD&0&000000",
                        serial="B000BBBBAAAA")
        with patch.object(eject_service, "inventory", return_value={}), \
             patch.object(eject_service, "WindowsBackend", Backend), \
             patch.object(eject_service, "run") as runner:
            runner.return_value = Outcome()
            eject_service.execute_eject(self.monitor(allow=False), identity)
        self.assertIs(captured["flag"], False)

    def test_flag_defaults_to_allow_when_config_manager_lacks_getter(self):
        self.assertTrue(eject_service._allow_dismount_without_lock(
            type("M", (), {"config_manager": object()})()))


def make_real_backend(snapshot):
    """真 WindowsBackend + IOCTL 打桩：用来跑完整事务。"""
    events = []
    backend = WindowsBackend(snapshot, "B000BBBBAAAA",
                             lambda event, data: events.append((event, dict(data))))
    backend.index = 4
    backend.allow_dismount_without_lock = True
    backend._diagnose = lambda handle: dict(EMPTY_DIAGNOSTIC)

    counter = [1000]

    def fake_open(path):
        counter[0] += 1
        backend.owned.add(counter[0])
        return counter[0]

    def fake_close(handle):
        backend.owned.discard(handle)

    backend.open = fake_open
    backend.close = fake_close

    def ioctl(handle, code, source=None, destination=None):
        if code == 0x2D1080:                     # IOCTL_STORAGE_GET_DEVICE_NUMBER
            if destination is not None:
                destination[0], destination[1], destination[2] = 7, 4, 2
            return 12
        if code == 0x2D1400:                     # STORAGE_DEVICE_DESCRIPTOR
            offset = 36
            serial = b"B000BBBBAAAA"
            payload = bytearray(offset + len(serial) + 1)
            payload[24:28] = offset.to_bytes(4, "little")
            payload[28] = 7                      # BusTypeUsb
            payload[offset:offset + len(serial)] = serial
            for i, value in enumerate(payload):
                destination[i] = value
            return len(payload)
        if code == 0x90018:                      # FSCTL_LOCK_VOLUME
            raise ctypes.WinError(5)
        if code == 0x700F0:                      # IOCTL_DISK_GET_DISK_ATTRIBUTES
            destination.attrs = 1
            return 8
        return 0                                 # dismount/offline/ATA 全部接受

    backend.ioctl = ioctl
    backend.cfg.CM_Request_Device_EjectW = lambda *args: 0
    return backend, events


BRIDGE = {"instance_id": "USB\\VID_174C&PID_55AA\\MSFT30AAAABBBB000B",
          "dev_inst": 1, "service": "UASPStor"}


def allow_elevated():
    return (
        patch("ctypes.windll.shell32.IsUserAnAdmin", return_value=1),
        patch("src.hal.win32_api.Win32API.find_native_eject_target",
              return_value=dict(BRIDGE)),
    )


class RealBackendTransactionTests(unittest.TestCase):
    """用真 WindowsBackend（IOCTL 全部打桩）验证 ReFS 直卸回退能跑完整事务。"""

    def test_refs_fallback_completes_offline_sleep_and_eject(self):
        backend, events = make_real_backend(make_snapshot("ReFS", "E"))
        admin, target = allow_elevated()
        with admin, target:
            result = run(backend)
        self.assertTrue(result.ejected)
        self.assertTrue(result.sleep_accepted)
        self.assertEqual(result.state, "ejected_sleep_accepted")
        self.assertEqual(result.error, "")
        names = [event for event, _ in events]
        self.assertIn("validated", names)
        self.assertIn("volume_dismounted_without_lock", names)
        self.assertNotIn("volume_locked", names)
        self.assertIn("volume_flush_skipped", names)
        self.assertIn("volume_dismount_skipped", names)
        self.assertIn("disk_offline", names)
        self.assertIn("offline_verified", names)
        self.assertIn("sleep_dispatch", names)
        self.assertIn("pnp_request", names)
        self.assertEqual(backend.owned, set())        # 句柄全部释放

    def test_ntfs_lock_denied_aborts_before_offline(self):
        backend, events = make_real_backend(make_snapshot("NTFS", "E"))
        backend.volume_meta[VOLUME_GUID]["filesystem"] = "NTFS"
        admin, target = allow_elevated()
        with admin, target:
            result = run(backend)
        self.assertFalse(result.ejected)
        self.assertEqual(result.state, "unchanged")
        self.assertIn("无法隔离卷", result.error)
        names = [event for event, _ in events]
        self.assertNotIn("disk_offline", names)
        self.assertNotIn("sleep_dispatch", names)
        self.assertEqual(backend.owned, set())


def make_volume_less_snapshot(style="RAW", volumes=None, query_error=None):
    snapshot = make_snapshot("ReFS", "E")
    snapshot["volumes"] = [] if volumes is None else volumes
    snapshot["disk"]["PartitionStyle"] = style
    snapshot["disk"]["NumberOfPartitions"] = 0
    snapshot["volume_query_error"] = query_error or (
        '找不到任何“DiskNumber”属性等于“3”的 MSFT_Partition 对象。请验证属性值，然后重试。')
    return snapshot


class VolumeLessDiskTests(unittest.TestCase):
    """无分区表（RAW）的盘没有卷可隔离，应允许整盘直接停转 + 弹出。"""

    def test_raw_disk_passes_validate_and_records_reason(self):
        backend, events = make_real_backend(make_volume_less_snapshot())
        admin, target = allow_elevated()
        with admin, target:
            backend.validate()
        self.assertTrue(backend.volume_less)
        self.assertEqual(backend.volumes, [])
        names = [event for event, _ in events]
        self.assertIn("volume_less_disk", names)
        self.assertIn("validated", names)
        payload = dict(events)["validated"]
        self.assertTrue(payload["volume_less"])

    def test_raw_disk_completes_full_transaction_without_volume_ops(self):
        backend, events = make_real_backend(make_volume_less_snapshot())
        admin, target = allow_elevated()
        with admin, target:
            result = run(backend)
        self.assertTrue(result.ejected)
        self.assertTrue(result.sleep_accepted)
        self.assertEqual(result.state, "ejected_sleep_accepted")
        names = [event for event, _ in events]
        self.assertIn("volume_less_disk", names)
        self.assertIn("disk_offline", names)
        self.assertIn("offline_verified", names)
        self.assertIn("sleep_dispatch", names)
        self.assertIn("pnp_request", names)
        for unexpected in ("volume_locked", "volume_dismounted",
                           "volume_dismounted_without_lock"):
            self.assertNotIn(unexpected, names)
        self.assertEqual(backend.owned, set())

    def test_partitioned_disk_without_volumes_still_fails_closed(self):
        backend, events = make_real_backend(make_volume_less_snapshot(style="GPT"))
        admin, target = allow_elevated()
        with admin, target:
            result = run(backend)
        self.assertEqual(result.state, "unchanged")
        self.assertIn("cannot prove complete isolation", result.error)
        self.assertIn("MSFT_Partition", result.error)   # 卷枚举错误原样带上
        names = [event for event, _ in events]
        self.assertNotIn("sleep_dispatch", names)
        self.assertNotIn("disk_offline", names)
        self.assertNotIn("volume_less_disk", names)
        self.assertEqual(backend.owned, set())

    def test_inventory_script_tolerates_partitionless_disk(self):
        from src.hal import eject_backend
        captured = {}

        class Done:
            returncode = 0
            stdout = ('{"disk": {"Number": 3, "PartitionStyle": "RAW"}, "volumes": [], '
                      '"volume_query_error": "no partitions", "physical": [], '
                      '"managers": [], "pagefiles": []}')
            stderr = ""

        def fake_run(args, **kwargs):
            captured["script"] = args[-1]
            return Done()

        with patch.object(eject_backend.subprocess, "run", fake_run):
            snapshot = eject_backend.inventory(disk_index=3)
        script = captured["script"]
        self.assertIn("catch", script)
        self.assertIn("volume_query_error", script)
        self.assertIn("PartitionStyle", script)
        self.assertNotIn("Get-Partition -DiskNumber $disk.Number | Get-Volume", script)
        self.assertEqual(snapshot["volumes"], [])
        self.assertEqual(snapshot["volume_query_error"], "no partitions")


if __name__ == "__main__":
    unittest.main(verbosity=2)
