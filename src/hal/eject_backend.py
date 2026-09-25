"""Strict Windows backend shared by GUI, native tray handoff, and probe."""
import ctypes as C
from ctypes import wintypes as W
import json
import logging
import re
import subprocess
import time
from src.hal.win32_api import Win32API, SCSI_PASS_THROUGH_DIRECT
from src.utils import volume_diag

def inventory(letter=None, disk_index=None):
    # No user text is interpolated except a validated single ASCII letter.
    if disk_index is None and not re.fullmatch("[A-Za-z]", letter or ""):
        raise ValueError("drive must be a single letter")
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$part = Get-Partition -DriveLetter 'LETTER'
$disk = Get-Disk -Number $part.DiskNumber
# 无分区表的盘（RAW）Get-Partition 会直接报错，这不是失败：
# 没有卷需要隔离，允许走"整盘直接停转+弹出"。真正的卷枚举错误写进 volume_query_error。
$vols = @()
$volumeError = ''
try {
    $parts = @(Get-Partition -DiskNumber $disk.Number -ErrorAction Stop)
} catch {
    $parts = @()
    $volumeError = $_.Exception.Message
}
if ($parts.Count -gt 0) {
    try {
        $vols = @($parts | Get-Volume -ErrorAction Stop |
            Select-Object -Property UniqueId,Path,DriveLetter,FileSystemType)
    } catch {
        $vols = @()
        $volumeError = $_.Exception.Message
    }
}
$physical = @(Get-CimInstance Win32_DiskDrive | Select-Object Index,PNPDeviceID,SerialNumber,Size)
$managers = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -like '*硬盘柜管理*' -and $_.CommandLine -notlike '*--shutdown-service*'
} | Select-Object ProcessId,Name,CommandLine)
[ordered]@{
 disk = ($disk | Select-Object Number,FriendlyName,SerialNumber,BusType,IsBoot,IsSystem,IsOffline,Size,PartitionStyle,NumberOfPartitions)
 volumes = $vols
 volume_query_error = $volumeError
 physical = $physical
 managers = $managers
 pagefiles = @(Get-CimInstance Win32_PageFileUsage | Select-Object Name)
} | ConvertTo-Json -Depth 6 -Compress
"""
    if disk_index is None:
        script = script.replace("LETTER", letter.upper())
    else:
        index = int(disk_index)
        if index < 0:
            raise ValueError("Invalid disk index")
        script = script.replace("$part = Get-Partition -DriveLetter 'LETTER'", "")
        script = script.replace("$disk = Get-Disk -Number $part.DiskNumber", f"$disk = Get-Disk -Number {index}")
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=30, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip())
    return json.loads(completed.stdout.lstrip("\ufeff"))


class WindowsBackend:
    def __init__(self, snapshot, expected_serial, record, owner_pid=None, expected_pnp=None):
        self.snapshot = snapshot
        self.owner_pid = owner_pid
        self.expected_pnp = expected_pnp
        self.expected_serial = expected_serial
        self.record = record
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p,
                                       W.DWORD, W.DWORD, W.HANDLE]
        self.k.CreateFileW.restype = W.HANDLE
        self.k.CloseHandle.argtypes = [W.HANDLE]
        self.k.CloseHandle.restype = W.BOOL
        self.k.DeviceIoControl.argtypes = [W.HANDLE, W.DWORD, C.c_void_p, W.DWORD,
                                           C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]
        self.k.DeviceIoControl.restype = W.BOOL
        self.k.FlushFileBuffers.argtypes = [W.HANDLE]
        self.k.FlushFileBuffers.restype = W.BOOL
        self.cfg = C.WinDLL("cfgmgr32")
        self.cfg.CM_Request_Device_EjectW.argtypes = [W.DWORD, C.POINTER(C.c_int),
                                                    W.LPWSTR, W.ULONG, W.ULONG]
        self.cfg.CM_Request_Device_EjectW.restype = W.ULONG
        self.owned = set()
        self.volumes = []
        # 句柄 -> 卷路径、卷路径 -> (盘符, 文件系统)：失败时用来生成可读提示。
        self._handle_volume = {}
        self.volume_meta = {}
        self._predismounted = set()
        # 无分区表（RAW）的盘没有卷需要隔离，标记后按整盘直接弹出。
        self.volume_less = False
        # ReFS/exFAT 等不支持 FSCTL_LOCK_VOLUME；True 时按 Windows 资源管理器
        # 的做法跳过锁定、直接卸载卷（由 eject_service 从配置注入）。
        self.allow_dismount_without_lock = True
        for vol in (snapshot or {}).get("volumes") or []:
            path = str(vol.get("Path") or vol.get("UniqueId") or "").rstrip("\\")
            if not path:
                continue
            self.volume_meta[path] = {
                "drive_letter": str(vol.get("DriveLetter") or "").strip().rstrip(":"),
                "filesystem": str(vol.get("FileSystemType")
                                  or vol.get("FileSystem") or "").strip().upper(),
            }

    def _meta_for(self, handle):
        return self.volume_meta.get(self._handle_volume.get(handle, ""), {})

    def _volume_label(self, handle):
        name = self._handle_volume.get(handle, "")
        meta = self.volume_meta.get(name, {})
        return volume_diag.volume_label(name, meta.get("drive_letter"), self.index)

    def _diagnose(self, handle):
        name = self._handle_volume.get(handle, "")
        meta = self.volume_meta.get(name, {})
        return volume_diag.diagnose_volume_isolation(
            name, meta.get("drive_letter"), meta.get("filesystem"), self.index)

    def validate(self):
        s, d = self.snapshot, self.snapshot["disk"]
        if not C.windll.shell32.IsUserAnAdmin():
            raise RuntimeError("Administrator token required")
        if (str(d["BusType"]).upper() not in ("USB", "7") or d["IsBoot"] or d["IsSystem"]
                or d["IsOffline"] or int(d["Size"]) == 0):
            raise RuntimeError("Only an online, non-system USB disk with media is allowed")
        if d["SerialNumber"].strip() != self.expected_serial:
            raise RuntimeError("Disk serial changed; refusing stale disk-number selection")
        if any(p["ProcessId"] != self.owner_pid for p in s["managers"]):
            raise RuntimeError("Exit the existing GUI through its tray menu first: "
                               + str([p["ProcessId"] for p in s["managers"]]))
        if not s["volumes"]:
            # 无分区表（RAW）的盘：没有任何卷需要隔离，允许整盘直接停转 + 弹出。
            # 有分区表却枚举不到卷，说明隔离无法证明，依旧 fail closed。
            style = str(d.get("PartitionStyle") or "").upper()
            if style != "RAW":
                detail = str(s.get("volume_query_error") or "").strip()
                raise RuntimeError(
                    "No volumes enumerated; cannot prove complete isolation"
                    + ("（卷枚举错误：%s）" % detail if detail else ""))
            self.volume_less = True
            self.record("volume_less_disk", {
                "disk": int(d["Number"]), "partition_style": style,
                "size": int(d["Size"]),
                "note": "该盘无分区表（RAW），没有卷需要锁定/卸载，整盘直接停转并弹出"})
        roots = []
        for vol in s["volumes"]:
            path = vol.get("Path") or vol.get("UniqueId") or ""
            if not path.startswith("\\\\?\\Volume{"):
                raise RuntimeError("Cannot resolve volume GUID: " + path)
            self.volumes.append(path.rstrip("\\"))
            if vol.get("DriveLetter"):
                roots.append(str(vol["DriveLetter"]).upper() + ":")
        if any(p["Name"][:2].upper() in roots for p in s["pagefiles"]):
            raise RuntimeError("Target contains a pagefile")
        # Select exactly one storage bridge, never a hub or a subtree fallback.
        self.index = int(d["Number"])
        raw = next(p for p in s["physical"] if int(p["Index"]) == self.index)
        if self.expected_pnp and raw["PNPDeviceID"].upper() != self.expected_pnp.upper():
            raise RuntimeError("PnP identity changed since selection")
        self.target = Win32API.find_native_eject_target(raw["PNPDeviceID"])
        if not self.target or (self.target.get("service") or "").upper() not in ("UASPSTOR", "USBSTOR"):
            raise RuntimeError("No dedicated USB storage bridge identified")
        members = []
        for p in s["physical"]:
            t = Win32API.find_native_eject_target(p["PNPDeviceID"])
            if t and t["instance_id"].upper() == self.target["instance_id"].upper():
                members.append(int(p["Index"]))
        if members != [self.index]:
            raise RuntimeError("Bridge affects multiple disks: " + str(members))
        self.record("validated", {"disk": d, "volumes": self.volumes,
                                  "volume_less": self.volume_less,
                                  "eject_target": self.target["instance_id"]})

    def ioctl(self, handle, code, source=None, destination=None):
        count = W.DWORD()
        ok = self.k.DeviceIoControl(handle, code,
            C.byref(source) if source is not None else None,
            C.sizeof(source) if source is not None else 0,
            C.byref(destination) if destination is not None else None,
            C.sizeof(destination) if destination is not None else 0,
            C.byref(count), None)
        if not ok:
            raise C.WinError(C.get_last_error())
        return count.value

    def open(self, path):
        h = self.k.CreateFileW(path, 0xC0000000, 3, None, 3, 0, None)
        if not h or h == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        self.owned.add(h)
        return h

    def check_number(self, h):
        number = (W.DWORD * 3)()
        self.ioctl(h, 0x2D1080, destination=number)
        if number[1] != self.index:
            raise RuntimeError("Device number changed after inventory")

    def open_disk(self):
        path = f"\\\\.\\PhysicalDrive{self.index}"
        try:
            h = self.open(path)
        except OSError as exc:
            raise RuntimeError(volume_diag.format_stage_failure(
                f"打开物理盘 {path} 句柄", "", getattr(exc, "winerror", None),
                hint="确认程序以管理员身份运行；该盘若被其他程序独占，请先关闭后重试。"))
        try:
            self.check_number(h)
            # Query the cached storage descriptor, checking identity again on the
            # handle we will actually use. Do this BEFORE offline/SLEEP.
            query = (C.c_ubyte * 12)()
            descriptor = (C.c_ubyte * 4096)()
            count = self.ioctl(h, 0x2D1400, query, descriptor)
            raw = bytes(descriptor[:count])
            if len(raw) < 36:
                raise RuntimeError("Short STORAGE_DEVICE_DESCRIPTOR")
            serial_offset = int.from_bytes(raw[24:28], "little")
            serial = raw[serial_offset:].split(b"\0", 1)[0].decode("ascii", "replace").strip()
            if serial_offset < 36 or serial != self.expected_serial or raw[28] != 7:
                raise RuntimeError("Handle serial/bus does not match approved USB disk")
            return h
        except Exception:
            self.close(h)
            raise

    def open_volume(self, name):
        try:
            h = self.open(name)
        except OSError as exc:
            meta = self.volume_meta.get(str(name).rstrip("\\"), {})
            raise RuntimeError(volume_diag.format_stage_failure(
                "打开卷 %s 句柄" % volume_diag.volume_label(
                    name, meta.get("drive_letter"), self.index),
                "", getattr(exc, "winerror", None),
                hint="关闭占用该卷的程序（资源管理器窗口、杀毒/索引服务等）后重试。"))
        try:
            self.check_number(h)
            self._handle_volume[h] = str(name).rstrip("\\")
            return h
        except Exception:
            self.close(h)
            raise

    def lock(self, h):
        """独占锁定卷；ReFS/exFAT 等不支持锁定的文件系统走直卸回退。"""
        meta = self._meta_for(h)
        filesystem = meta.get("filesystem", "")
        try:
            self.ioctl(h, 0x90018)
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if winerror == 5 and not volume_diag.filesystem_supports_lock(filesystem):
                if not self.allow_dismount_without_lock:
                    diagnostic = self._diagnose(h)
                    self.record("volume_lock_unsupported", {
                        "volume": self._volume_label(h), "filesystem": filesystem,
                        "winerror": winerror, "fallback": "disabled"})
                    raise RuntimeError(volume_diag.format_lock_failure(
                        diagnostic, winerror, None, filesystem)
                        + "\n该文件系统不支持锁卷，直卸回退当前被配置关闭"
                          "（eject_dismount_without_lock=false）。")
                self._dismount_without_lock(h, filesystem, winerror)
                return
            diagnostic = self._diagnose(h)
            self.record("volume_lock_failed", {
                "volume": self._volume_label(h), "filesystem": filesystem,
                "winerror": winerror, "occupiers": diagnostic.get("occupiers"),
                "self_handles": len(diagnostic.get("self_handles") or [])})
            raise RuntimeError(volume_diag.format_lock_failure(
                diagnostic, winerror, None, filesystem))
        self.record("volume_locked", {})

    def _dismount_without_lock(self, h, filesystem, lock_error):
        """ReFS/exFAT：跳过锁定直接卸载（Windows 资源管理器弹出同款做法）。

        占用诊断只在卸载也失败时做：成功路径上做全表句柄扫描会白白拖慢弹出。
        """
        label = self._volume_label(h)
        try:
            self.ioctl(h, 0x90020)
        except OSError as exc:
            dismount_error = getattr(exc, "winerror", None)
            diagnostic = self._diagnose(h)
            self.record("volume_isolation_failed", {
                "volume": label, "filesystem": filesystem,
                "lock_winerror": lock_error, "dismount_winerror": dismount_error})
            raise RuntimeError(volume_diag.format_lock_failure(
                diagnostic, lock_error, dismount_error, filesystem))
        self._predismounted.add(h)
        note = volume_diag.format_lock_unsupported(
            {"label": label, "filesystem": filesystem}, filesystem, True)
        logging.warning("[Eject] %s", note)
        self.record("volume_dismounted_without_lock", {
            "volume": label, "filesystem": filesystem,
            "lock_winerror": lock_error, "note": note})

    def flush_volume(self, h):
        if h in self._predismounted:
            # 已直卸的卷不能再 FlushFileBuffers：卷已卸载，调用只会失败并中止事务。
            self.record("volume_flush_skipped", {"volume": self._volume_label(h)})
            return
        if not self.k.FlushFileBuffers(h):
            raise C.WinError(C.get_last_error())

    def dismount(self, h):
        if h in self._predismounted:
            self.record("volume_dismount_skipped", {"volume": self._volume_label(h)})
            return
        self.ioctl(h, 0x90020)
        self.record("volume_dismounted", {})

    def offline(self, h, value):
        attrs = Win32API.SET_DISK_ATTRIBUTES()
        attrs.Version = C.sizeof(attrs)
        attrs.Persist = 0
        attrs.Attributes = int(value)
        attrs.AttributesMask = 1
        self.ioctl(h, 0x7C0F4, attrs)
        self.record("disk_offline" if value else "disk_online_rollback", {})

    def verify_offline(self, h):
        class Attributes(C.Structure):
            _fields_ = [("version", W.DWORD), ("reserved", W.DWORD), ("attrs", C.c_ulonglong)]
        attrs = Attributes()
        self.ioctl(h, 0x700F0, destination=attrs)
        if not attrs.attrs & 1:
            raise RuntimeError("Disk did not enter offline state; refusing SLEEP")
        self.record("offline_verified", {})

    def ata(self, h, command):
        class Packet(C.Structure):
            _fields_ = [("sptd", SCSI_PASS_THROUGH_DIRECT), ("sense", C.c_ubyte * 32)]
        packet = Packet()
        packet.sptd.Length = C.sizeof(SCSI_PASS_THROUGH_DIRECT)
        packet.sptd.CdbLength = 16
        packet.sptd.DataIn = 2  # SCSI_IOCTL_DATA_UNSPECIFIED (non-data command)
        packet.sptd.TimeOutValue = 3
        packet.sptd.SenseInfoLength = 32
        packet.sptd.SenseInfoOffset = Packet.sense.offset
        packet.sptd.Cdb[0], packet.sptd.Cdb[1], packet.sptd.Cdb[14] = 0x85, 6, command
        started = time.monotonic()
        ioctl_ok, winerror = False, None
        try:
            self.ioctl(h, 0x4D014, packet, packet)
            ioctl_ok = True
        except OSError as exc:
            winerror = exc.winerror
            raise
        finally:
            self.record("ata_result", {"command": hex(command), "seconds": time.monotonic() - started,
                                       "ioctl_ok": ioctl_ok, "winerror": winerror,
                                       "scsi_status": packet.sptd.ScsiStatus,
                                       "sense": bytes(packet.sense).hex()})
        if packet.sptd.ScsiStatus != 0:
            raise RuntimeError("ATA command not positively acknowledged: " + hex(command))

    def flush_disk(self, h): self.ata(h, 0xE7)
    def sleep(self, h): self.ata(h, 0xE6)

    def close(self, h):
        if h not in self.owned:
            raise RuntimeError("Double close prevented")
        if not self.k.CloseHandle(h):
            raise C.WinError(C.get_last_error())
        self.owned.remove(h)

    def eject(self):
        if self.owned:
            raise RuntimeError("Refusing PnP request while operation handles remain open")
        self.record("pnp_request", {"target": self.target["instance_id"]})
        veto = C.c_int()
        name = C.create_unicode_buffer(1024)
        started = time.monotonic()
        # Synchronous once only: do not pretend a timed-out background thread
        # cancelled the OS operation. The caller can observe the journal.
        cr = self.cfg.CM_Request_Device_EjectW(self.target["dev_inst"], C.byref(veto), name, len(name), 0)
        self.record("pnp_result", {"cr": cr, "veto_type": veto.value,
                                   "veto_name": name.value, "seconds": time.monotonic() - started})
        if cr:
            raise RuntimeError(f"PnP veto: CR={cr}, type={veto.value}, name={name.value}")

