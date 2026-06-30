import ctypes
from ctypes import wintypes
import logging

try:
    rstrtmgr = ctypes.windll.rstrtmgr
except Exception as e:
    rstrtmgr = None
    logging.error(f"无法加载 rstrtmgr.dll: {e}")

CCH_RM_MAX_APP_NAME = 255
CCH_RM_MAX_SVC_NAME = 63
RM_SESSION_KEY_LEN = 32

class RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [
        ("dwProcessId", wintypes.DWORD),
        ("ProcessStartTime", wintypes.FILETIME)
    ]

class RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [
        ("Process", RM_UNIQUE_PROCESS),
        ("strAppName", ctypes.c_wchar * (CCH_RM_MAX_APP_NAME + 1)),
        ("strServiceShortName", ctypes.c_wchar * (CCH_RM_MAX_SVC_NAME + 1)),
        ("ApplicationType", wintypes.DWORD),
        ("AppStatus", wintypes.ULONG),
        ("TSSessionId", wintypes.DWORD),
        ("bRestartable", wintypes.BOOL)
    ]

def get_locking_processes(path: str) -> list[str]:
    """
    使用 Windows Restart Manager 查找占用指定路径（如 'D:\\' 或具体文件）的进程名称。
    """
    if not rstrtmgr:
        return []

    session_handle = wintypes.DWORD(0)
    session_key = ctypes.create_unicode_buffer(RM_SESSION_KEY_LEN + 1)
    
    # 1. 启动会话
    res = rstrtmgr.RmStartSession(ctypes.byref(session_handle), 0, session_key)
    if res != 0:
        logging.debug(f"RmStartSession 失败，错误码: {res}")
        return []

    try:
        # 2. 注册资源
        # 注意：如果 path 是驱动器根目录，必须以反斜杠结尾，如 "D:\\"
        # 对于根目录，Restart Manager 有时候可能不会直接返回占用（特别是如果没有足够权限）
        # 尝试将路径转换为驱动器形式
        c_path = ctypes.c_wchar_p(path)
        c_path_array = (ctypes.c_wchar_p * 1)(c_path)
        
        res = rstrtmgr.RmRegisterResources(
            session_handle.value, 
            1, 
            ctypes.cast(c_path_array, ctypes.c_void_p), 
            0, None, 0, None
        )
        if res != 0:
            logging.debug(f"RmRegisterResources 失败，路径: {path}, 错误码: {res}")
            # 如果权限不足 (ERROR_ACCESS_DENIED = 5)，也可能静默失败
            return []

        # 3. 获取占用列表
        nProcInfoNeeded = wintypes.UINT(0)
        nProcInfo = wintypes.UINT(0)
        rebootReasons = wintypes.DWORD(0)
        
        # 第一次调用：获取需要的数组大小 (预期返回 ERROR_MORE_DATA = 234)
        res = rstrtmgr.RmGetList(
            session_handle.value,
            ctypes.byref(nProcInfoNeeded),
            ctypes.byref(nProcInfo),
            None,
            ctypes.byref(rebootReasons)
        )
        
        # 如果没有被占用，通常会返回 0，并且 nProcInfoNeeded 为 0
        # ERROR_ACCESS_DENIED (5) 是权限不足，静默忽略
        if res == 0 and nProcInfoNeeded.value == 0:
            return []
            
        if res != 234: 
            if res != 5: # 忽略权限不足日志
                logging.debug(f"RmGetList (1) 返回非预期代码: {res}")
            return []

        if nProcInfoNeeded.value == 0:
            return []

        # 分配数组并第二次调用
        process_info_array = (RM_PROCESS_INFO * nProcInfoNeeded.value)()
        nProcInfo.value = nProcInfoNeeded.value
        
        res = rstrtmgr.RmGetList(
            session_handle.value,
            ctypes.byref(nProcInfoNeeded),
            ctypes.byref(nProcInfo),
            ctypes.cast(process_info_array, ctypes.POINTER(RM_PROCESS_INFO)),
            ctypes.byref(rebootReasons)
        )
        
        if res != 0:
            logging.debug(f"RmGetList (2) 失败，错误码: {res}")
            return []

        locking_apps = []
        for i in range(nProcInfo.value):
            app_name = process_info_array[i].strAppName
            if app_name:
                locking_apps.append(app_name)
                
        # 去重
        return list(set(locking_apps))

    finally:
        # 4. 结束会话
        rstrtmgr.RmEndSession(session_handle.value)
