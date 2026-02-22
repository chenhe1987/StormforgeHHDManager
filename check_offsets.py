import ctypes
from src.hal.win32_api import SCSI_PASS_THROUGH_DIRECT

def print_offsets(cls):
    print(f"Structure: {cls.__name__} (Size: {ctypes.sizeof(cls)})")
    for field in cls._fields_:
        name = field[0]
        attr = getattr(cls, name)
        print(f"  Field: {name:20} Offset: {attr.offset}")

print_offsets(SCSI_PASS_THROUGH_DIRECT)
