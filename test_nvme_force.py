import sys
import logging
import ctypes
import os

# Add src to path so imports work
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.hal.asm_commander import ASMCommander

logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

def test_drive(index):
    print(f"\n--- Testing Drive {index} ---")
    with ASMCommander(index) as cmd:
        print("1. Trying NVMe Identify...")
        nvme_id = cmd.get_nvme_identify()
        if nvme_id:
            model, serial = ASMCommander.parse_nvme_identify_data(nvme_id)
            print(f"NVMe Identify Success! Model: {model}, Serial: {serial}")
        else:
            print("NVMe Identify Failed.")

        print("2. Trying NVMe SMART...")
        nvme_smart = cmd.get_nvme_smart_data()
        if nvme_smart:
            print(f"NVMe SMART Success! Read {len(nvme_smart)} bytes.")
        else:
            print("NVMe SMART Failed.")

if __name__ == "__main__":
    for i in range(2):
        test_drive(i)
