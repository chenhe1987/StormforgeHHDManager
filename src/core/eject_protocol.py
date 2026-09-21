"""Fail-closed SLEEP/eject transaction with no OS dependencies.

The backend must freeze application I/O before run(), validate a dedicated USB
target, enumerate ALL its volumes, and provide owned, checked handles.
SLEEP acceptance is NOT evidence of physical spindle speed.
"""
from dataclasses import dataclass, asdict


@dataclass
class Outcome:
    state: str = "unchanged"
    sleep_attempted: bool = False
    sleep_accepted: bool = False
    ejected: bool = False
    physical_stop_verified: bool = False
    error: str = ""


def run(backend, eject=True):
    result = Outcome()
    disk = None
    volumes = []
    offline_attempted = False
    try:
        backend.validate()
        disk = backend.open_disk()
        # Acquire every lock before dismounting ANY volume. Error 5 is a failure,
        # not evidence that a filesystem doesn't support locking.
        for name in backend.volumes:
            handle = backend.open_volume(name)
            volumes.append(handle)
            backend.lock(handle)
        for handle in volumes:
            backend.flush_volume(handle)
            backend.dismount(handle)
        offline_attempted = True
        backend.offline(disk, True)
        backend.verify_offline(disk)
        backend.flush_disk(disk)
        while volumes:
            backend.close(volumes[-1])
            volumes.pop()
        result.state = "isolated"
        # Journal BEFORE submitting the irreversible-to-normal-I/O command.
        # Even a failed IOCTL may mean the disk accepted it before timing out.
        result.sleep_attempted = True
        backend.record("sleep_dispatch", asdict(result))
        backend.sleep(disk)
        result.sleep_accepted = True
        result.state = "sleep_accepted_offline"
        backend.close(disk)
        disk = None
        if eject:
            backend.eject()
            result.ejected = True
            result.state = "ejected_sleep_accepted"
    except Exception as exc:
        result.error = str(exc)
        if result.sleep_attempted:
            if not result.sleep_accepted:
                result.state = "sleep_unknown_offline"
            # Never remount, poll TUR/SMART, or reset after an E6 attempt.
        elif offline_attempted and disk is not None:
            try:
                while volumes:
                    backend.close(volumes[-1])
                    volumes.pop()
                backend.offline(disk, False)
                result.state = "restored_online"
            except Exception as rollback:
                result.state = "recovery_required"
                result.error += f"; rollback failed: {rollback}"
    finally:
        for handle in volumes + ([disk] if disk is not None else []):
            try:
                backend.close(handle)
            except Exception as exc:
                result.state = "recovery_required"
                result.error += f"; close failed: {exc}"
        backend.record("outcome", asdict(result))
    return result
