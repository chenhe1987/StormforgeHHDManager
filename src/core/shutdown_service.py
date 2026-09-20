"""关机停转服务 —— 在 Windows 关机流程的正确阶段停转硬盘。

## 为什么需要服务（六轮实测 + 调研结论）

GUI 进程的宿命：实测 WM_QUERYENDSESSION 之后约 38 毫秒进程就被结束；
QES/ENDSESSION 期间应用还活着、外置盘的卷仍被资源管理器等占用
（实测 FSCTL_LOCK_VOLUME 返回 error=5，卸载卷失败），
于是"先卸载卷再停转"永远做不到——卷 flush 总会把停转的盘重新转起来，
断电时盘在转 → 不安全关机数再 +1。

调研结论（https://github.com/gfody/OnShutdown 等）：
Windows 关机流程里唯一可靠且足够晚的钩子是**服务**的
`SERVICE_CONTROL_PRESHUTDOWN`(0x0F) 通知——它发生在：
  1. 所有**应用程序已经退出**（卷句柄全部释放）之后；
  2. 其他服务停止、文件系统 flush、设备断电**之前**；
且服务可以把关机推迟最多约 125 秒（WaitToKillServiceTimeout），
有充足时间完成：刷卷缓存 → 尽力离线（纯加速）→ **无条件深度休眠** → 黑名单。
其余方案（GPO 关机脚本 / WMI 事件 / 任务计划事件触发）都会被提前终止。

> 策略已定稿（第 11 轮实测通过），固定技术原则见
> `docs/TECH_NOTES_弹出休眠机制.md` §8.38「技术原则（定稿）」：
> 深睡(ATA SLEEP 0xE6)本身就是黑名单，必须无条件执行；
> 离线/卸载只是可选加速，绝不成为深睡的前提。

服务安装为"自动启动"（LocalSystem），平时休眠等待，关机时工作；
GUI 负责在每次启动时确保服务已安装且正在运行。

本环境的 pywin32 实现注意：win32service 不导出 StartServiceCtrlDispatcher，
必须走 `win32serviceutil.ServiceFramework` + `servicemanager.Initialize /
StartServiceCtrlDispatcher`，PRESHUTDOWN 通过重写 GetAcceptedControls +
SvcOther(15) 接收。
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


def _park_all_disks(cached=None):
    """关机停转（v12）：刷卷缓存 → 尽力离线(纯加速) → **无条件深睡** → 移入黑名单。

    用户方案定稿（v9/v10）：
    - “关机时给硬盘深度休眠的命令，然后给 Windows 黑名单，让它整个关机流程
      都不管外置硬盘”；
    - “外置硬盘被程序占用无法卸载，但无法被卸载不代表无法被休眠：
      休眠按钮按下后硬盘进入黑名单，所有程序都无法访问它”。

    因此铁律：**深睡必须无条件执行**——不管卷能不能卸载、盘是否被占用，
    都先深睡；深睡本身就是黑名单（盘不再响应任何程序，桥接芯片对后续
    访问立刻回 3A/00，不会真把盘唤醒）。

    第 11 轮教训：离线/等卷卸载绝不能成为深睡的前置条件（那是第 9 轮
    失败的原因）。离线只是**可选加速**：
    - 第 10 轮实测：深睡后卷仍挂载，内核关机阶段 flush 卷时命令发向
      深睡盘超时 → UASPStor 复位设备，关机卡 58 秒；先离线（盘还转时
      卷同步卸载）就没卷可 flush → 关机不卡。
    - 离线失败/卷被占用：**直接跳过，不等待、不阻塞**，照常深睡。
      （这一分支等价于第 10 轮已确认生效的纯深睡方案。）

    v12 每块外置盘流程：
    1. 刷文件系统缓存（尽力而为，数据安全）；
    2. 尽力尝试 Windows 离线（Persist=False，下次开机自动在线）——
       纯加速，不等卷卸载完成；
    3. 无条件深睡（瞬时失败重试，最多 3 次）；若离线成功却深睡失败，
       恢复在线再重试一次（离线后 ATA 透传可能失败；深睡优先于离线）；
    4. 深睡成功 → 移入本程序黑名单。
    """
    from src.hal.win32_api import Win32API
    from src.core.device_manager import DeviceManager

    snap = _load_shared_disk_snapshot()
    sleeping = set((snap or {}).get("sleeping") or [])
    disks = cached or _refresh_disk_cache()
    if not disks:
        disks = _refresh_disk_cache()

    # 卷映射（Win32，无 WMI）：用于刷卷缓存
    by_disk = {}
    try:
        for letter, idx in (Win32API.get_volume_disk_mapping() or {}).items():
            by_disk.setdefault(idx, []).append(f"{letter}:")
    except Exception as e:
        logging.warning(f"[Svc] 卷映射失败: {e}")

    started = time.time()
    ok_count = 0
    for d in disks:
        idx = getattr(d, "index", None)
        if idx is None:
            continue
        serial = getattr(d, "serial_number", None)
        model = getattr(d, "model", None) or f"Disk{idx}"
        pnp = (getattr(d, "pnp_id", "") or "").upper()
        is_external = bool(getattr(d, "is_removable", False)) or \
            "USB" in pnp or "UASP" in pnp
        if not is_external or (serial and serial in sleeping):
            logging.info(f"[Svc] 跳过磁盘 {idx} ({model})：已休眠或非外置")
            continue

        t0 = time.time()
        try:
            volumes = by_disk.get(idx, [])

            # 1) 刷文件系统缓存（不锁卷、不卸载卷：被占用也能刷，数据安全）
            flushed = []
            for vol in volumes:
                try:
                    handle = Win32API.open_volume(vol)
                    if not handle:
                        continue
                    try:
                        ok_f, _ = Win32API.flush_volume_buffers(handle)
                        if ok_f:
                            flushed.append(vol)
                    finally:
                        Win32API.close_handle(handle)
                except Exception:
                    pass

            # 2) 尽力离线（纯加速，绝不决定/阻塞深睡）：盘还转时卷同步卸载，
            #    消除第 10 轮实测的内核关机 flush 卡 58 秒。失败就直接跳过。
            offline_ok = False
            try:
                offline_ok, off_err = Win32API.set_disk_offline(idx, offline=True)
            except Exception:
                offline_ok = False

            # 3) 无条件深睡（与“立即休眠硬盘”按钮同机制）——
            #    铁律：不管卷卸没卸掉、盘是否被占用，都必须执行。
            #    瞬时失败重试，最多 3 次；离线成功却深睡失败时恢复在线再重试。
            ok = False
            msg = "未执行"
            for attempt in range(3):
                if attempt:
                    time.sleep(1.0)
                ok, msg, _ = DeviceManager.deep_sleep_disk(idx, model=model, serial=serial)
                if ok:
                    break
                if attempt == 0 and offline_ok:
                    # 离线后 ATA 透传可能失败：恢复在线再试，深睡优先于离线
                    try:
                        Win32API.set_disk_offline(idx, offline=False)
                    except Exception:
                        pass
                    offline_ok = False

            # 4) 移入本程序黑名单
            if ok and serial:
                sleeping.add(serial)

            if ok:
                ok_count += 1
                note = f"{'已离线' if offline_ok else '离线未生效，直接深睡'}"
                if flushed:
                    note += "，已刷卷 " + ",".join(flushed)
                logging.info(
                    f"[Svc] 磁盘 {idx} ({model}) 已深睡并移入黑名单（{note}）"
                    f" ({time.time() - t0:.1f}s)"
                )
            else:
                logging.warning(f"[Svc] 磁盘 {idx} 深睡失败: {msg} ({time.time() - t0:.1f}s)")
        except Exception as e:
            logging.warning(f"[Svc] 磁盘 {idx} 深睡异常: {e}")

    logging.info(f"[Svc] 关机停转完成: {ok_count} 块已深睡，耗时 {time.time() - started:.1f}s")
    return ok_count


def bring_all_external_disks_online(cached=None):
    """开机恢复：把上次关机时离线的外置盘重新上线（卷自动重新挂载）。

    离线状态会被 Windows 持久化，若不开机恢复，用户下次开机会发现盘符消失。
    服务启动时和 GUI 启动时各执行一次（幂等）。
    """
    from src.hal.win32_api import Win32API

    disks = cached
    if not disks:
        snap = _load_shared_disk_snapshot()
        if snap:
            from types import SimpleNamespace
            disks = [SimpleNamespace(**d) for d in snap.get("disks", [])]

    n = 0
    for d in disks or []:
        idx = getattr(d, "index", None)
        if idx is None:
            continue
        pnp = (getattr(d, "pnp_id", "") or "").upper()
        is_external = bool(getattr(d, "is_removable", False)) or \
            "USB" in pnp or "UASP" in pnp
        if not is_external:
            continue
        try:
            ok, _ = Win32API.set_disk_offline(idx, offline=False)
            if ok:
                n += 1
        except Exception as e:
            logging.warning(f"[Svc] 磁盘 {idx} 恢复上线异常: {e}")
    if n:
        logging.info(f"[Svc] 开机恢复: {n} 块外置盘已重新上线")
    return n


def _make_service_class():
    import win32event
    import win32service
    import win32serviceutil

    class ShutdownParkService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = (
            "在 Windows 关机流程的 pre-shutdown 阶段，对外置硬盘发送 "
            "FLUSH CACHE + ATA SLEEP 深睡（深睡后盘不响应任何程序，"
            "等同于进入黑名单），保证断电时硬盘已停转；无需卸载卷。"
        )

        def __init__(self, args):
            super().__init__(args)
            self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
            self._park_requested = False
            self._cache = None

        def GetAcceptedControls(self):
            # 关键：接受 SERVICE_CONTROL_PRESHUTDOWN（0x0F）
            accepted = super().GetAcceptedControls()
            accepted |= getattr(win32service, "SERVICE_ACCEPT_PRESHUTDOWN", 0x100)
            return accepted

        def SvcStop(self):
            # 手动停止（sc stop）：直接退出，不做任何事
            logging.info("[Svc] 收到 STOP（手动停止服务）")
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.hWaitStop)

        def SvcShutdown(self):
            # 关机（若系统未发 PRESHUTDOWN 也会走到这里）
            logging.info("[Svc] 收到 SERVICE_CONTROL_SHUTDOWN，准备执行关机停转")
            self._park_requested = True
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=60000)
            win32event.SetEvent(self.hWaitStop)

        def SvcOther(self, control):
            preshutdown = getattr(win32service, "SERVICE_CONTROL_PRESHUTDOWN", 15)
            if control == preshutdown:
                # 应用已全部退出、其他服务尚未停止、文件系统 flush 之前 —— 最正确的时机
                logging.info("[Svc] 收到 SERVICE_CONTROL_PRESHUTDOWN，准备执行关机停转")
                self._park_requested = True
                self.ReportServiceStatus(
                    win32service.SERVICE_STOP_PENDING, waitHint=60000
                )
                win32event.SetEvent(self.hWaitStop)

        def SvcDoRun(self):
            logging.info("[Svc] 服务已启动，等待关机通知（SERVICE_CONTROL_PRESHUTDOWN）...")
            self._cache = _refresh_disk_cache()

            # 开机恢复：把上次关机时离线的外置盘重新上线（卷自动重新挂载）。
            # 离线用 Persist=False 理论上下次开机自动在线，这里兜底执行一次（幂等）。
            try:
                bring_all_external_disks_online(self._cache)
            except Exception as e:
                logging.warning(f"[Svc] 开机恢复执行异常: {e}")

            last_refresh = time.time()

            while True:
                rc = win32event.WaitForSingleObject(self.hWaitStop, 60000)
                if rc == win32event.WAIT_TIMEOUT:
                    # 周期刷新磁盘缓存（关机时 WMI 可能已停，届时直接用缓存）
                    if time.time() - last_refresh >= 300:
                        self._cache = _refresh_disk_cache() or self._cache
                        last_refresh = time.time()
                    continue

                if self._park_requested:
                    try:
                        _park_all_disks(self._cache)
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

    _, query_out, _ = _sc(["query", SERVICE_NAME])
    if "RUNNING" not in (query_out or ""):
        code, out, err = _sc(["start", SERVICE_NAME], timeout=90)
        logging.info(
            f"[Svc] 服务启动结果 {code}: {(out or '').strip()} {(err or '').strip()}"
        )

    return is_service_running()
