import subprocess
import logging
import re
import ctypes
from ctypes import wintypes
import win32api
import win32con
import win32security

# Windows SetupAPI Constants and Structures
setupapi = ctypes.windll.setupapi
cfgmgr32 = ctypes.windll.cfgmgr32

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

# ULONG_PTR definition based on architecture
if ctypes.sizeof(ctypes.c_void_p) == 8:
    ULONG_PTR = ctypes.c_ulonglong
else:
    ULONG_PTR = ctypes.c_ulong

class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", GUID),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ULONG_PTR),
    ]

DIGCF_PRESENT = 0x00000002
DIGCF_ALLCLASSES = 0x00000004
DIGCF_DEVICEINTERFACE = 0x00000010
SPDRP_FRIENDLYNAME = 0x0000000C
SPDRP_DEVICEDESC = 0x00000000

# SetupDiOpenDeviceInfo Flags
# Use SetupDiOpenDeviceInfo requires a valid HDEVINFO handle.
# Instead, we can create an empty HDEVINFO list and add the device via SetupDiOpenDeviceInfo?
# Actually, SetupDiGetClassDevs returns a handle.
# But we have InstanceID. SetupDiOpenDeviceInfo is for an existing HDEVINFO set.
# The correct way to get a specific device by InstanceID is:
# 1. hDevInfo = SetupDiCreateDeviceInfoList(None, None)
# 2. SetupDiOpenDeviceInfo(hDevInfo, instance_id, None, 0, byref(devInfoData))
# OR
# SetupDiGetClassDevs(None, instance_id, None, DIGCF_ALLCLASSES | DIGCF_PRESENT) -> doesn't work with InstanceID directly as Enumerator?
# Wait, SetupDiGetClassDevs second param is "Enumerator". For PnP devices, it can be the Instance ID if DIGCF_DEVICEINTERFACE is NOT set? No.
# "Enumerator" is usually a PnP enumerator name (e.g. "USB").
#
# Correct approach for specific Instance ID:
# hDevInfo = SetupDiGetClassDevs(None, instance_id, None, DIGCF_ALLCLASSES | DIGCF_PRESENT) ?? No.
#
# Use SetupDiOpenDeviceInfoW.
# It requires an existing HDEVINFO set.
# So:
# hDevInfo = SetupDiCreateDeviceInfoList(None, None)
# SetupDiOpenDeviceInfoW(hDevInfo, instance_id, None, 0, byref(devInfoData))

SetupDiCreateDeviceInfoList = setupapi.SetupDiCreateDeviceInfoList
SetupDiCreateDeviceInfoList.argtypes = [ctypes.POINTER(GUID), wintypes.HWND]
SetupDiCreateDeviceInfoList.restype = wintypes.HANDLE

SetupDiOpenDeviceInfoW = setupapi.SetupDiOpenDeviceInfoW
SetupDiOpenDeviceInfoW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)]
SetupDiOpenDeviceInfoW.restype = wintypes.BOOL

SetupDiSetDeviceRegistryPropertyW = setupapi.SetupDiSetDeviceRegistryPropertyW
SetupDiSetDeviceRegistryPropertyW.argtypes = [wintypes.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD, ctypes.POINTER(ctypes.c_byte), wintypes.DWORD]
SetupDiSetDeviceRegistryPropertyW.restype = wintypes.BOOL

SetupDiGetDeviceRegistryPropertyW = setupapi.SetupDiGetDeviceRegistryPropertyW
SetupDiGetDeviceRegistryPropertyW.argtypes = [wintypes.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_byte), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL

SetupDiDestroyDeviceInfoList = setupapi.SetupDiDestroyDeviceInfoList
SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

CM_Reenumerate_DevNode = cfgmgr32.CM_Reenumerate_DevNode
CM_Reenumerate_DevNode.argtypes = [wintypes.DWORD, wintypes.DWORD]
CM_Reenumerate_DevNode.restype = wintypes.DWORD

