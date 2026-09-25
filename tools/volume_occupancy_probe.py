"""手动诊断：这块盘/卷被谁占用？是不是本程序自己？

只读查询（Restart Manager + 自身句柄审计）。`--try-lock` 会额外尝试一次
FSCTL_LOCK_VOLUME 并立即释放句柄，用来确认"锁定返回 5"是不是本机的固有行为
（不做 dismount、不 offline、不发 SLEEP）。

用法：
  python tools/volume_occupancy_probe.py --disk 4 --letter E
  python tools/volume_occupancy_probe.py --disk 4 --letter E --try-lock
  python tools/volume_occupancy_probe.py --disk 4 --scan-others   # 跨进程扫描（慢且可能不完整）
"""
import argparse
import ctypes
from pathlib import Path
import sys
import time
from ctypes import wintypes as W

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.utils import volume_diag  # noqa: E402

INVALID = ctypes.c_void_p(-1).value


def volume_path_for(drive_letter):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetVolumeNameForVolumeMountPointW.argtypes = [W.LPCWSTR, W.LPWSTR, W.DWORD]
    kernel32.GetVolumeNameForVolumeMountPointW.restype = W.BOOL
    buffer = ctypes.create_unicode_buffer(512)
    letter = str(drive_letter).strip().rstrip(":").upper()
    if not kernel32.GetVolumeNameForVolumeMountPointW(letter + ":\\", buffer, 512):
        return ""
    return buffer.value.rstrip("\\")


def filesystem_for(drive_letter):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetVolumeInformationW.argtypes = [W.LPCWSTR, W.LPWSTR, W.DWORD,
                                               ctypes.POINTER(W.DWORD),
                                               ctypes.POINTER(W.DWORD),
                                               ctypes.POINTER(W.DWORD), W.LPWSTR, W.DWORD]
    kernel32.GetVolumeInformationW.restype = W.BOOL
    buffer = ctypes.create_unicode_buffer(64)
    letter = str(drive_letter).strip().rstrip(":").upper()
    if kernel32.GetVolumeInformationW(letter + ":\\", None, 0, None, None, None, buffer, 64):
        return buffer.value
    return ""


def try_lock(drive_letter):
    """尝试独占锁定卷并立即释放，返回 (ok, winerror)。只锁不发命令。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p,
                                     W.DWORD, W.DWORD, W.HANDLE]
    kernel32.CreateFileW.restype = W.HANDLE
    kernel32.DeviceIoControl.argtypes = [W.HANDLE, W.DWORD, ctypes.c_void_p, W.DWORD,
                                         ctypes.c_void_p, W.DWORD,
                                         ctypes.POINTER(W.DWORD), ctypes.c_void_p]
    kernel32.DeviceIoControl.restype = W.BOOL
    path = volume_path_for(drive_letter)
    if not path:
        return None, None
    handle = kernel32.CreateFileW(path, 0xC0000000, 3, None, 3, 0, None)
    if not handle or int(handle) == INVALID:
        return False, ctypes.get_last_error()
    try:
        returned = W.DWORD(0)
        ok = kernel32.DeviceIoControl(handle, 0x00090018, None, 0, None, 0,
                                      ctypes.byref(returned), None)
        return bool(ok), ctypes.get_last_error()
    finally:
        kernel32.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser(description="查看硬盘/卷被谁占用")
    parser.add_argument("--disk", type=int, required=True, help="物理盘序号（Get-Disk 的 Number）")
    parser.add_argument("--letter", default="", help="卷盘符，如 E")
    parser.add_argument("--filesystem", default="", help="文件系统（留空自动查询）")
    parser.add_argument("--scan-others", action="store_true",
                        help="跨进程句柄扫描（每个句柄约 0.7ms，可能超时且不完整）")
    parser.add_argument("--try-lock", action="store_true",
                        help="尝试一次 FSCTL_LOCK_VOLUME 并立即释放（用于确认 error=5）")
    args = parser.parse_args()

    filesystem = args.filesystem or (filesystem_for(args.letter) if args.letter else "")
    volume_path = volume_path_for(args.letter) if args.letter else ""
    print("物理盘 = %s   盘符 = %s   文件系统 = %s   卷 = %s"
          % (args.disk, args.letter or "-", filesystem or "-", volume_path or "-"))

    started = time.monotonic()
    diagnostic = volume_diag.diagnose_volume_isolation(
        volume_path, args.letter, filesystem, args.disk,
        include_others=args.scan_others, handle_timeout=8.0)
    print("诊断耗时 %.2fs（句柄表 %s 条）"
          % (time.monotonic() - started, diagnostic.get("scanned", "-")))
    print()
    print("占用说明：", volume_diag.format_occupancy(diagnostic))
    for holder in diagnostic.get("self_handles") or []:
        print("   本程序自身：%s  %s" % (volume_diag.describe_holder(holder),
                                        holder.get("object")))
    for holder in diagnostic.get("other_handles") or []:
        print("   其他程序：%s (PID %s)  %s"
              % (holder.get("process"), holder.get("pid"),
                 volume_diag.describe_holder(holder)))

    if args.try_lock and args.letter:
        ok, error = try_lock(args.letter)
        print()
        if ok:
            print("锁卷测试：成功（说明该卷支持 FSCTL_LOCK_VOLUME，锁已立即释放）")
        else:
            print("锁卷测试：失败 %s" % volume_diag.describe_winerror(error))
            if error == 5 and not volume_diag.filesystem_supports_lock(filesystem):
                print("            → %s 不支持锁定卷，属于预期结果（Windows 资源管理器"
                      "弹出会跳过锁定直接卸载）" % volume_diag.filesystem_display(filesystem))
    return 0


if __name__ == "__main__":
    sys.exit(main())
