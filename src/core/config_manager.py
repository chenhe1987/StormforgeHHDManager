import json
import os
import logging
import sys
import winreg
import subprocess
from src.utils.paths import get_base_path

class ConfigManager:
    def __init__(self, filename="app_config.json"):
        self.filename = os.path.join(get_base_path(), filename)
        self.config = self._load_config()
        self.app_name = "StormForgeDiskManager"
        self.task_name = "JiFengZhiHDDManager"
        
    def _load_config(self):
        default_config = {
            "intervals": {}, # {serial: seconds}
            "sleep_timers": {}, # {serial: minutes}
            "autostart": False
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
        # Default 24 hours (86400s)
        return self.config.get("intervals", {}).get(serial, 86400)
        
    def set_disk_interval(self, serial, seconds):
        if "intervals" not in self.config:
            self.config["intervals"] = {}
        self.config["intervals"][serial] = int(seconds)
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
                # 获取当前运行的可执行文件路径
                if getattr(sys, 'frozen', False):
                    path = sys.executable
                else:
                    path = os.path.abspath(sys.argv[0])
                    # 如果是脚本运行，通常不需要特殊处理，但为了稳妥指向 python
                    if path.endswith('.py'):
                        path = f'"{sys.executable}" "{path}"'
                    else:
                        path = f'"{path}"'

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
            # 1. 检查注册表
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
                winreg.QueryValueEx(key, self.app_name)
                winreg.CloseKey(key)
                is_enabled = True
            except FileNotFoundError:
                pass
            
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
