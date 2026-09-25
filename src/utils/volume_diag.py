"""弹出/停转失败时的占用诊断与可读文案。

只做**只读**查询，不发任何磁盘命令、不改变设备状态：

1. Restart Manager：谁在文件层面占用了这个卷（explorer / 杀毒 / 索引器 …）。
2. 存储句柄审计：谁打开了这个卷设备或物理盘——包括**本程序自己**。
   判定方式是 IOCTL_STORAGE_GET_DEVICE_NUMBER（返回磁盘号/分区号），
   而不是 NtQueryObject 取对象名：后者在管道/无响应设备上会阻塞，
   一次全表扫描会被拖到超时（实测踩过）。
   自身句柄不需要管理员权限即可解析，所以"是不是被本程序占用"总有确定答案；
   其他进程在权限不足时只统计数量，并明确写进文案。

设计原则：诊断只需要"够用且不误导"——任何一步失败都必须显式说明，
绝不能静默地报"没有占用者"。
"""

import ctypes
import logging
import os
import sys
import threading
import time
from ctypes import wintypes as W

# ----------------------------------------------------------------- WinError 文案

WINERROR_TEXT = {
    1: "功能不受支持",
    2: "找不到设备（盘可能已掉线或未被枚举）",
    5: "拒绝访问（被其他程序占用、权限不足，或该文件系统不支持此操作）",
    21: "设备未就绪（硬盘可能已停转）",
    32: "共享冲突（有其他程序正在使用该卷）",
    33: "文件被部分锁定（有其他程序锁定了卷内文件）",
    55: "设备不存在",
    87: "参数错误",
    1117: "I/O 设备错误（设备未就绪、链路复位或盘正在起转）",
    1167: "设备未连接",
    433: "设备未连接（USB 桥已断开）",
}

# 这些文件系统不支持 FSCTL_LOCK_VOLUME：锁定必然返回 ERROR_ACCESS_DENIED，
# Windows 资源管理器弹出时的做法是跳过锁定、直接卸载卷。
NO_LOCK_FILESYSTEMS = {"REFS", "EXFAT", "FAT32", "FAT", "UDF", "CDFS"}

_SYSTEM_EXTENDED_HANDLE_INFORMATION = 64
_PROCESS_DUP_HANDLE = 0x0040
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_DUPLICATE_SAME_ACCESS = 0x0002
_OBJECT_TYPE_INFORMATION = 2
_IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x2D1080
_MAX_HANDLE_ENTRIES = 4000000
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_DISK_LEVEL_PARTITION = 0xFFFFFFFF


def describe_winerror(code):
    """把裸错误码翻译成能给用户看的中文。"""
    if code is None:
        return "未知错误"
    try:
        number = int(code)
    except (TypeError, ValueError):
        return str(code)
    text = WINERROR_TEXT.get(number)
    if text:
        return "WinError %d %s" % (number, text)
    return "WinError %d" % number


_FILESYSTEM_DISPLAY = {"REFS": "ReFS", "NTFS": "NTFS", "EXFAT": "exFAT",
                       "FAT32": "FAT32", "FAT": "FAT", "UDF": "UDF", "CDFS": "CDFS"}


def filesystem_display(filesystem):
    """给用户看的文件系统名（保持 ReFS/exFAT 官方大小写）。"""
    name = str(filesystem or "").strip().upper()
    return _FILESYSTEM_DISPLAY.get(name, name)


def filesystem_supports_lock(filesystem):
    """ReFS/exFAT 等文件系统不支持 FSCTL_LOCK_VOLUME。"""
    name = str(filesystem or "").strip().upper()
    if not name or name in ("UNKNOWN", "RAW"):
        return True  # 未知就不预判，交给实际 IOCTL 结果
    return name not in NO_LOCK_FILESYSTEMS


def volume_label(unique_id, drive_letter=None, index=None):
    """生成人能读的卷标识：优先盘符，其次卷 GUID。"""
    if drive_letter:
        return "%s:" % str(drive_letter).strip().rstrip(":")
    text = str(unique_id or "").strip().rstrip("\\")
    if text.upper().startswith("\\\\?\\VOLUME"):
        return "卷 %s" % text.rsplit("\\", 1)[-1]
    if text:
        return text
    if index is not None:
        return "磁盘 %s 上的卷" % index
    return "目标卷"


