import json
import os
import logging
import sys
import winreg
import subprocess
import shutil
from src.utils.paths import get_base_path

class ConfigManager:
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(ConfigManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, filename="app_config.json"):
        if getattr(self, '_initialized', False):
            return
        cfg_root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or get_base_path()
        cfg_dir = os.path.join(cfg_root, "StormForgeDiskManager")
        try:
            os.makedirs(cfg_dir, exist_ok=True)
        except Exception:
            cfg_dir = get_base_path()
        self.filename = os.path.join(cfg_dir, filename)
        legacy_path = os.path.join(get_base_path(), filename)
        if not os.path.exists(self.filename) and os.path.exists(legacy_path):
            try:
                shutil.copy2(legacy_path, self.filename)
            except Exception:
                pass
        self.config = self._load_config()
        self.app_name = "StormForgeDiskManager"
        self.task_name = "JiFengZhiHDDManager"
        self._initialized = True
        
    def _load_config(self):
        default_config = {
            "intervals": {}, # {serial: seconds}
            "sleep_timers": {}, # {serial: minutes}
            "autostart": False,
            "shutdown_eject": True,
            "safe_remove_spindown": True
        }
        if not os.path.exists(self.filename):
            return default_config
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # Ensure all default keys exist
                for key, value in default_config.items():
                    if key not in config:
                        config[key] = value
                return config
        except Exception as e:
            logging.error(f"Failed to load config: {e}")
            return default_config
            
    def save_config(self):
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logging.error(f"Failed to save config: {e}")

    def get_disk_interval(self, serial):
        return self.config.get("intervals", {}).get(serial, 86400)
        
    def set_disk_interval(self, serial, seconds):
        if "intervals" not in self.config:
            self.config["intervals"] = {}
        self.config["intervals"][serial] = int(seconds)
        self.save_config()

    def get_sleeping_disks(self):
        """返回持久化的休眠硬盘序列号列表"""
        return self.config.get("sleeping_disks", [])

    def set_sleeping_disks(self, serials):
        """持久化保存休眠硬盘序列号列表"""
        self.config["sleeping_disks"] = list(serials)
        self.save_config()

    def add_sleeping_disk(self, serial):
        disks = set(self.config.get("sleeping_disks", []))
        disks.add(serial)
        self.config["sleeping_disks"] = list(disks)
        self.save_config()

    def remove_sleeping_disk(self, serial):
        disks = set(self.config.get("sleeping_disks", []))
        disks.discard(serial)
        self.config["sleeping_disks"] = list(disks)
        self.save_config()

    def get_sleep_timer(self, serial):
        """获取硬盘休眠时间设置 (分钟)"""
        return self.config.get("sleep_timers", {}).get(serial, 0) # 默认 0 (从不)

    def set_sleep_timer(self, serial, minutes):
        """保存硬盘休眠时间设置"""
        if "sleep_timers" not in self.config:
            self.config["sleep_timers"] = {}
        self.config["sleep_timers"][serial] = int(minutes)
        self.save_config()

    def get_report_config(self):
        return {
            "auto_report": self.config.get("auto_report", False),
            "report_url": self.config.get("report_url", "https://your-report-server.com/api/upload") 
        }

    def set_report_config(self, auto_report, report_url=None):
        self.config["auto_report"] = auto_report
        if report_url:
            self.config["report_url"] = report_url
        self.save_config()

    def _get_autostart_command(self):
        """构造当前版本应写入注册表的启动命令。"""
        if getattr(sys, 'frozen', False):
            return f'"{os.path.abspath(sys.executable)}" --silent'

        path = os.path.abspath(sys.argv[0])
        if path.endswith('.py'):
            return f'"{sys.executable}" "{path}" --silent'
        return f'"{path}" --silent'

    def _normalize_autostart_command(self, command):
        if not command:
            return ""
        return os.path.normcase(os.path.normpath(command.strip()))

    def _extract_command_path(self, command):
        """从注册表命令中提取主可执行路径。"""
        if not command:
            return ""

        command = command.strip()
        if command.startswith('"'):
            end_quote = command.find('"', 1)
            if end_quote > 1:
                return command[1:end_quote]

        return command.split(" ")[0]

    def set_autostart(self, enabled):
        """设置或取消开机自启动 (使用注册表 HKCU\Software\Microsoft\Windows\CurrentVersion\Run)"""
        try:
            # 1. 无论如何，都尝试清理旧的计划任务 (如果存在)
            try:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                cmd = f'schtasks /Delete /F /TN "{self.task_name}"'
                subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo, shell=True)
                logging.info("已清理旧版计划任务自启动项")
            except Exception as e:
                logging.debug(f"清理计划任务失败 (可能不存在): {e}")

            # 2. 操作注册表
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            if enabled:
                path = self._get_autostart_command()

                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
                winreg.SetValueEx(key, self.app_name, 0, winreg.REG_SZ, path)
                winreg.CloseKey(key)
                
                logging.info(f"已设置注册表自启动: {path}")
                self.config["autostart"] = True
                self.save_config()
                return True
            else:
                try:
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
                    winreg.DeleteValue(key, self.app_name)
                    winreg.CloseKey(key)
                except FileNotFoundError:
                    pass
                
                logging.info("已取消注册表自启动")
                self.config["autostart"] = False
                self.save_config()
                return True
                
        except Exception as e:
            logging.error(f"设置自启动异常: {e}")
            return False

    def is_autostart_enabled(self):
        """检查注册表确认是否已设置自启动"""
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        is_enabled = False
        try:
            reg_value = None
            # 1. 检查注册表
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
                reg_value, _ = winreg.QueryValueEx(key, self.app_name)
                winreg.CloseKey(key)
                is_enabled = True
            except FileNotFoundError:
                pass

            # 如果注册表存在，但仍指向旧版本 EXE，则自动修复为当前版本路径。
            if is_enabled and reg_value:
                expected_command = self._get_autostart_command()
                current_path = self._extract_command_path(reg_value)
                command_matches = self._normalize_autostart_command(reg_value) == self._normalize_autostart_command(expected_command)
                path_exists = os.path.exists(current_path) if current_path else False

                if not command_matches or not path_exists:
                    logging.warning(
                        f"检测到自启动项路径已过期或失效，当前值: {reg_value}，期望值: {expected_command}"
                    )
                    repaired = self.set_autostart(True)
                    if repaired:
                        is_enabled = True
                    else:
                        is_enabled = False
            
            # 2. 如果注册表没有，检查计划任务 (为了兼容旧版状态显示)
            if not is_enabled:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                cmd = f'schtasks /Query /TN "{self.task_name}"'
                result = subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo, shell=True)
                if result.returncode == 0:
                    is_enabled = True

            # 同步配置文件状态
            if self.config.get("autostart") != is_enabled:
                self.config["autostart"] = is_enabled
                self.save_config()
            return is_enabled
        except Exception as e:
            logging.error(f"查询自启动状态失败: {e}")
            return self.config.get("autostart", False)

    def get_shutdown_eject(self):
        """获取关机自动弹出设置"""
        return self.config.get("shutdown_eject", False)

    def set_shutdown_eject(self, enabled):
        """保存关机自动弹出设置"""
        self.config["shutdown_eject"] = enabled
        self.save_config()

    def get_safe_remove_spindown(self):
        """获取安全弹出停转补丁设置"""
        return self.config.get("safe_remove_spindown", True)

    def set_safe_remove_spindown(self, enabled):
        """保存安全弹出停转补丁设置"""
        self.config["safe_remove_spindown"] = enabled
        self.save_config()
