"""Pre-shutdown: flush, best-effort temporary offline, then ATA SLEEP.

Offline reduces later filesystem requests; failure does not prevent SLEEP.
Command acceptance is not proof of sustained physical spin-down.
"""

import logging
import subprocess
import time

SERVICE_NAME = "JiFengZhiShutdownSvc"
SERVICE_DISPLAY_NAME = "疾风知硬盘柜 关机停转服务"


def _load_shared_disk_snapshot():
    """读取 GUI 落盘的磁盘清单（服务进程里 WMI 枚举不可靠，用共享文件）。"""
    try:
        import json
        import os
        from src.utils.paths import get_base_path
        path = os.path.join(get_base_path(), "shutdown_disks.json")
        with open(path, "r", encoding="utf-8") as f:
            snap = json.load(f)
        if snap and snap.get("disks"):
            return snap
    except Exception:
        pass
    return None


def _refresh_disk_cache():
    """周期性地把物理盘列表缓存在内存里，关机时优先用缓存。

    优先读 GUI 写入的共享清单（shutdown_disks.json），
    WMI 枚举（在 SYSTEM 会话实测返回 0 设备）只作为兜底。
    """
    snap = _load_shared_disk_snapshot()
    if snap:
        from types import SimpleNamespace
        disks = [SimpleNamespace(**d) for d in snap["disks"]]
        logging.info(f"[Svc] 磁盘缓存已从共享文件刷新: {len(disks)} 个设备")
        return disks

    try:
        from src.core.device_manager import DeviceManager
        disks = DeviceManager.get_physical_disks()
        logging.info(f"[Svc] 磁盘缓存已刷新(WMI): {len(disks)} 个设备")
        return disks
    except Exception as e:
        logging.warning(f"[Svc] 刷新磁盘缓存失败: {e}")
        return []


def _validate_target_handle(handle, disk):
    """Check the actual opened disk, before sending any modifying commands."""
    import ctypes as C
    from src.hal.win32_api import Win32API
    handle = C.c_void_p(handle)
    if Win32API.get_device_number(handle) != disk["index"]:
        raise RuntimeError("磁盘编号已变化，拒绝操作")
    query = (C.c_ubyte * 12)()
    descriptor = (C.c_ubyte * 4096)()
    ok, count, error = Win32API.device_io_control(
        handle, 0x2D1400, C.byref(query), C.sizeof(query),
        C.byref(descriptor), C.sizeof(descriptor))
    raw = bytes(descriptor[:count])
    if not ok or len(raw) < 36:
        raise RuntimeError(f"无法核对磁盘身份: {error}")
    offset = int.from_bytes(raw[24:28], "little")
    if not 36 <= offset < len(raw):
        raise RuntimeError("磁盘序列号描述符无效")
    serial = raw[offset:].split(b"\0", 1)[0].decode("ascii", "replace")
    normalize = lambda value: "".join(str(value).split()).upper()
    if normalize(serial) != normalize(disk["serial_number"]) or raw[28] != 7:
        raise RuntimeError("实际句柄序列号或 USB 总线与白名单目标不匹配")


def _set_offline_handle(handle, offline):
    """Use the already validated handle; never reopen a possibly reassigned index."""
    import ctypes as C
    from src.hal.win32_api import Win32API
    attrs = Win32API.SET_DISK_ATTRIBUTES()
    attrs.Version = C.sizeof(attrs)
    attrs.Persist = 0
    attrs.AttributesMask = Win32API.DISK_ATTRIBUTE_OFFLINE
    attrs.Attributes = attrs.AttributesMask if offline else 0
    ok, _, error = Win32API.device_io_control(
        C.c_void_p(handle), Win32API.IOCTL_DISK_SET_DISK_ATTRIBUTES,
        C.byref(attrs), C.sizeof(attrs), None, 0)
    return bool(ok), error


