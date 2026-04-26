import os
import logging
import zipfile
import requests
import json
import traceback
from datetime import datetime
from src.utils.paths import get_base_path

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

    def send_report(self, url, description="", contact=""):
        """Send the packed report to the specified URL"""
        zip_path = self.pack_logs()
        if not zip_path:
            return False, "Failed to create log package"

        try:
            # This expects a multipart/form-data upload
            # Field 'file': the zip file
            # Field 'description': user description
            # Field 'contact': user contact info
            
            with open(zip_path, 'rb') as f:
                files = {'file': (os.path.basename(zip_path), f, 'application/zip')}
                data = {
                    'description': description, 
                    'contact': contact,
                    'timestamp': datetime.now().isoformat()
                }
                
                # Timeout set to 30s as upload might be slow
                response = requests.post(url, files=files, data=data, timeout=30)
                
            if response.status_code in [200, 201]:
                return True, "Report sent successfully"
            else:
                return False, f"Server returned error: {response.status_code} - {response.text}"

        except Exception as e:
            logging.error(f"Failed to send report: {e}")
            return False, str(e)
        finally:
            # Cleanup
            if zip_path and os.path.exists(zip_path):
                try:
                    os.remove(zip_path)
                except:
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