# ------------------------------------------------------------- 句柄审计（只读）


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", W.USHORT),
                ("MaximumLength", W.USHORT),
                ("Buffer", ctypes.c_void_p)]


class _HANDLE_ENTRY(ctypes.Structure):
    _fields_ = [("Object", ctypes.c_void_p),
                ("UniqueProcessId", ctypes.c_void_p),
                ("HandleValue", ctypes.c_void_p),
                ("GrantedAccess", ctypes.c_ulong),
                ("CreatorBackTraceIndex", ctypes.c_ushort),
                ("ObjectTypeIndex", ctypes.c_ushort),
                ("HandleAttributes", ctypes.c_ulong),
                ("Reserved", ctypes.c_ulong)]


_NTDLL = None
_KERNEL32 = None
_PROBE_FILE = None


def _ntdll():
    global _NTDLL
    if _NTDLL is None:
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtQuerySystemInformation.restype = ctypes.c_long
        ntdll.NtQuerySystemInformation.argtypes = [
            ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong)]
        ntdll.NtQueryObject.restype = ctypes.c_long
        ntdll.NtQueryObject.argtypes = [W.HANDLE, ctypes.c_ulong, ctypes.c_void_p,
                                        ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
        _NTDLL = ntdll
    return _NTDLL


def _kernel32():
    """一次性声明签名：x64 下句柄若按默认 c_int 传参会截断。"""
    global _KERNEL32
    if _KERNEL32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p,
                                  W.DWORD, W.DWORD, W.HANDLE]
        k.CreateFileW.restype = W.HANDLE
        k.CloseHandle.argtypes = [W.HANDLE]
        k.CloseHandle.restype = W.BOOL
        k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        k.OpenProcess.restype = W.HANDLE
        k.GetCurrentProcess.restype = W.HANDLE
        k.DuplicateHandle.argtypes = [W.HANDLE, W.HANDLE, W.HANDLE,
                                      ctypes.POINTER(W.HANDLE), W.DWORD, W.BOOL, W.DWORD]
        k.DuplicateHandle.restype = W.BOOL
        k.DeviceIoControl.argtypes = [W.HANDLE, W.DWORD, ctypes.c_void_p, W.DWORD,
                                      ctypes.c_void_p, W.DWORD,
                                      ctypes.POINTER(W.DWORD), ctypes.c_void_p]
        k.DeviceIoControl.restype = W.BOOL
        k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR,
                                                 ctypes.POINTER(W.DWORD)]
        k.QueryFullProcessImageNameW.restype = W.BOOL
        k.QueryDosDeviceW.argtypes = [W.LPCWSTR, W.LPWSTR, W.DWORD]
        k.QueryDosDeviceW.restype = W.DWORD
        _KERNEL32 = k
    return _KERNEL32


def _read_handle_table():
    """返回 [(pid, handle_value, object_type_index), ...]；失败返回空表。

    句柄表通常几十万条（实测本机 22 万+），缓冲区要 16 MB 级，
    且不能对条目数设小上限——否则会静默得出"没有占用者"的错误结论。
    """
    ntdll = _ntdll()
    size = 1 << 20
    buffer = None
    while size <= (1 << 28):
        buffer = ctypes.create_string_buffer(size)
        returned = ctypes.c_ulong(0)
        status = ntdll.NtQuerySystemInformation(
            _SYSTEM_EXTENDED_HANDLE_INFORMATION, buffer, size, ctypes.byref(returned))
        if status == 0:
            break
        buffer = None
        size *= 2
    if buffer is None:
        logging.debug("句柄表查询失败：缓冲区不足或权限不足")
        return []

    count = ctypes.c_size_t.from_buffer(buffer).value
    if not count or count > _MAX_HANDLE_ENTRIES:
        logging.debug("句柄表条目数异常: %s", count)
        return []
    entry_size = ctypes.sizeof(_HANDLE_ENTRY)
    base = ctypes.sizeof(ctypes.c_size_t) * 2  # NumberOfHandles + Reserved
    available = (size - base) // entry_size
    entries = []
    for i in range(min(int(count), int(available))):
        entry = _HANDLE_ENTRY.from_buffer(buffer, base + i * entry_size)
        entries.append((int(entry.UniqueProcessId or 0),
                        int(entry.HandleValue or 0),
                        int(entry.ObjectTypeIndex)))
    return entries


