"""Allow-list shutdown flow with no broadcast, reset, or SLEEP retry."""
from contextlib import contextmanager
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import time
from datetime import datetime, timezone

from src.core.eject_protocol import run
from src.hal.eject_backend import inventory, WindowsBackend
from src.core.disk_whitelist import is_external_disk
from src.utils.paths import get_base_path


def state_path():
    return Path(get_base_path()) / 'safe_shutdown_state.json'


def load_state():
    path = state_path()
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def save_state(state):
    path = state_path()
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


@contextmanager
def exclusive():
    import win32event
    import win32api
    mutex = win32event.CreateMutex(None, False, r'Global\JiFengZhiSafeShutdown')
    held = False
    try:
        rc = win32event.WaitForSingleObject(mutex, 0)
        held = rc in (0, 0x80)
        if not held:
            raise RuntimeError('另一个停转事务正在执行，请等待完成。')
        yield
    finally:
        if held:
            win32event.ReleaseMutex(mutex)
        win32api.CloseHandle(mutex)


def enroll(identity):
    """Validate an explicitly allow-listed external disk without touching its volumes."""
    from src.core.config_manager import ConfigManager
    from src.core.disk_whitelist import disk_id
    if not is_external_disk(identity) or disk_id(identity) not in ConfigManager().get_managed_disk_whitelist():
        raise RuntimeError('磁盘不在外置硬盘管理白名单中。')
    with exclusive():
        snapshot = inventory(disk_index=identity['index'])
        backend = WindowsBackend(snapshot, identity['serial'], lambda *a: None,
                                 owner_pid=os.getpid(), expected_pnp=identity['pnp_id'])
        backend.validate()
        h = backend.open_disk()
        backend.close(h)
        save_state({'target': identity, 'phase': 'armed'})


def current_boot_id():
    import ctypes
    uptime_ms = ctypes.windll.kernel32.GetTickCount64()
    return str(int(time.time() - uptime_ms / 1000))


def arm_whitelist(ids):
    boot_id = current_boot_id()
    allowed = sorted(set(ids or []))
    prior = load_state()
    if (prior.get('boot_id') == boot_id
            and prior.get('phase') not in ('armed', '')):
        # Checkbox changes must not re-arm a disk after an ambiguous/offline
        # transaction in the same boot. A reboot is the reset boundary.
        prior['managed_disk_whitelist'] = allowed
        save_state(prior)
        return
    state = {'phase': 'armed', 'boot_id': boot_id,
             'managed_disk_whitelist': allowed}
    save_state(state)


def park(monitor=None):
    """Park every explicitly whitelisted disk, stopping safely on first failure."""
    with exclusive():
        base = Path(get_base_path())
        manifest = json.loads((base / 'shutdown_disks.json').read_text(encoding='utf-8'))
        whitelist = set(manifest.get('managed_disk_whitelist') or [])
        armed = load_state().get('managed_disk_whitelist')
        if armed is not None and set(armed) != whitelist:
            return {'ok': False, 'error': '关机服务白名单与当前 GUI 快照不一致，拒绝操作。'}
        targets = [dict(index=d.get('index'), serial=d.get('serial_number'),
                        pnp_id=d.get('pnp_id'), model=d.get('model'))
                   for d in manifest.get('disks', [])
                   if d.get('managed_id') in whitelist and d.get('index') is not None
                   and is_external_disk(d)
                   and d.get('serial_number') and d.get('pnp_id')]
        if not targets:
            return {'ok': False, 'error': '管理白名单中没有可停转的硬盘。'}
        program_drive = base.drive.rstrip(':').upper()
        boot_id = current_boot_id()
        prior = load_state()
        if prior.get('boot_id') == boot_id and prior.get('phase') not in ('armed', ''):
            return {'ok': False, 'error': '本次开机已执行或尝试过停转，已阻止重复访问。'}

        state = {'phase': 'running', 'boot_id': boot_id,
                 'managed_disk_whitelist': sorted(whitelist),
                 'targets': [{'identity': t, 'phase': 'pending'} for t in targets]}
        save_state(state)
        deadline = time.monotonic() + 240
        outcomes = []
        for row in state['targets']:
            identity = row['identity']
            index = identity['index']
            try:
                snapshot = inventory(disk_index=index)
                if snapshot['disk']['SerialNumber'].strip() != identity['serial']:
                    raise RuntimeError('磁盘身份变化，拒绝停转。')
                roots = [str(v.get('DriveLetter') or '').upper() for v in snapshot['volumes']]
                if program_drive in roots:
                    raise RuntimeError('程序或关机日志位于目标盘，拒绝停转。')

                def record(event, data, slot=row, ident=identity):
                    logging.info('[SafeShutdown] disk=%s event=%s data=%s', ident['index'], event, data)
                    if event == 'sleep_dispatch':
                        if time.monotonic() >= deadline:
                            raise RuntimeError('停转准备超过 240 秒，未发送 SLEEP。')
                        slot['phase'] = 'sleep_dispatch'
                        save_state(state)
                    elif event == 'outcome':
                        slot['outcome'] = data
                        slot['phase'] = ('sleep_accepted' if succeeded(data) else
                                         'isolated' if data['sleep_attempted'] or data['state'] == 'recovery_required'
                                         else 'failed_before_sleep')
                        save_state(state)

                backend = WindowsBackend(snapshot, identity['serial'], record,
                                         owner_pid=os.getpid(), expected_pnp=identity['pnp_id'])
                result = asdict(run(backend, eject=False))
                row['outcome'] = result
                outcomes.append(result)
                if not succeeded(result):
                    state['phase'] = 'blocked' if result['sleep_attempted'] or any(
                        succeeded(x) for x in outcomes[:-1]) else 'armed'
                    state['error'] = f"磁盘 {index}: {result['error']}"
                    save_state(state)
                    return {'ok': False, 'error': state['error'], 'results': outcomes}
                row['phase'] = 'sleep_accepted'
                save_state(state)
            except Exception as exc:
                row['phase'] = 'failed_before_sleep'
                row['error'] = str(exc)
                state['phase'] = 'blocked' if any(succeeded(x) for x in outcomes) else 'armed'
                state['error'] = f"磁盘 {index}: {exc}"
                save_state(state)
                return {'ok': False, 'error': state['error'], 'results': outcomes}

        state['phase'] = 'sleep_accepted'
        state['completed_at'] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return {'ok': True, 'results': outcomes}


def succeeded(result):
    return (result.get('sleep_accepted') is True and
            result.get('state') == 'sleep_accepted_offline' and not result.get('error'))


def request_shutdown(result):
    if not result.get('ok'):
        raise RuntimeError('安全停转未成功，不发起关机。')
    import subprocess
    # No /f: never force applications closed. Shutdown cancellation is possible.
    completed = subprocess.run(['shutdown.exe', '/s', '/t', '0'],
                               capture_output=True, timeout=15,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    if completed.returncode:
        raise RuntimeError('Windows 拒绝关机请求；目标盘保持离线，请重新连接后恢复。')
