import ctypes
from ctypes import wintypes
import sys

def inspect_tree(disk_index=0):
    try:
        import wmi
        c = wmi.WMI()
        drives = list(c.Win32_DiskDrive(Index=disk_index))
        if not drives:
            print(f"No disk found at index {disk_index}")
            return
            
        drive = drives[0]
        pnp_id = drive.PNPDeviceID
        print(f"Disk {disk_index} PNP ID: {pnp_id}")
        
        cfgmgr32 = ctypes.WinDLL('cfgmgr32')
        dev_inst = wintypes.DWORD()
        res = cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(dev_inst), pnp_id, 0)
        if res != 0:
            print("Failed to locate devnode")
            return
            
        current = dev_inst
        for i in range(6):
            # Get ID
            id_buffer = (ctypes.c_wchar * 260)()
            cfgmgr32.CM_Get_Device_IDW(current, id_buffer, 260, 0)
            node_id = id_buffer.value
            
            # Get Service
            CM_DRP_SERVICE = 0x00000005
            buffer_size = wintypes.DWORD(260 * 2)
            buffer = (ctypes.c_byte * buffer_size.value)()
            res = cfgmgr32.CM_Get_DevNode_Registry_PropertyW(
                current, CM_DRP_SERVICE, None, ctypes.byref(buffer), ctypes.byref(buffer_size), 0
            )
            service = "Unknown"
            if res == 0:
                service = ctypes.cast(buffer, ctypes.c_wchar_p).value
            
            print(f"Level {i}: ID={node_id}, Service={service}")
            
            # Get Parent
            parent = wintypes.DWORD()
            res = cfgmgr32.CM_Get_Parent(ctypes.byref(parent), current, 0)
            if res != 0:
                break
            current = parent
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    # Inspect Disk 1 (assuming it is the external one, if exists)
    # If not, try Disk 0
    print("--- Inspecting Disk 1 ---")
    inspect_tree(1)
    print("\n--- Inspecting Disk 0 ---")
    inspect_tree(0)
