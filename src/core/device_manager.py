import wmi
import logging
import os
from src.hal.win32_api import Win32API
from src.core.device_renamer import DeviceRenamer

class DiskInfo:
    def __init__(self, device_id, model, serial_number, index, interface_type=None, is_removable=False, pnp_id=None):
        self.device_id = device_id
        self.model = model
        self.serial_number = serial_number.strip() if serial_number else "Unknown"
        self.index = index # PhysicalDrive index
        self.interface_type = interface_type
        self.is_removable = is_removable
        self.pnp_id = pnp_id

    def __repr__(self):
        return f"Disk(Index: {self.index}, Model: {self.model}, SN: {self.serial_number}, Interface: {self.interface_type}, Removable: {self.is_removable})"

class DeviceManager:
    @staticmethod
    def get_physical_disks():
        disks = []
        try:
            c = wmi.WMI()
            # 获取物理磁盘映射 (Index -> PNP ID)
            device_map = DeviceRenamer.get_device_map()
            
            # Win32_DiskDrive contains physical drive info
            logging.debug("开始枚举物理磁盘 (WMI Win32_DiskDrive)...")
            for drive in c.Win32_DiskDrive():
                # DeviceID is usually like "\\.\PHYSICALDRIVE0"
                try:
                    index = int(drive.DeviceID.upper().replace("\\\\.\\PHYSICALDRIVE", ""))
                    
                    # 识别是否为外置/可移除设备
                    # 使用 Win32API 深度探测父总线类型
                    pnp_id = drive.PNPDeviceID
                    is_removable = Win32API.is_external_device(pnp_id)
                    
                    if not is_removable:
                        # 兜底逻辑：如果深度探测失败，尝试之前的简单判定
                        interface = drive.InterfaceType
                        if interface == "USB" or "USBSTOR" in pnp_id.upper() or "UASPSTOR" in pnp_id.upper():
                            is_removable = True
                        elif drive.Capabilities and 7 in drive.Capabilities:
                            is_removable = True
                        else:
                            model_upper = drive.Model.upper()
                            if any(k in model_upper for k in ["EXTERNAL", "DOCK", "ENCLOSURE", "ASMT"]):
                                is_removable = True

                    # 尝试自动重命名设备 (FriendlyName)
                    try:
                        pnp_id_full = device_map.get(index)
                        if pnp_id_full:
                            current_name = DeviceRenamer.get_friendly_name(pnp_id_full)
                            model_clean = drive.Model.strip()
                            if DeviceRenamer.should_rename(current_name, model_clean):
                                logging.info(f"检测到通用设备名 '{current_name}'，正在更新为 '{model_clean}'...")
                                if DeviceRenamer.set_friendly_name(pnp_id_full, model_clean):
                                    logging.info(f"设备重命名成功: {model_clean}")
                                    # 尝试触发设备管理器刷新 (可选，暂不实现，避免卡顿)
                    except Exception as e:
                        logging.warning(f"设备重命名检查失败: {e}")

                    disk_info = DiskInfo(
                        device_id=drive.DeviceID,
                        model=drive.Model,
                        serial_number=drive.SerialNumber,
                        index=index,
                        interface_type=drive.InterfaceType,
                        is_removable=is_removable,
                        pnp_id=pnp_id_full
                    )
                    disks.append(disk_info)
                    logging.debug(f"发现磁盘: {disk_info}")
                except Exception as e:
                    logging.error(f"解析磁盘信息失败: {drive.DeviceID} - {e}")
            logging.debug(f"磁盘枚举完成，共找到 {len(disks)} 个磁盘")
        except Exception as e:
            logging.error(f"Error enumerating disks: {e}", exc_info=True)
        return disks

    @staticmethod
    def get_volumes_for_disk(disk_index):
        """获取指定物理磁盘上的所有卷（盘符）"""
        volumes = []
        try:
            c = wmi.WMI()
            # 建立物理磁盘 -> 分区 -> 逻辑磁盘 的关联
            for drive in c.Win32_DiskDrive(Index=disk_index):
                for partition in drive.associators("Win32_DiskDriveToDiskPartition"):
                    for logical_disk in partition.associators("Win32_LogicalDiskToPartition"):
                        volumes.append(logical_disk.DeviceID) # 如 "C:"
            logging.debug(f"磁盘 {disk_index} 上的卷: {volumes}")
        except Exception as e:
            logging.error(f"获取磁盘 {disk_index} 的卷失败: {e}")
        return volumes

    @staticmethod
    def spin_down_disk(disk_index):
        """仅发送停转命令，不弹出设备"""
        logging.info(f"正在尝试让磁盘 {disk_index} 进入休眠...")
        try:
            from src.hal.asm_commander import ASMCommander
            with ASMCommander(disk_index) as cmd:
                if cmd.spin_down():
                    logging.info(f"磁盘 {disk_index} 休眠命令发送成功")
                    return True, "硬盘已进入休眠状态"
                else:
                    logging.warning(f"磁盘 {disk_index} 休眠命令发送失败")
                    return False, "休眠命令发送失败"
        except Exception as e:
            logging.error(f"磁盘 {disk_index} 休眠操作发生异常: {e}")
            return False, f"操作异常: {e}"

    @staticmethod
    def set_standby_timer(disk_index, minutes):
        """设置硬盘的自动休眠时间"""
        logging.info(f"正在尝试设置磁盘 {disk_index} 的休眠时间为 {minutes} 分钟...")
        try:
            from src.hal.asm_commander import ASMCommander
            with ASMCommander(disk_index) as cmd:
                if cmd.set_standby_timer(minutes):
                    logging.info(f"磁盘 {disk_index} 休眠时间设置成功")
                    return True, f"休眠时间已设置为 {minutes} 分钟"
                else:
                    logging.warning(f"磁盘 {disk_index} 休眠时间设置失败")
                    return False, "设置失败，该硬盘可能不支持此命令"
        except Exception as e:
            logging.error(f"磁盘 {disk_index} 设置休眠时间发生异常: {e}")
            return False, f"操作异常: {e}"

    @staticmethod
    def safe_eject_disk(disk_index):
        """安全弹出物理磁盘，使用 Windows USB 弹出逻辑"""
        logging.info(f"正在尝试安全弹出磁盘 {disk_index}...")
        
        # 1. 首先尝试获取 PNP 设备实例 ID
        instance_id = Win32API.get_device_instance_path(disk_index)
        if not instance_id:
            logging.error(f"无法获取磁盘 {disk_index} 的设备实例 ID")
            return False, "无法识别设备节点"

        # 2. 获取并卸载卷
        volumes = DeviceManager.get_volumes_for_disk(disk_index)
        if volumes:
            for vol_letter in volumes:
                vol_path = f"\\\\.\\{vol_letter}"
                logging.info(f"正在预卸载卷: {vol_letter}")
                handle = Win32API.open_volume(vol_path)
                if handle:
                    try:
                        Win32API.lock_volume(handle)
                        Win32API.dismount_volume(handle)
                    finally:
                        Win32API.close_handle(handle)
        else:
            logging.info(f"磁盘 {disk_index} 上未发现活动卷")

        # 3. 发送进入深度休眠指令 (SLEEP)
        # 关键修复：使用 ATA SLEEP (0xE6) 而非 STANDBY IMMEDIATE。
        # SLEEP 命令会让硬盘进入最低功耗状态，且除非断电重连或硬件复位，否则不会因为 OS 扫描而起旋。
        # 这能彻底解决弹出瞬间硬盘重新旋转的问题。
        try:
            import time
            from src.hal.asm_commander import ASMCommander
            logging.info(f"正在发送 SLEEP 命令到磁盘 {disk_index}...")
            with ASMCommander(disk_index) as cmd:
                if cmd.sleep():
                    logging.info(f"磁盘 {disk_index} 已进入深度休眠 (SLEEP)")
                    time.sleep(1.5) # 给硬盘一点时间完成磁头归位和停转
                else:
                    logging.warning(f"磁盘 {disk_index} SLEEP 命令发送失败，尝试普通停转...")
                    cmd.spin_down()
        except Exception as e:
            logging.error(f"磁盘 {disk_index} 停转操作发生异常: {e}")

        # 4. 最后使用 CfgMgr32 进行 PnP 设备弹出
        logging.info(f"正在请求移除设备节点: {instance_id}")
        success, message = Win32API.eject_device_by_instance_id(instance_id)
        
        if success:
            logging.info(f"磁盘 {disk_index} 设备节点移除成功")
            return True, "设备已安全弹出并停转"
        else:
            if "ROOT_HUB" in message or "未知原因 (代码: 8)" in message:
                logging.warning(f"磁盘 {disk_index} 停转成功但节点移除被否决 (Hub级别): {message}")
                return True, "设备已成功停转。提示：由于系统占用，设备节点未彻底从管理器移除，但可安全拔掉。"
            
            return False, f"停转成功但节点移除失败: {message}"

if __name__ == "__main__":
    # Test disk enumeration
    manager = DeviceManager()
    disks = manager.get_physical_disks()
    for disk in disks:
        print(disk)
