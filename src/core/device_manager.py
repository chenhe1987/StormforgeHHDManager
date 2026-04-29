import wmi
import logging
import os
import time
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
    def spin_down_disk(disk_index, model=None, serial=None):
        """仅发送停转命令，不弹出设备"""
        logging.info(f"正在尝试让磁盘 {disk_index} 进入休眠...")
        try:
            from src.hal.asm_commander import ASMCommander
            with ASMCommander(disk_index, model_hint=model, serial_hint=serial) as cmd:
                # 使用 sleep() 代替 spin_down() 以实现更彻底的停转
                if cmd.sleep():
                    logging.info(f"磁盘 {disk_index} 休眠命令发送成功")
                    return True, "硬盘已进入休眠状态"
                else:
                    logging.warning(f"磁盘 {disk_index} 休眠命令发送失败")
                    return False, "休眠命令发送失败"
        except Exception as e:
            logging.error(f"磁盘 {disk_index} 休眠操作发生异常: {e}")
            return False, f"操作异常: {e}"

    @staticmethod
    def set_standby_timer(disk_index, minutes, model=None, serial=None):
        """设置硬盘的自动休眠时间"""
        try:
            minutes = int(minutes)
            logging.info(f"正在尝试设置磁盘 {disk_index} 的休眠时间为 {minutes} 分钟...")
            from src.hal.asm_commander import ASMCommander
            with ASMCommander(disk_index, model_hint=model, serial_hint=serial) as cmd:
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
    def safe_eject_disk(disk_index, model=None, serial=None):
        """先尝试停转，再按 Windows 安全删除硬件的思路弹出设备。"""
        started_at = time.perf_counter()
        logging.info("=" * 72)
        logging.info(
            f"开始安全弹出磁盘: index={disk_index}, model={model or 'Unknown'}, serial={serial or 'Unknown'}"
        )

        # 1. 获取该物理盘对应的设备实例 ID
        instance_id = Win32API.get_device_instance_path(disk_index)
        if not instance_id:
            logging.error(f"无法获取磁盘 {disk_index} 的设备实例 ID")
            logging.info("=" * 72)
            return False, "无法识别设备节点，请尝试重新扫描设备。"

        logging.info(f"磁盘 {disk_index} 的实例 ID: {instance_id}")

        # 2. 仅记录卷信息，卷释放交给 Windows 原生 eject 处理，避免手动锁卷造成长时间阻塞
        volumes = DeviceManager.get_volumes_for_disk(disk_index)
        if volumes:
            logging.info(f"磁盘 {disk_index} 当前关联卷: {', '.join(volumes)}")
        else:
            logging.info(f"磁盘 {disk_index} 上未发现活动卷")

        # 释放 WMI COM 对象，防止它们占用卷句柄导致后续的锁卷和弹出失败
        import gc
        gc.collect()
        try:
            import pythoncom
            pythoncom.CoFreeUnusedLibraries()
        except Exception:
            pass

        prepared_volumes = []
        volume_failures = []

        # 3. 对关联卷做短超时 lock + dismount，尽量还原 Windows 安全删除前的卷处理状态。
        if volumes:
            prepared_volumes, volume_failures = Win32API.prepare_volumes_for_safe_removal(
                volumes,
                lock_timeout_seconds=3.0,
            )
            if prepared_volumes:
                logging.info(
                    "卷预处理成功: " + ", ".join(item["volume"] for item in prepared_volumes)
                )
            if volume_failures:
                logging.warning(
                    "部分卷预处理失败: " +
                    "; ".join(f"{item['volume']}={item['message']}" for item in volume_failures)
                )

        # 4. 走更接近“安全删除硬件”的设备节点 eject。
        try:
            logging.info(f"正在按 Windows 安全删除硬件策略请求移除设备节点: {instance_id}")
            # Ensure any WMI COM objects are released before calling eject
            import pythoncom
            try:
                pythoncom.CoFreeUnusedLibraries()
            except Exception:
                pass
            
            success, message = Win32API.eject_device_by_instance_id(instance_id)
        finally:
            Win32API.release_prepared_volumes(prepared_volumes)

        total_elapsed = time.perf_counter() - started_at
        if success:
            logging.info(f"磁盘 {disk_index} 安全弹出成功，总耗时 {total_elapsed:.2f} 秒")
            logging.info("=" * 72)
            return True, "设备已按 Windows 原生方式安全弹出"

        if volume_failures and ("STORAGE\\Volume" in message or "被系统或其他程序占用" in message or "不可卸载" in message):
            # 将复杂的日志信息简化，让用户更容易理解
            volume_details = "; ".join(f"{item['volume']} 仍被占用" for item in volume_failures)
            message = f"磁盘正被占用，无法安全弹出。({volume_details})。请关闭占用该磁盘的程序或窗口（如资源管理器）后重试。"
        
        if not success and ("占用" in message or "STORAGE\\Volume" in message or "不可卸载" in message) and volumes:
            # 尝试通过 Restart Manager 获取到底是什么程序在占用
            occupying_apps = []
            try:
                from src.utils.restart_manager import get_locking_processes
                for vol in volumes:
                    vol_root = f"{vol}\\" if not vol.endswith("\\") else vol
                    apps = get_locking_processes(vol_root)
                    if apps:
                        occupying_apps.extend(apps)
            except Exception as rm_err:
                logging.debug(f"获取占用程序失败: {rm_err}")
                
            if occupying_apps:
                unique_apps = list(set(occupying_apps))
                message += f"\n可能正在占用该磁盘的程序: {', '.join(unique_apps)}"
                
        logging.error(f"磁盘 {disk_index} 弹出最终失败，总耗时 {total_elapsed:.2f} 秒: {message}")
        logging.info(f"安全弹出失败: index={disk_index}, total={total_elapsed:.2f}s, reason={message}")
        logging.info("=" * 72)
        return False, message

if __name__ == "__main__":
    # Test disk enumeration
    manager = DeviceManager()
    disks = manager.get_physical_disks()
    for disk in disks:
        print(disk)
