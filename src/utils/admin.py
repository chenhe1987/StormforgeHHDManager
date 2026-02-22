import ctypes
import sys
import os

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def run_as_admin():
    if is_admin():
        return True
    
    # 获取 Python 解释器的完整路径
    executable = sys.executable
    # 获取脚本的完整路径，并处理包含空格的情况
    params = f'"{os.path.abspath(sys.argv[0])}" ' + " ".join([f'"{arg}"' for arg in sys.argv[1:]])
    
    print(f"尝试以管理员权限启动: {executable} {params}")
    
    # ShellExecuteW: lpVerb='runas' 触发 UAC 提升
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, 
        "runas", 
        executable, 
        params, 
        os.path.abspath(os.getcwd()), # 设置工作目录为当前目录
        1 # SW_SHOWNORMAL
    )
    
    if int(ret) <= 32:
        print(f"管理员权限启动失败，错误代码: {ret}")
        return False
        
    return False # 返回 False 以便主进程退出
