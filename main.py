import sys
import os

# 确保程序根目录在 import 路径中
if getattr(sys, 'frozen', False):
    # 如果是打包后的环境 (PyInstaller)
    # sys._MEIPASS 通常指向 EXE 所在目录或其 _internal 子目录
    base_path = sys._MEIPASS
    # 强制将 base_path 和 _internal 都加入路径
    internal_path = os.path.join(base_path, '_internal')
    if internal_path not in sys.path:
        sys.path.insert(0, internal_path)
    if base_path not in sys.path:
        sys.path.insert(0, base_path)
else:
    # 如果是开发环境
    base_path = os.path.dirname(os.path.abspath(__file__))
    if base_path not in sys.path:
        sys.path.insert(0, base_path)

import logging
import traceback
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QSharedMemory
from src.ui.main_window import MainWindow
from src.utils.admin import run_as_admin, is_admin
from src.utils.paths import get_base_path, get_resource_path

# 设置单例运行检测
shared_mem_key = "StormForgeDiskManager_UniqueKey_v1"

def main():
    # 1. 基础路径准备
    base_dir = get_base_path()
    log_file = os.path.join(base_dir, 'app.log')
    os.chdir(base_dir)

    # 2. 初始化日志
    logging.basicConfig(
        filename=log_file,
        filemode='a', # 改为追加模式，方便追踪多次运行
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    try:
        logging.info("--- 程序启动尝试 ---")
        logging.info(f"管理员权限: {is_admin()}, 运行目录: {base_dir}")
        
        # 3. 管理员权限提升逻辑
        if not is_admin():
            logging.info("非管理员权限，请求提升...")
            if run_as_admin():
                logging.info("已处于管理员模式 (不应进入此分支)")
                pass
            else:
                logging.info("UAC请求已发出，原进程准备退出")
                sys.exit(0)

        # 4. 单例检测 (必须在提升权限后，或者确保权限一致)
        # 使用 QSharedMemory 确保只有一个实例运行
        app = QApplication(sys.argv)
        
        shared_memory = QSharedMemory(shared_mem_key)
        if not shared_memory.create(1):
            logging.warning("程序已在运行中，激活现有窗口并退出")
            # 可以在这里增加激活原窗口的代码，暂时先弹窗提示
            QMessageBox.warning(None, "程序运行中", "疾风知硬盘柜管理程序已经在运行中。\n请在系统托盘或任务栏查找。")
            sys.exit(0)

        logging.info("单例检测通过，开始初始化 UI")
        
        # 5. UI 初始化
        icon_path = get_resource_path(os.path.join("assets", "icon.png"))
        if os.path.exists(icon_path):
            from PySide6.QtGui import QIcon
            app.setWindowIcon(QIcon(icon_path))

        window = MainWindow()
        window.show()
        window.activateWindow()
        window.raise_()
        
        exit_code = app.exec()
        logging.info(f"程序正常退出，代码: {exit_code}")
        sys.exit(exit_code)

    except Exception as e:
        error_msg = f"程序运行发生异常:\n{str(e)}\n\n{traceback.format_exc()}"
        logging.error(error_msg)
        
        if not QApplication.instance():
            temp_app = QApplication(sys.argv)
        
        QMessageBox.critical(None, "程序运行错误", error_msg)
        sys.exit(1)

if __name__ == "__main__":
    main()