def _park_all_disks(cached=None):
    from src.core.disk_whitelist import disk_id, is_external_disk
    from src.hal.asm_commander import ASMCommander
    from src.hal.win32_api import Win32API
    from src.utils.paths import get_base_path
    import os
    snap = _load_shared_disk_snapshot() or {}
    allowed = set(snap.get("managed_disk_whitelist") or [])
    sleeping = set(snap.get("sleeping") or [])
    targets = []
    seen = set()
    already_asleep = 0
    for disk in snap.get("disks", []):
        idx = disk.get("index")
        if (not isinstance(idx, int) or idx < 0 or idx in seen or
                not disk_id(disk) or disk_id(disk) not in allowed or
                not is_external_disk(disk)):
            continue
        seen.add(idx)
        if disk.get("serial_number") not in sleeping:
            targets.append(disk)
        else:
            already_asleep += 1
    if not targets:
        # 绝不能静默返回：日志必须能回答"为什么这次关机什么都没停转"。
        total = len(snap.get("disks", []))
        if not snap:
            reason = "共享清单不存在或为空（GUI 未落盘 shutdown_disks.json）"
        elif not allowed:
            reason = "白名单为空：没有任何硬盘被勾选「纳入管理」"
        elif already_asleep:
            reason = "命中的白名单外置盘都已标记休眠，无需重复发命令"
        else:
            reason = ("共享清单里没有白名单外置盘（%d 个设备均不匹配；"
                      "常见原因：硬盘柜未上电/未被枚举，或盘已掉线）" % total)
        logging.warning("[Svc] 关机停转未命中任何目标: %s", reason)
        return 0
    # Only metadata mapping, once, before any disk is put to sleep.
    mapping = Win32API.get_volume_disk_mapping() or {}
    protected_letters = {os.path.splitdrive(get_base_path())[0].rstrip(":").upper(),
                         os.environ.get("SystemDrive", "C:").rstrip(":").upper()}
    protected = {idx for letter, idx in mapping.items()
                 if str(letter).rstrip(":").upper() in protected_letters}
    ok_count = 0
    for disk in targets:
        idx = disk["index"]
        if idx in protected:
            logging.warning("[Svc] 磁盘 %s 承载系统或程序目录，跳过停转", idx)
            continue
        started = time.monotonic()
        try:
            with ASMCommander(idx, model_hint=disk.get("model"),
                              serial_hint=disk.get("serial_number")) as cmd:
                if not cmd.handle:
                    raise RuntimeError("无法打开目标磁盘")
                _validate_target_handle(cmd.handle, disk)
                for letter, number in mapping.items():
                    if number != idx:
                        continue
                    volume = str(letter).rstrip(":") + ":"
                    try:
                        handle = Win32API.open_volume(volume)
                        if handle:
                            try:
                                flushed, error = Win32API.flush_volume_buffers(handle)
                                logging.info("[Svc] 磁盘 %s 卷刷新=%s error=%s", idx, flushed, error)
                            finally:
                                Win32API.close_handle(handle)
                    except Exception as exc:
                        logging.warning("[Svc] 磁盘 %s 卷刷新失败: %s", idx, exc)
                try:
                    flush_ok = cmd.flush_cache(timeout=3)
                except Exception as exc:
                    flush_ok = False
                    logging.warning("[Svc] 磁盘 %s ATA FLUSH 失败: %s", idx, exc)
                logging.info("[Svc] 磁盘 %s 缓存准备结束 FLUSH=%s 耗时=%.3fs",
                             idx, flush_ok, time.monotonic() - started)
                offline_start = time.monotonic()
                try:
                    offline, error = _set_offline_handle(cmd.handle, True)
                except Exception as exc:
                    offline, error = False, str(exc)
                logging.info("[Svc] 磁盘 %s 临时离线=%s error=%s 耗时=%.3fs",
                             idx, offline, error, time.monotonic() - offline_start)
                # Offline failure must not veto SLEEP. No STANDBY fallback.
                ok = cmd.sleep_only()
                if not ok and offline:
                    # Historical bridge fallback; at most one retry and no reset.
                    restored, error = _set_offline_handle(cmd.handle, False)
                    offline = not restored
                    if restored:
                        logging.warning("[Svc] 磁盘 %s 离线透传失败，恢复在线后重试一次 SLEEP", idx)
                        ok = cmd.sleep_only()
                ok_count += int(bool(ok))
                logging.info("[Svc] 磁盘 %s SLEEP已接受=%s 保持离线=%s 总耗时=%.3fs",
                             idx, ok, offline, time.monotonic() - started)
                if ok and not offline:
                    logging.warning("[Svc] 磁盘 %s 已尝试停转但未隔离卷，仍可能发生系统超时或复位", idx)
                # No probes/flush/online operations after successful SLEEP.
        except Exception:
            logging.exception("[Svc] 磁盘 %s 关机处理异常", idx)
    logging.info("[Svc] 关机停转命令完成: %s/%s 块已接受", ok_count, len(targets))
    return ok_count


