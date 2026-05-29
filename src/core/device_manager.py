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
        finally:
            try:
                del c
            except Exception:
                pass
            try:
                import pythoncom
                pythoncom.CoFreeUnusedLibraries()
            except Exception:
                pass
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
        finally:
            try:
                del c
            except Exception:
                pass
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

        # 2. 记录卷信息
        volumes = DeviceManager.get_volumes_for_disk(disk_index)
        if volumes:
            logging.info(f"磁盘 {disk_index} 当前关联卷: {', '.join(volumes)}")
        else:
            logging.info(f"磁盘 {disk_index} 上未发现活动卷")

        # 释放 WMI COM 对象
        import gc
        gc.collect()
        try:
            import pythoncom
            pythoncom.CoFreeUnusedLibraries()
        except Exception:
            pass

        # 3. 弹出前先发送 FLUSH CACHE + SLEEP，确保磁头归位并停转
        #    对于 2074+1153E 组合，这一步弥补了 Windows 不发送停转命令的缺陷
        spin_down_ok = False
        try:
            from src.hal.asm_commander import ASMCommander
            with ASMCommander(disk_index, model_hint=model, serial_hint=serial) as cmd:
                if cmd.sleep():
                    spin_down_ok = True
                    logging.info(f"磁盘 {disk_index} 弹出前停转成功")
                else:
                    logging.warning(f"磁盘 {disk_index} 弹出前停转命令失败，继续弹出流程")
        except Exception as e:
            logging.warning(f"磁盘 {disk_index} 弹出前停转异常: {e}")

        # 4. 策略 A (优先): Shell Eject
        #    与 Windows 原生安全删除硬件使用相同路径
        #    对 2074 Hub + 多 ASM1153E 桥接的组合最为可靠
        shell_msg = ""
        if volumes:
            primary_volume = volumes[0]
            logging.info(f"策略 A: 尝试 Shell Eject 弹出卷 {primary_volume}")
            shell_ok, shell_msg = Win32API.eject_volume_by_drive_letter(
                primary_volume,
                expected_volumes=volumes,
                timeout_seconds=15.0,
            )
            if shell_ok:
                total_elapsed = time.perf_counter() - started_at
                msg = "设备已安全弹出"
                if spin_down_ok:
                    msg += "，硬盘已停转"
                logging.info(f"磁盘 {disk_index} 策略 A (Shell Eject) 成功，总耗时 {total_elapsed:.2f} 秒")
                logging.info("=" * 72)
                return True, msg

        # 5. 策略 B (备选): PnP 设备节点移除
        #    适用于独立 USB-SATA 桥接芯片，对于 2074 hub 下的子设备可能被否决
        prepared_volumes = []
        volume_failures = []

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

        try:
            logging.info(f"策略 B: 按 PnP 设备节点移除策略请求移除设备: {instance_id}")
            try:
                pythoncom.CoFreeUnusedLibraries()
            except Exception:
                pass

            pnp_ok, pnp_msg = Win32API.eject_device_by_instance_id(instance_id)
        finally:
            Win32API.release_prepared_volumes(prepared_volumes)

        if pnp_ok:
            total_elapsed = time.perf_counter() - started_at
            msg = "设备已安全移除"
            if spin_down_ok:
                msg += "，硬盘已停转"
            logging.info(f"磁盘 {disk_index} 策略 B (PnP 移除) 成功，总耗时 {total_elapsed:.2f} 秒")
            logging.info("=" * 72)
            return True, msg

        # 6. 所有策略都失败，生成详细错误信息
        total_elapsed = time.perf_counter() - started_at

        messages = []
        if shell_msg:
            messages.append(f"Shell Eject: {shell_msg}")
        messages.append(f"PnP 移除: {pnp_msg}")

        if spin_down_ok:
            messages.append(
                "虽然 Windows 未能完成设备节点移除，但硬盘已成功停转，"
                "您可以稍后通过系统托盘安全删除硬件完成最终弹出。"
            )

        combined = "\n".join(messages)

        if volume_failures:
            volume_details = "; ".join(f"{item['volume']} 仍被占用" for item in volume_failures)
            combined = f"磁盘正被占用，无法安全弹出。({volume_details})。请关闭占用该磁盘的程序或窗口后重试。"

        if volumes:
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
                combined += f"\n可能正在占用该磁盘的程序: {', '.join(unique_apps)}"

        logging.error(f"磁盘 {disk_index} 弹出最终失败，总耗时 {total_elapsed:.2f} 秒: {combined}")
        logging.info("=" * 72)
        return False, combined

if __name__ == "__main__":
    # Test disk enumeration
    manager = DeviceManager()
    disks = manager.get_physical_disks()
    for disk in disks:
        print(disk)
