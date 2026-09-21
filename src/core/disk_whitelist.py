"""Stable allow-list identity for physical disks managed by this application."""


_UNKNOWN_SERIALS = {"", "UNKNOWN", "NONE", "N/A", "NA", "NULL", "0"}


def disk_id(disk):
    if isinstance(disk, dict):
        pnp = disk.get('pnp_id') or ''
        serial = disk.get('bridge_serial') or disk.get('serial_number') or disk.get('serial') or ''
    else:
        pnp = getattr(disk, 'pnp_id', '') or ''
        serial = getattr(disk, 'serial_number', '') or ''
    pnp = str(pnp).strip().upper()
    serial = ''.join(str(serial).split()).upper()
    if not pnp or serial in _UNKNOWN_SERIALS:
        return ''
    return pnp + '|' + serial


def is_external_disk(disk):
    """Only offer management for USB/UASP external disks, never internal disks."""
    if isinstance(disk, dict):
        removable = bool(disk.get('is_removable', False))
        pnp = disk.get('pnp_id') or ''
    else:
        removable = bool(getattr(disk, 'is_removable', False))
        pnp = getattr(disk, 'pnp_id', '') or ''
    pnp = str(pnp).upper()
    return removable or any(token in pnp for token in ('USB', 'UASP'))