def bring_all_external_disks_online(cached=None):
    """Persist=False restores on re-enumeration; never online disks by stale indices."""
    return 0


def _make_service_class():
    import win32event
    import win32service
    import win32serviceutil

    class ShutdownParkService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = "仅对 GUI 白名单中的 USB 硬盘逐盘执行安全停转；失败时不强制停转。"


        def __init__(self, args):
            super().__init__(args)
            self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
            self._park_requested = False
            self._cache = None

        def GetAcceptedControls(self):
            # Reserve time for cache flush/offline before final storage shutdown.
            accepted = super().GetAcceptedControls()
            accepted |= getattr(win32service, "SERVICE_ACCEPT_PRESHUTDOWN", 0x100)
            accepted |= win32service.SERVICE_ACCEPT_SHUTDOWN
            return accepted

        def SvcStop(self):
            # 手动停止（sc stop）：直接退出，不做任何事
            logging.info("[Svc] 收到 STOP（手动停止服务）")
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.hWaitStop)

        def SvcShutdown(self):
            logging.info("[Svc] 收到 SERVICE_CONTROL_SHUTDOWN，准备执行关机停转")
            self._park_requested = True
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=10000)
            win32event.SetEvent(self.hWaitStop)

        def SvcOther(self, control):
            if control == getattr(win32service, "SERVICE_CONTROL_PRESHUTDOWN", 15):
                logging.info("[Svc] 收到 SERVICE_CONTROL_PRESHUTDOWN，执行临时离线与停转")
                self._park_requested = True
                self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=30000)
                win32event.SetEvent(self.hWaitStop)

        def SvcDoRun(self):
            logging.info("[Svc] 服务已启动，等待 SERVICE_CONTROL_PRESHUTDOWN（缓存刷新→临时离线→SLEEP）")
            while True:
                win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)

                if self._park_requested:
                    try:
                        started = time.monotonic()
                        _park_all_disks()
                        logging.info("[Svc] 最终关机 SLEEP 总耗时 %.3fs", time.monotonic() - started)
                    except Exception as e:
                        logging.error(f"[Svc] 关机停转异常: {e}")
                    self._park_requested = False
                    # 停转完成 → 报告 STOPPED 并退出，让 SCM 继续关机
                    logging.info("[Svc] 关机停转完成，服务退出")
                    self.ReportServiceStatus(win32service.SERVICE_STOPPED)
                    return

                # 手动停止
                self.ReportServiceStatus(win32service.SERVICE_STOPPED)
                return

    return ShutdownParkService


def svc_main(debug=False):
    """服务入口（--shutdown-service）。debug=True 时立即执行一次停转并退出。"""
    import servicemanager

    if debug:
        logging.info("[Svc] 调试模式：立即执行一次关机停转")
        _park_all_disks()
        return 0

    svc_class = _make_service_class()
    # 本环境的 pywin32 服务托管顺序（servicemanager.PrepareToHostSingle 注册类，
    # StartServiceCtrlDispatcher 无参连接 SCM；C 层随后实例化类并调用 SvcRun()）。
    # 注意：servicemanager.Initialize() 是设置事件源用的，不是注册服务类。
    servicemanager.PrepareToHostSingle(svc_class)
    servicemanager.StartServiceCtrlDispatcher()
    return 0


