import sys
import logging
import traceback
import wmi

sys.path.append('.')
from src.hal.win32_api import Win32API
from src.hal.asm_commander import ASMCommander
from src.utils.smart_parser import SmartParser

logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

def test_drive(index, model, serial):
    print(f"\n--- Testing Drive {index} ({model}) ---")
    
    # 1. Simulate the exact flow in monitor_service.py
    try:
        with ASMCommander(index, model_hint=model, serial_hint=serial) as cmd:
            attributes = []
            
            # NVMe Try
            print("1. Trying NVMe Identify...")
            nvme_id = cmd.get_nvme_identify()
            if nvme_id:
                print("   NVMe Identify Success!")
            else:
                print("   NVMe Identify Failed.")
                
            print("2. Trying NVMe SMART...")
            nvme_data = cmd.get_nvme_smart_data()
            if nvme_data:
                print("   NVMe SMART Success!")
                attributes = SmartParser.parse_nvme(nvme_data)
            else:
                print("   NVMe SMART Failed.")
                
            # SATA Try
            if not attributes:
                print("3. Trying SATA Identify...")
                id_data = cmd.identify_device()
                if id_data:
                    print("   SATA Identify Success!")
                    
                print("4. Trying SATA SMART...")
                raw_data = cmd.get_smart_data()
                if raw_data:
                    print("   SATA SMART Success!")
                    attributes = SmartParser.parse_512(raw_data)
                else:
                    print("   SATA SMART Failed.")
                    
            # PowerShell Try
            if not attributes:
                print("5. Trying PowerShell Fallback...")
                import subprocess, json
                ps_cmd = f'powershell -NoProfile -Command "Get-PhysicalDisk -DeviceNumber {index} | Get-StorageReliabilityCounter | Select-Object DeviceId, Temperature, Wear, PowerOnHours, ReadErrorsTotal, WriteErrorsTotal | ConvertTo-Json"'
                try:
                    output = subprocess.check_output(ps_cmd, shell=True, text=True, stderr=subprocess.DEVNULL)
                except subprocess.CalledProcessError:
                    output = ""
                
                if not output or not output.strip():
                    ps_cmd2 = f'powershell -NoProfile -Command "Get-WmiObject -Namespace root/Microsoft/Windows/Storage -Class MSFT_StorageReliabilityCounter | Where-Object DeviceId -eq {index} | Select-Object DeviceId, Temperature, Wear, PowerOnHours, ReadErrorsTotal, WriteErrorsTotal | ConvertTo-Json"'
                    try:
                        output = subprocess.check_output(ps_cmd2, shell=True, text=True, stderr=subprocess.DEVNULL)
                    except subprocess.CalledProcessError:
                        output = ""
                        
                if output and output.strip():
                    data = json.loads(output)
                    if isinstance(data, list) and len(data) > 0:
                        data = data[0]
                    if data and isinstance(data, dict):
                        print("   PowerShell Success!")
                        attributes = SmartParser.parse_powershell_nvme(data)
                        
            if attributes:
                print("6. Getting Summary...")
                summary = SmartParser.get_summary(attributes, None)
                print(f"   Status: {summary['status']}")
            else:
                print("   All methods failed. Attributes is empty.")
                
    except Exception as e:
        print(f"Exception occurred for drive {index}: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    c = wmi.WMI()
    for drive in c.Win32_DiskDrive():
        index = int(drive.DeviceID.upper().replace("\\\\.\\PHYSICALDRIVE", ""))
        model = drive.Model
        serial = drive.SerialNumber if drive.SerialNumber else ""
        if "NVME" in model.upper() or "WD BLUE" in model.upper() or "980 PRO" in model.upper():
            test_drive(index, model, serial)