def _open_probe_handle():
    """打开一个已知的 File 句柄；**必须在句柄表快照之前**调用。"""
    global _PROBE_FILE
    kernel32 = _kernel32()
    candidates = [sys.executable,
                  os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "win.ini"),
                  os.environ.get("COMSPEC")]
    for candidate in candidates:
        if not candidate:
            continue
        handle = kernel32.CreateFileW(candidate, 0x80000000, 3, None, 3, 0x80, None)
        if handle and int(handle) != _INVALID_HANDLE:
            _PROBE_FILE = (handle, candidate)
            return handle
    return None


def _probe_file_type_index(entries, own_pid):
    """在已快照的句柄表里找到探针句柄，读出 File 对象类型索引。

    顺序反了（先快照后开句柄）就找不到，会导致他进程扫描被整体跳过——
    曾经因此静默地报"没有其他占用者"。
    """
    if not _PROBE_FILE:
        return None
    target = int(_PROBE_FILE[0])
    for pid, handle_value, type_index in entries:
        if pid == own_pid and handle_value == target:
            return type_index
    return None


def _close_probe_handle():
    global _PROBE_FILE
    if _PROBE_FILE:
        try:
            _kernel32().CloseHandle(_PROBE_FILE[0])
        except Exception:
            pass
        _PROBE_FILE = None


