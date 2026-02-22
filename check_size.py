import ctypes
from src.hal.win32_api import SCSI_PASS_THROUGH_DIRECT
print(f"Size of SCSI_PASS_THROUGH_DIRECT: {ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT)}")
