import os
import sys

def get_base_path():
    """获取程序运行时的基础路径（EXE所在目录或脚本目录）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def get_resource_path(relative_path):
    """获取资源文件的路径（兼容 PyInstaller 的临时目录）"""
    if getattr(sys, 'frozen', False):
        # PyInstaller 会将资源释放到 sys._MEIPASS
        base_path = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        return os.path.join(base_path, relative_path)
    # 开发模式下，资源目录在根目录下
    root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root_dir, relative_path)
