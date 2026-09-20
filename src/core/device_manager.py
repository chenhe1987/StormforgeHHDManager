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
                import gc
                gc.collect()
            except Exception:
                pass
            try:
                import pythoncom
                pythoncom.CoFreeUnusedLibraries()
                # 只在后台监控线程反初始化 COM，彻底释放 WMI 提供者持有的磁盘句柄；
                # 避免在主线程(UI/STA)反初始化影响 Qt。
                import threading
                if threading.current_thread() is not threading.main_thread():
                    pythoncom.CoUninitialize()
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
        """发送 FLUSH CACHE + SLEEP 停转命令（不发送 TUR 验证，避免唤醒已休眠的盘）"""
        logging.info(f"正在尝试让磁盘 {disk_index} 进入休眠...")
        try:
            from src.hal.asm_commander import ASMCommander
            with ASMCommander(disk_index, model_hint=model, serial_hint=serial) as cmd:
                if cmd.sleep():
                    logging.info(f"磁盘 {disk_index} SLEEP 命令已发送（桥已接受 FLUSH CACHE + SLEEP）")
                    return True, "硬盘休眠命令已发送"
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

        # 3. 策略 A (优先): Shell Eject
        #    与 Windows 原生安全删除硬件使用相同路径
        #    对 2074 Hub + 多 ASM1153E 桥接的组合最为可靠
        #    注意：**不要**先发 SLEEP。ATA SLEEP(0xE6) 是深睡，必须复位才能唤醒，
        #    先停转会让后续的卷锁定/设备移除因 I/O 无法完成而长时间挂起，
        #    盘还会被控制器复位重新转起来。停转交给设备移除的 QUERYREMOVE 阶段完成。
        spin_down_ok = False  # 停转由设备移除时的 QUERYREMOVE 阶段完成（见 SafeRemovalPatch）
        shell_msg = ""
        if volumes:
            primary_volume = volumes[0]
            logging.info(f"策略 A: 尝试 Shell Eject 弹出卷 {primary_volume}")
            shell_ok, shell_msg = Win32API.eject_volume_by_drive_letter(
                primary_volume,
                expected_volumes=volumes,
                timeout_seconds=10.0,
            )
            if shell_ok:
                total_elapsed = time.perf_counter() - started_at
                logging.info(f"磁盘 {disk_index} 策略 A (Shell Eject) 成功，总耗时 {total_elapsed:.2f} 秒")
                logging.info("=" * 72)
                return True, "设备已安全弹出"

        # 4. 锁定并卸载卷 —— 必须在硬盘仍在转动、能响应 I/O 时完成。
        #    卷锁不住说明有程序占用，直接给出可操作的原因，不继续移除设备。
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
                    "卷预处理失败: " +
                    "; ".join(f"{item['volume']}={item['message']}" for item in volume_failures)
                )
            if not prepared_volumes:
                volume_details = "; ".join(
                    f"{item['volume']} {item['message']}" for item in volume_failures
                )
                combined = (
                    f"磁盘正被占用，无法安全弹出。({volume_details})。"
                    "请关闭占用该磁盘的程序或窗口后重试。"
                )
                occupying_apps = []
                for vol in volumes:
                    vol_root = f"{vol}\\" if not vol.endswith("\\") else vol
                    occupying_apps.extend(Win32API.query_occupying_apps(vol_root))
                if occupying_apps:
                    combined += f"\n可能正在占用该磁盘的程序: {', '.join(sorted(set(occupying_apps)))}"
                logging.error(f"磁盘 {disk_index} 卷锁定失败，放弃弹出: {combined}")
                logging.info("=" * 72)
                return False, combined

        # 5. 策略 B (备选): PnP 设备节点移除（带超时保护）
        #    适用于独立 USB-SATA 桥接芯片，对于 2074 hub 下的子设备可能被否决
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
            logging.info(f"磁盘 {disk_index} 策略 B (PnP 移除) 成功，总耗时 {total_elapsed:.2f} 秒")
            logging.info("=" * 72)
            return True, "设备已安全移除"

        # 6. 所有策略都失败，生成详细错误信息
        total_elapsed = time.perf_counter() - started_at

        messages = []
        if shell_msg:
            messages.append(f"Shell Eject: {shell_msg}")
        messages.append(f"PnP 移除: {pnp_msg}")

        combined = "\n".join(messages)
        combined += "\n\n提示：请关闭可能访问该磁盘的程序（资源管理器窗口、播放器、下载工具）后重试。"

        logging.error(f"磁盘 {disk_index} 弹出最终失败，总耗时 {total_elapsed:.2f} 秒: {combined}")
        logging.info("=" * 72)
        return False, combined

    @staticmethod
    def deep_sleep_disk(disk_index, model=None, serial=None):
        """深度休眠：FLUSH CACHE → ATA SLEEP(0xE6)。

        与“立即休眠硬盘”按钮完全相同的机制：SLEEP 后盘进入最低功耗，
        不响应任何程序（桥接芯片会对后续访问立即回 3A/00 无介质，不会唤醒盘），
        直到重新上电。这正是关机场景要的“黑名单”：不需要卸载卷、不需要锁卷，
        被程序占用也不影响休眠。
        """
        started_at = time.perf_counter()
        try:
            from src.hal.asm_commander import ASMCommander
            with ASMCommander(disk_index, model_hint=model, serial_hint=serial) as cmd:
                flush_ok = cmd.flush_cache(timeout=3)
                sleep_ok = cmd.sleep()
            elapsed = time.perf_counter() - started_at
            if sleep_ok:
                return True, f"已深睡(FLUSH={'OK' if flush_ok else 'FAIL'})", elapsed
            return False, "ATA SLEEP 失败", elapsed
        except Exception as e:
            elapsed = time.perf_counter() - started_at
            logging.warning(f"磁盘 {disk_index} 深睡异常: {e}")
            return False, f"异常: {e}", elapsed

    @staticmethod
    def flush_volumes_for_disk(disk_index, timeout_seconds=2.0):
        """关机前把该盘所有卷的文件系统缓存刷到盘上（FlushFileBuffers）。

        只做 flush，不 lock/dismount：关机流程中锁卷会干扰 Windows 自己的卸载流程。
        """
        deadline = time.perf_counter() + timeout_seconds
        flushed = []
        try:
            volumes = DeviceManager.get_volumes_for_disk(disk_index)
        except Exception as e:
            logging.debug(f"磁盘 {disk_index} 卷枚举失败: {e}")
            return flushed

        for vol in volumes or []:
            if time.perf_counter() >= deadline:
                logging.warning(f"磁盘 {disk_index} 卷 flush 超时，跳过剩余卷")
                break
            try:
                handle = Win32API.open_volume(vol)
                if not handle:
                    continue
                try:
                    ok, err = Win32API.flush_volume_buffers(handle)
                    if ok:
                        flushed.append(vol)
                    else:
                        logging.debug(f"卷 {vol} flush 失败 error={err}")
                finally:
                    Win32API.close_handle(handle)
            except Exception as e:
                logging.debug(f"卷 {vol} flush 异常: {e}")
        return flushed

    @staticmethod
    def prepare_disk_for_shutdown(disk_index, model=None, serial=None, verify=False):
        """关机/系统休眠前的停转：FLUSH CACHE → STANDBY IMMEDIATE。

        与“立即休眠硬盘”按钮不同，这里**绝不使用 ATA SLEEP**，因为关机流程后面
        还会有服务停止/文件系统 flush/断电等动作，SLEEP 深睡会让这些 I/O 挂起，
        导致控制器复位硬盘（盘重新旋转）→ 断电时磁头紧急回收 → C0 计数 +1。
        STANDBY IMMEDIATE 是可恢复停转（磁头已卸载、马达已停），
        且正是 Windows 自己对硬盘停转时使用的命令。

        verify=True 时，STANDBY 之后等 1 秒再用 TEST UNIT READY 探测一次，
        用于判断停转命令是否真的被 USB-SATA 桥传递给了硬盘
        （历史笔记里曾怀疑 ASMT 桥会过滤 STANDBY IMMEDIATE，需要实测证据）。
        探测结果只写日志，用于事后判断策略是否真的生效。
        """
        started_at = time.perf_counter()
        try:
            from src.hal.asm_commander import ASMCommander
            with ASMCommander(disk_index, model_hint=model, serial_hint=serial) as cmd:
                flush_ok = cmd.flush_cache(timeout=3)
                standby_ok = cmd.standby_immediate(timeout=5)

                if standby_ok and verify:
                    time.sleep(1.0)
                    still_ready = cmd.is_responding()
                    elapsed = time.perf_counter() - started_at
                    if still_ready:
                        msg = (f"STANDBY 已被桥接受，但 1 秒后 TUR 仍有响应 → "
                               f"盘可能仍在旋转（桥可能未传递停转命令）")
                        logging.warning(f"磁盘 {disk_index} 停转校验: {msg}")
                        return True, msg + f"(FLUSH={'OK' if flush_ok else 'FAIL'})", elapsed
                    msg = "已确认停转(FLUSH=%s, TUR 无响应)" % ("OK" if flush_ok else "FAIL")
                    logging.info(f"磁盘 {disk_index} 停转校验: {msg}")
                    return True, msg, elapsed

            elapsed = time.perf_counter() - started_at
            if standby_ok:
                return True, f"已停转(FLUSH={'OK' if flush_ok else 'FAIL'})", elapsed
            return False, "STANDBY IMMEDIATE 失败", elapsed
        except Exception as e:
            elapsed = time.perf_counter() - started_at
            logging.warning(f"磁盘 {disk_index} 关机停转异常: {e}")
            return False, f"异常: {e}", elapsed

if __name__ == "__main__":
    # Test disk enumeration
    manager = DeviceManager()
    disks = manager.get_physical_disks()
    for disk in disks:
        print(disk)