class DeviceRenamer:
    @staticmethod
    def get_device_map():
        """
        获取物理磁盘索引到 PnP 设备 ID 的映射
        返回: {disk_index (int): pnp_device_id (str)}

        注意：改用 WMI(Win32_DiskDrive) 直接读取，不依赖 powershell 子进程。
        powershell 子进程在部分环境会挂起（导致磁盘枚举阻塞、SMART 检测卡死），
        WMI 与项目其他模块一致、更稳定。
        """
        device_map = {}
        try:
            import wmi
            c = wmi.WMI()
            for drive in c.Win32_DiskDrive():
                try:
                    dev_id = drive.DeviceID or ""
                    pnp_id = drive.PNPDeviceID or ""
                    match = re.search(r"PHYSICALDRIVE(\d+)", dev_id, re.IGNORECASE)
                    if match and pnp_id:
                        index = int(match.group(1))
                        device_map[index] = pnp_id
                except Exception:
                    continue
        except Exception as e:
            logging.error(f"获取设备映射异常: {e}")

        return device_map

    @staticmethod
    def _get_device_info(pnp_id):
        """Helper to get HDEVINFO and SP_DEVINFO_DATA for a given PnP ID"""
        hDevInfo = SetupDiCreateDeviceInfoList(None, None)
        if hDevInfo == -1 or hDevInfo is None: # INVALID_HANDLE_VALUE is -1
             logging.error(f"SetupDiCreateDeviceInfoList failed: {ctypes.GetLastError()}")
             return None, None

        devInfoData = SP_DEVINFO_DATA()
        devInfoData.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)

        if not SetupDiOpenDeviceInfoW(hDevInfo, pnp_id, None, 0, ctypes.byref(devInfoData)):
            # logging.error(f"SetupDiOpenDeviceInfoW failed for {pnp_id}: {ctypes.GetLastError()}")
            SetupDiDestroyDeviceInfoList(hDevInfo)
            return None, None

        return hDevInfo, devInfoData

    @staticmethod
    def get_friendly_name(pnp_id):
        """读取当前的 FriendlyName using SetupAPI"""
        hDevInfo, devInfoData = DeviceRenamer._get_device_info(pnp_id)
        if not hDevInfo:
            return None

        try:
            prop_type = wintypes.DWORD()
            buffer = (ctypes.c_byte * 1024)()
            required_size = wintypes.DWORD()

            if SetupDiGetDeviceRegistryPropertyW(hDevInfo, ctypes.byref(devInfoData), SPDRP_FRIENDLYNAME,
                                               ctypes.byref(prop_type), buffer, 1024, ctypes.byref(required_size)):
                return ctypes.cast(buffer, ctypes.c_wchar_p).value

            # Try DeviceDesc if FriendlyName fails
            if SetupDiGetDeviceRegistryPropertyW(hDevInfo, ctypes.byref(devInfoData), SPDRP_DEVICEDESC,
                                               ctypes.byref(prop_type), buffer, 1024, ctypes.byref(required_size)):
                val = ctypes.cast(buffer, ctypes.c_wchar_p).value
                return val.split(";")[-1] if ";" in val else val

        except Exception as e:
            logging.error(f"SetupAPI Get failed: {e}")
        finally:
            SetupDiDestroyDeviceInfoList(hDevInfo)

        return None

    @staticmethod
    def _enable_privilege(privilege_name):
        try:
            h_token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_ADJUST_PRIVILEGES | win32con.TOKEN_QUERY)
            luid = win32security.LookupPrivilegeValue(None, privilege_name)
            win32security.AdjustTokenPrivileges(h_token, 0, [(luid, win32con.SE_PRIVILEGE_ENABLED)])
            win32api.CloseHandle(h_token)
            return True
        except Exception as e:
            logging.error(f"Failed to enable privilege {privilege_name}: {e}")
            return False

    @staticmethod
    def _grant_admin_access(pnp_id):
        """尝试授予管理员对 Enum 注册表项的完全控制权"""
        key_path = f"SYSTEM\\CurrentControlSet\\Enum\\{pnp_id}"
        try:
            # 1. Enable SeTakeOwnershipPrivilege
            DeviceRenamer._enable_privilege(win32security.SE_TAKE_OWNERSHIP_NAME)

            # 2. Get Administrators SID
            admin_sid = win32security.LookupAccountName(None, "Administrators")[0]

            # 3. Open Key with WRITE_OWNER (needs SeTakeOwnershipPrivilege)
            try:
                reg_key = win32api.RegOpenKeyEx(win32con.HKEY_LOCAL_MACHINE, key_path, 0, win32con.WRITE_OWNER)
            except Exception as e:
                logging.warning(f"无法打开注册表项修改所有者: {e}")
                return False

            # 4. Set Owner to Administrators
            sd = win32security.SECURITY_DESCRIPTOR()
            sd.SetSecurityDescriptorOwner(admin_sid, False)
            win32api.RegSetKeySecurity(reg_key, win32security.OWNER_SECURITY_INFORMATION, sd)
            win32api.RegCloseKey(reg_key)

            # 5. Now we own it, open with WRITE_DAC
            reg_key = win32api.RegOpenKeyEx(win32con.HKEY_LOCAL_MACHINE, key_path, 0,
                                          win32con.WRITE_DAC | win32con.READ_CONTROL)

            # 6. Get current DACL and add Full Control
            sd = win32api.RegGetKeySecurity(reg_key, win32security.DACL_SECURITY_INFORMATION)
            dacl = sd.GetSecurityDescriptorDacl()
            if dacl is None:
                dacl = win32security.ACL()

            dacl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.KEY_ALL_ACCESS, admin_sid)
            sd.SetSecurityDescriptorDacl(1, dacl, 0)

            # 7. Apply new DACL
            win32api.RegSetKeySecurity(reg_key, win32security.DACL_SECURITY_INFORMATION, sd)
            win32api.RegCloseKey(reg_key)

            logging.info(f"已授予管理员对 {pnp_id} 的注册表写权限")
            return True

        except Exception as e:
            logging.error(f"修改注册表权限失败: {e}")
            return False

    @staticmethod
    def set_friendly_name(pnp_id, new_name):
        """设置 FriendlyName using SetupAPI with permission fix fallback"""
        hDevInfo, devInfoData = DeviceRenamer._get_device_info(pnp_id)
        if not hDevInfo:
            return False

        try:
            # Convert string to bytes (UTF-16LE for W API)
            name_bytes = new_name.encode('utf-16le') + b'\x00\x00'
            buffer = (ctypes.c_byte * len(name_bytes)).from_buffer_copy(name_bytes)

            success = SetupDiSetDeviceRegistryPropertyW(hDevInfo, ctypes.byref(devInfoData), SPDRP_FRIENDLYNAME,
                                               buffer, len(name_bytes))

            if not success:
                err = ctypes.GetLastError()
                logging.warning(f"SetupAPI 初次尝试失败 ({pnp_id}), Error: {err}")

                # 如果是权限错误 (5)，尝试修复权限
                if err == 5:
                    logging.info("尝试修复注册表权限...")
                    if DeviceRenamer._grant_admin_access(pnp_id):
                        # Retry
                        success = SetupDiSetDeviceRegistryPropertyW(hDevInfo, ctypes.byref(devInfoData), SPDRP_FRIENDLYNAME,
                                                           buffer, len(name_bytes))
                        if success:
                            logging.info("权限修复后 SetupAPI 成功")
                        else:
                            logging.error(f"权限修复后 SetupAPI 仍失败, Error: {ctypes.GetLastError()}")

            if success:
                logging.info(f"SetupAPI: 已更新设备名: {pnp_id} -> {new_name}")
                # Trigger re-enumeration to refresh Device Manager
                ret = CM_Reenumerate_DevNode(devInfoData.DevInst, 0)
                if ret == 0:
                    logging.info("设备节点重枚举成功 (UI 应已刷新)")
                else:
                    logging.warning(f"设备节点重枚举失败: {ret}")
                return True

            return False
        except Exception as e:
            logging.error(f"SetupAPI Set Exception: {e}")
            return False
        finally:
            SetupDiDestroyDeviceInfoList(hDevInfo)

    @staticmethod
    def should_rename(current_name, target_name, force=False):
        """
        判断是否需要重命名
        如果当前名字包含 'USB', 'SCSI', 'ATA Device', 'ASMT' 等通用词，且不包含目标型号的关键部分，则建议重命名。

        force=True 用于 USB 硬盘柜等可换盘盘位：盘位的 PnP ID 固定不变，
        但插入的硬盘可以更换（如台达固态换成 HC550）。此时 FriendlyName
        必须始终跟随当前盘的真实型号，否则设备管理器会一直显示旧盘的名字
        （实测：换盘后 3 个 HC550 显示成 2 个 HC550 + 1 个台达固态）。
        """
        if not current_name or not target_name:
            return False

        if current_name == target_name:
            return False

        # 清理目标名字中的非法字符（如果有）
        clean_target = target_name.strip()

        # 如果当前名字已经是目标名字（忽略大小写），不需要改
        if clean_target.lower() in current_name.lower():
            return False

        # 外置可换盘盘位：名字必须跟随当前盘的真实型号
        if force:
            return True

        # 检查是否是通用名
        generic_keywords = ["USB Device", "SCSI Disk Device", "ATA Device", "ASMT", "USBSTOR", "External USB 3.0", "Sabrent", "JMicron"]
        is_generic = any(k.lower() in current_name.lower() for k in generic_keywords)

        # 如果当前名字太短（可能是默认驱动名），也改
        if len(current_name) < 5:
            is_generic = True

        return is_generic
