import sys
import time
import logging
from src.core.monitor_service import MonitorService
from src.utils.admin import run_as_admin

# Configure logging to output to stdout (which is redirected to monitor_test.log)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)

# Redirect stdout to file for debugging
class Logger(object):
    def __init__(self):
        self.terminal = sys.stdout
        self.log = open("monitor_test.log", "a", encoding='utf-8')

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)
        self.log.flush()

    def flush(self):
        self.terminal.flush()
        self.log.flush()

sys.stdout = Logger()
sys.stderr = sys.stdout

def mock_notify(title, msg):
    print(f"NOTIFY: {title} - {msg}")

def mock_update_ui(data):
    print(f"UPDATE UI: Received {len(data)} items")
    for item in data:
        model = item.get('model', 'Unknown')
        serial = item.get('serial', 'Unknown')
        print(f"  Disk {item.get('index')}: {model} (SN: {serial})")
        print(f"    Status: {item.get('status')} - Temp: {item.get('temp')}")
        print(f"    [Summary Data]")
        print(f"    Reallocated: {item.get('reallocated')}")
        print(f"    Pending: {item.get('pending')}")
        print(f"    SSD Life: {item.get('ssd_life_left')}")
        print(f"    Total Writes: {item.get('total_writes_gb')}")
        
        if item.get('index') == 0: # Print details for Disk 0
            print(f"    Health Advice: {item.get('health_advice')}")
            print(f"    Power Info: {item.get('power_info')}")
            print("    Attributes:")
            for attr in item.get('attributes', [])[:5]: # Print first 5 attributes
                print(f"      {attr.get('id')} {attr.get('name_cn')}: {attr.get('value')} (Raw: {attr.get('raw')})")

def main():
    if not run_as_admin():
        print("需要管理员权限运行测试")
        # Don't exit here if we are the parent process waiting for child
        return 
        
    print("启动 MonitorService 测试...")
    try:
        service = MonitorService(callback_notify=mock_notify, callback_update_ui=mock_update_ui)
        
        # 手动触发一次检测
        service.check_all_smart()
    except Exception as e:
        print(f"测试过程中发生异常: {e}")
        import traceback
        traceback.print_exc()
    
    print("测试完成")
    input("按回车键退出...")

if __name__ == "__main__":
    main()
