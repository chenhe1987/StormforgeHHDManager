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
        """设置或取消开机自启动。

        注意：程序以管理员权限运行(uac_admin=True)，HKCU Run 注册表自启动
        会在开机时触发 UAC 弹窗拦截导致程序不启动，因此必须使用任务计划程序
        (schtasks /SC ONLOGON /RL HIGHEST) —— 计划任务以最高权限运行，
        登录时自动启动且不需要 UAC 交互。
        """
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            task_name = self.task_name
            if enabled:
                exe_path = self._get_autostart_command()
                # 创建计划任务：登录时以最高权限运行
                cmd = (
                    f'schtasks /Create /F /TN "{task_name}" '
                    f'/TR "{exe_path}" /SC ONLOGON /RL HIGHEST '
                    f'/DELAY 0005:00'
                )
                result = subprocess.run(cmd, capture_output=True, text=True,
                                        startupinfo=startupinfo, shell=True,
                                        timeout=30)
                if result.returncode == 0:
                    logging.info(f"已创建计划任务自启动: {task_name} -> {exe_path}")
                    # 删除旧版注册表自启动项（避免 UAC 弹窗干扰）
                    try:
                        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
                        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
                        winreg.DeleteValue(key, self.app_name)
                        winreg.CloseKey(key)
                        logging.info("已清理旧版注册表自启动项")
                    except FileNotFoundError:
                        pass
                    self.config["autostart"] = True
                    self.save_config()
                    return True
                logging.error(f"创建计划任务失败: {result.stderr or result.stdout}")
                # 回退：尝试注册表（部分系统计划任务被策略禁用时）
                try:
                    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
                    winreg.SetValueEx(key, self.app_name, 0, winreg.REG_SZ, exe_path)
                    winreg.CloseKey(key)
                    logging.info(f"回退使用注册表自启动: {exe_path}")
                    self.config["autostart"] = True
                    self.save_config()
                    return True
                except Exception as e2:
                    logging.error(f"注册表自启动回退失败: {e2}")
                    return False
            else:
                # 删除计划任务
                cmd = f'schtasks /Delete /F /TN "{task_name}"'
                subprocess.run(cmd, capture_output=True, text=True,
                               startupinfo=startupinfo, shell=True, timeout=30)
                # 同时删除注册表项（兼容旧版）
                try:
                    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
                    winreg.DeleteValue(key, self.app_name)
                    winreg.CloseKey(key)
                except FileNotFoundError:
                    pass
                logging.info("已取消自启动（计划任务 + 注册表）")
                self.config["autostart"] = False
                self.save_config()
                return True
        except Exception as e:
            logging.error(f"设置自启动异常: {e}")
            return False

    def _task_exists(self):
        """检查计划任务是否存在且指向当前程序"""
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            cmd = f'schtasks /Query /TN "{self.task_name}"'
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    startupinfo=startupinfo, shell=True, timeout=30)
            return result.returncode == 0
        except Exception:
            return False

    def _fix_task_path(self):
        """计划任务存在但指向旧路径时重建为当前路径"""
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            cmd = f'schtasks /Query /TN "{self.task_name}" /XML'
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    startupinfo=startupinfo, shell=True, timeout=30)
            expected = self._get_autostart_command()
            if result.returncode == 0 and expected:
                if expected.split('"')[1] not in result.stdout:
                    logging.warning("检测到计划任务路径已过期，正在重建")
                    return self.set_autostart(True)
            return True
        except Exception:
            return False

    def is_autostart_enabled(self):
        """检查自启动状态：优先计划任务，其次注册表（兼容旧版）"""
        is_enabled = False
        try:
            # 1. 计划任务（当前方案）
            if self._task_exists():
                is_enabled = True
                self._fix_task_path()
            # 2. 注册表（旧版兼容）
            if not is_enabled:
                try:
                    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
                    reg_value, _ = winreg.QueryValueEx(key, self.app_name)
                    winreg.CloseKey(key)
                    if reg_value:
                        # 路径有效则迁移到计划任务方案
                        current_path = self._extract_command_path(reg_value)
                        if current_path and os.path.exists(current_path):
                            is_enabled = True
                            logging.info("检测到旧版注册表自启动，正在迁移到计划任务")
                            self.set_autostart(True)
                        else:
                            # 路径失效，清理
                            self.set_autostart(False)
                except FileNotFoundError:
                    pass

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
