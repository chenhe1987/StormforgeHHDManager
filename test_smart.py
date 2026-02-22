from src.core.device_manager import DeviceManager
from src.hal.asm_commander import ASMCommander
from src.utils.admin import is_admin, run_as_admin
import sys

def test():
    if not is_admin():
        print("正在尝试以管理员权限重新启动...")
        if not run_as_admin():
            sys.exit(0)
        return

    print("正在扫描硬盘...")
    disks = DeviceManager.get_physical_disks()
    
    if not disks:
        print("未发现硬盘。请确保以管理员权限运行。")
        return

    for disk in disks:
        print(f"\n检查硬盘: {disk.model} (SN: {disk.serial_number}, Index: {disk.index})")
        with ASMCommander(disk.index) as cmd:
            smart_data = cmd.get_smart_data()
            if smart_data:
                print(f"成功获取 SMART 数据 (长度: {len(smart_data)} 字节)")
                # 打印前 16 字节作为示例
                print(f"数据预览: {smart_data[:16].hex(' ')}")
            else:
                print("获取 SMART 数据失败。该硬盘可能不支持 SAT 或不是 ASM1153E 芯片。")

if __name__ == "__main__":
    test()
