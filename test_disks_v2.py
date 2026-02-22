
import logging
import wmi
import ctypes
import sys
import os

# Add project root to sys.path
sys.path.append(os.getcwd())

from src.core.device_manager import DeviceManager
from src.hal.asm_commander import ASMCommander
from src.utils.smart_parser import SmartParser
from src.utils.admin import is_admin, run_as_admin

logging.basicConfig(
    level=logging.DEBUG, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='test_output.log',
    filemode='w'
)

def log_print(msg):
    print(msg)
    logging.info(msg)

def test_nvme_detection():
    if not is_admin():
        log_print("Not admin, elevating...")
        if run_as_admin():
            log_print("Elevation successful")
        else:
            log_print("Elevation failed or user cancelled")
        return

    log_print("--- 磁盘检测测试 (Admin) ---")
    disks = DeviceManager.get_physical_disks()
    for disk in disks:
        log_print(f"\n磁盘 {disk.index}:")
        log_print(f"  Model: {disk.model}")
        log_print(f"  Interface: {disk.interface_type}")
        log_print(f"  SN: {disk.serial_number}")
        
        is_nvme = (disk.interface_type == "NVMe" or 
                  (disk.model and "NVME" in disk.model.upper()) or 
                  (disk.model and "SN580" in disk.model.upper()) or 
                  (disk.model and "980 PRO" in disk.model.upper()) or
                  (disk.model and "SSD" in disk.model.upper() and disk.interface_type == "IDE"))
        
        log_print(f"  Is detected as NVMe: {is_nvme}")
        
        with ASMCommander(disk.index) as cmd:
            if is_nvme:
                log_print("  尝试 NVMe SMART 读取...")
                raw_data = cmd.get_nvme_smart_data()
                if raw_data:
                    log_print("  NVMe SMART 读取成功!")
                    attrs = SmartParser.parse_nvme(raw_data)
                    summary = SmartParser.get_summary(attrs)
                    log_print(f"  温度: {summary['temp']}°C, 状态: {summary['status']}")
                else:
                    log_print("  NVMe SMART 读取失败")
            
            log_print("  尝试 SATA SMART 读取...")
            raw_data = cmd.get_smart_data()
            if raw_data:
                log_print("  SATA SMART 读取成功!")
                attrs = SmartParser.parse_512(raw_data)
                summary = SmartParser.get_summary(attrs)
                log_print(f"  温度: {summary['temp']}°C, 状态: {summary['status']}")
            else:
                log_print("  SATA SMART 读取失败")

if __name__ == "__main__":
    test_nvme_detection()
