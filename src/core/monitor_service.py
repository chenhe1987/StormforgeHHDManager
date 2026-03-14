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
        self.scan_lock = threading.Lock()

    def run(self):
        logging.info("监控服务已启动...")
        # Initial scan immediately
        self.check_all_smart(force=True)
        self.check_event_logs()
        self.last_event_check = time.time()

        while self.running:
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
        try:
            # We need to know which disks are available.
            # Ideally we maintain a list of active disks.
            # For now, let's re-scan physical disks list quickly or cache it?
            # Re-scanning WMI every 10s might be heavy.
            # But we need to handle hot-plug.
            # Let's assume we scan list every minute or so?
            # Or just scan all now.
            disks = DeviceManager.get_physical_disks()
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
        try:
            if target_disks is None:
                disks = DeviceManager.get_physical_disks()
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
            serial = disk.serial_number
            
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
                      ("SSD" in model_upper and disk.interface_type == "IDE"))
            
            logging.info(f"DEBUG: 磁盘 {disk.index} ({disk.model}) Interface: {disk.interface_type}, is_nvme: {is_nvme}")
            
            if is_nvme:
                logging.info(f"DEBUG: 跳过 NVMe 硬盘: {disk.model}")
                continue

            # Add basic info first
            interface_type = disk.interface_type
            if interface_type == "IDE" and (disk.is_removable or "USB" in disk.pnp_id):
                interface_type = "USB (SATA)"
            
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
                    # 尝试读取 NVMe 数据（针对某些显示为 IDE 的 SSD）
                    nvme_data = cmd.get_nvme_smart_data()
                    if nvme_data:
                        logging.info(f"DEBUG: 硬盘 {disk.index} 识别为 NVMe 协议，跳过显示")
                        is_nvme = True
                        continue # Skip this disk as it's NVMe
                    else:
                        # Try SATA/SAT protocol
                        id_data = cmd.identify_device()
                        if not id_data:
                            logging.warning(f"硬盘 {disk.index} IDENTIFY 失败，可能不支持 SAT")
                            attributes = []
                        else:
                            logging.info(f"DEBUG: 硬盘 {disk.index} IDENTIFY 成功")
                            real_model, real_serial = ASMCommander.parse_identify_data(id_data)
                            if real_model:
                                logging.info(f"DEBUG: 识别到真实硬盘型号: {real_model}, 序列号: {real_serial}")
                                disk_info["model"] = real_model
                                if real_serial and len(real_serial) > 5:
                                    disk_info["serial"] = real_serial

                                # 尝试修复设备管理器中的显示名称 (DeviceRenamer)
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
                            else:
                                attributes = []

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
            self.sleeping_disks.add(serial)

    def mark_disk_awake(self, serial):
        if serial in self.sleeping_disks:
            logging.info(f"将硬盘标记为唤醒: {serial}")
            self.sleeping_disks.remove(serial)

    def stop(self):
        self.running = False