def _open_process_for_duplication(pid):
    """每轮扫描每个进程只开一次句柄（逐句柄重开会拖垮全表扫描）。"""
    handle = _kernel32().OpenProcess(
        _PROCESS_DUP_HANDLE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    return handle or None


def _process_name(pid):
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = W.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    except Exception:
        pass
    finally:
        kernel32.CloseHandle(handle)
    return ""


def _object_type_name(handle):
    ntdll = _ntdll()
    buffer = ctypes.create_string_buffer(1024)
    returned = ctypes.c_ulong(0)
    if ntdll.NtQueryObject(handle, _OBJECT_TYPE_INFORMATION, buffer,
                           ctypes.sizeof(buffer), ctypes.byref(returned)) != 0:
        return ""
    name = _UNICODE_STRING.from_buffer(buffer)
    if not name.Buffer or not name.Length:
        return ""
    return ctypes.wstring_at(name.Buffer, name.Length // 2)


def _handle_storage_id(source, handle_value):
    """把句柄判成存储设备句柄。

    返回 (状态, 磁盘号, 分区号)：状态是 storage / other / denied。
    用 IOCTL_STORAGE_GET_DEVICE_NUMBER 判定——对文件/管道会立刻返回
    "功能不受支持"，不像取对象名那样可能阻塞。
    """
    kernel32 = _kernel32()
    if not source:
        return "denied", None, None
    duplicate = W.HANDLE()
    try:
        if not kernel32.DuplicateHandle(source, W.HANDLE(handle_value),
                                        kernel32.GetCurrentProcess(),
                                        ctypes.byref(duplicate), 0, False,
                                        _DUPLICATE_SAME_ACCESS):
            return "denied", None, None
        if _object_type_name(duplicate).lower() != "file":
            return "other", None, None
        number = (W.DWORD * 3)()
        returned = W.DWORD(0)
        ok = kernel32.DeviceIoControl(duplicate, _IOCTL_STORAGE_GET_DEVICE_NUMBER,
                                      None, 0, ctypes.byref(number),
                                      ctypes.sizeof(number), ctypes.byref(returned), None)
        if not ok:
            return "other", None, None
        return "storage", int(number[1]), int(number[2])
    except Exception:
        return "denied", None, None
    finally:
        if duplicate:
            kernel32.CloseHandle(duplicate)


def _handle_object_name(source, handle_value):
    """只为命中的少数句柄取对象名（用于展示），失败返回空串。"""
    kernel32 = _kernel32()
    ntdll = _ntdll()
    if not source:
        return ""
    duplicate = W.HANDLE()
    try:
        if not kernel32.DuplicateHandle(source, W.HANDLE(handle_value),
                                        kernel32.GetCurrentProcess(),
                                        ctypes.byref(duplicate), 0, False,
                                        _DUPLICATE_SAME_ACCESS):
            return ""
        buffer = ctypes.create_string_buffer(4096)
        returned = ctypes.c_ulong(0)
        if ntdll.NtQueryObject(duplicate, 1, buffer, ctypes.sizeof(buffer),
                               ctypes.byref(returned)) != 0:
            return ""
        name = _UNICODE_STRING.from_buffer(buffer)
        if not name.Buffer or not name.Length:
            return ""
        return ctypes.wstring_at(name.Buffer, name.Length // 2)
    except Exception:
        return ""
    finally:
        if duplicate:
            kernel32.CloseHandle(duplicate)


def audit_storage_holders(disk_index, timeout_seconds=3.0, include_others=True):
    """审计谁持有目标物理盘（及其上卷）的句柄。

    返回 {"self": [...], "others": [...], "unresolved": n, "scanned": n,
          "timed_out": bool, "table_unavailable": bool, "type_index_unknown": bool}
    """
    own_pid = os.getpid()
    target = int(disk_index) if disk_index is not None else None
    result = {"self": [], "others": [], "unchecked": [], "unresolved": 0, "scanned": 0,
              "timed_out": False, "table_unavailable": False,
              "type_index_unknown": False}
    if target is None:
        return result

    def _entry(pid, handle_value, source, device_number, partition):
        return {"pid": pid, "process": _process_name(pid) or ("PID %d" % pid),
                "disk": device_number,
                "partition": None if partition == _DISK_LEVEL_PARTITION else partition,
                "level": "disk" if partition == _DISK_LEVEL_PARTITION else "volume",
                "object": _handle_object_name(source, handle_value)}

    def _scan():
        _open_probe_handle()
        try:
            entries = _read_handle_table()
        except Exception as exc:
            logging.debug("句柄表读取失败: %s", exc)
            _close_probe_handle()
            return
        result["scanned"] = len(entries)
        if not entries:
            result["table_unavailable"] = True
            _close_probe_handle()
            return
        deadline = time.monotonic() + max(0.2, float(timeout_seconds))
        file_type_index = _probe_file_type_index(entries, own_pid)
        _close_probe_handle()
        sources = {}

        def source_for(pid):
            if pid not in sources:
                sources[pid] = _open_process_for_duplication(pid)
            return sources[pid]

        try:
            # 第一步：先把自己查清楚——自身句柄不需要权限，必须有确定结论。
            own_source = source_for(own_pid)
            for pid, handle_value, _type_index in entries:
                if pid != own_pid:
                    continue
                if time.monotonic() > deadline:
                    result["timed_out"] = True
                    break
                status, device_number, partition = _handle_storage_id(own_source, handle_value)
                if status == "storage" and device_number == target:
                    result["self"].append(_entry(pid, handle_value, own_source,
                                                 device_number, partition))

            if not include_others:
                return
            if file_type_index is None:
                result["type_index_unknown"] = True
                return
            # 第二步：他进程只解析 File 类型句柄，命中一个即可（每进程一条）。
            matched = set()
            for pid, handle_value, type_index in entries:
                if pid == own_pid or pid <= 4 or type_index != file_type_index:
                    continue
                if pid in matched or sources.get(pid, True) is None:
                    continue
                if time.monotonic() > deadline:
                    result["timed_out"] = True
                    break
                status, device_number, partition = _handle_storage_id(source_for(pid), handle_value)
                if status == "denied":
                    if pid not in sources:
                        sources[pid] = None
                        result["unresolved"] += 1
                        name = _process_name(pid) or ("PID %d" % pid)
                        result["unchecked"].append(name)
                    continue
                if status == "storage" and device_number == target:
                    matched.add(pid)
                    result["others"].append(_entry(pid, handle_value, sources.get(pid),
                                                   device_number, partition))
        finally:
            for handle in sources.values():
                if handle:
                    _kernel32().CloseHandle(handle)

    worker = threading.Thread(target=_scan, daemon=True)
    worker.start()
    worker.join(max(0.5, float(timeout_seconds) + 0.5))
    if worker.is_alive():
        result["timed_out"] = True
    return result


def describe_holder(entry):
    """把一条占用记录变成短文案，例如 "磁盘4 分区2" 或 "整盘"。"""
    if entry.get("level") == "disk":
        return "磁盘 %s（整盘句柄）" % entry.get("disk")
    return "磁盘 %s 分区 %s" % (entry.get("disk"), entry.get("partition"))


# ------------------------------------------------------------------ 卷占用诊断


def volume_holder_patterns(volume_path, drive_letter=None, disk_index=None):
    """保留用于日志的卷设备名（不参与占用判定，判定按磁盘号做）。"""
    patterns = []
    path = str(volume_path or "").strip()
    if path.upper().startswith("\\\\?\\VOLUME"):
        patterns.append(path.rstrip("\\").split("\\")[-1])
    if drive_letter:
        try:
            buf = ctypes.create_unicode_buffer(512)
            letter = str(drive_letter).strip().rstrip(":").upper()
            # 必须带冒号："E" 返回 ERROR_FILE_NOT_FOUND(2)，"E:" 才是设备名查询。
            if _kernel32().QueryDosDeviceW(letter + ":", buf, 512) and buf.value:
                patterns.append(buf.value.strip("\\"))
        except Exception as exc:
            logging.debug("QueryDosDevice 失败 %s: %s", drive_letter, exc)
    return [p for p in patterns if p]


def query_file_occupiers(volume_root, timeout_seconds=3.0):
    """Restart Manager：文件层面占用该卷的程序名（去重）。"""
    from src.hal.win32_api import Win32API
    return Win32API.query_occupying_apps(volume_root, timeout_seconds=timeout_seconds)


def diagnose_volume_isolation(volume_path, drive_letter=None, filesystem=None,
                              disk_index=None, disk_number=None, with_handles=True,
                              handle_timeout=3.0, include_others=False):
    """汇总一次卷隔离失败的全部可读信息（只读）。

    默认只审计**本程序自身**的句柄（快、结论确定），其他程序用 Restart Manager
    做文件层面检查。全表跨进程扫描实测每个句柄约 0.7 ms（本机 1.6 万个文件句柄，
    且句柄表按分配顺序排列，目标进程常常在后面），做不完也不该拖住弹出流程，
    因此只在手工诊断时用 include_others=True 打开。
    """
    index = disk_index if disk_index is not None else disk_number
    label = volume_label(volume_path, drive_letter, index)
    diagnostic = {
        "label": label,
        "drive_letter": (str(drive_letter).strip().rstrip(":").upper() + ":")
                        if drive_letter else "",
        "filesystem": str(filesystem or "").strip().upper(),
        "disk_index": index,
        "device_names": volume_holder_patterns(volume_path, drive_letter, index),
        "occupiers": [],
        "self_handles": [],
        "other_handles": [],
        "unchecked": [],
        "unresolved": 0,
        "timed_out": False,
        "table_unavailable": False,
        "other_scan": "full" if include_others else "file_level_only",
    }
    root = diagnostic["drive_letter"] + "\\" if diagnostic["drive_letter"] else ""
    if root:
        try:
            diagnostic["occupiers"] = query_file_occupiers(root)
        except Exception as exc:
            logging.debug("查询占用程序失败: %s", exc)
    if with_handles and index is not None:
        try:
            audit = audit_storage_holders(index, timeout_seconds=handle_timeout,
                                          include_others=include_others)
            diagnostic["self_handles"] = audit["self"]
            diagnostic["other_handles"] = audit["others"]
            diagnostic["unchecked"] = audit.get("unchecked") or []
            diagnostic["unresolved"] = audit["unresolved"]
            diagnostic["timed_out"] = audit["timed_out"]
            diagnostic["table_unavailable"] = audit["table_unavailable"]
            diagnostic["scanned"] = audit["scanned"]
        except Exception as exc:
            logging.debug("句柄审计失败: %s", exc)
    return diagnostic


def format_occupancy(diagnostic):
    """把诊断结果压成一段给用户看的占用说明。"""
    parts = []
    occupiers = [a for a in (diagnostic.get("occupiers") or []) if a]
    if occupiers:
        parts.append("占用者（文件层面）：" + "、".join(sorted(set(occupiers))))
    else:
        parts.append("占用者（文件层面）：未发现其他程序")
    self_handles = diagnostic.get("self_handles") or []
    if self_handles:
        # 容忍两种形态：新的占用记录 dict，以及历史/测试传入的字符串。
        detail = "、".join(sorted({
            describe_holder(h) if isinstance(h, dict) else str(h)
            for h in self_handles}))
        parts.append("本程序自身持有该盘句柄 %d 个（%s）" % (len(self_handles), detail))
    else:
        parts.append("本程序自身持有该盘句柄：0 个")
    others = diagnostic.get("other_handles") or []
    if others:
        detail = "、".join(sorted({
            "%s (%s)" % (o.get("process") or o.get("pid"), describe_holder(o))
            if isinstance(o, dict) else str(o)
            for o in others}))
        parts.append("其他程序持有该盘句柄：" + detail)
    if diagnostic.get("other_scan") == "file_level_only":
        parts.append("其他程序仅做文件层面检查（Restart Manager）")
    if diagnostic.get("unresolved"):
        names = [n for n in (diagnostic.get("unchecked") or []) if n]
        shown = "、".join(sorted(set(names))[:6]) if names else ""
        text = "另有 %d 个进程的句柄因权限不足未能检查" % diagnostic["unresolved"]
        if shown:
            text += "（%s%s）" % (shown, " 等" if len(set(names)) > 6 else "")
        parts.append(text)
    if diagnostic.get("table_unavailable"):
        parts.append("系统句柄表不可读，未能确认是否被本程序持有")
    if diagnostic.get("type_index_unknown"):
        parts.append("句柄类型索引未知，未扫描其他程序")
    if diagnostic.get("timed_out"):
        parts.append("句柄审计超时，结果可能不完整")
    return "；".join(parts)


def format_lock_failure(diagnostic, lock_error, dismount_error=None, filesystem=None):
    """卷无法独占锁定（或无法卸载）时的用户可读结论。"""
    filesystem = str(filesystem or diagnostic.get("filesystem") or "").upper()
    label = diagnostic.get("label") or "目标卷"
    fs_text = " (%s)" % filesystem_display(filesystem) if filesystem else ""
    message = "无法隔离卷 %s%s：锁定卷失败（%s）" % (
        label, fs_text, describe_winerror(lock_error))
    if dismount_error is not None:
        message += "，直接卸载也失败（%s）" % describe_winerror(dismount_error)
    message += "。" if dismount_error is not None else "。\n"
    message += format_occupancy(diagnostic)
    if dismount_error is not None:
        message += ("\n建议：关闭上述程序（或在资源管理器中关闭该盘窗口）后重试；"
                    "若未列出任何程序，请点“刷新硬盘状态”后重试，"
                    "也可先对该硬盘执行“深度休眠”再弹出。")
    else:
        message += "\n建议：关闭占用该卷的程序后重试。"
    return message


def format_stage_failure(stage, detail, error_code=None, hint=""):
    """阶段失败（打开物理盘/打开卷等）的可读文案。"""
    text = "%s失败" % stage
    if error_code is not None:
        text += "：%s" % describe_winerror(error_code)
    if detail:
        text += "（%s）" % detail
    if hint:
        text += "\n建议：" + hint
    return text


def format_lock_unsupported(diagnostic, filesystem=None, dismount_ok=True):
    """ReFS/exFAT 等不支持锁卷时的说明（跳过锁定直接卸载）。"""
    filesystem = str(filesystem or diagnostic.get("filesystem") or "").upper()
    label = diagnostic.get("label") or "目标卷"
    fs_text = " (%s)" % filesystem_display(filesystem) if filesystem else ""
    if dismount_ok:
        return ("卷 %s%s 不支持锁定卷（ReFS/exFAT 等文件系统没有 FSCTL_LOCK_VOLUME，"
                "WinError 5 拒绝访问是预期结果），已按 Windows 资源管理器弹出方式"
                "跳过锁定、直接卸载卷。" % (label, fs_text))
    return "卷 %s%s 不支持锁定卷，且直接卸载也失败。" % (label, fs_text)
