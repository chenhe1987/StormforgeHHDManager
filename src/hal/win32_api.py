import ctypes
from ctypes import wintypes
import logging
import subprocess
import re
import threading
import time

# Windows Constants
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = -1

# Message Constants
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
ENDSESSION_CLOSEAPP = 0x0001
ENDSESSION_CRITICAL = 0x40000000
ENDSESSION_LOGOFF = 0x80000000

# CfgMgr32 Constants
CR_SUCCESS = 0x00000000
CM_LOCATE_DEVNODE_NORMAL = 0x00000000
CM_LOCATE_DEVNODE_PHANTOM = 0x00000001
CM_REMOVE_UI_NOT_OK = 0x00000002

IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x4D014
IOCTL_ATA_PASS_THROUGH = 0x4D02C
# 注意：旧值 0x2D0504 是错误的（驱动返回 ERROR_INVALID_FUNCTION），
# 正确 = CTL_CODE(IOCTL_STORAGE_BASE=0x2D, 0x0500, METHOD_BUFFERED, FILE_ANY_ACCESS)
IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400

# Storage Query Property Structures
class STORAGE_PROPERTY_QUERY(ctypes.Structure):
    _fields_ = [
        ("PropertyId", ctypes.c_int),
        ("QueryType", ctypes.c_int),
        ("AdditionalParameters", ctypes.c_ubyte * 40) # Large enough for STORAGE_PROTOCOL_SPECIFIC_DATA
    ]

class STORAGE_DEVICE_DESCRIPTOR(ctypes.Structure):
    """STORAGE_DEVICE_DESCRIPTOR（StorageDeviceProperty 查询的返回）。"""
    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("Size", ctypes.c_ulong),
        ("DeviceType", ctypes.c_byte),
        ("DeviceTypeModifier", ctypes.c_byte),
        ("RemovableMedia", ctypes.c_byte),
        ("CommandQueueing", ctypes.c_byte),
        ("VendorIdOffset", ctypes.c_ulong),
        ("ProductIdOffset", ctypes.c_ulong),
        ("ProductRevisionOffset", ctypes.c_ulong),
        ("SerialNumberOffset", ctypes.c_ulong),
        ("BusType", ctypes.c_byte),
        ("RawPropertiesLength", ctypes.c_byte * 3),
    ]

# BusType（STORAGE_DEVICE_DESCRIPTOR.BusType）
BusTypeUsb = 7

class STORAGE_PROTOCOL_SPECIFIC_DATA(ctypes.Structure):
    _fields_ = [
        ("ProtocolType", ctypes.c_int),
        ("DataType", ctypes.c_int),
        ("ProtocolDataOffset", ctypes.c_ulong),
        ("ProtocolDataLength", ctypes.c_ulong),
        ("FixedProtocolReturnData", ctypes.c_ulong),
        ("ProtocolDataRequestValue", ctypes.c_ulong),
        ("ProtocolDataRequestSubValue", ctypes.c_ulong),
        ("ProtocolDataRequestSubValue2", ctypes.c_ulong),
        ("ProtocolDataRequestSubValue3", ctypes.c_ulong),
        ("Reserved", ctypes.c_ulong),
    ]

class STORAGE_PROTOCOL_DATA_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("Size", ctypes.c_ulong),
        ("ProtocolSpecificData", STORAGE_PROTOCOL_SPECIFIC_DATA)
    ]

# NVMe Protocol Types
ProtocolTypeNvme = 3
NVMeDataTypeLogPage = 1
NVMeLogPageHealthInfo = 2
NVMeDataTypeIdentify = 2
NVMeIdentifyController = 1
NVMeIdentifyNamespace = 0

# SCSI PASS THROUGH DIRECT Structure
class SCSI_PASS_THROUGH_DIRECT(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("ScsiStatus", ctypes.c_ubyte),
        ("PathId", ctypes.c_ubyte),
        ("TargetId", ctypes.c_ubyte),
        ("Lun", ctypes.c_ubyte),
        ("CdbLength", ctypes.c_ubyte),
        ("SenseInfoLength", ctypes.c_ubyte),
        ("DataIn", ctypes.c_ubyte),
        ("DataTransferLength", ctypes.c_ulong),
        ("TimeOutValue", ctypes.c_ulong),
        ("DataBuffer", ctypes.c_void_p),
        ("SenseInfoOffset", ctypes.c_ulong),
        ("Cdb", ctypes.c_ubyte * 16),
    ]

class SCSI_PASS_THROUGH_DIRECT_WITH_SENSE(ctypes.Structure):
    _fields_ = [
        ("sptd", SCSI_PASS_THROUGH_DIRECT),
        ("sense", ctypes.c_ubyte * 32),
    ]

# ATA PASS THROUGH Structure
class ATA_PASS_THROUGH_EX(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("AtaFlags", ctypes.c_ushort),
        ("PathId", ctypes.c_ubyte),
        ("TargetId", ctypes.c_ubyte),
        ("Lun", ctypes.c_ubyte),
        ("ReservedAsUchar", ctypes.c_ubyte),
        ("DataTransferLength", ctypes.c_ulong),
        ("TimeOutValue", ctypes.c_ulong),
        ("ReservedAsUlong", ctypes.c_ulong),
        ("DataBufferOffset", ctypes.c_void_p),
        ("PreviousTaskFile", ctypes.c_ubyte * 8),
        ("CurrentTaskFile", ctypes.c_ubyte * 8),
    ]

class ATA_PASS_THROUGH_EX_WITH_BUFFER(ctypes.Structure):
    _fields_ = [
        ("apt", ATA_PASS_THROUGH_EX),
        ("Data", ctypes.c_ubyte * 512),
    ]

ATA_FLAGS_DRDY_REQUIRED = 1
ATA_FLAGS_DATA_IN = 2
ATA_FLAGS_DATA_OUT = 4
ATA_FLAGS_48BIT_COMMAND = 8
ATA_FLAGS_USE_DMA = 16
ATA_FLAGS_NO_MULTIPLE = 32

SCSI_IOCTL_DATA_OUT = 0
SCSI_IOCTL_DATA_IN = 1
SCSI_IOCTL_DATA_UNSPECIFIED = 2

IOCTL_STORAGE_EJECT_MEDIA = 0x2D4808
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x2D1080
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020

class STORAGE_DEVICE_NUMBER(ctypes.Structure):
    _fields_ = [
        ("DeviceType", wintypes.DWORD),
        ("DeviceNumber", wintypes.DWORD),
        ("PartitionNumber", wintypes.DWORD),
    ]

