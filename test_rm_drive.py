import sys
import ctypes
import os
import time

def test_rm():
    from src.utils.restart_manager import get_locking_processes
    
    # 模拟锁定一个文件
    test_file = "test_lock.txt"
    f = open(test_file, "w")
    f.write("test")
    
    # 尝试获取占用
    try:
        path = os.path.abspath(test_file)
        print(f"Checking {path}...")
        apps = get_locking_processes(path)
        print(f"Apps locking {test_file}: {apps}")
    finally:
        f.close()
        
    print("\nNow checking a drive (e.g. C:\\ or D:\\)")
    for drive in ["D:\\", "E:\\", "C:\\"]:
        if os.path.exists(drive):
            print(f"Checking {drive}...")
            try:
                apps = get_locking_processes(drive)
                print(f"Apps locking {drive}: {apps}")
                if apps:
                    break
            except Exception as e:
                print(f"Error checking {drive}: {e}")

if __name__ == "__main__":
    sys.path.insert(0, os.path.abspath("."))
    test_rm()