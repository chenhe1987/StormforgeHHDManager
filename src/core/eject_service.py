"""One serialized transaction for GUI and Windows native tray requests."""
from dataclasses import asdict
import logging
import os

from src.core.eject_protocol import Outcome, run
from src.hal.eject_backend import inventory, WindowsBackend
from src.core.disk_whitelist import disk_id, is_external_disk


def execute_eject(monitor, identity, progress=None):
    index, pnp = identity["index"], identity["pnp_id"]
    result = Outcome()
    if not is_external_disk(identity):
        result.error = "仅允许管理 USB/UASP 外置硬盘，拒绝弹出。"
        return asdict(result)
    if disk_id(identity) not in monitor.config_manager.get_managed_disk_whitelist():
        result.error = "磁盘不在管理白名单中，拒绝弹出。"
        return asdict(result)
    if not monitor.scan_lock.acquire(timeout=30):
        result.error = "后台磁盘访问尚未结束，本次未发送停转命令。"
        return asdict(result)
    try:
        if monitor.shutdown_mode:
            raise RuntimeError("系统正在关机/休眠，本次不启动弹出事务。")
        if pnp.upper() in monitor.eject_quarantine:
            raise RuntimeError("磁盘仍处于上次停转后的隔离状态，不能重复访问。请重新连接设备后恢复。")
        snapshot = inventory(disk_index=index)
        def record(event, data):
            logging.info("[EjectTransaction] disk=%s event=%s data=%s", index, event, data)
            if progress:
                progress(event)
            # Persist intent before E6: a crash or timeout must not restart SMART.
            if event == "sleep_dispatch":
                monitor.quarantine_eject(pnp)
        backend = WindowsBackend(snapshot, identity["serial"], record,
                                 owner_pid=os.getpid(), expected_pnp=pnp)
        result = run(backend)
        if result.ejected:
            monitor.mark_disk_ejected(identity.get("ui_serial") or identity["serial"], index)
            monitor.eject_quarantine.discard(pnp.upper())
            monitor.config_manager.config["eject_quarantine"] = sorted(monitor.eject_quarantine)
            monitor.config_manager.save_config()
        elif result.sleep_attempted or result.state == "recovery_required":
            monitor.quarantine_eject(pnp)
        return asdict(result)
    except Exception as exc:
        result.error = str(exc)
        logging.exception("[EjectTransaction] preflight failed: disk=%s", index)
        return asdict(result)
    finally:
        monitor.scan_lock.release()
