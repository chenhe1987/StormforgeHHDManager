import sys
import logging
from src.core.device_manager import DeviceManager
from src.hal.asm_commander import ASMCommander

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(message)s')

def list_disks():
    print("Enumerating disks...")
    dm = DeviceManager()
    disks = dm.get_physical_disks()
    for disk in disks:
        print(f"Index: {disk.index} | Model: {disk.model} | Serial: {disk.serial_number}")
    return disks

def test_spin_down(index):
    print(f"Attempting to spin down disk {index}...")
    try:
        with ASMCommander(index) as cmd:
            if cmd.spin_down():
                print(f"SUCCESS: Spin down command sent to disk {index}.")
            else:
                print(f"FAILURE: Failed to send spin down command to disk {index}.")
    except Exception as e:
        print(f"ERROR: Exception occurred: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python verify_disk_sleep.py [list|test <index>]")
        sys.exit(1)

    action = sys.argv[1]
    
    if action == "list":
        list_disks()
    elif action == "test":
        if len(sys.argv) < 3:
            print("Usage: python verify_disk_sleep.py test <index>")
            sys.exit(1)
        index = int(sys.argv[2])
        test_spin_down(index)
    else:
        print("Unknown command")