class Win32API:
    @staticmethod
    def open_volume(volume_path):
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p

        normalized_path = Win32API.normalize_volume_device_path(volume_path) or volume_path
        handle = kernel32.CreateFileW(
            normalized_path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None
        )

        if handle == 0 or handle == 0xFFFFFFFFFFFFFFFF or handle == -1:
            logging.warning(f"打开卷句柄失败: {normalized_path}, error={ctypes.get_last_error()}")
            return None
        return handle

    @staticmethod
    def get_device_number(handle):
        sdn = STORAGE_DEVICE_NUMBER()
        result, bytes_returned, error = Win32API.device_io_control(
            handle,
            IOCTL_STORAGE_GET_DEVICE_NUMBER,
            None, 0,
            ctypes.byref(sdn), ctypes.sizeof(sdn)
        )
        if result:
            return sdn.DeviceNumber
        return None

    @staticmethod
    def lock_volume(handle):
        result, _, error = Win32API.device_io_control(handle, FSCTL_LOCK_VOLUME, None, 0, None, 0)
        if not result:
            logging.warning(f"Lock volume failed. Error: {error}")
        return bool(result)

    @staticmethod
    def dismount_volume(handle):
        result, _, error = Win32API.device_io_control(handle, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0)
        if not result:
            logging.warning(f"Dismount volume failed. Error: {error}")
        return bool(result)

    @staticmethod
    def eject_media(handle):
        result, _, _ = Win32API.device_io_control(handle, IOCTL_STORAGE_EJECT_MEDIA, None, 0, None, 0)
        return bool(result)

    @staticmethod
    def normalize_volume_device_path(volume_name):
        """将 'E:' 之类的盘符统一为卷设备路径 '\\\\.\\E:'。"""
        volume_root = Win32API.normalize_volume_root(volume_name)
        if not volume_root:
            return None
        return f"\\\\.\\{volume_root[0]}:"

    @staticmethod
    def normalize_volume_root(volume_name):
        """将 'E:' 之类的盘符统一为 Shell 可接受的根路径 'E:\\'。"""
        if not volume_name:
            return None

        volume_name = volume_name.strip().rstrip("\\/")
        if re.fullmatch(r"[A-Za-z]:", volume_name):
            return f"{volume_name}\\"
        if re.fullmatch(r"[A-Za-z]:[\\/]", volume_name):
            return f"{volume_name[0]}:\\"
        return None

    @staticmethod
    def is_volume_present(volume_name):
        """检查盘符当前是否仍由系统挂载。"""
        volume_root = Win32API.normalize_volume_root(volume_name)
        if not volume_root:
            return False

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        drive_type = kernel32.GetDriveTypeW(volume_root)
        return drive_type != 1  # DRIVE_NO_ROOT_DIR

    @staticmethod
    def flush_volume_buffers(handle):
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        result = kernel32.FlushFileBuffers(handle)
        return bool(result), ctypes.get_last_error()

    @staticmethod
    def query_occupying_apps(volume_root, timeout_seconds=3.0):
        """用 Restart Manager 查询占用卷的程序（带超时保护）。

        设备未就绪时（例如硬盘已停转）RmGetList 可能阻塞几十秒，
        不设上限会把整个弹出流程拖死，所以超时就直接放弃查询。
        """
        result = {"apps": []}

        def _run():
            try:
                from src.utils.restart_manager import get_locking_processes
                result["apps"] = get_locking_processes(volume_root)
            except Exception as e:
                logging.debug(f"获取占用程序失败: {e}")
                result["apps"] = []

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout_seconds)
        if worker.is_alive():
            logging.warning(f"查询占用程序超时（>{timeout_seconds}s），跳过: {volume_root}")
            return []
        return result.get("apps") or []

    @staticmethod
    def prepare_volume_for_safe_removal(volume_name, lock_timeout_seconds=1.5,
                                        retry_interval=0.2, query_occupiers=True):
        """
        对卷执行短超时的 lock + dismount。
        设备节点移除前由上层决定何时释放卷句柄，避免卷对象长期被当前进程持有。
        """
        volume_root = Win32API.normalize_volume_root(volume_name)
        volume_path = Win32API.normalize_volume_device_path(volume_name)
        if not volume_root or not volume_path:
            return False, f"无效盘符: {volume_name}", None
        volume_label = volume_root.rstrip("\\")

        handle = Win32API.open_volume(volume_name)
        if not handle:
            return False, f"无法打开卷句柄: {volume_label}", None

        try:
            flush_ok, flush_error = Win32API.flush_volume_buffers(handle)
            if not flush_ok:
                logging.debug(f"卷 {volume_root} FlushFileBuffers 未成功，error={flush_error}")

            deadline = time.perf_counter() + lock_timeout_seconds
            last_error = 0
            while time.perf_counter() < deadline:
                result, _, last_error = Win32API.device_io_control(handle, FSCTL_LOCK_VOLUME, None, 0, None, 0)
                if result:
                    logging.info(f"卷 {volume_root} 已锁定")
                    break
                time.sleep(retry_interval)
            else:
                # ERROR_ACCESS_DENIED(5)：ReFS/exFAT 等文件系统不支持 FSCTL_LOCK_VOLUME
                # （实测 ReFS 卷锁定恒返回 5，而 DISMOUNT 可成功、Restart Manager 也
                # 查不到占用者）。按 Windows 托盘弹出的做法：跳过锁定，直接卸载卷。
                if last_error == 5:
                    result, _, dismount_error = Win32API.device_io_control(
                        handle, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0
                    )
                    if not result:
                        return False, (
                            f"卷不支持锁定且卸载失败 (error={dismount_error})"
                        ), handle
                    logging.info(f"卷 {volume_root} 不支持锁定（ReFS/exFAT），已直接卸载")
                    return True, "卷不支持锁定（ReFS/exFAT），已直接卸载", handle

                # ERROR_NOT_READY(21)/ERROR_IO_DEVICE(1117)/ERROR_DEV_NOT_EXIST(55)
                # 说明设备未就绪（盘已停转或掉线），不是被程序占用：
                # 此时查 Restart Manager 没意义而且很慢，直接跳过。
                unready = last_error in (21, 1117, 55)
                occupying_apps = (
                    [] if unready or not query_occupiers
                    else Win32API.query_occupying_apps(volume_root)
                )

                if unready:
                    msg = (f"卷未就绪，无法锁定 (error={last_error})："
                           "硬盘可能已停转/未就绪，请稍后重试")
                else:
                    msg = f"卷仍被占用，无法锁定 (error={last_error})"
                    if occupying_apps:
                        msg += f"，可能被以下程序占用: {', '.join(occupying_apps)}"

                return False, msg, handle

            result, _, dismount_error = Win32API.device_io_control(handle, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0)
            if not result:
                return False, f"卷锁定成功，但卸载失败 (error={dismount_error})", handle

            logging.info(f"卷 {volume_root} 已锁定并卸载")
            return True, "卷已锁定并卸载", handle
        except Exception as e:
            return False, f"卷预处理异常: {e}", handle

    @staticmethod
    def prepare_volumes_for_safe_removal(volumes, lock_timeout_seconds=1.5,
                                         query_occupiers=True):
        prepared = []
        failures = []
        seen = set()

        for volume in volumes or []:
            volume_root = Win32API.normalize_volume_root(volume)
            if not volume_root or volume_root in seen:
                continue
            seen.add(volume_root)

            success, message, handle = Win32API.prepare_volume_for_safe_removal(
                volume,
                lock_timeout_seconds=lock_timeout_seconds,
                query_occupiers=query_occupiers,
            )
            if success and handle:
                prepared.append({
                    "volume": volume_root.rstrip("\\"),
                    "handle": handle,
                })
            else:
                failures.append({
                    "volume": volume_root.rstrip("\\"),
                    "message": message,
                })
                if handle:
                    Win32API.close_handle(handle)

        return prepared, failures

    @staticmethod
    def release_prepared_volumes(prepared_volumes):
        for item in prepared_volumes or []:
            handle = item.get("handle")
            volume = item.get("volume", "<unknown>")
            try:
                if handle:
                    Win32API.close_handle(handle)
                    logging.info(f"已释放卷句柄: {volume}")
            except Exception as e:
                logging.warning(f"释放卷句柄失败 {volume}: {e}")

    @staticmethod
    def eject_volume_by_drive_letter(volume_name, expected_volumes=None, timeout_seconds=12.0, poll_interval=0.25):
        """
        通过 Shell 的 Eject 动作触发和资源管理器一致的原生弹出流程。
        成功发起后，轮询关联盘符是否全部消失，用于确认系统已完成卸载。
        """
        volume_root = Win32API.normalize_volume_root(volume_name)
        if not volume_root:
            return False, f"无效盘符: {volume_name}"

        expected = []
        for item in expected_volumes or [volume_name]:
            normalized = Win32API.normalize_volume_root(item)
            if normalized and normalized not in expected:
                expected.append(normalized)
        if not expected:
            expected = [volume_root]

        shell32 = ctypes.WinDLL('shell32', use_last_error=True)
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        shell32.ShellExecuteW.argtypes = [
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_int,
        ]

        logging.info(
            f"正在调用 Windows Shell 原生弹出: target={volume_root}, expected={', '.join(expected)}"
        )
        result = shell32.ShellExecuteW(None, "Eject", volume_root, None, None, 0)
        result_code = ctypes.cast(result, ctypes.c_void_p).value or 0
        if result_code <= 32:
            last_error = ctypes.get_last_error()
            logging.warning(
                f"Shell 原生弹出调用失败: target={volume_root}, return={result_code}, last_error={last_error}"
            )
            return False, f"Windows Shell 原生弹出调用失败: {result_code}"

        deadline = time.perf_counter() + timeout_seconds
        while time.perf_counter() < deadline:
            remaining = [item for item in expected if Win32API.is_volume_present(item)]
            if not remaining:
                logging.info(f"Windows Shell 原生弹出完成，相关盘符已卸载: {', '.join(expected)}")
                return True, "设备已按 Windows 原生方式安全弹出"
            time.sleep(poll_interval)

        remaining = [item for item in expected if Win32API.is_volume_present(item)]
        remaining_display = ", ".join(item.rstrip("\\") for item in remaining) if remaining else ", ".join(expected)
        logging.warning(f"Windows Shell 原生弹出超时，仍在线的盘符: {remaining_display}")
        return False, f"Windows 原生弹出未完成，卷仍处于挂载状态: {remaining_display}"

    @staticmethod
    def rescan_hardware():
        """
        强制系统重新扫描硬件总线，并执行深度恢复流程。
        这是目前最强力的恢复逻辑，结合了全局重新枚举、控制器重置和异常设备修复。
        """
        try:
            logging.info("开始执行深度硬件扫描与恢复流程...")
            cfgmgr32 = ctypes.WinDLL('cfgmgr32')
            dev_inst = wintypes.DWORD()
            
            # 1. 全局重新枚举（根节点）
            res = cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(dev_inst), None, 0)
            if res == 0:
                # CM_REENUMERATE_RETRY_INSTALLATION | CM_REENUMERATE_SYNCHRONOUS
                cfgmgr32.CM_Reenumerate_DevNode(dev_inst, 0x00000001 | 0x00000002)
                logging.info("已发送全局同步硬件重新枚举指令")

            # 2. 调用 pnputil 触发系统级扫描
            try:
                subprocess.run('pnputil /scan-devices', shell=True, capture_output=True, timeout=10)
                logging.info("已通过 pnputil 触发系统扫描")
            except Exception as e:
                logging.warning(f"pnputil 扫描失败: {e}")

            # 3. 执行强力恢复逻辑 (重启父设备等)
            # 这一步是之前成功的核心：它会找到所有 Problem != 0 的磁盘并尝试重启其父节点
            recovery_success = Win32API.force_recover_problem_devices()
            
            # 4. 针对性地处理“准备安全删除”状态的设备
            # 即使不是 -Present 的设备也能通过这个树遍历找到
            safe_remove_success = Win32API.restart_safe_removed_devices()
            
            # 5. 暴力重置所有 USB 和 SCSI 控制器（可选，但对于硬盘柜失踪非常有效）
            # 我们只针对有问题的控制器
            ps_ctrl_cmd = """
            $controllers = Get-PnpDevice -Present -ErrorAction SilentlyContinue | Where-Object { 
                $_.Class -eq "USB" -or $_.Class -eq "SCSIAdapter" -or $_.Service -eq "UASPStor"
            }
            foreach ($ctrl in $controllers) {
                if ($ctrl.Status -ne "OK" -or $ctrl.Problem -ne 0) {
                    Write-Output "重置有问题的控制器: $($ctrl.FriendlyName)"
                    pnputil /restart-device $ctrl.InstanceId /quiet
                }
            }
            """
            subprocess.run(["powershell", "-Command", ps_ctrl_cmd], capture_output=True, text=True, shell=True)

            logging.info(f"深度扫描完成。恢复状态: 强力恢复={recovery_success}, 安全删除修复={safe_remove_success}")
            return True
        except Exception as e:
            logging.error(f"执行硬件扫描时发生异常: {e}")
            return False

    @staticmethod
    def restart_safe_removed_devices():
        """
        查找并重启处于“准备安全删除”状态（代码 47）的设备。
        这用于在未物理拔出的情况下重新挂载已弹出的硬盘。
        """
        try:
            cfgmgr32 = ctypes.WinDLL('cfgmgr32')
            
            # Constants
            CR_SUCCESS = 0
            # 我们关心的异常状态
            CM_PROB_HELD_FOR_EJECT = 47 # 代码 47
            CM_PROB_PHANTOM = 45       # 代码 45
            CM_PROB_DISABLED = 22      # 代码 22
            
            found_count = 0
            
            # Helper to traverse tree
            def traverse(dev_inst):
                nonlocal found_count
                
                # Check status
                status = wintypes.DWORD()
                problem = wintypes.DWORD()
                
                res = cfgmgr32.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem), dev_inst, 0)
                
                # 获取设备 ID 用于判断类型
                id_buffer = (ctypes.c_wchar * 260)()
                cfgmgr32.CM_Get_Device_IDW(dev_inst, id_buffer, 260, 0)
                node_id = id_buffer.value.upper()

                # 如果是代码 47，或者 (是磁盘相关设备且有其他错误代码，排除 45 PHANTOM)
                is_target_error = (problem.value == CM_PROB_HELD_FOR_EJECT) or \
                                 (problem.value == CM_PROB_DISABLED and \
                                  any(k in node_id for k in ["USBSTOR", "SCSI", "DISK"]))

                # 优化：只关注可能与外置硬盘柜相关的设备，提高准确性和安全性
                is_target_error = is_target_error and any(k in node_id for k in ["USB", "SCSI", "VEN_174C", "ASMT", "VEN_152D", "VEN_0BDA", "VEN_14CD", "STORMFOR"])

                if res == CR_SUCCESS and is_target_error:
                    logging.info(f"发现异常状态设备: {node_id} (错误码: {problem.value})，尝试重启...")
                    
                    # Restart: Disable -> Enable
                    disable_res = cfgmgr32.CM_Disable_DevNode(dev_inst, 0)
                    if disable_res == CR_SUCCESS:
                        enable_res = cfgmgr32.CM_Enable_DevNode(dev_inst, 0)
                        if enable_res == CR_SUCCESS:
                            logging.info(f"设备 {node_id} 重启成功")
                            found_count += 1
                        else:
                            logging.error(f"设备 {node_id} 启用失败: {enable_res}")
                    else:
                        logging.error(f"设备 {node_id} 禁用失败: {disable_res}")
                
                # Traverse children
                child = wintypes.DWORD()
                if cfgmgr32.CM_Get_Child(ctypes.byref(child), dev_inst, 0) == CR_SUCCESS:
                    traverse(child)
                    
                    # Traverse siblings
                    sibling = wintypes.DWORD()
                    curr = child
                    while cfgmgr32.CM_Get_Sibling(ctypes.byref(sibling), curr, 0) == CR_SUCCESS:
                        traverse(sibling)
                        curr = sibling

            # Start from root
            root_inst = wintypes.DWORD()
            if cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(root_inst), None, 0) == CR_SUCCESS:
                # Root itself usually doesn't have siblings, but traversal logic handles children
                traverse(root_inst)
                
            if found_count > 0:
                logging.info(f"共重启了 {found_count} 个设备")
                return True
            else:
                logging.info("未发现处于‘准备安全删除’状态的设备")
                return False
                
        except Exception as e:
            logging.error(f"重启设备时发生异常: {e}")
            return False

    @staticmethod
    def is_external_device(instance_id):
        """
        通过向上遍历 PnP 设备树，判断设备是否连接在 USB 控制器或 ASMedia 控制器上。
        这是区分内外置硬盘最可靠的方法。
        """
        try:
            cfgmgr32 = ctypes.WinDLL('cfgmgr32')
            CR_SUCCESS = 0
            
            dev_inst = wintypes.DWORD()
            res = cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(dev_inst), instance_id, 0)
            if res != CR_SUCCESS:
                return False
            
            curr = dev_inst
            parent = wintypes.DWORD()
            
            # 向上追溯最多 10 层
            for _ in range(10):
                res = cfgmgr32.CM_Get_Parent(ctypes.byref(parent), curr, 0)
                if res != CR_SUCCESS:
                    break
                
                # 获取父设备的实例 ID
                id_buffer = (ctypes.c_wchar * 260)()
                cfgmgr32.CM_Get_Device_IDW(parent, id_buffer, 260, 0)
                parent_id = id_buffer.value.upper()
                
                # 典型的外置/移动总线判定
                # USB\: 标准 USB 设备
                # USBSTOR: USB 存储驱动
                # UASPSTOR: UASP 存储驱动
                if any(k in parent_id for k in ["USB\\", "USBSTOR", "UASPSTOR"]):
                    return True
                
                # 特别针对 ASMedia, JMicron, Realtek 等硬盘柜控制器
                # 即使它在某些驱动下显示为 SCSI，其父节点通常仍包含这些特征
                if any(k in parent_id for k in ["VEN_174C", "ASMT", "VEN_152D", "VEN_0BDA", "VEN_14CD"]):
                    return True
                    
                curr = parent
                
            return False
        except Exception as e:
            logging.error(f"判断设备是否外置时发生异常: {e}")
            return False

    @staticmethod
    def get_volume_disk_mapping():
        """
        用 Win32 API 枚举所有卷，返回 {盘符(单字母, 大写): 物理磁盘序号} 映射。
        完全不依赖 WMI/COM，可在任意线程调用，避免 UI 线程 COM 输入同步错误
        (RPC_E_CANTCALLOUT_ININPUTSYNC)。
        """
        mapping = {}
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

        kernel32.FindFirstVolumeW.restype = ctypes.c_void_p
        kernel32.FindFirstVolumeW.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
        kernel32.FindNextVolumeW.restype = wintypes.BOOL
        kernel32.FindNextVolumeW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
        kernel32.FindVolumeClose.restype = wintypes.BOOL
        kernel32.FindVolumeClose.argtypes = [ctypes.c_void_p]

        kernel32.GetVolumePathNamesForVolumeNameW.restype = wintypes.BOOL
        kernel32.GetVolumePathNamesForVolumeNameW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
        ]

        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000

        class DISK_EXTENT(ctypes.Structure):
            _fields_ = [
                ("DiskNumber", wintypes.DWORD),
                ("StartingOffset", ctypes.c_longlong),
                ("ExtentLength", ctypes.c_longlong),
            ]

        class VOLUME_DISK_EXTENTS(ctypes.Structure):
            _fields_ = [
                ("NumberOfDiskExtents", wintypes.DWORD),
                ("Extents", DISK_EXTENT * 8),
            ]

        INVALID = ctypes.c_void_p(INVALID_HANDLE_VALUE).value

        vol_buf = ctypes.create_unicode_buffer(512)
        search_handle = kernel32.FindFirstVolumeW(vol_buf, 512)
        if search_handle == 0 or search_handle == INVALID:
            return mapping

        try:
            while True:
                volume_guid = vol_buf.value
                if volume_guid:
                    # 获取该卷挂载的盘符（多字符串，取第一个，如 "C:\\"）
                    path_buf = ctypes.create_unicode_buffer(512)
                    needed = wintypes.DWORD(0)
                    ok = kernel32.GetVolumePathNamesForVolumeNameW(
                        volume_guid, path_buf, 512, ctypes.byref(needed)
                    )
                    drive_letter = ""
                    if ok and path_buf.value:
                        first = path_buf.value
                        letter = first.strip().rstrip("\\:").upper()
                        if len(letter) == 1 and letter.isalpha():
                            drive_letter = letter

                    if drive_letter:
                        vol_path = f"\\\\.\\{drive_letter}:"
                        h = kernel32.CreateFileW(
                            vol_path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE,
                            None, OPEN_EXISTING, 0, None
                        )
                        if h and h != INVALID:
                            try:
                                extents = VOLUME_DISK_EXTENTS()
                                result, _, _ = Win32API.device_io_control(
                                    h, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS,
                                    None, 0, ctypes.byref(extents), ctypes.sizeof(extents)
                                )
                                if result and extents.NumberOfDiskExtents >= 1:
                                    mapping[drive_letter] = extents.Extents[0].DiskNumber
                            finally:
                                kernel32.CloseHandle(h)

                if not kernel32.FindNextVolumeW(search_handle, vol_buf, 512):
                    break
        finally:
            kernel32.FindVolumeClose(search_handle)

        return mapping

    @staticmethod
    def get_disk_number_from_device_path(device_path):
        """从磁盘/卷设备接口路径 (GUID_DEVINTERFACE_DISK/VOLUME 的 dbcc_name) 打开设备，
        用 IOCTL_STORAGE_GET_DEVICE_NUMBER 获取物理磁盘序号。用于 DBT_DEVICEQUERYREMOVE
        时把 DEV_BROADCAST_DEVICEINTERFACE.dbcc_name 映射到 PhysicalDriveN。"""
        if not device_path:
            return None

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        INVALID = ctypes.c_void_p(-1).value
        h = kernel32.CreateFileW(
            device_path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING, 0, None
        )
        if not h or h == INVALID:
            return None

        try:
            sdn = STORAGE_DEVICE_NUMBER()
            result, _, _ = Win32API.device_io_control(
                h, IOCTL_STORAGE_GET_DEVICE_NUMBER,
                None, 0, ctypes.byref(sdn), ctypes.sizeof(sdn)
            )
            if result:
                return int(sdn.DeviceNumber)
            return None
        finally:
            kernel32.CloseHandle(h)

    @staticmethod
    def disable_content_indexing(drive_letter):
        """为卷根目录设置 FILE_ATTRIBUTE_NOT_CONTENT_INDEXED (0x2000)，
        阻止 Windows Search 索引该卷，避免 svchost.exe(WSearch) 占用外置硬盘
        导致“设备正在使用中”而无法安全弹出。"""
        volume_root = Win32API.normalize_volume_root(drive_letter)
        if not volume_root:
            return False

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.GetFileAttributesW.restype = wintypes.DWORD
        kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
        kernel32.SetFileAttributesW.restype = wintypes.BOOL
        kernel32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]

        attrs = kernel32.GetFileAttributesW(volume_root)
        if attrs == 0xFFFFFFFF:  # INVALID_FILE_ATTRIBUTES
            return False

        new_attrs = attrs | 0x2000  # FILE_ATTRIBUTE_NOT_CONTENT_INDEXED
        if new_attrs == attrs:
            return True
        return bool(kernel32.SetFileAttributesW(volume_root, new_attrs))

    @staticmethod
    def get_windows_disk_idle_settings():
        """
        读取当前电源计划中“在此时间后关闭硬盘”的 AC/DC 设置。
        返回:
            {
                "ac_seconds": int | None,
                "dc_seconds": int | None,
                "is_never_sleep_ac": bool,
                "is_never_sleep_dc": bool,
            }
        """
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            result = subprocess.run(
                ["powercfg", "/query", "scheme_current", "SUB_DISK", "DISKIDLE"],
                capture_output=True,
                text=True,
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW
            )

            if result.returncode != 0:
                logging.warning(f"读取 Windows 硬盘休眠设置失败: {result.stderr}")
                return None

            ac_match = re.search(r"当前交流电源设置索引:\s*0x([0-9a-fA-F]+)", result.stdout)
            dc_match = re.search(r"当前直流电源设置索引:\s*0x([0-9a-fA-F]+)", result.stdout)

            if not ac_match and not dc_match:
                logging.warning("无法从 powercfg 输出中解析硬盘休眠设置")
                return None

            ac_seconds = int(ac_match.group(1), 16) if ac_match else None
            dc_seconds = int(dc_match.group(1), 16) if dc_match else None

            return {
                "ac_seconds": ac_seconds,
                "dc_seconds": dc_seconds,
                "is_never_sleep_ac": ac_seconds == 0 if ac_seconds is not None else False,
                "is_never_sleep_dc": dc_seconds == 0 if dc_seconds is not None else False,
            }
        except Exception as e:
            logging.warning(f"检测 Windows 硬盘休眠设置异常: {e}")
            return None

    @staticmethod
    def get_device_instance_path(disk_index):
        """通过 WMI 获取物理磁盘的设备实例路径"""
        import wmi
        try:
            c = wmi.WMI()
            for drive in c.Win32_DiskDrive(Index=disk_index):
                return drive.PNPDeviceID
        except Exception:
            return None
        return None

    @staticmethod
    def is_disk_usb_attached(disk_index):
        """WMI-free 判定磁盘是否 USB 连接（BusType=USB，含 USBSTOR/UASP 桥接）。

        注意：部分存储栈（含本机 ASMT/UASP）对 StorageDeviceProperty 查询
        返回 BusType=0(Unknown)，此判定不可靠。保留供兼容环境使用，
        句柄通知注册的兜底过滤不依赖本函数。
        """
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        INVALID = ctypes.c_void_p(-1).value
        h = kernel32.CreateFileW(
            f"\\\\.\\PhysicalDrive{disk_index}",
            0,  # 0 访问权限：只查适配器属性，无需读写权限
            FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None
        )
        if not h or h == INVALID:
            return False
        try:
            query = STORAGE_PROPERTY_QUERY()
            query.PropertyId = 1  # StorageDeviceProperty（StorageAdapterProperty 在 Win8+ 已失效，实测 error=1）
            query.QueryType = 0   # PropertyStandardQuery
            desc = STORAGE_DEVICE_DESCRIPTOR()
            result, bytes_returned, error = Win32API.device_io_control(
                h, IOCTL_STORAGE_QUERY_PROPERTY,
                ctypes.byref(query), ctypes.sizeof(query),
                ctypes.byref(desc), ctypes.sizeof(desc)
            )
            return bool(result) and int(desc.BusType) == BusTypeUsb
        finally:
            kernel32.CloseHandle(h)

    @staticmethod
    def get_usbstor_parent_device_id(disk_index):
        """获取磁盘对应的 USBSTOR 父设备实例 ID，用于 pnputil 重启"""
        import subprocess as _sp
        try:
            r = _sp.run(
                f'wmic path Win32_DiskDrive where Index={disk_index} get PNPDeviceID /value',
                capture_output=True, text=True, shell=True, timeout=10
            )
            for line in r.stdout.split('\n'):
                if 'PNPDeviceID=' in line:
                    pid = line.split('=')[1].strip()
                    # Walk up the device tree
                    parts = pid.rsplit('\\', 1)
                    parent_id = parts[0] if len(parts) == 2 else pid
                    logging.info(f"USBSTOR parent for disk {disk_index}: {parent_id}")
                    return parent_id
        except Exception as e:
            logging.warning(f"获取 USBSTOR 父设备失败: {e}")
        return None

    @staticmethod
    def _get_devnode_id(dev_inst):
        """获取 DevNode 的实例 ID 字符串"""
        try:
            cfgmgr32 = ctypes.WinDLL('cfgmgr32')
            id_buffer = (ctypes.c_wchar * 260)()
            res = cfgmgr32.CM_Get_Device_IDW(dev_inst, id_buffer, 260, 0)
            if res == 0:
                return id_buffer.value
        except Exception as e:
            logging.debug(f"获取设备实例 ID 失败: {e}")
        return None

    @staticmethod
    def _is_critical_service(service_name):
        """避免对 USB Hub、控制器或系统总线发起弹出"""
        if not service_name:
            return False

        critical_services = [
            "USBHUB3", "USBHUB", "IUSB3HUB", "ASMTXHCI", "XHCI", "EHCI", "UHCI",
            "PCI", "ACPI", "STORAHCI", "IASTORA", "NVME", "ROOT", "VOLMGR", "PARTMGR"
        ]
        service_upper = service_name.upper()
        return any(keyword in service_upper for keyword in critical_services)

    @staticmethod
    def _score_eject_candidate(node_id, service_name):
        """为更接近 Windows 原生可移除设备节点的候选项打分"""
        if not node_id:
            return -1

        node_upper = node_id.upper()
        service_upper = service_name.upper() if service_name else ""

        if Win32API._is_critical_service(service_name):
            return -1

        score = 0

        # Windows 在独立 USB-SATA 桥接上通常会暴露这些节点类型。
        if node_upper.startswith("USB\\"):
            score = max(score, 100)
        if "UASPSTOR" in node_upper:
            score = max(score, 95)
        if "USBSTOR" in node_upper:
            score = max(score, 90)
        if any(keyword in node_upper for keyword in ["VEN_174C", "ASMT", "VEN_152D", "JMS", "VEN_0BDA", "VEN_14CD"]):
            score = max(score, 85)
        if service_upper in ["UASPSTOR", "USBSTOR"]:
            score = max(score, 88)

        return score

    @staticmethod
    def find_native_eject_target(instance_id, max_depth=8):
        """
        查找最接近 Windows 原生“安全删除硬件”行为的目标节点。
        返回值:
            {
                "dev_inst": DWORD,
                "instance_id": str,
                "service": str | None,
                "score": int,
                "chain": list[str]
            } | None
        """
        try:
            cfgmgr32 = ctypes.WinDLL('cfgmgr32')

            dev_inst = wintypes.DWORD()
            res = cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(dev_inst), instance_id, 0)
            if res != 0:
                logging.error(f"CM_Locate_DevNodeW 失败，无法定位原生弹出目标: {res}")
                return None

            current_inst = dev_inst
            best_candidate = None
            traversed_chain = []

            for depth in range(max_depth):
                node_id = Win32API._get_devnode_id(current_inst)
                service_name = Win32API._get_devnode_property(current_inst, 0x00000005)  # CM_DRP_SERVICE
                traversed_chain.append(f"{depth}:{node_id or '<unknown>'}|{service_name or 'None'}")

                score = Win32API._score_eject_candidate(node_id, service_name)
                if score >= 0:
                    candidate = {
                        "dev_inst": wintypes.DWORD(current_inst.value),
                        "instance_id": node_id,
                        "service": service_name,
                        "score": score,
                    }
                    if not best_candidate or score > best_candidate["score"]:
                        best_candidate = candidate

                parent_inst = wintypes.DWORD()
                res = cfgmgr32.CM_Get_Parent(ctypes.byref(parent_inst), current_inst, 0)
                if res != 0:
                    break

                parent_service = Win32API._get_devnode_property(parent_inst, 0x00000005)
                if Win32API._is_critical_service(parent_service):
                    parent_id = Win32API._get_devnode_id(parent_inst)
                    traversed_chain.append(f"{depth + 1}:{parent_id or '<unknown>'}|{parent_service or 'None'}")
                    break

                current_inst = parent_inst

            if best_candidate:
                best_candidate["chain"] = traversed_chain
                logging.info(
                    f"已定位原生弹出目标: {best_candidate['instance_id']} "
                    f"(Service: {best_candidate['service']}, Score: {best_candidate['score']})"
                )
                logging.info(f"设备父链: {' -> '.join(traversed_chain)}")
                return best_candidate

            logging.warning(f"未在父链中找到合适的原生弹出目标: {' -> '.join(traversed_chain)}")
            return None
        except Exception as e:
            logging.error(f"查找原生弹出目标时发生异常: {e}")
            return None

    @staticmethod
    def _locate_devnode(instance_id):
        try:
            cfgmgr32 = ctypes.WinDLL('cfgmgr32')
            dev_inst = wintypes.DWORD()
            res = cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(dev_inst), instance_id, 0)
            if res == 0:
                return dev_inst
            logging.warning(f"CM_Locate_DevNodeW 失败，无法定位设备节点 {instance_id}: {res}")
        except Exception as e:
            logging.error(f"定位设备节点失败 {instance_id}: {e}")
        return None

    @staticmethod
    def _call_request_device_eject(cfgmgr32, dev_inst, display_name):
        veto_type = ctypes.c_int(0)
        veto_name = (ctypes.c_wchar * 260)()

        logging.info(f"正在请求弹出节点: {display_name}")
        started_at = time.perf_counter()
        res = cfgmgr32.CM_Request_Device_EjectW(
            dev_inst,
            ctypes.byref(veto_type),
            veto_name,
            260,
            0
        )
        elapsed = time.perf_counter() - started_at
        return res, veto_type.value, veto_name.value, elapsed

    @staticmethod
    def _call_query_and_remove_subtree(cfgmgr32, dev_inst, display_name):
        veto_type = ctypes.c_int(0)
        veto_name = (ctypes.c_wchar * 260)()

        logging.info(f"正在请求移除设备子树: {display_name}")
        started_at = time.perf_counter()
        res = cfgmgr32.CM_Query_And_Remove_SubTreeW(
            dev_inst,
            ctypes.byref(veto_type),
            veto_name,
            260,
            CM_REMOVE_UI_NOT_OK
        )
        elapsed = time.perf_counter() - started_at
        return res, veto_type.value, veto_name.value, elapsed

    @staticmethod
    def eject_device_by_instance_id(instance_id, timeout_seconds=25.0):
        """带超时保护的设备弹出。

        CM_Request_Device_EjectW / CM_Query_And_Remove_SubTreeW 会同步等待所有
        持有句柄的程序响应；遇到顽固占用或正在停转的盘时可能长时间不返回。
        这里放到独立线程执行并限时，超时立刻返回错误，
        避免后台线程和界面永久卡在“正在弹出...”。
        """
        result = {"value": (False, "弹出未执行")}

        def _run():
            try:
                result["value"] = Win32API._eject_device_impl(instance_id)
            except Exception as e:
                logging.error(f"弹出线程执行异常: {e}")
                result["value"] = (False, f"弹出异常: {e}")

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout_seconds)
        if worker.is_alive():
            logging.error(f"设备移除请求超过 {timeout_seconds:.0f} 秒未返回: {instance_id}")
            return False, (
                f"系统移除设备的请求超过 {timeout_seconds:.0f} 秒仍未响应。\n"
                "常见原因：仍有程序占用该磁盘，或硬盘正在停转无法完成 I/O。\n"
                "请关闭占用该磁盘的程序/窗口后重试。"
            )
        return result["value"]

    @staticmethod
    def _eject_device_impl(instance_id):
        """
        弹出设备，模拟“安全删除硬件”。
        对于基于 UASP/USB 桥接芯片的硬盘柜，需要找到最合适的父节点。
        """
        try:
            cfgmgr32 = ctypes.WinDLL('cfgmgr32')
            
            # 1. 查找最适合弹出的节点
            target_info = Win32API.find_native_eject_target(instance_id)
            
            if target_info:
                target_id = target_info['instance_id']
                dev_inst = target_info['dev_inst']
                logging.info(f"找到最佳弹出节点: {target_id} (得分: {target_info['score']}, 服务: {target_info.get('service')})")
                
                # 使用 CM_Request_Device_EjectW 弹出
                res, veto_type, veto_name, elapsed = Win32API._call_request_device_eject(
                    cfgmgr32, dev_inst, target_id
                )
                
                if res == 0:
                    logging.info(f"原生设备弹出成功，耗时 {elapsed:.2f} 秒")
                    return True, "设备已安全移除"
                else:
                    reason = Win32API._get_veto_reason_str(veto_type, veto_name)
                    logging.warning(f"原生设备弹出失败，耗时 {elapsed:.2f} 秒: {reason}")
                    # 不在此处直接 return False，而是继续尝试向下回退到移除原始磁盘节点。
                    # 因为对于多盘位硬盘盒，如果其他盘被占用，父节点 (USB) 的弹出会被否决，
                    # 但目标子节点 (SCSI\\DISK) 仍然可以被单独卸载。
            else:
                logging.warning("未找到最佳弹出节点，将尝试直接移除原始磁盘节点")

            # 2. 如果原生弹出失败或找不到，退回到 CM_Query_And_Remove_SubTreeW 移除磁盘节点本身
            original_dev_inst = Win32API._locate_devnode(instance_id)
            if original_dev_inst:
                res, veto_type, veto_name, elapsed = Win32API._call_query_and_remove_subtree(
                    cfgmgr32, original_dev_inst, instance_id
                )
                
                if res == 0:
                    logging.info(f"原始磁盘节点移除成功，耗时 {elapsed:.2f} 秒")
                    return True, "设备已安全移除（节点卸载）"
                    
                reason = Win32API._get_veto_reason_str(veto_type, veto_name)
                logging.warning(f"原始磁盘节点移除被否决，耗时 {elapsed:.2f} 秒: {reason}")
                return False, reason

            return False, "无法定位原始磁盘节点"
        except Exception as e:
            logging.error(f"弹出过程发生异常: {e}")
            return False, str(e)

    @staticmethod
    def _get_devnode_property(dev_inst, property_id):
        """Helper to get DevNode registry property"""
        try:
            cfgmgr32 = ctypes.WinDLL('cfgmgr32')
            buffer_size = wintypes.DWORD(0)
            
            # First call to get required size
            res = cfgmgr32.CM_Get_DevNode_Registry_PropertyW(
                dev_inst, property_id, None, None, ctypes.byref(buffer_size), 0
            )
            
            # CR_BUFFER_SMALL is 0x1A. If it's not success or small buffer, return None.
            if res != 0 and res != 0x1A:
                return None
            
            if buffer_size.value == 0:
                # If size not returned, try a default large buffer
                buffer_size = wintypes.DWORD(2048)

            buffer = (ctypes.c_byte * buffer_size.value)()
            res = cfgmgr32.CM_Get_DevNode_Registry_PropertyW(
                dev_inst, property_id, None, ctypes.byref(buffer), ctypes.byref(buffer_size), 0
            )
            
            if res == 0:
                # Interpret as wide string (W version of API)
                return ctypes.cast(buffer, ctypes.c_wchar_p).value
            return None
        except Exception as e:
            logging.debug(f"Error getting devnode property: {e}")
            return None

    @staticmethod
    def _get_veto_reason_str(veto_type, veto_name):
        """将 PNP_VETO_TYPE 转换为人类可读的字符串"""
        veto_types = {
            1: "设备仍有活动句柄 (PNP_VetoWindowsApp)",
            2: "系统正在使用该设备 (PNP_VetoWindowsService)",
            3: "驱动程序拒绝请求 (PNP_VetoOutstandingOpen)",
            4: "设备仍有挂起的 I/O 操作 (PNP_VetoDevice)",
            5: "驱动程序不支持弹出 (PNP_VetoDriver)",
            6: "设备被占用或不可卸载 (PNP_VetoIllegalDeviceRequest)",
            7: "有子设备正在运行 (PNP_VetoInsufficientPower)",
            8: "权限不足或非可弹出节点 (PNP_VetoNonRecursive)",
            # ... 更多类型可以根据需要添加
        }
        reason = veto_types.get(veto_type, f"未知原因 (代码: {veto_type})")
        if veto_name and veto_name.upper().startswith("STORAGE\\VOLUME"):
            return "磁盘正被系统或其他程序占用（如资源管理器、杀毒软件等），请关闭相关程序或窗口后重试"
        if veto_name:
            reason += f" - 涉及对象: {veto_name}"
        return reason

    @staticmethod
    def set_dark_mode(hwnd):
        """为窗口启用 Windows 10/11 深色模式标题栏"""
        try:
            dwmapi = ctypes.WinDLL('dwmapi')
            # DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            # 在某些旧版 Win10 上可能是 19
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            rendering_policy = ctypes.c_int(1)
            
            # 设置深色模式
            dwmapi.DwmSetWindowAttribute(
                hwnd, 
                DWMWA_USE_IMMERSIVE_DARK_MODE, 
                ctypes.byref(rendering_policy), 
                ctypes.sizeof(rendering_policy)
            )
            return True
        except Exception as e:
            print(f"设置深色模式失败: {e}")
            return False

    @staticmethod
    def open_physical_drive(drive_number):
        path = f"\\\\.\\PhysicalDrive{drive_number}"
        # Use kernel32 with use_last_error=True
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p
        ]
        
        handle = kernel32.CreateFileW(
            path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None
        )
        
        if handle == 0 or handle == 0xFFFFFFFFFFFFFFFF or handle == -1:
            err = ctypes.get_last_error()
            print(f"DEBUG: CreateFileW for {path} failed. Error: {err}")
            return None
        
        return handle

    @staticmethod
    def close_handle(handle):
        if handle:
            # 必须显式设置签名，否则 64 位句柄被按 32 位截断，
            # CloseHandle 失败导致磁盘句柄泄漏，进而导致“设备正在使用中”。
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle(handle)

    @staticmethod
    def device_io_control(handle, ioctl_code, in_buffer, in_buffer_size, out_buffer, out_buffer_size):
        bytes_returned = wintypes.DWORD(0)
        
        # Use a local WinDLL instance with use_last_error=True to capture errors reliably
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        
        result = kernel32.DeviceIoControl(
            handle,
            ioctl_code,
            in_buffer,
            in_buffer_size,
            out_buffer,
            out_buffer_size,
            ctypes.byref(bytes_returned),
            None
        )
        
        error_code = ctypes.get_last_error()
        return result, bytes_returned.value, error_code

    # ---- 磁盘离线/在线（关机“黑名单”机制） ----
    IOCTL_DISK_SET_DISK_ATTRIBUTES = 0x0007C0F4
    DISK_ATTRIBUTE_OFFLINE = 0x0000000000000001

    class SET_DISK_ATTRIBUTES(ctypes.Structure):
        # 现代 ntdddisk.h 的结构是 40 字节（旧文档只有 16 字节，
        # 用 16 字节缓冲会得到 ERROR_BAD_LENGTH=24 —— 第八轮实测踩坑）
        _fields_ = [
            ("Version", wintypes.DWORD),
            ("Persist", ctypes.c_ubyte),
            ("Reserved1", ctypes.c_ubyte * 3),
            ("Attributes", ctypes.c_ulonglong),
            ("AttributesMask", ctypes.c_ulonglong),
            ("Reserved2", wintypes.DWORD * 4),
        ]

    @staticmethod
    def set_disk_offline(disk_index, offline=True):
        """把磁盘设为 Windows 离线/在线（IOCTL_DISK_SET_DISK_ATTRIBUTES）。

        离线后卷管理器会卸载该盘上的所有卷，此后整个系统（包括关机流程的
        卷 flush / 卸载 / 断电检查）都不再访问这块盘 —— 相当于把盘加入
        Windows 的“黑名单”。这是配合 ATA SLEEP 深睡的关键：盘离线后，
        不会再有任何 I/O 把它唤醒，深睡能一直保持到断电。

        Persist=False：不持久化离线状态，设备重新枚举（下次开机）后自动在线。
        另外服务/程序启动时仍会兜底执行 set_disk_offline(idx, offline=False)。
        """
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        path = f"\\\\.\\PhysicalDrive{disk_index}"
        handle = kernel32.CreateFileW(
            path, GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE, None,
            OPEN_EXISTING, 0, None
        )
        INVALID = ctypes.c_void_p(-1).value
        if not handle or handle == INVALID:
            err = ctypes.get_last_error()
            logging.warning(f"set_disk_offline: 打开 {path} 失败 error={err}")
            return False, err
        try:
            attrs = Win32API.SET_DISK_ATTRIBUTES()
            attrs.Version = ctypes.sizeof(Win32API.SET_DISK_ATTRIBUTES)  # 40
            attrs.Persist = 0  # 不持久化：下次开机自动在线（仍保留兜底恢复）
            attrs.Attributes = Win32API.DISK_ATTRIBUTE_OFFLINE if offline else 0
            attrs.AttributesMask = Win32API.DISK_ATTRIBUTE_OFFLINE
            result, _, error_code = Win32API.device_io_control(
                handle, Win32API.IOCTL_DISK_SET_DISK_ATTRIBUTES,
                ctypes.byref(attrs), ctypes.sizeof(attrs), None, 0
            )
            if result:
                logging.info(f"PhysicalDrive{disk_index} 已设为{'离线' if offline else '在线'}")
            else:
                logging.warning(
                    f"PhysicalDrive{disk_index} {'离线' if offline else '在线'}失败 error={error_code}"
                )
            return bool(result), error_code
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def restart_device_via_pnputil(instance_id):
        """使用 Pnputil 命令行工具重启设备"""
        try:
            logging.info(f"正在尝试使用 Pnputil 重启设备: {instance_id}")
            # 创建 STARTUPINFO 以隐藏控制台窗口
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            # pnputil /restart-device "InstanceID"
            # 注意：instance_id 可能包含 & 符号，需要引号包裹
            cmd = f'pnputil /restart-device "{instance_id}"'
            
            # 使用 subprocess.run 执行
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True, 
                startupinfo=startupinfo,
                shell=True # 需要 shell=True 来正确解析命令
            )
            
            if result.returncode == 0:
                logging.info(f"Pnputil 重启设备成功: {instance_id}")
                return True
            else:
                # 某些情况下 pnputil 返回非0但也成功了，或者需要管理员权限
                logging.error(f"Pnputil 重启设备返回代码 {result.returncode}: {instance_id}\nStdout: {result.stdout}\nStderr: {result.stderr}")
                return False
        except Exception as e:
            logging.error(f"调用 Pnputil 异常: {e}")
            return False

    @staticmethod
    def reset_devnode(instance_id):
        """
        使用 CfgMgr32 API 直接重置设备节点（Disable -> Enable）。
        比 Pnputil 更底层，可能绕过某些检查。
        """
        try:
            cfgmgr32 = ctypes.windll.cfgmgr32
            dnDevInst = ctypes.c_ulong()
            
            # 1. Locate DevNode
            ret = cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(dnDevInst), instance_id, CM_LOCATE_DEVNODE_NORMAL)
            if ret != CR_SUCCESS:
                logging.error(f"CM_Locate_DevNodeW 失败 (Code {ret}): {instance_id}")
                return False
            
            # 2. Disable
            logging.info(f"正在禁用设备节点: {instance_id}")
            ret = cfgmgr32.CM_Disable_DevNode(dnDevInst, 0)
            if ret != CR_SUCCESS:
                logging.warning(f"CM_Disable_DevNode 警告 (Code {ret})，继续尝试启用...")
            
            time.sleep(0.5)
            
            # 3. Enable
            logging.info(f"正在启用设备节点: {instance_id}")
            ret = cfgmgr32.CM_Enable_DevNode(dnDevInst, 0)
            if ret != CR_SUCCESS:
                logging.error(f"CM_Enable_DevNode 失败 (Code {ret})")
                return False
                
            return True
        except Exception as e:
            logging.error(f"DevNode 重置异常: {e}")
            return False

    @staticmethod
    def force_recover_problem_devices():
        """
        强力恢复模式：
        1. 使用 PowerShell 查找所有状态异常（包括 Error, Degradated 等）的磁盘驱动器
        2. 获取它们的父设备 ID
        3. 尝试重启设备本身
        4. 如果设备本身重启失败，尝试重启其父设备（仅限非 PCI/系统总线设备）
        """
        try:
            logging.info("开始执行强力设备恢复流程...")
            
            # PowerShell 命令：
            # 查找所有 Class 为 DiskDrive 的设备（不仅是 -Present 的，因为有些安全删除后可能不被视为 Present）
            # 过滤掉没有 Problem 的设备，以及 Problem 为 CM_PROB_PHANTOM (已拔出的幽灵设备)
            ps_cmd = """
            $devices = Get-PnpDevice -Class DiskDrive -ErrorAction SilentlyContinue | Where-Object { $_.Problem -ne 0 -and $_.Problem -ne $null -and $_.Problem -ne 'CM_PROB_PHANTOM' -and $_.Problem -ne 45 }
            foreach ($dev in $devices) {
                $parent = Get-PnpDeviceProperty -InstanceId $dev.InstanceId -KeyName DEVPKEY_Device_Parent -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Data
                Write-Output "$($dev.InstanceId)|$($dev.Problem)|$parent"
            }
            """
            
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            process = subprocess.run(
                ["powershell", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            
            if process.returncode != 0:
                logging.error(f"PowerShell 查询失败: {process.stderr}")
                return False
                
            # 获取输出
            lines = [line.strip() for line in process.stdout.splitlines() if line.strip()]
            device_info_list = []
            
            for line in lines:
                parts = line.split('|')
                if len(parts) >= 1:
                    iid = parts[0]
                    prob = parts[1] if len(parts) > 1 else "Unknown"
                    parent = parts[2] if len(parts) > 2 else None
                    logging.info(f"发现异常设备: {iid}, 错误代码: {prob}, 父设备: {parent}")
                    device_info_list.append((iid, parent))
            
            if not device_info_list:
                logging.info("PowerShell 未发现存在问题的磁盘设备")
                return Win32API.restart_safe_removed_devices()
                
            logging.info(f"发现 {len(device_info_list)} 个异常设备，准备修复...")
            success_count = 0
            
            processed_parents = set()
            
            for iid, parent in device_info_list:
                # 1. 尝试直接重启设备
                if Win32API.restart_device_via_pnputil(iid):
                    success_count += 1
                    continue
                
                # 2. 如果失败，尝试直接使用 CfgMgr32 API 重置设备
                if Win32API.reset_devnode(iid):
                    success_count += 1
                    continue
                
                # 3. 如果失败，且有父设备，尝试重启父设备
                # 过滤掉危险的父设备（如 PCI 控制器、ACPI 等）
                if parent and parent not in processed_parents:
                    is_safe_parent = True
                    unsafe_prefixes = ['PCI\\', 'ACPI\\', 'ROOT\\', 'HTREE\\']
                    for prefix in unsafe_prefixes:
                        if parent.upper().startswith(prefix):
                            is_safe_parent = False
                            break
                    
                    if is_safe_parent:
                        logging.info(f"尝试重启父设备以恢复子设备: {parent}")
                        if Win32API.restart_device_via_pnputil(parent):
                            success_count += 1
                        else:
                            # 如果父设备重启也失败，尝试对父设备进行 CfgMgr32 重置
                            logging.info(f"父设备重启失败，尝试使用 CfgMgr32 API 重置父设备: {parent}")
                            if Win32API.reset_devnode(parent):
                                success_count += 1
                            else:
                                # 最后尝试 pnputil disable/enable
                                subprocess.run(f'pnputil /disable-device "{parent}"', shell=True, startupinfo=startupinfo)
                                subprocess.run(f'pnputil /enable-device "{parent}"', shell=True, startupinfo=startupinfo)
                            
                            # 稍微等待一下让父设备恢复
                            time.sleep(1)
                            
                        processed_parents.add(parent)
                    else:
                        logging.warning(f"跳过不安全的父设备重启: {parent}")
                
                # 4. 如果还是失败，最后尝试 pnputil disable/enable 子设备
                logging.info(f"尝试先禁用再启用设备: {iid}")
                subprocess.run(f'pnputil /disable-device "{iid}"', shell=True, startupinfo=startupinfo)
                subprocess.run(f'pnputil /enable-device "{iid}"', shell=True, startupinfo=startupinfo)
            
            return success_count > 0
            
        except Exception as e:
            logging.error(f"强力恢复过程异常: {e}")
            return Win32API.restart_safe_removed_devices()


# ====== Safe Removal Spin-Down Patch (DBT_DEVICEQUERYREMOVE Hook) ======

WM_DEVICECHANGE = 0x0219
DBT_DEVICEQUERYREMOVE = 0x8001
DBT_DEVICEQUERYREMOVEFAILED = 0x8002
DBT_DEVICEREMOVEPENDING = 0x8003
DBT_DEVICEREMOVECOMPLETE = 0x8004
DBT_DEVTYP_VOLUME = 0x00000002
DBT_DEVTYP_HANDLE = 0x00000006
DBT_DEVTYP_DEVICEINTERFACE = 0x00000005

DBT_CONFIGCHANGED = 0x0018
DBT_DEVNODES_CHANGED = 0x0007
DBT_DEVICEARRIVAL = 0x8000
DBT_DEVICEREMOVECOMPLETE_BROADCAST = 0x8004

DEVICE_NOTIFY_WINDOW_HANDLE = 0
DEVICE_NOTIFY_SERVICE_HANDLE = 1

# GUID_DEVINTERFACE_VOLUME: {53F5630D-B6BF-11D0-94F2-00A0C91EFB8B}
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

GUID_DEVINTERFACE_VOLUME = GUID(
    0x53F5630D, 0xB6BF, 0x11D0,
    (ctypes.c_ubyte * 8)(0x94, 0xF2, 0x00, 0xA0, 0xC9, 0x1E, 0xFB, 0x8B),
)

# GUID_DEVINTERFACE_DISK: {53F56307-B6BF-11D0-94F2-00A0C91EFB8B}
GUID_DEVINTERFACE_DISK = GUID(
    0x53F56307, 0xB6BF, 0x11D0,
    (ctypes.c_ubyte * 8)(0x94, 0xF2, 0x00, 0xA0, 0xC9, 0x1E, 0xFB, 0x8B),
)

class DEV_BROADCAST_HDR(ctypes.Structure):
    _fields_ = [
        ("dbch_size", wintypes.DWORD),
        ("dbch_devicetype", wintypes.DWORD),
        ("dbch_reserved", wintypes.DWORD),
    ]

class DEV_BROADCAST_VOLUME(ctypes.Structure):
    _fields_ = [
        ("dbcv_size", wintypes.DWORD),
        ("dbcv_devicetype", wintypes.DWORD),
        ("dbcv_reserved", wintypes.DWORD),
        ("dbcv_unitmask", wintypes.DWORD),
        ("dbcv_flags", wintypes.WORD),
    ]

class DEV_BROADCAST_DEVICEINTERFACE(ctypes.Structure):
    _fields_ = [
        ("dbcc_size", wintypes.DWORD),
        ("dbcc_devicetype", wintypes.DWORD),
        ("dbcc_reserved", wintypes.DWORD),
        ("dbcc_classguid", GUID),
        ("dbcc_name", ctypes.c_wchar * 1),
    ]

class DEV_BROADCAST_HANDLE(ctypes.Structure):
    """DBT_DEVTYP_HANDLE 通知负载。
    注册打开的设备句柄后，Windows 在设备移除前会向该句柄的所有者
    发送 DBT_DEVICEQUERYREMOVE (devicetype=DBT_DEVTYP_HANDLE)。
    这是唯一能在系统托盘弹出 USB 硬盘柜时可靠收到 QUERYREMOVE 的机制——
    接口通知(GUID_DEVINTERFACE_*)在 Windows 11 25H2 上只收到 REMOVECOMPLETE。"""
    _fields_ = [
        ("dbch_size", wintypes.DWORD),
        ("dbch_devicetype", wintypes.DWORD),
        ("dbch_reserved", wintypes.DWORD),
        ("dbch_handle", wintypes.HANDLE),
        ("dbch_hdevnotify", ctypes.c_void_p),
        ("dbch_eventguid", ctypes.c_ubyte * 16),
        ("dbch_nameoffset", wintypes.LONG),
        ("dbch_data", ctypes.c_ubyte * 1),
    ]


# Compatibility export; the replacement never sends I/O in a device callback.
from src.hal.removal_notifications import SafeRemovalPatcher
