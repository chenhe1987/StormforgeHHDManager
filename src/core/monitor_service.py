import time
import threading
import logging
from src.core.device_manager import DeviceManager
from src.core.device_renamer import DeviceRenamer
from src.hal.asm_commander import ASMCommander
from src.utils.smart_parser import SmartParser
from src.core.event_log_monitor import EventLogMonitor
from src.core.history_manager import HistoryManager
from src.core.config_manager import ConfigManager

class MonitorService(threading.Thread):
    def __init__(self, callback_notify=None, callback_update_ui=None):
        super().__init__()
        self.daemon = True
        self.running = True
        self.shutdown_mode = False
        self.callback_notify = callback_notify # Function to call for notifications (title, msg)
        self.callback_update_ui = callback_update_ui # Function to call to update UI data (data_dict)
        
        self.event_log_interval = 5 * 60 # 5 minutes
        
        # Force initial check
        self.last_event_check = 0
        
        self.event_monitor = EventLogMonitor()
        self.history_manager = HistoryManager()
        self.config_manager = ConfigManager()
        
        self.disk_health_status = {} # {serial: summary}
        self.disk_last_check_times = {} # {serial: timestamp}
        self.sleeping_disks = set() # {serial} - Disks that should not be polled
        self.ejected_disks = set() # {serial} - Disks successfully ejected; ignore until manual refresh
        self.cached_disks = []
        self.last_inventory_scan_time = 0
        self.device_inventory_interval = 60
        self.scan_lock = threading.Lock()

    def run(self):
        logging.info("监控服务已启动...")
        # Initial scan immediately
        self.check_all_smart(force=True)
        self.check_event_logs()
        self.last_event_check = time.time()

        while self.running:
            if self.shutdown_mode:
                logging.info("监控服务进入关机静默模式，停止后台检测")
                break

            current_time = time.time()
            
            # 1. Check Event Log (High frequency, no disk wake-up)
            if current_time - self.last_event_check >= self.event_log_interval:
                self.check_event_logs()
                self.last_event_check = current_time
            
            # 2. Check SMART (Per-disk interval)
            # We don't check ALL at once anymore, we check individually if interval expired
            self.check_disks_schedule()
                
            time.sleep(10) # Wake up every 10 seconds to check timers

    def check_event_logs(self):
        errors = self.event_monitor.check_new_errors()
        for err in errors:
            msg = f"系统日志发现磁盘错误!\n来源: {err['source']}\nID: {err['id']}\n时间: {err['time']}"
            logging.warning(msg)
            if self.callback_notify:
                self.callback_notify("磁盘系统错误报警", msg)

    def check_disks_schedule(self):
        """Check each disk independently based on its configured interval"""
        if self.shutdown_mode:
            logging.info("关机静默模式下跳过定时磁盘检测")
            return

        try:
            disks = self._get_cached_or_scan_disks(force=False)
            current_time = time.time()
            
            disks_to_check = []
            
            for disk in disks:
                serial = disk.serial_number
                last_check = self.disk_last_check_times.get(serial, 0)
                interval = self.config_manager.get_disk_interval(serial)
                
                if current_time - last_check >= interval:
                    disks_to_check.append(disk)
            
            if disks_to_check:
                self.check_specific_disks(disks_to_check)
                
        except Exception as e:
            logging.error(f"Schedule check failed: {e}")

    def check_specific_disks(self, disks):
        logging.info(f"执行调度检测: {len(disks)} 个硬盘")
        
        # ... logic similar to check_all_smart but for specific list ...
        # Reuse check_all_smart logic by refactoring it?
        # Let's refactor check_all_smart to accept a list of disks.
        self.check_all_smart(target_disks=disks)

    def _get_cached_or_scan_disks(self, force=False):
        """
        获取磁盘列表。
        只要存在手动休眠的硬盘，后台就完全冻结自动重新枚举，直到用户手动唤醒或执行刷新。
        """
        now = time.time()

        if self.shutdown_mode:
            logging.info("关机静默模式下复用现有磁盘缓存，不再重新枚举")
            return self._filter_excluded_disks(self.cached_disks)

        if self.sleeping_disks and not force:
            logging.info(f"检测到 {len(self.sleeping_disks)} 个手动休眠硬盘，后台停止自动重扫，直接复用缓存")
            return list(self.cached_disks)

        if force or not self.cached_disks or now - self.last_inventory_scan_time >= self.device_inventory_interval:
            self.cached_disks = DeviceManager.get_physical_disks()
            self.last_inventory_scan_time = now
            logging.info(f"更新物理磁盘缓存: {len(self.cached_disks)} 个设备")
        else:
            logging.info(f"复用物理磁盘缓存: {len(self.cached_disks)} 个设备")

        return self._filter_excluded_disks(self.cached_disks)

    def check_all_smart(self, force=False, target_disks=None):
        if not self.scan_lock.acquire(blocking=False):
            if force:
                # If forced (manual refresh), we wait for the lock
                logging.info("等待其他扫描任务完成...")
                self.scan_lock.acquire()
            else:
                logging.info("已有扫描任务在运行，跳过本次调度")
                return
        
        try:
            self._check_all_smart_impl(force, target_disks)
        finally:
            self.scan_lock.release()

    def _check_all_smart_impl(self, force=False, target_disks=None):
        logging.info("执行 SMART 健康检测...")
        if self.shutdown_mode:
            logging.info("关机静默模式下跳过 SMART 健康检测")
            return

        try:
            if target_disks is None:
                disks = self._get_cached_or_scan_disks(force=force)
            else:
                disks = target_disks
                
            logging.info(f"DEBUG: 扫描 {len(disks)} 个物理磁盘")
        except Exception as e:
            logging.error(f"DEBUG: 获取磁盘列表失败: {e}")
            disks = []
        
        # Prepare data for UI update
        ui_data = []
        current_time = time.time()
        
        for disk in disks:
            if self.shutdown_mode:
                logging.info("检测过程中收到关机静默请求，提前结束本轮 SMART 检测")
                break

            serial = disk.serial_number

            if serial in self.ejected_disks:
                logging.info(f"跳过已弹出的硬盘: {disk.model} ({serial})")
                continue
            
            # Check if disk is marked as sleeping
            if serial in self.sleeping_disks and not force:
                logging.info(f"跳过检测处于休眠状态的硬盘: {disk.model} ({serial})")
                # Still add to ui_data but with a "Sleeping" status
                disk_info = {
                    "index": disk.index,
                    "model": disk.model,
                    "serial": serial,
                    "interface": disk.interface_type,
                    "temp": "N/A",
                    "status": "Sleeping",
                    "reallocated": "N/A",
                    "pending": "N/A",
                    "attributes": []
                }
                ui_data.append(disk_info)
                continue

            # Update last check time
            self.disk_last_check_times[serial] = current_time
            
            logging.info(f"DEBUG: 正在检测磁盘 {disk.index} (WMI Model: {disk.model})...")
            
            # Improved NVMe detection
            model_upper = disk.model.upper()
            is_nvme = (disk.interface_type == "NVMe" or 
                      "NVME" in model_upper or 
                      "SN580" in model_upper or 
                      "980 PRO" in model_upper or
                      "990 PRO" in model_upper or
                      "SOLIDIGM" in model_upper or
                      "WD_BLACK" in model_upper or
                      "TIPLUS" in model_upper or
                      "KIOXIA" in model_upper)
                      
            # 根据用户要求，在程序中彻底屏蔽 NVMe 硬盘的展示
            if is_nvme:
                logging.info(f"屏蔽 NVMe 硬盘的展示: {disk.model}")
                continue
            
            logging.info(f"DEBUG: 磁盘 {disk.index} ({disk.model}) Interface: {disk.interface_type}, is_nvme: {is_nvme}")
            
            # Add basic info first
            interface_type = disk.interface_type
            if interface_type == "IDE" and (disk.is_removable or "USB" in (disk.pnp_id or "").upper()):
                interface_type = "USB (SATA)"
            elif "USB" in (disk.interface_type or "").upper():
                interface_type = "USB"
            elif is_nvme:
                interface_type = "NVMe"
            
            disk_info = {
                "index": disk.index,
                "model": disk.model,
                "serial": disk.serial_number,
                "interface": interface_type,
                "is_removable": disk.is_removable,
                "temp": "N/A",
                "status": "Unknown",
                "reallocated": "N/A",
                "pending": "N/A",
                "attributes": [] # Full list of SMART attributes
            }

            try:
                with ASMCommander(disk.index, model_hint=disk.model, serial_hint=disk.serial_number) as cmd:
                    attributes = []
                    
                    # 1. 优先尝试获取真实型号和序列号 (Identify)
                    # 无论是 NVMe 还是 SATA，获取真实型号是第一要务
                    if is_nvme:
                        # 尝试 NVMe Identify
                        nvme_id = cmd.get_nvme_identify()
                        if nvme_id:
                            real_model, real_serial = ASMCommander.parse_nvme_identify_data(nvme_id)
                            if real_model:
                                logging.info(f"DEBUG: NVMe Identify 识别到真实型号: {real_model}")
                                disk_info["model"] = real_model
                                if real_serial and len(real_serial) > 5:
                                    disk_info["serial"] = real_serial
                                    
                                # 自动重命名逻辑
                                try:
                                    if disk.pnp_id and real_model and real_model != "Unknown":
                                        current_friendly = DeviceRenamer.get_friendly_name(disk.pnp_id)
                                        if DeviceRenamer.should_rename(current_friendly, real_model):
                                            logging.info(f"检测到 NVMe 设备名需更新: '{current_friendly}' -> '{real_model}'")
                                            DeviceRenamer.set_friendly_name(disk.pnp_id, real_model)
                                except Exception as e:
                                    logging.warning(f"NVMe 自动重命名尝试失败: {e}")
                        
                        # 2. 尝试获取 NVMe SMART
                        nvme_data = cmd.get_nvme_smart_data()
                        if nvme_data:
                            logging.info(f"DEBUG: 硬盘 {disk.index} NVMe SMART 数据读取成功")
                            attributes = SmartParser.parse_nvme(nvme_data)
                    
                    # 3. 如果不是 NVMe 或 NVMe SMART 读取失败，尝试 SATA/SAT
                    if not attributes:
                        id_data = cmd.identify_device()
                        if id_data:
                            logging.info(f"DEBUG: 硬盘 {disk.index} SATA IDENTIFY 成功")
                            real_model, real_serial = ASMCommander.parse_identify_data(id_data)
                            if real_model:
                                logging.info(f"DEBUG: SATA 识别到真实型号: {real_model}")
                                disk_info["model"] = real_model
                                if real_serial and len(real_serial) > 5:
                                    disk_info["serial"] = real_serial

                                # 自动重命名逻辑
                                try:
                                    if disk.pnp_id and real_model and real_model != "Unknown":
                                        current_friendly = DeviceRenamer.get_friendly_name(disk.pnp_id)
                                        if DeviceRenamer.should_rename(current_friendly, real_model):
                                            logging.info(f"检测到设备名需更新: '{current_friendly}' -> '{real_model}'")
                                            DeviceRenamer.set_friendly_name(disk.pnp_id, real_model)
                                except Exception as e:
                                    logging.warning(f"自动重命名尝试失败: {e}")
                            
                            raw_data = cmd.get_smart_data()
                            if raw_data:
                                attributes = SmartParser.parse_512(raw_data)

                    # 4. 如果所有底层 API 均失败，尝试 PowerShell / WMI 回退 (通用)
                    if not attributes:
                        logging.info(f"DEBUG: 底层 API 读取失败，尝试通过 WMI 获取 SMART (硬盘 {disk.index})...")
                        try:
                            import wmi
                            w = wmi.WMI(namespace="root/Microsoft/Windows/Storage")
                            counters = w.MSFT_StorageReliabilityCounter(DeviceId=str(disk.index))
                            if counters:
                                counter = counters[0]
                                data = {
                                    'Temperature': getattr(counter, 'Temperature', 0),
                                    'Wear': getattr(counter, 'Wear', 0),
                                    'PowerOnHours': getattr(counter, 'PowerOnHours', 0),
                                    'ReadErrorsTotal': getattr(counter, 'ReadErrorsTotal', 0),
                                    'WriteErrorsTotal': getattr(counter, 'WriteErrorsTotal', 0)
                                }
                                logging.info(f"DEBUG: WMI 获取 NVMe SMART 成功: {data}")
                                attributes = SmartParser.parse_powershell_nvme(data)
                        except Exception as e:
                            logging.debug(f"WMI NVMe fallback failed: {e}")
                            
                        # 如果 WMI 失败，尝试 PowerShell 作为最后手段
                        if not attributes:
                            try:
                                import subprocess, json
                                ps_cmd = f'powershell -NoProfile -Command "Get-PhysicalDisk -DeviceNumber {disk.index} | Get-StorageReliabilityCounter | Select-Object DeviceId, Temperature, Wear, PowerOnHours, ReadErrorsTotal, WriteErrorsTotal | ConvertTo-Json"'
                                output = subprocess.check_output(ps_cmd, shell=True, text=True, stderr=subprocess.DEVNULL)
                                if output and output.strip():
                                    data = json.loads(output)
                                    if isinstance(data, list) and len(data) > 0:
                                        data = data[0]
                                    if data and isinstance(data, dict):
                                        logging.info(f"DEBUG: PowerShell 获取 NVMe SMART 成功")
                                        attributes = SmartParser.parse_powershell_nvme(data)
                            except Exception as e:
                                logging.debug(f"PowerShell NVMe fallback failed: {e}")

                    if attributes:
                        logging.info(f"DEBUG: 硬盘 {disk.index} 数据解析成功")
                        current_serial = disk_info["serial"]
                        history_entry = self.history_manager.get_disk_history(current_serial)
                        
                        summary = SmartParser.get_summary(attributes, history_entry)
                        self.disk_health_status[current_serial] = summary
                        
                        # Update persistent history
                        self.history_manager.update_disk_history(current_serial, attributes)
                        
                        disk_info["temp"] = f"{summary['temp']}°C" if summary['temp'] is not None else "N/A"
                        disk_info["status"] = summary["status"]
                        disk_info["reallocated"] = summary["reallocated"]
                        disk_info["pending"] = summary["pending"]
                        disk_info["power_on_hours"] = str(summary["power_on_hours"])
                        disk_info["health_advice"] = summary["health_advice"]
                        # Include dynamic analysis for UI
                        disk_info["attributes_analysis"] = summary["attributes_analysis"]
                        
                        # Add SSD specific fields
                        disk_info["ssd_life_left"] = summary["ssd_life_left"]
                        disk_info["total_writes_gb"] = summary["total_writes_gb"]
                        
                        # Convert attributes to serializable format
                        disk_info["attributes"] = [
                            {
                                "id": f"0x{attr.id:02X}",
                                "name": attr.name,
                                "name_cn": attr.name_cn,
                                "value": str(attr.value),
                                "worst": str(attr.worst),
                                "raw": str(attr.raw)
                            } for attr in attributes
                        ]
                        
                        if summary["status"] != "Healthy":
                            msg = f"硬盘健康异常! ({disk_info['model']})\n状态: {summary['status']}\n重映射扇区: {summary['reallocated']}\n待处理扇区: {summary['pending']}"
                            if self.callback_notify:
                                self.callback_notify("硬盘 SMART 预警", msg)
                    else:
                        logging.warning(f"无法读取硬盘 {disk.index} 的数据")
                        disk_info["status"] = "Read Failed"
            except Exception as e:
                logging.error(f"检测硬盘 {disk.index} 时发生异常: {e}", exc_info=True)
                disk_info["status"] = "Error"

            ui_data.append(disk_info)

        # Notify UI to update
        logging.info(f"DEBUG: 准备更新 UI，数据条数: {len(ui_data)}")
        if self.callback_update_ui:
            self.callback_update_ui(ui_data)
            logging.info("DEBUG: UI 更新回调已执行")
        else:
            logging.warning("DEBUG: 未设置 UI 更新回调")

    def mark_disk_sleeping(self, serial):
        if serial:
            logging.info(f"将硬盘标记为休眠: {serial}")
            self.ejected_disks.discard(serial)
            self.sleeping_disks.add(serial)

    def mark_disk_ejected(self, serial):
        if serial:
            logging.info(f"将硬盘标记为已弹出: {serial}")
            self.sleeping_disks.discard(serial)
            self.ejected_disks.add(serial)
            self.cached_disks = [
                disk for disk in self.cached_disks
                if getattr(disk, "serial_number", None) != serial
            ]
            self.disk_health_status.pop(serial, None)
            self.disk_last_check_times.pop(serial, None)
            # 允许后续正常扫描剩余设备，但不要再把已弹出的设备重新加回来，直到用户手动刷新。
            self.last_inventory_scan_time = 0

    def mark_disk_awake(self, serial):
        changed = False
        if serial in self.sleeping_disks:
            logging.info(f"将硬盘标记为唤醒: {serial}")
            self.sleeping_disks.remove(serial)
            changed = True
        if serial in self.ejected_disks:
            logging.info(f"将硬盘从已弹出名单移除: {serial}")
            self.ejected_disks.remove(serial)
            changed = True
        if changed:
            # 醒来后尽快允许下一次重新枚举，保证设备列表及时同步。
            self.last_inventory_scan_time = 0

    def clear_disk_exclusions(self):
        """手动刷新时清空休眠/已弹出屏蔽名单，允许系统重新发现设备。"""
        self.sleeping_disks.clear()
        self.ejected_disks.clear()
        self.last_inventory_scan_time = 0

    def _filter_excluded_disks(self, disks):
        if not self.ejected_disks:
            return list(disks)
        return [
            disk for disk in disks
            if getattr(disk, "serial_number", None) not in self.ejected_disks
        ]

    def stop(self):
        logging.info("监控服务收到停止请求")
        self.shutdown_mode = True
        self.running = False
