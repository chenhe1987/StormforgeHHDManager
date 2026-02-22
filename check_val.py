import ctypes
from src.hal.win32_api import INVALID_HANDLE_VALUE
print(f"INVALID_HANDLE_VALUE: {INVALID_HANDLE_VALUE}")
print(f"Hex: {hex(INVALID_HANDLE_VALUE & 0xFFFFFFFFFFFFFFFF)}")
