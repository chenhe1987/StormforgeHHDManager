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

    def set_autostart(self, enabled):
        """设置或取消开机自启动 (使用 Windows 计划任务以支持 Win11/Admin)"""
        try:
            # 1. 尝试清理旧的注册表启动项 (为了兼容性)
            try:
                key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
                winreg.DeleteValue(key, self.app_name)
                winreg.CloseKey(key)
                logging.info("已清理旧版注册表自启动项")
            except:
                pass

            # 2. 使用 schtasks 管理计划任务
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            if enabled:
                # 获取当前运行的可执行文件路径
                if getattr(sys, 'frozen', False):
                    path = sys.executable
                else:
                    path = os.path.abspath(sys.argv[0])
                
                # 构建命令: schtasks /Create /F /RL HIGHEST /SC ONLOGON /TN "TaskName" /TR "'Path'"
                # 注意引号处理：/TR "'C:\Program Files\App.exe'"
                cmd = f'schtasks /Create /F /RL HIGHEST /SC ONLOGON /TN "{self.task_name}" /TR "\'{path}\'"'
                
                result = subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo, shell=True)
                if result.returncode == 0:
                    logging.info(f"已设置计划任务自启动: {cmd}")
                    self.config["autostart"] = True
                    self.save_config()
                    return True
                else:
                    logging.error(f"设置计划任务失败: {result.stderr}")
                    return False
            else:
                # 删除任务
                cmd = f'schtasks /Delete /F /TN "{self.task_name}"'
                subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo, shell=True)
                logging.info("已取消计划任务自启动")
                self.config["autostart"] = False
                self.save_config()
                return True
                
        except Exception as e:
            logging.error(f"设置自启动异常: {e}")
            return False

    def is_autostart_enabled(self):
        """检查计划任务确认是否已设置自启动"""
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            # 查询任务状态
            cmd = f'schtasks /Query /TN "{self.task_name}"'
            result = subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo, shell=True)
            
            enabled = (result.returncode == 0)
            
            # 同步配置文件状态
            if self.config.get("autostart") != enabled:
                self.config["autostart"] = enabled
                self.save_config()
            return enabled
        except Exception as e:
            logging.error(f"查询自启动状态失败: {e}")
            return self.config.get("autostart", False)