def is_service_running(name=SERVICE_NAME):
    """GUI 侧查询服务是否在运行（决定 QES 阶段是否降级为立即停转）。"""
    try:
        import win32service
        hscm = win32service.OpenSCManager(
            None, None, win32service.SC_MANAGER_CONNECT
        )
        try:
            hsvc = win32service.OpenService(
                hscm, name, win32service.SERVICE_QUERY_STATUS
            )
        except Exception:
            win32service.CloseServiceHandle(hscm)
            return False
        try:
            status = win32service.QueryServiceStatus(hsvc)
            return status[1] == win32service.SERVICE_RUNNING
        finally:
            win32service.CloseServiceHandle(hsvc)
            win32service.CloseServiceHandle(hscm)
    except Exception:
        return False


def _sc(cmd_args, timeout=30):
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        r = subprocess.run(
            ["sc"] + cmd_args,
            capture_output=True, text=True,
            startupinfo=startupinfo, timeout=timeout,
        )
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return -1, "", str(e)


def remove_shutdown_service():
    """Remove the obsolete pre-shutdown service before using the GUI sleep path."""
    try:
        _, out, _ = _sc(["query", SERVICE_NAME])
        installed = "SERVICE_NAME" in (out or "") and "1060" not in (out or "")
        if not installed:
            return True
        _sc(["stop", SERVICE_NAME], timeout=30)
        code, delete_out, delete_err = _sc(["delete", SERVICE_NAME], timeout=20)
        logging.info(
            "[Svc] 已移除旧关机服务: code=%s %s %s",
            code, (delete_out or "").strip(), (delete_err or "").strip()
        )
        return code == 0
    except Exception:
        logging.exception("[Svc] 移除旧关机服务失败")
        return False


def ensure_service_installed_and_running(exe_path):
    """确保服务已安装（指向当前 exe）且正在运行。

    由 GUI（管理员权限）在启动时调用。返回是否可用。
    """
    _, out, _ = _sc(["query", SERVICE_NAME])
    installed = "SERVICE_NAME" in out and "1060" not in out

    if installed:
        # 路径过期（程序更新换位置）→ 重建
        _, qc_out, _ = _sc(["qc", SERVICE_NAME])
        if exe_path.lower() not in (qc_out or "").lower():
            logging.info(f"[Svc] 服务路径过期，重建: {exe_path}")
            _sc(["stop", SERVICE_NAME], timeout=60)
            _sc(["delete", SERVICE_NAME])
            installed = False

    if not installed:
        binpath = f'"{exe_path}" --shutdown-service'
        code, out, err = _sc(
            ["create", SERVICE_NAME,
             "binPath=", binpath, "start=", "auto",
             "DisplayName=", SERVICE_DISPLAY_NAME],
            timeout=20,
        )
        logging.info(
            f"[Svc] 服务创建结果 {code}: {(out or '').strip()} {(err or '').strip()}"
        )

    # This is an upper bound, not a fixed wait. SCM continues when we stop.
    try:
        import win32service
        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        try:
            service = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_CHANGE_CONFIG)
            try:
                win32service.ChangeServiceConfig2(
                    service, win32service.SERVICE_CONFIG_PRESHUTDOWN_INFO, 30000)
            finally:
                win32service.CloseServiceHandle(service)
        finally:
            win32service.CloseServiceHandle(scm)
    except Exception:
        logging.exception("[Svc] 无法设置 30 秒预关机处理上限")
        return False

    _, query_out, _ = _sc(["query", SERVICE_NAME])
    if "RUNNING" not in (query_out or ""):
        code, out, err = _sc(["start", SERVICE_NAME], timeout=90)
        logging.info(
            f"[Svc] 服务启动结果 {code}: {(out or '').strip()} {(err or '').strip()}"
        )

    return is_service_running()
