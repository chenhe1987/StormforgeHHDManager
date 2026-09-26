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
from src.core.disk_whitelist import disk_id

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
        # 从持久化配置加载休眠状态（跨程序重启保持）
        persisted = []
        if self.config_manager:
            persisted = self.config_manager.get_sleeping_disks()
            if persisted and self._started_soon_after_boot():
                # 刚开机（开机后首次启动本程序）：硬盘柜随主机断电又重新上电，
                # 里面的盘必然是转起来的，持久化的“休眠”标记此时一定是错的。
                # 不清掉会导致：开机后所有外置盘都显示“休眠”、跳过 SMART 检测
                # （拿不到健康数据和关机计数基线），用户还得手动唤醒。
                logging.info(
                    f"检测到本次为开机后首次启动，清空过期的休眠标记: {persisted}"
                )
                self.config_manager.set_sleeping_disks([])
                persisted = []
        self.sleeping_disks = set(persisted) # {serial}
        self.ejected_disks = set() # {serial}
        self.cached_disks = []
        self.last_ui_data = []  # 最近一次推送给 UI 的完整列表（部分盘扫描时用于合并）
        self.last_inventory_scan_time = 0
        # 兜底轮询间隔（秒，0 = 只靠设备到达事件）。见 config_manager。
        try:
            self.device_inventory_interval = self.config_manager.get_device_inventory_interval()
        except Exception:
            self.device_inventory_interval = 120
        self.scan_lock = threading.RLock()
        self.removal_pending = threading.Event()
        self.eject_quarantine = set(self.config_manager.config.get("eject_quarantine", []))

        self._idle_tracker = {}  # {disk_index: {last_r, last_w, idle_since_ts, timeout_mins}}
        self._idle_last_check = 0

    @staticmethod
    def _started_soon_after_boot(tolerance_seconds=600):
        """判断本进程是否在系统开机后不久启动（开机自启场景）。

        容差必须是 600 秒：计划任务的登录自启动带 5 分钟延迟
        （旧任务 /DELAY 0005:00），180 秒的窗口会永远错过，
        导致开机后过期的休眠标记不被清空、外置盘一直显示"休眠"。
        """
        try:
            import ctypes
            uptime_ms = ctypes.windll.kernel32.GetTickCount64()
            return uptime_ms < tolerance_seconds * 1000
        except Exception:
            return False

    def run(self):
        logging.info("监控服务已启动...")
        # Initial scan immediately
        self.check_all_smart(force=True)
        self.check_event_logs()
        self.last_event_check = time.time()
        ml_cleanup_counter = 0

        while self.running:
            if self.shutdown_mode:
                # 关机/休眠期间暂停一切检测（不访问硬盘），但**不退出线程**：
                # 关机被用户或其他程序取消、或系统从休眠恢复后，必须能继续工作。
                # 旧实现直接 break，导致取消关机 / 休眠唤醒后监控永久停摆
                # （表现为唤醒后列表状态再也不刷新）。
                time.sleep(1)
                continue

            current_time = time.time()
            
            # 1. Check Event Log (High frequency, no disk wake-up)
            if current_time - self.last_event_check >= self.event_log_interval:
                self.check_event_logs()
                self.last_event_check = current_time
            
            # 2. Check SMART (Per-disk interval)
            self.check_disks_schedule()

            # 3. Check idle timers (software-based, reads perf counters)
            if current_time - self._idle_last_check >= 10:
                self._check_idle_timers()
                self._idle_last_check = current_time

            # 4. 定期释放 COM 引用，防止 WMI 提供者持有设备句柄导致弹出失败
            ml_cleanup_counter += 1
            if ml_cleanup_counter >= 6:
                try:
                    import pythoncom
                    pythoncom.CoFreeUnusedLibraries()
                except Exception:
                    pass
                ml_cleanup_counter = 0
                
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
        with self.scan_lock:
            return self._get_cached_or_scan_disks_locked(force)

    def _get_cached_or_scan_disks_locked(self, force=False):
        if self.removal_pending.is_set():
            return self._filter_excluded_disks(self.cached_disks)
        now = time.time()

        if self.shutdown_mode:
            logging.info("关机静默模式下复用现有磁盘缓存，不再重新枚举")
            return self._filter_excluded_disks(self.cached_disks)

        # device_inventory_interval 为 0 时表示只靠设备到达事件触发重扫，
        # 不能写成"每次都重新枚举"（now - last >= 0 恒真）。
        due = (self.device_inventory_interval > 0 and
               now - self.last_inventory_scan_time >= self.device_inventory_interval)
        if force or not self.cached_disks or due:
            self.cached_disks = DeviceManager.get_physical_disks(
                allowlist=self.config_manager.get_managed_disk_whitelist())
            self.last_inventory_scan_time = now
            logging.info(f"更新物理磁盘缓存: {len(self.cached_disks)} 个设备")
        else:
            logging.info(f"复用物理磁盘缓存: {len(self.cached_disks)} 个设备")

        return self._filter_excluded_disks(self.cached_disks)

    def check_all_smart(self, force=False, target_disks=None):
        if self.removal_pending.is_set():
            return
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
        whitelist = self.config_manager.get_managed_disk_whitelist()
        if target_disks is None:
            for disk in disks:
                managed_id = disk_id(disk)
                if managed_id and managed_id in whitelist:
                    continue
                ui_data.append({
                    "index": disk.index, "model": disk.model,
                    "serial": disk.serial_number, "pnp_id": disk.pnp_id or "",
                    "managed_id": managed_id, "is_removable": disk.is_removable,
                    "interface": disk.interface_type or "Unknown",
                    "status": "未加入白名单（不管理）", "temp": "N/A",
                    "reallocated": "N/A", "pending": "N/A", "attributes": []
                })
        
        for disk in disks:
            if self.removal_pending.is_set():
                break
            if self.is_eject_blocked(disk):
                continue
            managed_id = disk_id(disk)
            if not managed_id or managed_id not in whitelist:
                continue
            if self.shutdown_mode:
                logging.info("检测过程中收到关机静默请求，提前结束本轮 SMART 检测")
                break

            serial = disk.serial_number

            if serial in self.ejected_disks:
                logging.info(f"跳过已弹出的硬盘: {disk.model} ({serial})")
                continue
            
            # Check if disk is marked as sleeping
            if serial in self.sleeping_disks:
                logging.info(f"跳过检测处于休眠状态的硬盘: {disk.model} ({serial})")
                # Still add to ui_data but with a "Sleeping" status
                disk_info = {
                    "index": disk.index,
                    "model": disk.model,
                    "serial": serial,
                    "interface": disk.interface_type,
                    "pnp_id": disk.pnp_id or "", "managed_id": managed_id,
                    "temp": "N/A",
                    "status": "Sleeping",
                    "is_removable": disk.is_removable,
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
                "pnp_id": disk.pnp_id or "", "managed_id": managed_id,
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
                                        # 外置可换盘盘位(USB硬盘柜)强制同步真实型号，
                                        # 修复换盘后设备管理器仍显示旧盘名的问题
                                        force_sync = bool(getattr(disk, 'is_removable', False))
                                        if DeviceRenamer.should_rename(current_friendly, real_model, force=force_sync):
                                            logging.info(f"检测到设备名需更新: '{current_friendly}' -> '{real_model}' (force={force_sync})")
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
                        finally:
                            try:
                                del w
                            except Exception:
                                pass
                            
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
                        # 区分“盘在休眠/未起转”与“真的读取失败”：
                        # USB-SATA 桥对休眠盘返回 sense 3A/00 (MEDIUM NOT PRESENT)，
                        # 这属于正常休眠状态而不是故障。用轻量 TUR 探测确认，
                        # 避免把休眠盘误报成「Read Failed」让用户以为硬盘坏了。
                        asleep = serial in self.sleeping_disks
                        if not asleep:
                            try:
                                with ASMCommander(disk.index, model_hint=disk.model,
                                                  serial_hint=serial) as probe:
                                    asleep = not probe.is_responding()
                            except Exception as probe_err:
                                logging.debug(f"TUR 探测失败 (disk {disk.index}): {probe_err}")

                        if asleep:
                            logging.info(
                                f"硬盘 {disk.index} SMART 无响应且 TUR 无响应 → 判定为休眠/停转状态"
                            )
                            disk_info["status"] = "Sleeping"
                        else:
                            disk_info["status"] = "Read Failed"
            except Exception as e:
                logging.error(f"检测硬盘 {disk.index} 时发生异常: {e}", exc_info=True)
                disk_info["status"] = "Error"

            ui_data.append(disk_info)

        # Notify UI to update
        logging.info(f"DEBUG: 准备更新 UI，数据条数: {len(ui_data)}")
        if self.callback_update_ui:
            if target_disks is not None:
                # 调度检测只扫描了部分磁盘：把结果合并进上一次的全量列表再推送，
                # 避免侧边栏被覆盖成只剩这几块盘。
                # （休眠盘永远“到期”，若不合并，定时检测会把列表洗成只剩休眠盘）
                merged = self._merge_ui_data(self.last_ui_data, ui_data)
                self.last_ui_data = merged
                self.callback_update_ui(merged)
                logging.info(f"DEBUG: UI 更新回调已执行 (合并后 {len(merged)} 条)")
            else:
                self.last_ui_data = ui_data
                self.callback_update_ui(ui_data)
                logging.info("DEBUG: UI 更新回调已执行")
        else:
            logging.warning("DEBUG: 未设置 UI 更新回调")

    def _merge_ui_data(self, base, updates):
        """把部分磁盘的检测结果合并进完整列表。

        规则：
        - base 中与 updates 同 serial（其次同 index）的条目被更新条目替换；
        - base 中已弹出（ejected_disks）的条目被丢弃；
        - base 中 serial 与 index 都无法在设备缓存里匹配的条目，才视为已移除的盘丢弃；
        - updates 中出现但 base 中没有的新磁盘追加到末尾。

        注意：UI 记录里的 serial 可能是硬盘真实序列号（SATA IDENTIFY 得到），
        而设备缓存里是桥接芯片序列号（如 B000BBBBAAAA）。因此绝不能只按 serial 判定
        盘是否还在——必须 index 或 serial 命中其一即保留，否则会把正在使用的盘误删。
        """
        by_serial = {u.get("serial"): u for u in updates if u.get("serial")}
        by_index = {u.get("index"): u for u in updates if u.get("index") is not None}

        cached_serials = {
            getattr(d, "serial_number", None)
            for d in self.cached_disks
            if getattr(d, "serial_number", None)
        }
        cached_indexes = {
            getattr(d, "index", None)
            for d in self.cached_disks
            if getattr(d, "index", None) is not None
        }

        merged = []
        for d in base:
            serial = d.get("serial")
            index = d.get("index")
            if serial and serial in self.ejected_disks:
                continue
            if cached_serials or cached_indexes:
                serial_known = bool(serial) and serial in cached_serials
                index_known = index is not None and index in cached_indexes
                if not serial_known and not index_known:
                    logging.info(
                        f"DEBUG: 合并时移除已不在设备列表中的记录: "
                        f"{d.get('model')} (index={index}, serial={serial})"
                    )
                    continue
            replacement = None
            if serial:
                replacement = by_serial.get(serial)
            if replacement is None and index is not None:
                replacement = by_index.get(index)
            merged.append(replacement if replacement is not None else d)

        known_serials = {d.get("serial") for d in base if d.get("serial")}
        known_indexes = {d.get("index") for d in base if d.get("index") is not None}
        for u in updates:
            serial = u.get("serial")
            if serial and serial in known_serials:
                continue
            if u.get("index") is not None and u.get("index") in known_indexes:
                continue
            merged.append(u)

        return merged

    def _serials_for(self, serial, disk_index=None):
        """收集一个盘位对应的所有序列号。

        UI 记录里的 serial 可能是硬盘真实序列号（SATA IDENTIFY 得到），
        而设备缓存里是桥接芯片序列号（如 B000BBBBAAAA）。屏蔽/弹出时必须两者都覆盖，
        否则后台监控仍会访问正在弹出的盘，导致锁卷失败、弹出被否决。
        """
        targets = {s for s in [serial] if s}
        if disk_index is not None:
            for d in self.cached_disks:
                if getattr(d, "index", None) == disk_index:
                    s = getattr(d, "serial_number", None)
                    if s:
                        targets.add(s)
        return targets

    def mark_disk_sleeping(self, serial, disk_index=None):
        for s in self._serials_for(serial, disk_index):
            logging.info(f"将硬盘标记为休眠: {s}")
            self.ejected_disks.discard(s)
            self.sleeping_disks.add(s)
            if self.config_manager:
                self.config_manager.add_sleeping_disk(s)

    def mark_disk_ejected(self, serial, disk_index=None):
        targets = self._serials_for(serial, disk_index)
        logging.info(f"将硬盘标记为已弹出: {serial} (index={disk_index})")

        # 休眠屏蔽要按两种序列号一起清除（桥接序列号可能挂在休眠名单里）
        for s in targets:
            self.sleeping_disks.discard(s)
            if self.config_manager:
                self.config_manager.remove_sleeping_disk(s)

        if serial:
            self.ejected_disks.add(serial)
            self.disk_health_status.pop(serial, None)
            self.disk_last_check_times.pop(serial, None)

        # 立刻把该盘位从设备缓存中剔除：避免旧缓存把盘位里的新盘识别成旧盘。
        # 注意不要把桥接序列号加入 ejected_disks——同一个盘位换入新盘后，
        # 新盘应能在下次枚举时正常出现，而不是要等用户手动刷新。
        if disk_index is not None:
            self.cached_disks = [
                disk for disk in self.cached_disks
                if getattr(disk, "index", None) != disk_index
            ]
        elif serial:
            self.cached_disks = [
                disk for disk in self.cached_disks
                if getattr(disk, "serial_number", None) != serial
            ]
        # 允许后续正常扫描剩余设备，但不要再把已弹出的设备重新加回来，直到用户手动刷新。
        self.last_inventory_scan_time = 0

    def clear_all_sleeping(self):
        """pnputil 重启设备后所有盘一起醒来"""
        if self.sleeping_disks:
            logging.info(f"pnputil 唤醒后清除所有休眠标记: {self.sleeping_disks}")
            self.sleeping_disks.clear()
            self.last_inventory_scan_time = 0
            if self.config_manager:
                self.config_manager.set_sleeping_disks([])

    def mark_disk_awake(self, serial, disk_index=None):
        targets = self._serials_for(serial, disk_index)
        changed = False
        for s in targets:
            if s in self.sleeping_disks:
                logging.info(f"将硬盘标记为唤醒: {s}")
                self.sleeping_disks.remove(s)
                if self.config_manager:
                    self.config_manager.remove_sleeping_disk(s)
                changed = True
            if s in self.ejected_disks:
                logging.info(f"将硬盘从已弹出名单移除: {s}")
                self.ejected_disks.remove(s)
                changed = True
        if changed:
            self.last_inventory_scan_time = 0
            self.reset_idle_timer(serial)

    def clear_disk_exclusions(self):
        """手动刷新时清空休眠/已弹出屏蔽名单，允许系统重新发现设备。"""
        self.sleeping_disks.clear()
        self.ejected_disks.clear()
        if self.config_manager:
            self.config_manager.set_sleeping_disks([])
        self.last_inventory_scan_time = 0

    # ------------------------------------------------- 热插拔即时发现（v1.3.89）

    @staticmethod
    def _device_keys(disks):
        return {(getattr(d, "index", None), getattr(d, "serial_number", None))
                for d in disks}

    @staticmethod
    def _describe(disk):
        return "%s(%s) index=%s" % (getattr(disk, "model", "?"),
                                    getattr(disk, "serial_number", "?"),
                                    getattr(disk, "index", "?"))

    def _device_diff(self, before, after):
        old, new = self._device_keys(before), self._device_keys(after)
        added = [d for d in after
                 if (getattr(d, "index", None), getattr(d, "serial_number", None)) not in old]
        removed = [d for d in before
                   if (getattr(d, "index", None), getattr(d, "serial_number", None)) not in new]
        return added, removed

    def _heal_reappeared_devices(self, disks):
        """同 pnp_id 的盘重新出现 → 解除"已弹出/隔离"屏蔽。

        这正是提示文案承诺的"请重新连接设备后恢复"：设备物理上已经回来了，
        继续屏蔽只会让用户看不到这块盘。只在**新鲜枚举到**该设备时才解除。
        """
        healed = []
        for disk in disks:
            pnp = (getattr(disk, "pnp_id", "") or "").upper()
            serial = getattr(disk, "serial_number", None)
            if pnp and pnp in self.eject_quarantine:
                self.eject_quarantine.discard(pnp)
                try:
                    self.config_manager.config["eject_quarantine"] = sorted(self.eject_quarantine)
                    self.config_manager.save_config()
                except Exception as exc:
                    logging.warning("保存隔离名单失败: %s", exc)
                healed.append("%s（解除隔离）" % pnp)
            if serial and serial in self.ejected_disks:
                self.ejected_disks.discard(serial)
                healed.append("%s（解除已弹出标记）" % serial)
        return healed

    def _rows_for_devices_without_probe(self, disks, previous):
        """只用设备缓存构建完整列表：**不发任何 SMART/IDENTIFY**（热插拔事件路径专用）。

        0x0007 是"任何设备变化"都会发的通知，如果每次事件都跑一遍 SMART 检测，
        会把空闲停转但未标记休眠的盘反复唤醒。这里改为：
        - 已知盘直接沿用上一次的行（保留温度/健康数据，零磁盘访问）；
        - 新盘给一行占位（白名单盘给"待检测"，由正常的按盘调度在下一轮立刻补上，
          因为新盘的 disk_last_check_times 为空 = 立即到期）。
        """
        known = {(row.get("index"), row.get("serial")): row
                 for row in (previous or [])}
        whitelist = self.config_manager.get_managed_disk_whitelist()
        rows = []
        for disk in disks:
            serial = getattr(disk, "serial_number", None)
            index = getattr(disk, "index", None)
            old = known.get((index, serial))
            if old is not None:
                rows.append(old)
                continue
            pnp = getattr(disk, "pnp_id", "") or ""
            removable = bool(getattr(disk, "is_removable", False))
            interface = getattr(disk, "interface_type", "Unknown") or "Unknown"
            if interface == "IDE" and (removable or "USB" in pnp.upper()):
                interface = "USB (SATA)"
            elif "USB" in interface.upper():
                interface = "USB"
            managed_id = disk_id(disk)
            if serial and serial in self.sleeping_disks:
                status = "Sleeping"
            elif managed_id and managed_id in whitelist:
                status = "待检测"
            else:
                status = "未加入白名单（不管理）"
            rows.append({
                "index": index, "model": getattr(disk, "model", "Unknown"),
                "serial": serial, "pnp_id": pnp, "managed_id": managed_id,
                "is_removable": removable, "interface": interface,
                "status": status, "temp": "N/A",
                "reallocated": "N/A", "pending": "N/A", "attributes": [],
            })
        return rows

    def rescan_devices(self, reason="device_change"):
        """设备到达/移除后立即重扫，并把**完整**列表推给 UI。

        为什么必须"全量"：定时检测走 check_specific_disks(target_disks=...)，
        非白名单盘在那条路径里被 continue 跳过，而完整列表（含"未加入白名单
        （不管理）"行）只在 target_disks=None 时生成 → 新插入且还未勾选
        "纳入管理"的盘永远进不了侧栏，只能重启或手动刷新。

        枚举只读元数据（WMI/PnP/卷号映射），不发 ATA 命令、不唤醒休眠盘；
        句柄即用即关（实测 8 次枚举进程句柄增量 0）。
        """
        if not getattr(self, "running", False):
            return {"skipped": "not_running"}
        if self.shutdown_mode:
            logging.info("[Rescan] 关机静默模式，跳过设备重扫: reason=%s", reason)
            return {"skipped": "shutdown_mode"}
        if self.removal_pending.is_set():
            logging.info("[Rescan] 弹出/移除事务进行中，跳过设备重扫: reason=%s", reason)
            return {"skipped": "removal_pending"}

        with self.scan_lock:                       # RLock：内部再取锁是安全的
            before = list(self.cached_disks)
            self.last_inventory_scan_time = 0      # 强制下一次调用真的去枚举
            disks = self._get_cached_or_scan_disks(force=True)
            after = list(self.cached_disks)
            added, removed = self._device_diff(before, after)
            healed = self._heal_reappeared_devices(after)
            if added or removed or healed:
                logging.info(
                    "[Rescan] reason=%s 新增=[%s] 移除=[%s] 解除屏蔽=[%s]",
                    reason,
                    ", ".join(self._describe(d) for d in added) or "-",
                    ", ".join(self._describe(d) for d in removed) or "-",
                    ", ".join(healed) or "-")
            else:
                logging.info("[Rescan] reason=%s 设备清单无变化（%d 个）", reason, len(after))
            # 用设备缓存重建**完整**列表（含"未加入白名单（不管理）"的新盘），零磁盘访问：
            # 新盘的 SMART 数据由正常的按盘调度在下一轮补上（新盘立即到期）。
            rows = self._rows_for_devices_without_probe(after, self.last_ui_data)
            self.last_ui_data = rows
            if self.callback_update_ui:
                self.callback_update_ui(rows)
        return {"added": len(added), "removed": len(removed),
                "healed": healed, "total": len(disks), "rows": len(rows)}

    def pause_for_removal(self, disk_index):
        """系统即将移除设备，立即释放该盘所有资源"""
        logging.info(f"暂停监控以允许设备移除: PhysicalDrive{disk_index}")
        self.last_inventory_scan_time = time.time() + 120
        self.cached_disks = [
            d for d in self.cached_disks
            if getattr(d, "index", None) != disk_index
        ]
        try:
            import pythoncom
            pythoncom.CoFreeUnusedLibraries()
        except Exception:
            pass

    def resume_after_removal(self):
        """设备移除完成后恢复扫描"""
        logging.info("设备移除完毕，恢复监控扫描")
        self.last_inventory_scan_time = 0

    def _check_idle_timers(self):
        if self.removal_pending.is_set():
            return
        with self.scan_lock:
            if not self.removal_pending.is_set():
                self._check_idle_timers_locked()

    def _check_idle_timers_locked(self):
        """简单倒计时——超时后触发休眠（等同于点击立即休眠按钮）"""
        try:
            disks = self._get_cached_or_scan_disks(force=False)
            now = time.time()

            for disk in disks:
                idx = disk.index
                serial = disk.serial_number

                if self.removal_pending.is_set():
                    return
                if (self.is_eject_blocked(disk) or
                        disk_id(disk) not in self.config_manager.get_managed_disk_whitelist() or
                        serial in self.sleeping_disks or serial in self.ejected_disks):
                    self._idle_tracker.pop(idx, None)
                    continue

                timeout_mins = self.config_manager.get_sleep_timer(serial)
                if timeout_mins <= 0:
                    self._idle_tracker.pop(idx, None)
                    continue

                tracker = self._idle_tracker.get(idx)
                if tracker is None:
                    self._idle_tracker[idx] = {"start": now, "timeout": timeout_mins}
                    logging.info(
                        f"Idle timer started: Disk {idx} -> SLEEP in {timeout_mins} min"
                    )
                    continue

                current_timeout = self.config_manager.get_sleep_timer(serial)
                if current_timeout != tracker.get("timeout"):
                    tracker["timeout"] = current_timeout
                    tracker["start"] = now

                elapsed = now - tracker["start"]
                if elapsed >= tracker["timeout"] * 60:
                    logging.info(
                        f"Idle timer fired: Disk {idx}, timeout={tracker['timeout']}min, "
                        f"elapsed={elapsed:.0f}s"
                    )
                    try:
                        from src.core.device_manager import DeviceManager
                        if not DeviceManager.is_managed_disk(idx):
                            logging.info("自动休眠前白名单复核失败，跳过磁盘 %s", idx)
                            self._idle_tracker.pop(idx, None)
                            continue
                        from src.hal.asm_commander import ASMCommander
                        with ASMCommander(idx) as cmd:
                            if cmd.sleep():
                                self.mark_disk_sleeping(serial, idx)
                                logging.info(f"Auto-sleep OK: Disk {idx}")
                            else:
                                logging.warning(f"Auto-sleep FAIL: Disk {idx}")
                    except Exception as e:
                        logging.warning(f"Auto-sleep exception: {e}")
                    self._idle_tracker.pop(idx, None)

        except Exception as e:
            logging.debug(f"Idle timer check failed: {e}")

    def reset_idle_timer(self, serial, disk_index=None):
        """用户修改休眠时间或唤醒后调用——重新开始倒计时"""
        if disk_index is not None:
            self._idle_tracker.pop(disk_index, None)
            return
        for idx, tracker in list(self._idle_tracker.items()):
            if tracker.get("serial") == serial:
                del self._idle_tracker[idx]

    def is_eject_blocked(self, disk):
        return (getattr(disk, "pnp_id", "") or "").upper() in self.eject_quarantine

    def reset_unmanaged_idle_timers(self):
        allowed = self.config_manager.get_managed_disk_whitelist()
        for disk in self.cached_disks:
            if disk_id(disk) not in allowed:
                self._idle_tracker.pop(getattr(disk, 'index', None), None)

    def quarantine_eject(self, pnp_id):
        if pnp_id:
            self.eject_quarantine.add(pnp_id.upper())
            self.config_manager.config["eject_quarantine"] = sorted(self.eject_quarantine)
            self.config_manager.save_config()

    def _filter_excluded_disks(self, disks):
        return [disk for disk in disks if not self.is_eject_blocked(disk)
                and getattr(disk, "serial_number", None) not in self.ejected_disks]

    def stop(self):
        logging.info("监控服务收到停止请求")
        self.shutdown_mode = True
        self.running = False
