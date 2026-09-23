import os
import logging
import zipfile
import requests
import json
import traceback
from datetime import datetime
from urllib.parse import quote
from src.utils.paths import get_base_path, get_resource_path

# ============================================================
# 日志报告云端方案（技术原则见 docs/TECH_NOTES_弹出休眠机制.md §8.39）
# 客户端一键上传 → 云主机固定目录 → 网页/密钥调取，
# 并按「版本 + 时间」判断报告有效性（过期/旧版本自动标记）。
#
# ⚠ 云端信息（服务器地址、访问密钥）**不写入源码**：
# 由程序目录下的 report_server.json 提供（随发行包分发，不进公开仓库）。
# 服务端实现与部署配置保存在本地 venv/私有资料/。
# ============================================================
SERVER_CONFIG_FILE = "report_server.json"


def load_report_server_config():
    """读取程序目录下的 report_server.json（云端地址与密钥，不随源码公开）。"""
    try:
        path = os.path.join(get_base_path(), SERVER_CONFIG_FILE)
        if not os.path.isfile(path):
            # Bundled client configuration contains an upload-only credential.
            path = get_resource_path("report_client.json")
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        upload_url = (cfg.get("upload_url") or "").strip()
        token = (cfg.get("token") or "").strip()
        if not upload_url or not token:
            return None
        browse_url = (cfg.get("browse_url") or "").strip()
        if not browse_url:
            browse_url = upload_url.rsplit("/", 1)[0] + "/"
        return {
            "upload_url": upload_url,
            "browse_url": browse_url,
            "download_url": (cfg.get("download_url") or "").strip(),
            "token": token,
        }
    except Exception as e:
        logging.warning(f"读取报告服务器配置失败: {e}")
        return None


class LogReporter:
    def __init__(self):
        self.base_dir = get_base_path()
        self.log_file = os.path.join(self.base_dir, "app.log")
        self.operation_log_file = os.path.join(self.base_dir, "operations.log")
        self.history_file = os.path.join(self.base_dir, "smart_history.json")
        self.config_file = os.path.join(self.base_dir, "config.json")

    def pack_logs(self):
        """Pack relevant log files into a zip buffer or file path"""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_filename = f"JiFengZhi_Report_{timestamp}.zip"
            zip_path = os.path.join(self.base_dir, zip_filename)

            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                if os.path.exists(self.log_file):
                    zipf.write(self.log_file, "app.log")

                if os.path.exists(self.operation_log_file):
                    zipf.write(self.operation_log_file, "operations.log")
                
                if os.path.exists(self.history_file):
                    zipf.write(self.history_file, "smart_history.json")
                    
                if os.path.exists(self.config_file):
                    # sanitize config? maybe not needed for internal tool
                    zipf.write(self.config_file, "config.json")
                    
                # System Info
                import platform
                sys_info = {
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "machine": platform.machine(),
                    "timestamp": timestamp
                }
                zipf.writestr("system_info.json", json.dumps(sys_info, indent=2))

            return zip_path
        except Exception as e:
            logging.error(f"Failed to pack logs: {e}")
            return None

    def send_report(self, description="", contact="", version="", url=None):
        """把打包好的报告 zip 一键上传到云主机（POST 原始字节流）。

        服务端按 version + 上传时间归档，并按版本/时间判断报告有效性
        （超过 7 天标记过期、低于最新版本标记旧版本）。
        """
        zip_path = self.pack_logs()
        if not zip_path:
            return False, "Failed to create log package"

        cfg = load_report_server_config()
        if not cfg:
            return False, "报告服务器未配置（缺少 report_server.json）"

        try:
            url = url or cfg["upload_url"]
            with open(zip_path, 'rb') as f:
                payload = f.read()

            headers = {
                "X-Upload-Token": cfg["token"],
                "X-Filename": quote(os.path.basename(zip_path)),
                "X-Version": quote(version or ""),
                "X-Description": quote(description or ""),
                "X-Contact": quote(contact or ""),
                "Content-Type": "application/zip",
            }

            # 超时放宽到 60s：日志包可能较大
            response = requests.post(url, data=payload, headers=headers, timeout=60)

            if response.status_code in [200, 201]:
                try:
                    body = response.json()
                except Exception:
                    body = {}
                msg = body.get("message") or "上传成功"
                return True, msg
            else:
                detail = ""
                try:
                    detail = response.json().get("message") or response.text[:200]
                except Exception:
                    detail = response.text[:200]
                return False, f"服务器返回 {response.status_code}: {detail}"

        except Exception as e:
            logging.error(f"Failed to send report: {e}")
            return False, str(e)
        finally:
            # Cleanup
            if zip_path and os.path.exists(zip_path):
                try:
                    os.remove(zip_path)
                except Exception:
                    pass

    def analyze_logs(self):
        """
        Analyze the local app.log for common errors and return a summary.
        This corresponds to the 'automatic read error log' feature.
        """
        if not os.path.exists(self.log_file):
            return ["Log file not found."]

        issues = []
        try:
            lines = []
            if os.path.exists(self.log_file):
                with open(self.log_file, 'r', encoding='utf-8', errors='ignore') as f:
                    lines.extend(f.readlines())

            if os.path.exists(self.operation_log_file):
                with open(self.operation_log_file, 'r', encoding='utf-8', errors='ignore') as f:
                    lines.extend(f.readlines())

            # Simple keyword analysis
            # We look at the last 1000 lines to avoid old history
            recent_lines = lines[-2000:] 
            
            for i, line in enumerate(recent_lines):
                if "ERROR" in line or "CRITICAL" in line:
                    # Capture context (previous line usually has timestamp/caller)
                    issues.append(line.strip())
                elif "WARNING" in line:
                    # Filter some common warnings?
                    if "WMI" in line or "SMART" in line or "IOCTL" in line:
                        issues.append(line.strip())
            
            if not issues:
                return ["No obvious errors found in the recent logs."]
            
            # Grouping/Deduplication could be done here
            return issues

        except Exception as e:
            return [f"Failed to analyze logs: {e}"]
