"""Native tray handoff. No disk commands or waits in WM_DEVICECHANGE.

The initial query is explicitly deferred with BCAST_QUERY_DENY. Only a matching
QUERYREMOVEFAILED authorizes the GUI to start the normal offline/SLEEP/eject
transaction. Windows may show its initial veto notification.
"""
import ctypes
from ctypes import wintypes
import logging
import time

BCAST_QUERY_DENY = 0x424D5144


class SafeRemovalPatcher:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._hwnd = None
        self.enabled = True
        self.shutdown_in_progress = False
        self.monitor_service = None
        self.on_native_request = None
        self._handle_disk_map = {}
        self._handle_notify_map = {}
        self._identity = {}
        self.pending = {}
        self.managed = set()

    def refresh_cache(self):
        # Identities come from the monitor cache. Never enumerate volumes/TUR
        # in response to generic device-tree changes.
        return

    def register(self, hwnd):
        self._hwnd = hwnd
        self._register_handle_notifications()
        return bool(self._handle_notify_map)

    def _apis(self):
        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        user.RegisterDeviceNotificationW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        user.RegisterDeviceNotificationW.restype = ctypes.c_void_p
        user.UnregisterDeviceNotification.argtypes = [ctypes.c_void_p]
        user.UnregisterDeviceNotification.restype = wintypes.BOOL
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        return user, kernel

    def _register_handle_notifications(self):
        if not self._hwnd or self.shutdown_in_progress or not self.monitor_service:
            return
        monitor = self.monitor_service
        if monitor.removal_pending.is_set():
            return
        from src.hal.win32_api import DEV_BROADCAST_HANDLE
        user, kernel = self._apis()
        for disk in list(monitor.cached_disks):
            idx = disk.index
            identity = {"index": idx, "model": disk.model,
                        "serial": disk.serial_number, "pnp_id": disk.pnp_id or ""}
            if not disk.is_removable or not identity["pnp_id"]:
                continue
            if (idx in self.managed or idx in self.pending or monitor.is_eject_blocked(disk)
                    or disk.serial_number in monitor.ejected_disks):
                continue
            existing = self._identity.get(idx)
            if existing and existing["pnp_id"] != identity["pnp_id"]:
                self._release_handle_for_disk(idx)
            if idx in self._handle_disk_map.values():
                continue  # Incremental per-disk registration, not all-or-none.
            h = kernel.CreateFileW(f"\\\\.\\PhysicalDrive{idx}", 0, 3, None, 3, 0, None)
            if not h or h == ctypes.c_void_p(-1).value:
                continue
            packet = DEV_BROADCAST_HANDLE()
            packet.dbch_size = ctypes.sizeof(packet)
            packet.dbch_devicetype = 6
            packet.dbch_handle = h
            notify = user.RegisterDeviceNotificationW(self._hwnd, ctypes.byref(packet), 0)
            if not notify:
                kernel.CloseHandle(h)
                logging.warning("[NativeEject] notification registration failed: disk=%s", idx)
                continue
            self._handle_disk_map[h] = idx
            self._handle_notify_map[notify] = h
            self._identity[idx] = identity
            logging.info("[NativeEject] registered disk=%s handle=%s pnp=%s", idx, h, disk.pnp_id)

    def _release_handle_for_disk(self, index):
        user, kernel = self._apis()
        for notify, handle in list(self._handle_notify_map.items()):
            if self._handle_disk_map.get(handle) != index:
                continue
            user.UnregisterDeviceNotification(notify)
            if not kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle_notify_map.pop(notify, None)
            self._handle_disk_map.pop(handle, None)

    def begin_managed(self, index):
        self.managed.add(index)
        self._release_handle_for_disk(index)

    def finish_managed(self, index):
        self.managed.discard(index)
        self.pending.pop(index, None)

    def native_ready(self, index):
        return bool(self.pending.get(index, {}).get("ready"))

    def unregister(self):
        for index in set(self._handle_disk_map.values()):
            self._release_handle_for_disk(index)
        self._hwnd = None

    def handle_wm_devicechange(self, event, lparam):
        # Untargeted arrival/removal/tree events must never probe or sleep all disks.
        if event not in (0x8001, 0x8002, 0x8003, 0x8004) or not lparam:
            return 1
        from src.hal.win32_api import DEV_BROADCAST_HDR, DEV_BROADCAST_HANDLE
        header = ctypes.cast(lparam, ctypes.POINTER(DEV_BROADCAST_HDR)).contents
        if header.dbch_devicetype != 6:
            return 1
        packet = ctypes.cast(lparam, ctypes.POINTER(DEV_BROADCAST_HANDLE)).contents
        index = self._handle_disk_map.get(packet.dbch_handle)
        if index is None:
            index = self._handle_disk_map.get(self._handle_notify_map.get(packet.dbch_hdevnotify))
        if index is None:
            return 1
        logging.info("[NativeEject] event=0x%04X disk=%s", event, index)
        if event == 0x8001:
            if self.shutdown_in_progress or not self.enabled or index in self.managed or not self.on_native_request:
                self._release_handle_for_disk(index)
                return 1
            if index not in self.pending:
                identity = dict(self._identity[index])
                self.pending[index] = {"ready": False, "started": time.monotonic()}
                try:
                    self.on_native_request(identity)
                except Exception:
                    self.pending.pop(index, None)
                    logging.exception("[NativeEject] cannot schedule handoff")
                    self._release_handle_for_disk(index)
                    return 1
                logging.info("[NativeEject] defer native query for controlled SLEEP: disk=%s", index)
            return BCAST_QUERY_DENY
        if event == 0x8002:
            if index in self.pending:
                self.pending[index]["ready"] = True
                logging.info("[NativeEject] original query finished; handoff ready: disk=%s", index)
        elif event in (0x8003, 0x8004):
            self._release_handle_for_disk(index)
        return 1
