from PySide6.QtWidgets import (QMainWindow, QLabel, QVBoxLayout, QHBoxLayout, QWidget, 
                             QMessageBox, QSystemTrayIcon, QMenu, QApplication, QStyle, 
                             QTableWidget, QTableWidgetItem, QHeaderView, QListWidget, 
                             QListWidgetItem, QFrame, QScrollArea, QPushButton, QSlider,
                             QDialog, QTextEdit, QFileDialog, QCheckBox, QInputDialog)
from PySide6.QtWidgets import QTabWidget, QGridLayout
from PySide6.QtGui import QIcon, QAction, QColor, QFont, QPalette, QPixmap
from PySide6.QtCore import Qt, Slot, Signal, QSize, QThread, QTimer
import logging
import os
import sys
import time
import zipfile
import shutil
import subprocess
import requests
import webbrowser
import ctypes
import threading
from ctypes import wintypes
from datetime import datetime

from src.hal.win32_api import Win32API, SafeRemovalPatcher, WM_DEVICECHANGE
from src.core.monitor_service import MonitorService
from src.core.device_manager import DeviceManager
from src.core.config_manager import ConfigManager
from src.core.shutdown_guard import ShutdownGuard
from src.utils.paths import get_resource_path
from src.utils.log_reporter import LogReporter, load_report_server_config

logging.info("Win32API 导入成功")

# Win32 Constants
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012

class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
#       ("lPrivate", wintypes.DWORD), # Padding sometimes needed
    ]

class UpdateCheckThread(QThread):
    """后台线程检查 Gitee 更新"""
    finished_signal = Signal(dict) # Returns a dict with update info

    def __init__(self, current_version):
        super().__init__()
        self.current_version = current_version.lower().lstrip('v')
        self.repo_url = "https://gitee.com/api/v5/repos/stormforge/JiFengZhiHDDManager/releases/latest"

    def run(self):
        try:
            response = requests.get(self.repo_url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                latest_tag = data.get("tag_name", "v0.0.0").lower().lstrip('v')
                
                # Simple version comparison
                is_newer = self.compare_versions(latest_tag, self.current_version) > 0
                
                self.finished_signal.emit({
                    "success": True,
                    "is_newer": is_newer,
                    "latest_version": data.get("tag_name"),
                    "changelog": data.get("body", "无更新日志"),
                    "download_url": data.get("html_url")
                })
            else:
                self.finished_signal.emit({"success": False, "error": f"API Error: {response.status_code}"})
        except Exception as e:
            logging.error(f"检查更新失败: {e}")
            self.finished_signal.emit({"success": False, "error": str(e)})

    def compare_versions(self, v1, v2):
        """Compare two version strings like 1.3.33 and 1.3.32"""
        try:
            parts1 = [int(p) for p in v1.split('.')]
            parts2 = [int(p) for p in v2.split('.')]
            
            # Pad with zeros
            max_len = max(len(parts1), len(parts2))
            parts1.extend([0] * (max_len - len(parts1)))
            parts2.extend([0] * (max_len - len(parts2)))
            
            for i in range(max_len):
                if parts1[i] > parts2[i]:
                    return 1
                if parts1[i] < parts2[i]:
                    return -1
            return 0
        except Exception:
            return 0

class ListHandler(logging.Handler):
    def __init__(self, log_list=None):
        super().__init__()
        self.records = log_list if log_list is not None else []

    def emit(self, record):
        try:
            msg = self.format(record)
            self.records.append(msg)
        except Exception:
            pass

class LogDialog(QDialog):
    def __init__(self, logs, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设备扫描与恢复日志")
        self.resize(700, 500)
        layout = QVBoxLayout(self)
        
        info_label = QLabel("如果设备未能成功恢复，请查看以下日志寻找原因：")
        layout.addWidget(info_label)
        
        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setPlainText("\n".join(logs))
        self.text_edit.setStyleSheet("font-family: Consolas, monospace; font-size: 10pt;")
        layout.addWidget(self.text_edit)
        
        btn_layout = QHBoxLayout()
        copy_btn = QPushButton("复制日志")
        copy_btn.clicked.connect(self.copy_logs)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(copy_btn)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)
        
    def copy_logs(self):
        clipboard = QApplication.clipboard()
        clipboard.setText(self.text_edit.toPlainText())
        QMessageBox.information(self, "提示", "日志已复制到剪贴板")

class ReportDialog(QDialog):
    _upload_finished = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("错误报告与分析")
        self.resize(700, 600)
        self.setStyleSheet("""
            QDialog { background-color: #1a1a1a; color: #ffffff; }
            QLabel { color: #ffffff; font-size: 14px; }
            QPushButton { background-color: #333333; color: white; border: 1px solid #555; padding: 6px 12px; border-radius: 4px; }
            QPushButton:hover { background-color: #444444; }
            QTextEdit { background-color: #262626; color: #dddddd; border: 1px solid #3d3d3d; font-family: Consolas; font-size: 12px; }
            QCheckBox { color: #aaaaaa; }
        """)
        
        self.reporter = LogReporter()
        self.config_manager = parent.config_manager if parent else ConfigManager()
        self.report_config = self.config_manager.get_report_config()
        self.app_version = getattr(parent, "version", None) or "1.3.74"
        self._server_cfg = load_report_server_config()
        self._upload_finished.connect(self._on_upload_finished)
        
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # 1. Header
        header = QLabel("自动日志分析结果")
        header.setStyleSheet("font-weight: bold; color: #76b900; font-size: 16px;")
        layout.addWidget(header)
        
        # 2. Analysis Text
        self.analysis_text = QTextEdit()
        self.analysis_text.setReadOnly(True)
        layout.addWidget(self.analysis_text)
        
        # Load analysis immediately
        self.load_analysis()
        
        # 3. Settings
        # 自动上传未启用：一键上传已覆盖人工反馈场景，避免误传
        # （报告服务器地址/密钥由程序目录 report_server.json 提供，如需自动上传可在此开启）

        # 4. Actions
        btn_layout = QHBoxLayout()
        
        self.export_btn = QPushButton("仅导出到本地...")
        self.export_btn.clicked.connect(self.do_export)
        
        self.send_btn = QPushButton("一键上传服务器 (推荐)")
        self.send_btn.setStyleSheet("background-color: #76b900; color: black; font-weight: bold; border: none;")
        self.send_btn.clicked.connect(self.do_upload_report)
        
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.accept)
        
        btn_layout.addWidget(self.export_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.send_btn)
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)
        
    def load_analysis(self):
        issues = self.reporter.analyze_logs()
        if not issues:
            self.analysis_text.setPlainText("未发现明显的错误日志。")
        else:
            self.analysis_text.setPlainText("\n\n".join(issues))
            
    def do_export(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"JiFengZhi_Logs_{timestamp}.zip"
        
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出错误日志", 
            os.path.join(os.path.expanduser("~"), "Desktop", default_name),
            "Zip Files (*.zip)"
        )
        
        if file_path:
            zip_path = self.reporter.pack_logs()
            if zip_path:
                shutil.copy2(zip_path, file_path)
                QMessageBox.information(self, "导出成功", f"日志已保存至:\n{file_path}")
                try:
                    subprocess.Popen(f'explorer /select,"{file_path}"')
                except:
                    pass
            else:
                QMessageBox.critical(self, "导出失败", "无法创建日志包")
        
    def do_upload_report(self):
        """一键上传：打包日志 → POST 到云主机固定目录。"""
        if not self._server_cfg:
            QMessageBox.warning(
                self, "未配置报告服务器",
                "程序目录下缺少 report_server.json（云端地址与密钥）。\n"
                "请改用「仅导出到本地」保存日志包。",
            )
            return
        dialog = QInputDialog(self)
        dialog.setWindowTitle("上传日志报告")
        dialog.setLabelText("请简单描述遇到的问题（可留空）：")
        dialog.setOption(QInputDialog.UsePlainTextEditForTextInput, True)
        dialog.setOkButtonText("上传")
        dialog.setCancelButtonText("取消")
        dialog.setStyleSheet("""
            QInputDialog { background-color: #1a1a1a; color: #ffffff; }
            QLabel { color: #ffffff; }
            QPlainTextEdit, QTextEdit, QLineEdit {
                background-color: #262626; color: #ffffff;
                border: 1px solid #555555; border-radius: 4px;
                padding: 8px; font-size: 14px;
                selection-background-color: #9147ff;
                selection-color: #ffffff;
            }
        """)
        dialog.resize(520, 280)
        if dialog.exec() != QDialog.Accepted:
            return
        desc = dialog.textValue().strip()[:500]

        self.send_btn.setEnabled(False)
        self.send_btn.setText("正在上传...")
        self.status_hint = desc
        QApplication.processEvents()

        threading.Thread(
            target=self._upload_worker,
            args=(desc, self.app_version),
            daemon=True,
        ).start()

    def _upload_worker(self, description, version):
        try:
            ok, msg = self.reporter.send_report(
                description=description, version=version
            )
            result = {"ok": ok, "msg": msg}
        except Exception as e:
            logging.error(f"上传日志异常: {e}")
            result = {"ok": False, "msg": str(e)}
        self._upload_finished.emit(result)

    def _on_upload_finished(self, result):
        self.send_btn.setEnabled(True)
        self.send_btn.setText("一键上传服务器 (推荐)")

        if result.get("ok"):
            QMessageBox.information(
                self, "上传成功",
                "日志已上传成功，可用于排查问题。\n请将问题发生的时间和操作步骤告知技术支持。"
            )
            self.accept()
        else:
            box = QMessageBox(self)
            box.setWindowTitle("上传失败")
            box.setText(
                f"上传失败：{result.get('msg')}\n\n"
                "可以改用「仅导出到本地」保存日志包，稍后重试上传。"
            )
            save_btn = box.addButton("导出到本地", QMessageBox.ActionRole)
            box.addButton("关闭", QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() == save_btn:
                self.do_export()
            
    def accept(self):
        # Save checkbox state if it existed
        # self.config_manager.set_report_config(self.auto_report_cb.isChecked())
        super().accept()

NVIDIA_STYLE = """
QMainWindow {
    background-color: #0c0c0c;
    font-family: "OPPO Sans", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 14px;
}
QWidget {
    font-family: "OPPO Sans", "Microsoft YaHei", "Segoe UI", sans-serif;
    color: #ffffff;
}
QWidget#CentralWidget {
    background-color: #0c0c0c;
}
QListWidget {
    background-color: #1a1a1a;
    border: none;
    outline: none;
    padding: 0px;
}
QListWidget::item {
    background-color: #262626;
    border: 1px solid #2d2d2d;
    border-radius: 6px;
    margin: 3px 8px;
}
QListWidget::item:selected {
    background-color: #333333;
    border: 1px solid #9147ff;
}
QListWidget::item:hover {
    background-color: #2d2d2d;
}
QLabel {
    color: #ffffff;
}
QLabel#TitleLabel {
    font-size: 25px;
    font-weight: bold;
    color: #9147ff;
}
QLabel#StatusLabel {
    font-size: 14px;
    color: #aaaaaa;
}
QFrame#Card, QFrame#DetailCard {
    background-color: #1a1a1a;
    border: 1px solid #2d2d2d;
    border-radius: 8px;
}
QTableWidget {
    background-color: #1a1a1a;
    color: #ffffff;
    gridline-color: #2d2d2d;
    border: 1px solid #2d2d2d;
    border-radius: 6px;
    selection-background-color: #333333;
}
QHeaderView::section {
    background-color: #262626;
    color: #9147ff;
    padding: 8px;
    border: none;
    border-right: 1px solid #2d2d2d;
    font-weight: bold;
}
QTabWidget::pane {
    border: none;
    background: transparent;
    top: -1px;
}
QTabBar::tab {
    background: transparent;
    color: #aaaaaa;
    padding: 10px 22px;
    margin-right: 6px;
    border-bottom: 2px solid transparent;
    font-weight: bold;
}
QTabBar::tab:selected {
    color: #ffffff;
    border-bottom: 2px solid #9147ff;
}
QTabBar::tab:hover { color: #c19bff; }
QPushButton {
    min-height: 36px;
    padding: 0 16px;
    border-radius: 7px;
    border: 1px solid #444444;
    background-color: #333333;
    color: #ffffff;
    font-weight: bold;
}
QPushButton:hover { background-color: #444444; border-color: #666666; }
QPushButton:pressed { background-color: #262626; }
QPushButton:disabled { background-color: #1a1a1a; color: #666666; border-color: #2d2d2d; }
QPushButton#PrimaryAction {
    background-color: #76b900;
    color: #000000;
    border-color: #76b900;
    min-height: 42px;
    padding: 0 24px;
}
QPushButton#PrimaryAction:hover { background-color: #88d000; }
QPushButton#SecondaryAction {
    background-color: #9147ff;
    color: #ffffff;
    border-color: #9147ff;
    min-height: 42px;
    padding: 0 24px;
}
QPushButton#SecondaryAction:hover { background-color: #a66dff; border-color: #a66dff; }
QCheckBox { spacing: 9px; color: #dddddd; }
QCheckBox::indicator { width: 20px; height: 20px; }
QSlider::groove:horizontal {
    height: 6px;
    background: #1a1a1a;
    border: 1px solid #3d3d3d;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    width: 18px;
    height: 18px;
    margin: -6px 0;
    background: #76b900;
    border: 1px solid #76b900;
    border-radius: 9px;
}
QScrollBar:vertical {
    background: transparent;
    width: 9px;
}
QScrollBar::handle:vertical {
    background: #333333;
    border-radius: 4px;
    min-height: 40px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QMenu {
    background-color: #1a1a1a;
    color: #ffffff;
    border: 1px solid #2d2d2d;
}
QMenu::item {
    padding: 8px 25px;
}
QMenu::item:selected {
    background-color: #333333;
    color: #9147ff;
}
QMessageBox {
    background-color: #1a1a1a;
}
QMessageBox QLabel {
    color: #ffffff;
}
"""

class MainWindow(QMainWindow):
    # Signal to update UI from background thread
    update_data_signal = Signal(list)
    _refresh_done_signal = Signal(dict)
    _wake_done_signal = Signal(dict)
    # 注意：后台线程里**不能**用 QTimer.singleShot 回主线程——它依赖调用线程的
    # 事件循环，普通 threading.Thread 没有事件循环，回调永远不会触发
    # （表现为按钮一直停在“正在弹出.../正在深度休眠...”）。跨线程回主线程必须用信号。
    _eject_done_signal = Signal(dict)
    _spindown_done_signal = Signal(dict)
    _eject_progress_signal = Signal(str)

    # WM_DEVICECHANGE 的 wParam：设备到达 / 移除完成 / 设备树变化 → 需要重扫
    DEVICE_CHANGE_TRIGGERS = (0x8000, 0x8004, 0x0007)
    DEVICE_CHANGE_DEBOUNCE_MS = 1500

    def __init__(self, silent_mode=False):
        logging.info("正在初始化 MainWindow...")
        super().__init__()
        self._silent_mode = silent_mode
        self.version = "1.3.91"
        self.setWindowTitle(f"疾风知硬盘柜管理程序 v{self.version}")
        self.resize(1220, 800)
        self.setMinimumSize(1040, 680)
        self.setStyleSheet(NVIDIA_STYLE)
        
        # 提高进程关机优先级 (0x280 > 0x100)
        try:
            ctypes.windll.kernel32.SetProcessShutdownParameters(0x280, 0)
            logging.info("进程关机优先级已设置为 0x280")
        except Exception as e:
            logging.warning(f"设置关机优先级失败: {e}")
        
        # Initialize Managers
        self.config_manager = ConfigManager()
        self.device_manager = DeviceManager()
        
        # Load Icon
        icon_path = get_resource_path(os.path.join("assets", "icon.png"))
        if os.path.exists(icon_path):
            app_icon = QIcon(icon_path)
            self.setWindowIcon(app_icon)
        else:
            logging.warning(f"Icon file not found at {icon_path}")

        # 启用深色标题栏
        try:
            Win32API.set_dark_mode(int(self.winId()))
        except Exception as e:
            logging.warning(f"启用深色标题栏失败: {e}")
        
        # Connect signal
        self.update_data_signal.connect(self.handle_data_update)
        self._refresh_done_signal.connect(self._on_refresh_done)
        self._wake_done_signal.connect(self._on_wake_complete)
        self._eject_done_signal.connect(self._on_eject_complete)
        self._spindown_done_signal.connect(self._on_spin_down_complete)
        self._eject_progress_signal.connect(self._on_eject_progress)
        self._eject_active = None
        self._hardware_refresh_active = False

        # 热插拔：设备到达/移除/设备树变化后去抖重扫（USB 硬盘柜上电会连发一串事件）。
        # 单次触发 + 重复 start() 重新计时 = 去抖；真正的重扫在工作线程里做。
        self._pending_device_change = 0
        self._device_change_timer = QTimer(self)
        self._device_change_timer.setSingleShot(True)
        self._device_change_timer.setInterval(self.DEVICE_CHANGE_DEBOUNCE_MS)
        self._device_change_timer.timeout.connect(self._on_device_change_settled)
        
        # UI Setup
        central_widget = QWidget()
        central_widget.setObjectName("CentralWidget")
        self.setCentralWidget(central_widget)
        
        self.main_layout = QHBoxLayout(central_widget)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        
        # Left Sidebar Area (Logo + List)
        self.sidebar_container = QWidget()
        self.sidebar_container.setFixedWidth(340)
        self.sidebar_container.setStyleSheet("background-color: #1a1a1a; border-right: 1px solid #2d2d2d;")
        self.sidebar_layout = QVBoxLayout(self.sidebar_container)
        self.sidebar_layout.setContentsMargins(0, 0, 0, 0)
        self.sidebar_layout.setSpacing(0)
        
        # Logo Area
        self.logo_label = QLabel()
        self.logo_label.setAlignment(Qt.AlignCenter)
        self.logo_label.setContentsMargins(0, 20, 0, 20)
        # Try loading user logo first
        logo_path = get_resource_path(os.path.join("assets", "资源 2stormforge_logo.png"))
        if not os.path.exists(logo_path):
             logo_path = get_resource_path(os.path.join("assets", "stormforge_logo_d4.png"))
        if not os.path.exists(logo_path):
             logo_path = get_resource_path(os.path.join("assets", "icon.png")) # Fallback to app icon
             
        if os.path.exists(logo_path):
            pixmap = QPixmap(logo_path)
            # Scale to fit width, keep aspect ratio
            scaled_pixmap = pixmap.scaled(200, 100, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.logo_label.setPixmap(scaled_pixmap)
        else:
            self.logo_label.setText("疾风知硬盘柜")
            self.logo_label.setStyleSheet("color: #9147ff; font-weight: bold; font-size: 20px; padding: 20px;")
            
        self.sidebar_layout.addWidget(self.logo_label)

        # Disk List
        self.disk_list_title = QLabel("管理硬盘")
        self.disk_list_title.setStyleSheet(
            "color: #ffffff; font-size: 17px; font-weight: bold; padding: 4px 14px 0 14px;"
        )
        self.sidebar_layout.addWidget(self.disk_list_title)
        self.disk_list_hint = QLabel("勾选“管理”后，程序才会读取 SMART、休眠、弹出或在关机时停转该硬盘。")
        self.disk_list_hint.setWordWrap(True)
        self.disk_list_hint.setStyleSheet(
            "color: #aaaaaa; font-size: 13px; padding: 2px 14px 10px 14px;"
        )
        self.sidebar_layout.addWidget(self.disk_list_hint)

        self.sidebar = QListWidget()
        self.sidebar.setFrameShape(QFrame.NoFrame) # Remove border as container has it
        self.sidebar.setSpacing(3)
        self.sidebar.setStyleSheet("""
            QListWidget { background: #1a1a1a; border: none; outline: none; }
            QListWidget::item { border-radius: 5px; margin: 0 7px; }
            QListWidget::item:selected { background: #303030; }
            QListWidget::item:hover { background: #252525; }
        """)
        self.sidebar.itemClicked.connect(self.on_disk_selected)
        self._whitelist_checkboxes = {}
        # Give stretch to list so it takes available space
        self.sidebar_layout.addWidget(self.sidebar, 1)

        # Settings Area (Placed in Sidebar, AT THE BOTTOM)
        self.settings_container = QWidget()
        self.settings_container.setStyleSheet("background-color: #1a1a1a; border-top: 1px solid #2d2d2d;")
        self.settings_layout = QVBoxLayout(self.settings_container)
        self.settings_layout.setContentsMargins(10, 10, 10, 10)
        self.settings_layout.setSpacing(8)

        # 1. Refresh Button (Green)
        self.refresh_btn = QPushButton("刷新硬盘状态")
        self.refresh_btn.setToolTip("重新读取已管理硬盘的状态，不重启硬盘柜或存储控制器。")
        self.refresh_btn.setObjectName("RefreshButton")
        self.refresh_btn.setFixedHeight(38)
        self.refresh_btn.setStyleSheet("""
            QPushButton#RefreshButton {
                background-color: #76b900;
                color: #000000;
                border: none;
                border-radius: 4px;
                font-weight: bold;
                font-size: 13px;
            }
            QPushButton#RefreshButton:hover { background-color: #88d000; }
        """)
        self.refresh_btn.clicked.connect(self.on_refresh_clicked)
        self.settings_layout.addWidget(self.refresh_btn)
        self.refresh_warning = QLabel("如设备未出现，建议关闭读写任务后重启对应硬盘柜。")
        self.refresh_warning.setWordWrap(True)
        self.refresh_warning.setStyleSheet("color: #8f9bac; font-size: 12px;")
        self.settings_layout.addWidget(self.refresh_warning)


        # 2. Check Update Button (Purple accent)
        self.check_update_btn = QPushButton("检查软件更新")
        self.check_update_btn.setObjectName("CheckUpdateButton")
        self.check_update_btn.setFixedHeight(32)
        self.check_update_btn.setStyleSheet("""
            QPushButton#CheckUpdateButton {
                background-color: #333333;
                color: #ffffff;
                border: 1px solid #9147ff;
                border-radius: 4px;
                font-size: 12px;
            }
            QPushButton#CheckUpdateButton:hover { background-color: #444444; border-color: #a875ff; }
        """)
        self.check_update_btn.clicked.connect(self.on_check_update_clicked)
        self.settings_layout.addWidget(self.check_update_btn)

        # 3. Export Logs Button (Grey)
        self.export_logs_btn = QPushButton("错误报告与分析")
        self.export_logs_btn.setObjectName("ExportLogsButton")
        self.export_logs_btn.setFixedHeight(32)
        self.export_logs_btn.setStyleSheet("""
            QPushButton#ExportLogsButton {
                background-color: #333333;
                color: #ffffff;
                border: 1px solid #444444;
                border-radius: 4px;
                font-size: 12px;
            }
            QPushButton#ExportLogsButton:hover { background-color: #444444; }
        """)
        self.export_logs_btn.clicked.connect(self.export_logs)
        self.settings_layout.addWidget(self.export_logs_btn)

        # 4. Autostart Checkbox — 驱动级功能：固定启用，无需 UI 操作
        from PySide6.QtWidgets import QCheckBox
        self.autostart_checkbox = QCheckBox("随系统启动（驱动级，固定开启）")
        self.autostart_checkbox.setStyleSheet("color: #888888; font-size: 12px; padding: 5px;")
        self.autostart_checkbox.setChecked(True)
        self.autostart_checkbox.setEnabled(False)
        self.autostart_checkbox.setToolTip("程序已注册为开机自启动，静默运行守护硬盘休眠保护。")
        self.settings_layout.addWidget(self.autostart_checkbox)

        # 5. Shutdown Auto-Eject Checkbox — 驱动级功能：固定启用
        self.shutdown_eject_checkbox = QCheckBox("关机/休眠时自动停转白名单硬盘（固定开启）")
        self.shutdown_eject_checkbox.setStyleSheet("color: #888888; font-size: 12px; padding: 5px;")
        self.shutdown_eject_checkbox.setChecked(True)
        self.shutdown_eject_checkbox.setEnabled(False)
        self.shutdown_eject_checkbox.setToolTip("系统关机时仅处理白名单外置硬盘；休眠事件遵循相同白名单限制。")
        self.settings_layout.addWidget(self.shutdown_eject_checkbox)

        # 6. Safe Removal Spin-Down Patch Checkbox — 驱动级功能：固定启用
        self.spindown_patch_checkbox = QCheckBox("GUI 停转并安全弹出")
        self.spindown_patch_checkbox.setStyleSheet("color: #888888; font-size: 12px; padding: 5px;")
        self.spindown_patch_checkbox.setChecked(True)
        self.spindown_patch_checkbox.setEnabled(False)
        self.spindown_patch_checkbox.setToolTip(
            "请在本程序界面选择硬盘并点击“停转并安全弹出”。\n"
            "Windows 系统托盘弹出不再由本程序接管。"
        )
        self.settings_layout.addWidget(self.spindown_patch_checkbox)
        
        # Add Settings Container to Sidebar (Fixed height by content, NO stretch)
        self.sidebar_layout.addWidget(self.settings_container, 0)

        self.main_layout.addWidget(self.sidebar_container)
        
        # Right Area: Details
        self.detail_area = QWidget()
        self.detail_layout = QVBoxLayout(self.detail_area)
        self.detail_layout.setContentsMargins(30, 30, 30, 30)
        self.detail_layout.setSpacing(20)
        
        self.main_layout.addWidget(self.detail_area)
        
        # Header in details
        self.title_label = QLabel("硬盘状态概览")
        self.title_label.setObjectName("TitleLabel")
        self.detail_layout.addWidget(self.title_label)
        
        self.status_label = QLabel("正在初始化后台监控服务...")
        self.status_label.setObjectName("StatusLabel")
        self.detail_layout.addWidget(self.status_label)

        # Interval Config Area
        self.interval_container = QWidget()
        self.interval_layout = QHBoxLayout(self.interval_container)
        self.interval_label = QLabel("SMART 检测频率: 24小时/次")
        self.interval_label.setStyleSheet("color: #aaaaaa; font-size: 13px;")
        
        self.interval_slider = QSlider(Qt.Horizontal)
        self.interval_slider.setMinimum(5) # 5 minutes
        self.interval_slider.setMaximum(1440) # 24 hours (1440 mins)
        self.interval_slider.setValue(1440)
        self.interval_slider.setSingleStep(5)
        self.interval_slider.setPageStep(60)
        self.interval_slider.setStyleSheet("""
            QSlider::groove:horizontal {
                border: 1px solid #3d3d3d;
                height: 8px;
                background: #1a1a1a;
                margin: 2px 0;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #76b900;
                border: 1px solid #76b900;
                width: 18px;
                height: 18px;
                margin: -7px 0;
                border-radius: 9px;
            }
        """)
        self.interval_slider.valueChanged.connect(self.on_interval_changed)
        self.interval_slider.sliderReleased.connect(self.on_interval_set)
        
        self.interval_layout.addWidget(self.interval_label)
        self.interval_layout.addWidget(self.interval_slider)
        self.detail_layout.addWidget(self.interval_container)

        # Sleep Timer Config Area
        self.sleep_timer_container = QWidget()
        self.sleep_timer_layout = QHBoxLayout(self.sleep_timer_container)
        self.sleep_timer_label = QLabel("硬盘休眠时间: 从不休眠")
        self.sleep_timer_label.setStyleSheet("color: #aaaaaa; font-size: 13px;")
        
        self.sleep_timer_slider = QSlider(Qt.Horizontal)
        self.sleep_timer_slider.setMinimum(0) # 0 = Never
        self.sleep_timer_slider.setMaximum(60) # 60 minutes
        self.sleep_timer_slider.setValue(0)
        self.sleep_timer_slider.setSingleStep(1)
        self.sleep_timer_slider.setPageStep(5)
        self.sleep_timer_slider.setStyleSheet("""
            QSlider::groove:horizontal {
                border: 1px solid #3d3d3d;
                height: 8px;
                background: #1a1a1a;
                margin: 2px 0;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #9147ff;
                border: 1px solid #9147ff;
                width: 18px;
                height: 18px;
                margin: -7px 0;
                border-radius: 9px;
            }
        """)
        self.sleep_timer_slider.valueChanged.connect(self.on_sleep_timer_changed)
        self.sleep_timer_slider.sliderReleased.connect(self.on_sleep_timer_set)
        
        self.sleep_timer_layout.addWidget(self.sleep_timer_label)
        self.sleep_timer_layout.addWidget(self.sleep_timer_slider)
        self.detail_layout.addWidget(self.sleep_timer_container)

        # Action Buttons Area
        self.actions_layout = QHBoxLayout()
        
        self.spin_down_button = QPushButton("深度休眠")
        self.spin_down_button.setObjectName("SpinDownButton")
        self.spin_down_button.setFixedWidth(180)
        self.spin_down_button.setStyleSheet("""
            QPushButton#SpinDownButton {
                background-color: #9147ff;
                color: #ffffff;
                border: 1px solid #9147ff;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton#SpinDownButton:hover {
                background-color: #a66dff;
                border-color: #a66dff;
            }
            QPushButton#SpinDownButton:pressed {
                background-color: #7a35e0;
            }
            QPushButton#SpinDownButton:disabled {
                background-color: #1a1a1a;
                color: #4d4d4d;
                border-color: #2d2d2d;
            }
        """)
        self.spin_down_button.clicked.connect(self.on_spin_down_clicked)
        self.spin_down_button.setEnabled(False) # Default disabled
        self.actions_layout.addWidget(self.spin_down_button)

        self.actions_layout.addSpacing(10)

        self.eject_button = QPushButton("停转并安全弹出")
        self.eject_button.setObjectName("EjectButton")
        self.eject_button.setFixedWidth(180)
        self.eject_button.clicked.connect(self.on_eject_clicked)
        self.eject_button.setEnabled(False) # Default disabled
        self.actions_layout.addWidget(self.eject_button)
        self.actions_layout.addStretch()
        self.detail_layout.addLayout(self.actions_layout)

        # Windows 休眠机制调整说明（常驻提示，配合深度休眠/黑名单机制使用）
        self.windows_sleep_hint = QLabel(
            "Windows 系统休眠调整方法：控制面板 → 电源选项 → 更改计划设置 → 更改高级电源设置 → "
            "硬盘 → 在此时间后关闭硬盘，建议设置为 10-20 分钟（不要设为「从不」，"
            "否则 Windows 可能会周期性唤醒已休眠的硬盘）。\n"
            "注意：Windows 不会自动停转 USB 硬盘柜中的硬盘，深度休眠与关机保护由本程序负责。"
        )
        self.windows_sleep_hint.setWordWrap(True)
        self.windows_sleep_hint.setStyleSheet(
            "color: #999999; font-size: 12px; padding: 6px 2px; line-height: 1.6;"
        )
        self.detail_layout.addWidget(self.windows_sleep_hint)
        
        # Content Scroll Area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("background: transparent;")
        
        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_layout.setSpacing(20)
        
        # Summary Card
        self.summary_card = QFrame()
        self.summary_card.setObjectName("DetailCard")
        self.summary_layout = QVBoxLayout(self.summary_card)
        self.summary_label = QLabel("基本信息")
        self.summary_label.setStyleSheet("font-weight: bold; color: #76b900; font-size: 16px;")
        self.summary_layout.addWidget(self.summary_label)
        
        self.summary_table = QTableWidget()
        self.summary_table.setColumnCount(2)
        self.summary_table.verticalHeader().setVisible(False)
        self.summary_table.horizontalHeader().setVisible(False)
        self.summary_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.summary_table.setFixedHeight(200)
        self.summary_layout.addWidget(self.summary_table)
        self.scroll_layout.addWidget(self.summary_card)

        # Health Advice Card
        self.advice_card = QFrame()
        self.advice_card.setObjectName("DetailCard")
        self.advice_layout = QVBoxLayout(self.advice_card)
        self.advice_title = QLabel("健康状态总结与建议")
        self.advice_title.setStyleSheet("font-weight: bold; color: #76b900; font-size: 16px;")
        self.advice_layout.addWidget(self.advice_title)
        
        self.advice_text = QLabel("正在分析健康数据...")
        self.advice_text.setWordWrap(True)
        self.advice_text.setStyleSheet("color: #ffffff; line-height: 1.5; font-size: 14px; padding: 10px;")
        self.advice_layout.addWidget(self.advice_text)
        self.scroll_layout.addWidget(self.advice_card)
        
        # SMART Attributes Card
        self.smart_card = QFrame()
        self.smart_card.setObjectName("DetailCard")
        self.smart_layout = QVBoxLayout(self.smart_card)
        self.smart_title = QLabel("SMART 详细参数")
        self.smart_title.setStyleSheet("font-weight: bold; color: #76b900; font-size: 16px;")
        self.smart_layout.addWidget(self.smart_title)
        
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["ID", "属性名称", "中文说明", "当前值", "最差值", "原始数据"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(400)
        self.smart_layout.addWidget(self.table)
        self.scroll_layout.addWidget(self.smart_card)
        
        self.scroll_layout.addStretch()
        scroll.setWidget(self.scroll_content)
        self.detail_layout.addWidget(scroll)

        # Rebuild the screen into a clear desktop information architecture while
        # retaining the established widgets and their signal connections.
        self._apply_formal_layout(scroll)

        # Tray Icon Setup
        logging.info("正在设置系统托盘...")
        self.setup_tray()

        self.current_disk_serial = None # Track currently selected disk

        # 开机恢复（兜底）：旧版本可能留下离线/隔离记录；当前关机路径只做深度休眠。
        try:
            from src.core.shutdown_service import bring_all_external_disks_online
            if not self.config_manager.config.get("eject_quarantine"):
                bring_all_external_disks_online()
            else:
                logging.warning("存在未完成弹出隔离记录，跳过自动恢复在线")
        except Exception as e:
            logging.warning(f"开机恢复外置盘失败: {e}")

        # Start Monitor Service
        logging.info("正在启动监控服务线程...")
        self.monitor_service = MonitorService(
            callback_notify=self.show_notification,
            callback_update_ui=self.emit_update_signal
        )
        self.monitor_service.start()

        # Windows 原生托盘弹出不由本程序接管；停转与弹出统一从 GUI 发起。

        # 关机/休眠与界面的“立即休眠”使用同一深度休眠路径。
        self.shutdown_guard = ShutdownGuard(
            config_manager=self.config_manager,
            monitor_service=self.monitor_service,
        )

        # 正式版常驻设置：启动即注册开机自启动，并固化关机与弹出保护开关。
        try:
            if not self.config_manager.is_autostart_enabled():
                if self.config_manager.set_autostart(True):
                    logging.info("已自动注册开机自启动")
            self.config_manager.set_shutdown_eject(True)
            self.config_manager.set_safe_remove_spindown(True)
        except Exception as e:
            logging.warning(f"常驻保护配置失败: {e}")

        # 关机最终停转必须由 LocalSystem PRESHUTDOWN 服务完成。
        # WM_QUERYENDSESSION 只负责冻结后台访问；此时 Windows 仍可能在应用退出后
        # 刷新卷，若在这里直接 SLEEP，后续收尾会把硬盘重新唤醒。
        try:
            import sys
            from src.core.shutdown_service import ensure_service_installed_and_running
            self._shutdown_svc_available = bool(
                ensure_service_installed_and_running(sys.executable)
            )
            if self._shutdown_svc_available:
                logging.info("关机停转服务已就绪（PRESHUTDOWN，临时离线后 SLEEP）")
            else:
                logging.warning("关机停转服务不可用，关机时将使用 GUI 兜底路径")
        except Exception as e:
            self._shutdown_svc_available = False
            logging.warning(f"注册关机停转服务失败，关机时将使用 GUI 兜底路径: {e}")

        # 保存物理盘与白名单快照，供日志核对和启动后的状态恢复使用。
        self._write_shutdown_disks_snapshot()
        self._svc_snapshot_timer = QTimer(self)
        self._svc_snapshot_timer.setInterval(60000)
        self._svc_snapshot_timer.timeout.connect(self._write_shutdown_disks_snapshot)
        self._svc_snapshot_timer.start()

        self.status_label.setText("监控服务运行中 (系统日志实时监控)")
        logging.info("MainWindow 初始化完成")

    def _apply_formal_layout(self, legacy_scroll):
        """Arrange existing controls by task: select, inspect, act, configure."""
        self.sidebar_container.setFixedWidth(330)
        self.sidebar_container.setStyleSheet(
            "background-color: #1a1a1a; border-right: 1px solid #2d2d2d;"
        )
        self.logo_label.setContentsMargins(0, 14, 0, 10)
        self.logo_label.setMaximumHeight(92)
        self.disk_list_title.setText("硬盘列表")
        self.disk_list_title.setStyleSheet(
            "color: #ffffff; font-size: 18px; font-weight: bold; padding: 8px 16px 2px 16px;"
        )
        self.disk_list_hint.setText("勾选硬盘前的“纳入管理”，程序才会读取状态或执行休眠、弹出。")
        self.disk_list_hint.setStyleSheet(
            "color: #aaaaaa; font-size: 13px; padding: 2px 16px 10px 16px;"
        )
        self.sidebar.setSpacing(4)
        self.sidebar.setStyleSheet("""
            QListWidget { background: #1a1a1a; border: none; outline: none; }
            QListWidget::item { background: #262626; border: 1px solid #2d2d2d;
                                border-radius: 6px; margin: 3px 8px; }
            QListWidget::item:selected { background: #333333; border: 1px solid #9147ff; }
            QListWidget::item:hover { background: #2d2d2d; }
        """)

        # Maintenance and protection controls belong to Settings, not the disk picker.
        self.settings_container.hide()
        legacy_scroll.hide()
        while self.detail_layout.count():
            self.detail_layout.takeAt(0)
        self.detail_layout.setContentsMargins(28, 24, 28, 24)
        self.detail_layout.setSpacing(16)

        self.shutdown_notice = QFrame()
        self.shutdown_notice.setObjectName("ShutdownNotice")
        self.shutdown_notice.setStyleSheet("""
            QFrame#ShutdownNotice { background: #2a2116; border: 1px solid #725124; border-radius: 8px; }
            QLabel#NoticeTitle { color: #ffd18b; font-size: 14px; font-weight: bold; }
            QLabel#NoticeText { color: #e0c49a; font-size: 13px; }
        """)
        notice_layout = QHBoxLayout(self.shutdown_notice)
        notice_layout.setContentsMargins(16, 11, 16, 11)
        notice_layout.setSpacing(14)
        notice_mark = QLabel("!")
        notice_mark.setAlignment(Qt.AlignCenter)
        notice_mark.setFixedSize(30, 30)
        notice_mark.setStyleSheet(
            "background: #d9942b; color: #17110a; border-radius: 15px; font-size: 18px; font-weight: 700;"
        )
        notice_text_layout = QVBoxLayout()
        notice_text_layout.setSpacing(2)
        notice_title = QLabel("关机前，请先保存文件，等待传输完成")
        notice_title.setObjectName("NoticeTitle")
        notice_text = QLabel("① 保存正在编辑的文件，并关闭使用这些文件的软件。② 等待硬盘柜上的复制、移动、下载和备份完成，再点击 Windows“关机”。程序会向勾选“纳入管理”的硬盘发送休眠命令。")
        notice_text.setObjectName("NoticeText")
        notice_text.setWordWrap(True)
        notice_text_layout.addWidget(notice_title)
        notice_text_layout.addWidget(notice_text)
        notice_layout.addWidget(notice_mark, 0, Qt.AlignVCenter)
        notice_layout.addLayout(notice_text_layout, 1)
        self.detail_layout.addWidget(self.shutdown_notice)

        header_card = QFrame()
        header_card.setObjectName("Card")
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(20, 16, 20, 16)
        header_layout.setSpacing(18)
        identity_layout = QVBoxLayout()
        identity_layout.setSpacing(5)
        self.title_label.setText("请选择一块硬盘")
        self.title_label.setStyleSheet("")
        self.status_label.setText("等待硬盘状态")
        self.status_label.setStyleSheet("")
        identity_layout.addWidget(self.title_label)
        identity_layout.addWidget(self.status_label)
        header_layout.addLayout(identity_layout, 1)

        action_layout = QHBoxLayout()
        action_layout.setSpacing(10)
        self.spin_down_button.setText("立即休眠")
        self.spin_down_button.setObjectName("SecondaryAction")
        self.spin_down_button.setStyleSheet("")
        self.spin_down_button.setFixedWidth(150)
        self.eject_button.setText("停转并安全弹出")
        self.eject_button.setObjectName("PrimaryAction")
        self.eject_button.setStyleSheet("")
        self.eject_button.setFixedWidth(190)
        action_layout.addWidget(self.spin_down_button)
        action_layout.addWidget(self.eject_button)
        header_layout.addLayout(action_layout)
        self.detail_layout.addWidget(header_card)

        self.main_tabs = QTabWidget()
        self.main_tabs.setDocumentMode(True)

        overview_tab = QWidget()
        overview_tab_layout = QVBoxLayout(overview_tab)
        overview_tab_layout.setContentsMargins(0, 14, 0, 0)
        overview_scroll = QScrollArea()
        overview_scroll.setWidgetResizable(True)
        overview_scroll.setFrameShape(QFrame.NoFrame)
        overview_scroll.setStyleSheet("background: transparent;")
        overview_content = QWidget()
        overview_content_layout = QVBoxLayout(overview_content)
        overview_content_layout.setContentsMargins(0, 0, 6, 0)
        overview_content_layout.setSpacing(14)
        self.summary_card.setObjectName("Card")
        self.summary_layout.setContentsMargins(18, 16, 18, 18)
        self.summary_label.setStyleSheet("color: #76b900; font-size: 16px; font-weight: bold;")
        self.summary_table.setFixedHeight(220)
        overview_content_layout.addWidget(self.summary_card)
        self.advice_card.setObjectName("Card")
        self.advice_layout.setContentsMargins(18, 16, 18, 18)
        self.advice_title.setStyleSheet("color: #76b900; font-size: 16px; font-weight: bold;")
        self.advice_text.setStyleSheet("color: #ffffff; font-size: 14px; padding: 6px 2px;")
        overview_content_layout.addWidget(self.advice_card)
        overview_content_layout.addStretch()
        overview_scroll.setWidget(overview_content)
        overview_tab_layout.addWidget(overview_scroll)
        self.main_tabs.addTab(overview_tab, "概览")

        smart_tab = QWidget()
        smart_tab_layout = QVBoxLayout(smart_tab)
        smart_tab_layout.setContentsMargins(0, 14, 0, 0)
        self.smart_card.setObjectName("Card")
        self.smart_layout.setContentsMargins(18, 16, 18, 18)
        self.smart_title.setStyleSheet("color: #76b900; font-size: 16px; font-weight: bold;")
        self.table.setMinimumHeight(430)
        smart_tab_layout.addWidget(self.smart_card)
        self.main_tabs.addTab(smart_tab, "SMART 参数")

        settings_tab = QWidget()
        settings_tab_layout = QVBoxLayout(settings_tab)
        settings_tab_layout.setContentsMargins(0, 14, 0, 0)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.NoFrame)
        settings_scroll.setStyleSheet("background: transparent;")
        settings_content = QWidget()
        settings_layout = QVBoxLayout(settings_content)
        settings_layout.setContentsMargins(0, 0, 6, 0)
        settings_layout.setSpacing(14)

        monitor_card = QFrame()
        monitor_card.setObjectName("Card")
        monitor_layout = QVBoxLayout(monitor_card)
        monitor_layout.setContentsMargins(18, 16, 18, 18)
        monitor_layout.setSpacing(12)
        monitor_title = QLabel("当前硬盘的监控设置")
        monitor_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #76b900;")
        monitor_help = QLabel("这些选项仅对左侧当前选中且已纳入管理的硬盘生效。")
        monitor_help.setStyleSheet("color: #aaaaaa; font-size: 13px;")
        monitor_layout.addWidget(monitor_title)
        monitor_layout.addWidget(monitor_help)
        for container in (self.interval_container, self.sleep_timer_container):
            container.setStyleSheet("background: transparent;")
            monitor_layout.addWidget(container)
        self.interval_slider.setStyleSheet("")
        self.sleep_timer_slider.setStyleSheet("""
            QSlider::groove:horizontal { border: 1px solid #3d3d3d; height: 8px;
                background: #1a1a1a; margin: 2px 0; border-radius: 4px; }
            QSlider::handle:horizontal { background: #9147ff; border: 1px solid #9147ff;
                width: 18px; height: 18px; margin: -7px 0; border-radius: 9px; }
        """)
        self.interval_label.setStyleSheet("color: #aaaaaa; font-size: 14px;")
        self.sleep_timer_label.setStyleSheet("color: #aaaaaa; font-size: 14px;")
        settings_layout.addWidget(monitor_card)

        protection_card = QFrame()
        protection_card.setObjectName("Card")
        protection_layout = QVBoxLayout(protection_card)
        protection_layout.setContentsMargins(18, 16, 18, 18)
        protection_layout.setSpacing(10)
        protection_title = QLabel("保护状态")
        protection_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #76b900;")
        protection_layout.addWidget(protection_title)
        self.autostart_checkbox.setText("随 Windows 启动")
        self.shutdown_eject_checkbox.setText("关机或系统休眠时，休眠已管理硬盘")
        self.spindown_patch_checkbox.setText("安全弹出前先停转硬盘")
        for checkbox in (self.autostart_checkbox, self.shutdown_eject_checkbox,
                         self.spindown_patch_checkbox):
            checkbox.setStyleSheet("color: #aaaaaa; font-size: 14px; padding: 3px;")
            protection_layout.addWidget(checkbox)
        self.windows_sleep_hint.setText(
            "关机休眠采用与“立即休眠”相同的深度休眠命令。程序只处理左侧已纳入管理的硬盘。"
        )
        self.windows_sleep_hint.setStyleSheet(
            "color: #999999; font-size: 13px; padding: 6px 2px;"
        )
        protection_layout.addWidget(self.windows_sleep_hint)
        settings_layout.addWidget(protection_card)

        maintenance_card = QFrame()
        maintenance_card.setObjectName("Card")
        maintenance_layout = QVBoxLayout(maintenance_card)
        maintenance_layout.setContentsMargins(18, 16, 18, 18)
        maintenance_layout.setSpacing(10)
        maintenance_title = QLabel("应用维护")
        maintenance_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #76b900;")
        maintenance_layout.addWidget(maintenance_title)
        maintenance_buttons = QHBoxLayout()
        self.refresh_btn.setText("刷新硬盘状态")
        self.refresh_btn.setStyleSheet("")
        self.check_update_btn.setStyleSheet("")
        self.export_logs_btn.setStyleSheet("")
        for button in (self.refresh_btn, self.check_update_btn, self.export_logs_btn):
            button.setFixedHeight(38)
            maintenance_buttons.addWidget(button)
        maintenance_layout.addLayout(maintenance_buttons)
        self.refresh_warning.setStyleSheet("color: #999999; font-size: 12px;")
        maintenance_layout.addWidget(self.refresh_warning)
        settings_layout.addWidget(maintenance_card)
        settings_layout.addStretch()
        settings_scroll.setWidget(settings_content)
        settings_tab_layout.addWidget(settings_scroll)
        self.main_tabs.addTab(settings_tab, "设置")

        self.detail_layout.addWidget(self.main_tabs, 1)

    def _write_shutdown_disks_snapshot(self):
        """把物理盘清单与休眠名单写入共享快照，供日志核对使用。"""
        try:
            import json
            from src.utils.paths import get_base_path
            cached = self._cached_disks_snapshot()
            sleeping = []
            if hasattr(self, 'monitor_service') and self.monitor_service:
                sleeping = sorted(
                    getattr(self.monitor_service, 'sleeping_disks', set()) or set()
                )
            from src.core.disk_whitelist import disk_id
            allowed = self.config_manager.get_managed_disk_whitelist()
            data = {
                "written_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "sleeping": sleeping,
                "managed_disk_whitelist": sorted(allowed),
                "disks": [
                    {
                        "index": getattr(d, "index", None),
                        "model": getattr(d, "model", None) or "",
                        "serial_number": getattr(d, "serial_number", None) or "",
                        "managed_id": disk_id(d),
                        "is_removable": bool(getattr(d, "is_removable", False)),
                        "pnp_id": getattr(d, "pnp_id", "") or "",
                    }
                    for d in cached
                ],
            }
            path = os.path.join(get_base_path(), "shutdown_disks.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            logging.warning(f"写关机服务共享磁盘清单失败: {e}")

    def emit_update_signal(self, data):
        self.update_data_signal.emit(data)

    @Slot(list)
    def handle_data_update(self, data):
        logging.info(f"UI 收到数据更新: {len(data)} 条记录")

        # 核对“不安全关机数”(C0)：本次开机首次读到某盘时与上次关机前的记录对比，
        # 增量 0 = 上次关机是安全关机（新停转策略生效）。同时持续刷新基线。
        try:
            if hasattr(self, 'shutdown_guard'):
                self.shutdown_guard.process_smart_data(data)
        except Exception as e:
            logging.warning(f"关机安全性核对失败: {e}")

        # 每次数据更新都刷新共享磁盘清单（首次启动时 cached_disks 还没就绪，
        # 只靠 60 秒定时器会在开机后一段时间内拿到空清单）
        try:
            self._write_shutdown_disks_snapshot()
        except Exception as e:
            logging.warning(f"刷新关机服务共享清单失败: {e}")
        
        # 1. 排序：外置硬盘在前，内置硬盘在后。同一类别按物理索引排序。
        # sorted() 是稳定的，所以我们可以先按索引排，再按是否可移动排（取反，True=0, False=1）
        data.sort(key=lambda x: (not x.get("is_removable", False), x.get("index", 0)))
        
        self.disk_data = data
        
        # 记录当前选中的硬盘序列号，以便刷新后恢复选中
        selected_serial = None
        current_item = self.sidebar.currentItem()
        if current_item:
            selected_serial = current_item.data(Qt.UserRole).get("serial")
            
        self.sidebar.clear()
        self._whitelist_checkboxes = {}
        for disk in data:
            model = disk.get("model", "Unknown")
            status = disk.get("status", "Unknown")
            is_removable = disk.get("is_removable", False)
            
            # 侧栏用两行大字显示，避免型号和状态挤在同一行。
            display_name = f"{model[:30]}..." if len(model) > 30 else model
            
            managed_id = disk.get("managed_id") or ""
            from src.core.disk_whitelist import is_external_disk
            item = QListWidgetItem()
            item.setData(Qt.UserRole, disk)
            item.setSizeHint(QSize(310, 78))
            
            # 设置基本颜色（根据健康状态）
            if status == "Healthy":
                base_color = QColor("#76b900") # Green
            elif status == "Warning":
                base_color = QColor("orange")
            elif "Error" in status or "Failed" in status or status == "Critical":
                base_color = QColor("#ffaa00") # Orange-Yellow
            else:
                base_color = QColor("#ffffff")
                
            item.setToolTip(f"{'外置/移动设备' if is_removable else '内置硬盘'} - {model}")
                
            self.sidebar.addItem(item)

            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(12, 8, 12, 8)
            row_layout.setSpacing(12)

            checkbox = QCheckBox("纳入管理")
            eligible = bool(managed_id) and is_external_disk(disk)
            checkbox.setEnabled(eligible)
            checkbox.setChecked(eligible and
                                managed_id in self.config_manager.get_managed_disk_whitelist())
            checkbox.setText("✓ 已管理" if checkbox.isChecked() else "纳入管理")
            checkbox.setMinimumWidth(98)
            checkbox.setStyleSheet("""
                QCheckBox { color: #dddddd; font-size: 13px; font-weight: bold; spacing: 7px; }
                QCheckBox::indicator { width: 22px; height: 22px; }
                QCheckBox::indicator:unchecked { background: #202020; border: 2px solid #888888; border-radius: 5px; }
                QCheckBox::indicator:checked { background: #9147ff; border: 2px solid #b17aff; border-radius: 5px; }
                QCheckBox:disabled { color: #666666; }
                QCheckBox::indicator:disabled { background: #222222; border-color: #444444; }
            """)
            checkbox.setToolTip(
                "勾选后才执行 SMART、休眠、弹出及关机停转；未勾选时仅显示设备信息。"
                if eligible else "仅支持可识别的 USB/UASP 外置硬盘；序列号缺失或内置磁盘不能加入白名单。")

            text_layout = QVBoxLayout()
            text_layout.setContentsMargins(0, 0, 0, 0)
            text_layout.setSpacing(2)
            name_label = QLabel(display_name)
            name_label.setStyleSheet("color: #ffffff; font-size: 16px; font-weight: bold;")
            status_label = QLabel(status)
            status_label.setStyleSheet(f"color: {base_color.name()}; font-size: 13px;")
            text_layout.addWidget(name_label)
            text_layout.addWidget(status_label)
            row_layout.addWidget(checkbox, 0, Qt.AlignVCenter)
            row_layout.addLayout(text_layout, 1)

            checkbox.clicked.connect(
                lambda checked, row=item, did=managed_id: self._on_whitelist_changed(row, did, checked))
            name_label.mousePressEvent = lambda event, row=item: self._select_disk_row(row)
            status_label.mousePressEvent = lambda event, row=item: self._select_disk_row(row)
            self.sidebar.setItemWidget(item, row_widget)
            if managed_id:
                self._whitelist_checkboxes[managed_id] = checkbox
            
            # 恢复选中
            if selected_serial and disk.get("serial") == selected_serial:
                self.sidebar.setCurrentItem(item)
                
        # 如果没有选中的，默认选第一个
        if not self.sidebar.currentItem() and self.sidebar.count() > 0:
            self.sidebar.setCurrentRow(0)
            
        # 更新详情面板（如果当前有选中的）
        if self.sidebar.currentItem():
            self.on_disk_selected(self.sidebar.currentItem())

    def _select_disk_row(self, item):
        self.sidebar.setCurrentItem(item)
        self.on_disk_selected(item)

    def _on_whitelist_changed(self, item, managed_id, checked):
        if not managed_id:
            return
        disk = item.data(Qt.UserRole) or {}
        from src.core.disk_whitelist import is_external_disk
        if checked and not is_external_disk(disk):
            checkbox = self._whitelist_checkboxes.get(managed_id)
            if checkbox:
                checkbox.setChecked(False)
            return
        self.sidebar.setCurrentItem(item)
        self.config_manager.set_disk_managed(managed_id, checked)
        checkbox = self._whitelist_checkboxes.get(managed_id)
        if checkbox:
            checkbox.setText("✓ 已管理" if checked else "纳入管理")
        try:
            from src.core.safe_shutdown import arm_whitelist
            arm_whitelist(self.config_manager.get_managed_disk_whitelist())
        except Exception:
            logging.exception("更新关机白名单状态失败")
        if hasattr(self, 'monitor_service'):
            self.monitor_service.reset_unmanaged_idle_timers()
            threading.Thread(target=self.monitor_service.check_all_smart,
                             kwargs={'force': True}, daemon=True).start()
        self._write_shutdown_disks_snapshot()
        self.on_disk_selected(item)

    def on_interval_changed(self, value):
        minutes = value
        if minutes < 60:
            text = f"{minutes}分钟"
        else:
            hours = minutes / 60
            text = f"{hours:.1f}小时"
        self.interval_label.setText(f"SMART 检测频率: {text}/次")
        
    def on_interval_set(self):
        if not self.current_disk_serial or not getattr(self, "current_disk_managed", False):
            return
            
        minutes = self.interval_slider.value()
        seconds = minutes * 60
        logging.info(f"Setting interval for {self.current_disk_serial} to {seconds}s")
        self.config_manager.set_disk_interval(self.current_disk_serial, seconds)
        
        # Notify user (optional, or just update label color to confirm)
        self.interval_label.setStyleSheet("color: #9147ff; font-size: 13px; font-weight: bold;")
        # Reset style after a delay? For now just leave it green.

    def on_sleep_timer_changed(self, value):
        if value == 0:
            text = "从不休眠"
        else:
            text = f"{value} 分钟"
        self.sleep_timer_label.setText(f"硬盘休眠时间: {text}")
        
    def on_sleep_timer_set(self):
        if (not self.current_disk_serial or not hasattr(self, 'current_disk_index')
                or not getattr(self, "current_disk_managed", False)):
            return
            
        minutes = self.sleep_timer_slider.value()
        logging.info(f"Setting sleep timer for {self.current_disk_serial} to {minutes} min")
        
        self.config_manager.set_sleep_timer(self.current_disk_serial, minutes)

        if hasattr(self, 'monitor_service'):
            self.monitor_service.reset_idle_timer(
                self.current_disk_serial, disk_index=self.current_disk_index
            )

        self.sleep_timer_label.setStyleSheet("color: #9147ff; font-size: 13px; font-weight: bold;")
    @Slot()
    def on_refresh_clicked(self):
        """Read-only refresh of allow-listed disks; never restart hardware nodes."""
        if self._eject_active is not None or self.monitor_service.eject_quarantine:
            QMessageBox.warning(self, "不能刷新", "弹出事务或隔离状态尚未解除。")
            return
        self._hardware_refresh_active = True
        self.refresh_btn.setText("正在刷新硬盘状态...")
        self.refresh_btn.setEnabled(False)
        def worker():
            try:
                self.monitor_service.check_all_smart(force=True)
                self._refresh_done_signal.emit({'ok': True})
            except Exception as exc:
                logging.exception('白名单设备刷新失败')
                self._refresh_done_signal.emit({'ok': False, 'error': str(exc)})
        threading.Thread(target=worker, daemon=True).start()

    def _on_refresh_done(self, result):
        self._hardware_refresh_active = False
        self.refresh_btn.setText("刷新硬盘状态")
        self.refresh_btn.setEnabled(True)
        if result.get('ok'):
            self.status_label.setText('已刷新管理硬盘的状态')
        else:
            QMessageBox.warning(self, '刷新失败', result.get('error', '未知错误'))

    def show_refresh_result(self, has_issue, recovery_success, logs):
        """显示刷新结果对话框"""
        # 如果是 Shift 调试模式，总是显示日志
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ShiftModifier:
            dialog = LogDialog(logs, self)
            dialog.exec()
            return

        # 正常模式下，根据结果显示提示
        if recovery_success:
            msg = "已成功恢复异常设备！\n硬件列表已更新。"
            icon = QMessageBox.Information
        elif has_issue:
            msg = "扫描过程中发现异常或错误。\n请查看日志了解详情。"
            icon = QMessageBox.Warning
        else:
            msg = "扫描完成，未发现异常设备。"
            icon = QMessageBox.Information
            
        # Create custom box
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("刷新结果")
        msg_box.setText(msg)
        msg_box.setIcon(icon)
        
        btn_ok = msg_box.addButton("确定", QMessageBox.AcceptRole)
        btn_logs = msg_box.addButton("查看日志", QMessageBox.ActionRole)
        
        msg_box.exec()
        
        if msg_box.clickedButton() == btn_logs:
            dialog = LogDialog(logs, self)
            dialog.exec()

    def export_logs(self):
        """Show report dialog instead of just exporting"""
        dialog = ReportDialog(self)
        dialog.exec()

    def on_check_update_clicked(self):
        """Handle check update button click"""
        self.check_update_btn.setEnabled(False)
        self.check_update_btn.setText("正在检查更新...")
        
        self.update_thread = UpdateCheckThread(self.version)
        self.update_thread.finished_signal.connect(self.on_update_check_finished)
        self.update_thread.start()

    @Slot(dict)
    def on_update_check_finished(self, result):
        """Callback when update check is finished"""
        self.check_update_btn.setEnabled(True)
        self.check_update_btn.setText("检查软件更新")
        
        if not result.get("success"):
            QMessageBox.warning(self, "检查更新失败", f"无法连接到 Gitee 服务器: {result.get('error')}")
            return
            
        if result.get("is_newer"):
            latest_version = result.get("latest_version")
            changelog = result.get("changelog")
            download_url = result.get("download_url")
            
            msg = f"发现新版本: {latest_version}\n\n更新日志:\n{changelog}\n\n是否前往 Gitee 下载最新版本？"
            reply = QMessageBox.question(
                self, "发现更新", msg,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
            )
            
            if reply == QMessageBox.Yes:
                self.open_url(download_url)
        else:
            # If already latest, still offer to visit Gitee
            reply = QMessageBox.information(
                self, "检查更新", 
                "当前已是最新版本。\n\n是否前往项目主页查看更多信息？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.open_url("https://gitee.com/stormforge/JiFengZhiHDDManager")

    def open_url(self, url):
        """Robustly open URL in browser, especially when running as Admin"""
        if not url: return
        try:
            # Try webbrowser first
            import webbrowser
            webbrowser.open(url)
        except Exception as e:
            logging.warning(f"webbrowser.open failed: {e}")
            try:
                # Fallback to os.startfile on Windows
                import os
                os.startfile(url)
            except Exception as e2:
                logging.error(f"os.startfile failed: {e2}")
                QMessageBox.warning(self, "无法打开浏览器", f"请手动访问: {url}")

    @Slot()
    def on_autostart_changed(self, state):
        enabled = self.autostart_checkbox.isChecked()
        success = self.config_manager.set_autostart(enabled)
        if not success:
            # Revert checkbox if failed
            self.autostart_checkbox.blockSignals(True)
            self.autostart_checkbox.setChecked(not enabled)
            self.autostart_checkbox.blockSignals(False)
            QMessageBox.warning(self, "错误", "无法设置开机自启动，请检查管理员权限。")
        else:
            status = "已开启" if enabled else "已关闭"
            logging.info(f"用户通过 UI {status} 了开机自启动")

    @Slot(int)
    def on_shutdown_eject_changed(self, state):
        enabled = (state == Qt.Checked)
        self.config_manager.set_shutdown_eject(enabled)
        logging.info(f"关机自动休眠设置已更新: {enabled}")

    @Slot(int)
    def on_spindown_patch_changed(self, state):
        enabled = (state == Qt.Checked)
        self.config_manager.set_safe_remove_spindown(enabled)
        self.spindown_patcher.enabled = enabled
        if enabled:
            self.config_manager.set_autostart(True)
            self.autostart_checkbox.setChecked(True)
        logging.info(f"系统弹出附加停转补丁设置已更新: {enabled}")

    def _register_spindown_patcher(self):
        try:
            hwnd = int(self.winId())
            self.spindown_patcher.register(hwnd)
        except Exception as e:
            logging.warning(f"注册停转补丁失败: {e}")

        # 兜底重试：设备到达风暴中 WMI 不可靠，句柄通知可能注册为 0 个外置盘
        # （实测：硬盘柜上电瞬间注册 0 个 → 托盘弹出无停转）。
        # 每 30 秒检查一次，为空则刷新缓存并重新注册，直到成功为止。
        self._patcher_retry_timer = QTimer(self)
        self._patcher_retry_timer.setInterval(5000)
        self._patcher_retry_timer.timeout.connect(self._retry_patcher_registration)
        self._patcher_retry_timer.start()

    def _retry_patcher_registration(self):
        try:
            patcher = getattr(self, "spindown_patcher", None)
            if not patcher:
                return
            patcher.refresh_cache()
            hwnd = int(self.winId())
            patcher.register(hwnd)
        except Exception as e:
            logging.warning(f"[SafeRemovalPatch] 重试注册失败: {e}")

    def nativeEvent(self, eventType, message):
        """Handle Windows native events to detect shutdown"""
        try:
            if eventType.data() == b"windows_generic_MSG":
                msg = MSG.from_address(int(message))

                if msg.message == WM_QUERYENDSESSION:
                    logging.info("收到系统关机信号 (WM_QUERYENDSESSION)")
                    # The service runs later, after application/volume handles
                    # are gone.  Starting SLEEP here would allow a later Windows
                    # flush to wake the disk again.
                    self.shutdown_guard.freeze_background_access()
                    try:
                        from src.core.shutdown_service import is_service_running
                        svc_ok = bool(is_service_running())
                    except Exception:
                        svc_ok = False
                    if not svc_ok:
                        logging.warning(
                            "关机停转服务未运行，使用 GUI 兜底停转；可能被后续系统收尾唤醒"
                        )
                        self.shutdown_guard.start_shutdown_parking(
                            self._cached_disks_snapshot(), event_type="关机兜底"
                        )
                    return True, 1

                elif msg.message == WM_ENDSESSION:
                    logging.info(f"系统会话结束 (WM_ENDSESSION, wParam={msg.wParam})")
                    if not msg.wParam:
                        # wParam==0 表示会话并未真正结束（关机被取消），必须恢复后台监控，
                        # 否则监控服务会永久停摆。
                        logging.info("关机被取消，恢复后台监控与设备事件处理")
                        self.shutdown_guard.thaw_background_access()

                elif msg.message == WM_POWERBROADCAST:
                    if msg.wParam == PBT_APMSUSPEND:
                        logging.info("收到系统休眠信号 (PBT_APMSUSPEND)")
                        # 系统休眠使用与“立即休眠”相同的白名单路径。
                        self.shutdown_guard.start_shutdown_parking(
                            self._cached_disks_snapshot(), event_type="系统休眠"
                        )
                    elif msg.wParam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                        logging.info(f"系统已从休眠恢复 (wParam=0x{msg.wParam:04X})")
                        # 唤醒后恢复监控与设备事件处理
                        self.shutdown_guard.thaw_background_access()
                    return True, 0

                elif msg.message == WM_DEVICECHANGE:
                    # TRUE allows removal; BCAST_QUERY_DENY defers the request
                    # until the shared offline/SLEEP/eject transaction can run.
                    # 设备到达/移除/树变化 → 去抖后立即重扫（v1.3.89 热插拔即时发现）。
                    # 只加副作用：返回值语义保持原样（QUERYREMOVE 必须返回 1 才允许移除）。
                    try:
                        if self.device_change_needs_rescan(msg.wParam):
                            self._schedule_device_rescan(msg.wParam)
                    except Exception as e:
                        logging.warning(f"处理设备变化事件失败: {e}")
                    return super().nativeEvent(eventType, message)
        except Exception as e:
            logging.error(f"nativeEvent error: {e}")

        return super().nativeEvent(eventType, message)

    @classmethod
    def device_change_needs_rescan(cls, wparam):
        """该 WM_DEVICECHANGE 的 wParam 是否需要触发重扫。

        0x8000 DBT_DEVICEARRIVAL（新设备到位）、0x8004 DBT_DEVICEREMOVECOMPLETE（已移除）、
        0x0007 DBT_DEVNODES_CHANGED（设备树变化；TECH_NOTES §15 实测本机热插拔最先收到的
        就是它，且"任何设备变化"都会发 → 只能当触发器，必须重新枚举比对）。
        其余（如 0x8001 QUERYREMOVE）不触发：它们的返回值语义不允许改动。
        """
        try:
            return int(wparam or 0) in cls.DEVICE_CHANGE_TRIGGERS
        except (TypeError, ValueError):
            return False

    def _schedule_device_rescan(self, wparam):
        try:
            self._pending_device_change = int(wparam or 0)
        except (TypeError, ValueError):
            self._pending_device_change = 0
        timer = getattr(self, "_device_change_timer", None)
        if timer is None:
            return
        timer.start()      # 重复 start() 会重新计时 → 事件风暴合并成一次重扫

    def _on_device_change_settled(self):
        wparam = getattr(self, "_pending_device_change", 0)
        self._pending_device_change = 0
        service = getattr(self, "monitor_service", None)
        if service is None:
            return
        reason = "WM_DEVICECHANGE:0x%04X" % wparam

        def worker():
            try:
                result = service.rescan_devices(reason=reason)
                logging.info(f"设备变化重扫结果: {result}")
            except Exception:
                logging.exception("设备变化重扫失败")
        threading.Thread(target=worker, daemon=True).start()

    def prepare_disks_for_shutdown(self, event_type="关机", budget_seconds=4.0):
        """Compatibility entry for the allow-listed deep-sleep shutdown path."""
        try:
            cached = self._cached_disks_snapshot()
            if not cached:
                logging.warning("[ShutdownGuard] 缺少物理盘缓存，跳过关机停转以避免扫描唤醒硬盘")
                return
            self.shutdown_guard.start_shutdown_parking(cached, event_type=event_type)
        except Exception as e:
            logging.error(f"{event_type}停转保护执行异常: {e}")

    def _cached_disks_snapshot(self):
        """关机阶段只使用物理盘缓存，绝不做在线扫描（扫描会唤醒硬盘）。"""
        if hasattr(self, 'monitor_service') and self.monitor_service:
            return list(getattr(self.monitor_service, 'cached_disks', []) or [])
        return []

    def eject_all_removable_disks(self, is_system_sleep=False):
        """兼容旧调用点：转交给新的关机停转策略。"""
        self.prepare_disks_for_shutdown(
            event_type="系统休眠" if is_system_sleep else "关机"
        )

    def on_disk_selected(self, item):
        if not item:
            self.spin_down_button.setEnabled(False)
            self.eject_button.setEnabled(False)
            self.current_disk_managed = False
            return
        
        disk = item.data(Qt.UserRole)
        from src.core.disk_whitelist import is_external_disk
        is_managed = (is_external_disk(disk) and
                      disk.get("managed_id") in self.config_manager.get_managed_disk_whitelist())
        self.title_label.setText(disk.get("model", "未知型号"))
        
        status = disk.get("status", "")
        is_removable = disk.get("is_removable", False)
        
        if status == "Sleeping":
            self.spin_down_button.setText("唤醒并解除黑名单")
            self.spin_down_button.setEnabled(is_managed)
        else:
            self.spin_down_button.setText("深度休眠")
            self.spin_down_button.setEnabled(is_managed)

        # 弹出按钮只按“是否外置/移动设备”判断，不依赖休眠状态：
        # 软件对休眠状态的识别可能滞后，休眠中的移动盘也应允许直接弹出。
        if is_external_disk(disk) and is_managed:
            self.eject_button.setEnabled(True)
            self.eject_button.setToolTip("安全弹出并停止该外置硬盘（休眠中的硬盘也可直接弹出）")
        else:
            self.eject_button.setEnabled(False)
            self.eject_button.setToolTip("请先将硬盘加入管理白名单" if not is_managed else "内置硬盘不支持安全弹出")
            
        self.current_disk_index = disk.get("index")
        self.current_disk_serial = disk.get("serial")
        self.current_disk_model = disk.get("model")
        self.current_disk_managed = is_managed
        self.interval_slider.setEnabled(is_managed)
        self.sleep_timer_slider.setEnabled(is_managed)

        # Load interval config
        interval_sec = self.config_manager.get_disk_interval(self.current_disk_serial)
        mins = max(5, interval_sec // 60) # Ensure min 5
        self.interval_slider.blockSignals(True)
        self.interval_slider.setValue(mins)
        self.interval_slider.blockSignals(False)
        self.on_interval_changed(mins)
        
        # Reset label style
        self.interval_label.setStyleSheet("color: #aaaaaa; font-size: 13px;")

        # Load sleep timer config
        sleep_mins = self.config_manager.get_sleep_timer(self.current_disk_serial)
        self.sleep_timer_slider.blockSignals(True)
        self.sleep_timer_slider.setValue(sleep_mins)
        self.sleep_timer_slider.blockSignals(False)
        self.on_sleep_timer_changed(sleep_mins)
        self.sleep_timer_label.setStyleSheet("color: #aaaaaa; font-size: 13px;")

        # Check if we should recommend higher frequency
        status = disk.get("status", "Healthy")
        if status != "Healthy" and mins > 60:
            self.interval_label.setText(self.interval_label.text() + " (建议调高频率)")
            self.interval_label.setStyleSheet("color: #ff9900; font-size: 13px; font-weight: bold;")
        
        # 1. Update Summary Table
        summary_details = [
            ("设备索引", str(disk.get("index", ""))),
            ("序列号", disk.get("serial", "")),
            ("当前状态", disk.get("status", "")),
            ("温度", disk.get("temp", "")),
            ("通电时间", f"{disk.get('power_on_hours', '0')} 小时"),
        ]
        
        # 显示接口信息，方便调试
        summary_details.append(("接口类型", disk.get("interface", "Unknown")))
        summary_details.append(("设备类型", "外置/移动设备" if disk.get("is_removable") else "内置硬盘"))

        # Conditionally add fields if they are present (not None)
        reallocated = disk.get("reallocated")
        if reallocated is not None:
             summary_details.append(("重映射扇区 (05)", str(reallocated)))
             
        pending = disk.get("pending")
        if pending is not None:
             summary_details.append(("待处理扇区 (C5)", str(pending)))
             
        ssd_life = disk.get("ssd_life_left")
        if ssd_life is not None:
             summary_details.append(("SSD 剩余寿命", f"{ssd_life}%"))
             
        total_writes = disk.get("total_writes_gb")
        if total_writes is not None:
             summary_details.append(("累计写入量", f"{total_writes} GB"))

        self.summary_table.setRowCount(len(summary_details))
        for row, (key, value) in enumerate(summary_details):
            key_item = QTableWidgetItem(key)
            val_item = QTableWidgetItem(value)
            
            if key == "当前状态":
                if value == "Healthy": val_item.setForeground(QColor("#76b900")) # Reverted to Green
                elif value == "Warning": val_item.setForeground(QColor("#ffaa00"))
                elif "Failed" in value or "Error" in value: val_item.setForeground(QColor("#ffaa00")) # Changed from Red to Orange-Yellow
            
            self.summary_table.setItem(row, 0, key_item)
            self.summary_table.setItem(row, 1, val_item)

        # 2. Update Detailed SMART Table
        attributes = disk.get("attributes", [])
        analysis_map = disk.get("attributes_analysis", {})  # Get the new analysis map
        self.table.setRowCount(len(attributes))
        
        if not attributes:
            self.advice_card.hide()
        else:
            self.advice_card.show()
            for row, attr in enumerate(attributes):
                attr_id = attr.get("id", "")
                self.table.setItem(row, 0, QTableWidgetItem(attr_id))
                self.table.setItem(row, 1, QTableWidgetItem(attr.get("name", "")))
                self.table.setItem(row, 2, QTableWidgetItem(attr.get("name_cn", "")))
                self.table.setItem(row, 3, QTableWidgetItem(attr.get("value", "")))
                self.table.setItem(row, 4, QTableWidgetItem(attr.get("worst", "")))
                
                # Use analysis for the last column or tooltip
                analysis = analysis_map.get(attr_id, {})
                interpretation = analysis.get("interpretation", "")
                raw_data = attr.get("raw", "")
                
                raw_item = QTableWidgetItem(raw_data)
                if interpretation:
                    raw_item.setToolTip(f"分析: {interpretation}")
                self.table.setItem(row, 5, raw_item)

                # Set background color based on status if analysis is critical
                if "警告" in interpretation or "严重" in interpretation:
                    for col in range(6):
                        item = self.table.item(row, col)
                        if item:
                            item.setBackground(QColor(60, 40, 100))  # Dark Purple background for dark theme
                elif "增量" in interpretation or "增加" in interpretation:
                    for col in range(6):
                        item = self.table.item(row, col)
                        if item:
                            item.setBackground(QColor(230, 242, 255))  # Light blue

        # 3. Update Health Advice Text
        advice = disk.get("health_advice", "状态良好。")
        self.advice_text.setText(advice)

        # 4. Update Interval Slider Visibility
        self.interval_container.show()
        self.interval_slider.show()
 
    def on_spin_down_clicked(self):
        if self._eject_active is not None or self.monitor_service.eject_quarantine:
            return
        if not getattr(self, 'current_disk_managed', False):
            return
        if not hasattr(self, 'current_disk_index'):
            return

        current_item = self.sidebar.currentItem()
        if current_item:
            disk = current_item.data(Qt.UserRole)
            if disk.get("status") == "Sleeping":
                self._wake_sleeping_disk()
                return

        reply = QMessageBox.question(
            self, '确认深度休眠', 
            f"确定要让磁盘 {self.current_disk_index} 进入深度休眠吗？\n\n"
            "深度休眠后硬盘将停止转动并进入黑名单，任何程序（包括本程序）都无法访问它。\n\n"
            "要再次访问该硬盘，请在本程序中点击「唤醒并解除黑名单」把它从黑名单移除。\n\n"
            "注意：如果磁盘正在读写数据，此操作可能会导致数据传输中断。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            logging.info(f"用户触发磁盘 {self.current_disk_index} 休眠")
            if hasattr(self, 'monitor_service') and self.current_disk_serial:
                self.monitor_service.mark_disk_sleeping(self.current_disk_serial)

            self.spin_down_button.setEnabled(False)
            self.spin_down_button.setText("正在深度休眠...")
            self.status_label.setText("正在发送深度休眠命令...")
            QApplication.processEvents()
            
            threading.Thread(
                target=self._spin_down_and_verify_thread,
                args=(self.current_disk_index, self.current_disk_model, self.current_disk_serial),
                daemon=True,
            ).start()

    def _spin_down_and_verify_thread(self, disk_index, model, serial):
        result = {}
        try:
            with self.monitor_service.scan_lock:
                if self.monitor_service.removal_pending.is_set():
                    raise RuntimeError("正在执行安全弹出，取消休眠操作")
                success, message = DeviceManager.spin_down_disk(disk_index, model=model, serial=serial)
            result = {"success": success, "message": message, "serial": serial}
        except Exception as e:
            result = {"success": False, "message": str(e), "serial": serial}
        self._spindown_done_signal.emit(result)

    def _on_spin_down_complete(self, result):
        success = result.get("success", False)
        message = result.get("message", "")
        serial = result.get("serial")

        if success:
            self._mark_current_disk_sleeping_in_ui()
            full_message = (
                message
                + "\n\n硬盘已进入黑名单：任何程序（包括本程序）都无法访问它。\n"
                "要再次访问，请点击「唤醒并解除黑名单」将硬盘从黑名单移除。"
                + self._build_windows_sleep_guidance()
            )
            QMessageBox.information(self, "深度休眠成功", full_message)
            self.status_label.setText("硬盘已深度休眠并进入黑名单")
        else:
            if hasattr(self, 'monitor_service') and serial:
                self.monitor_service.mark_disk_awake(serial)
            QMessageBox.warning(self, "深度休眠失败", message)
            self.spin_down_button.setText("深度休眠")
            self.spin_down_button.setEnabled(getattr(self, 'current_disk_managed', False))
            self.status_label.setText("深度休眠失败")

    def on_eject_clicked(self):
        if self._eject_active is not None or not hasattr(self, 'current_disk_index'):
            return
        if not getattr(self, 'current_disk_managed', False):
            QMessageBox.warning(self, '白名单限制', '请先勾选该硬盘名前的白名单框。')
            return
        disk = next((d for d in self.monitor_service.cached_disks
                     if d.index == self.current_disk_index), None)
        if disk is None:
            QMessageBox.warning(self, "设备信息已过期", "请等待设备列表自动更新。若设备仍未恢复，请先关闭读写任务，再重启对应硬盘柜后重试。")
            return
        identity = {"index": disk.index, "model": disk.model,
                    "serial": disk.serial_number, "pnp_id": disk.pnp_id,
                    "is_removable": disk.is_removable,
                    "ui_serial": self.current_disk_serial, "source": "gui"}
        if QMessageBox.question(self, "停转并安全弹出",
                f"确定要停转并安全弹出磁盘 {disk.index} 吗？\n全部关联卷将被锁定、卸载并离线。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self._start_eject(identity)

    def _on_native_eject(self, identity):
        identity = dict(identity, source="windows_tray")
        index = identity["index"]
        if self._eject_active is not None or self._hardware_refresh_active:
            self.show_notification("暂不能弹出", "另一项磁盘操作尚未完成，请稍后重试。")
            return
        started = time.monotonic()
        def wait_for_native_cancel():
            if self.spindown_patcher.native_ready(index):
                self._start_eject(identity)
            elif time.monotonic() - started < 10:
                QTimer.singleShot(100, wait_for_native_cancel)
            else:
                self.show_notification("未启动停转", "Windows 未确认原弹出请求已结束，请使用程序内的弹出按钮。")
        wait_for_native_cancel()

    def _start_eject(self, identity):
        from src.core.disk_whitelist import disk_id
        if disk_id(identity) not in self.config_manager.get_managed_disk_whitelist():
            self.show_notification("白名单限制", "未勾选的硬盘不会执行弹出操作。")
            return
        if self._eject_active is not None or self._hardware_refresh_active:
            self.show_notification("暂不能弹出", "另一项磁盘操作尚未完成。")
            return
        self._eject_active = identity
        self.monitor_service.removal_pending.set()
        self.eject_button.setEnabled(False)
        self.spin_down_button.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.eject_button.setText("正在停转并弹出...")
        self.status_label.setText(f"磁盘 {identity['index']}：等待后台访问结束，随后锁卷、离线并停转...")
        threading.Thread(target=self._eject_disk_thread, args=(identity,), daemon=True).start()

    def _eject_disk_thread(self, identity):
        from src.core.eject_service import execute_eject
        result = execute_eject(self.monitor_service, identity, self._eject_progress_signal.emit)
        self._eject_done_signal.emit(dict(identity, **result))

    def _on_eject_progress(self, event):
        labels = {"volume_locked": "卷已锁定", "disk_offline": "磁盘已离线",
                  "sleep_dispatch": "正在发送 SLEEP 停转命令",
                  "pnp_request": "等待 Windows 完成安全弹出（请勿重复操作）",
                  "volume_dismounted_without_lock": "该文件系统不支持锁卷，已直接卸载卷",
                  "volume_lock_failed": "锁定卷被拒绝，正在诊断占用者",
                  "volume_isolation_failed": "卷无法锁定也无法卸载",
                  "volume_less_disk": "该盘无分区表（RAW），直接整盘停转并弹出"}
        if event in labels:
            self.status_label.setText(labels[event])

    def _on_eject_complete(self, result):
        index = result["index"]
        self.monitor_service.removal_pending.clear()
        self._eject_active = None
        self.eject_button.setText("停转并安全弹出")
        self.refresh_btn.setEnabled(True)
        if result.get("ejected"):
            message = "Windows 已完成安全弹出，SLEEP 命令已接受。"
            self._remove_ejected_disk_from_ui(result.get("ui_serial") or result.get("serial"), index)
            self.status_label.setText(message)
            if result.get("source") == "windows_tray":
                self.show_notification("停转并弹出完成", message)
            else:
                QMessageBox.information(self, "安全弹出完成", message)
        else:
            isolated = result.get("sleep_attempted") or result.get("state") == "recovery_required"
            message = result.get("error") or "Windows 未完成设备移除。"
            if isolated:
                message += "\n磁盘保持隔离，停转/弹出状态未完全确认。请勿重复读盘或直接拔盘。"
            self.status_label.setText(message)
            self.eject_button.setEnabled(not isolated and getattr(self, 'current_disk_managed', False))
            self.spin_down_button.setEnabled(not isolated and getattr(self, 'current_disk_managed', False))
            if result.get("source") == "windows_tray":
                self.show_notification("弹出未完成", message)
            else:
                QMessageBox.warning(self, "弹出未完成", message)

    def _remove_ejected_disk_from_ui(self, serial, disk_index):
        """先本地更新列表，避免成功弹出后同步刷新阻塞 UI。"""
        if not hasattr(self, "disk_data") or not self.disk_data:
            self.sidebar.clearSelection()
            return

        remaining_disks = [
            disk for disk in self.disk_data
            if not (
                (serial and disk.get("serial") == serial) or
                (disk_index is not None and disk.get("index") == disk_index)
            )
        ]
        self.handle_data_update(remaining_disks)

    def _mark_current_disk_sleeping_in_ui(self):
        """本地直接把当前磁盘标记为休眠（黑名单），避免立即触发一次全盘扫描导致再次唤醒。"""
        if not hasattr(self, "disk_data") or not self.disk_data:
            self.status_label.setText("硬盘已深度休眠并进入黑名单")
            return

        updated_disks = []
        for disk in self.disk_data:
            updated_disk = dict(disk)
            if self.current_disk_serial and updated_disk.get("serial") == self.current_disk_serial:
                updated_disk["status"] = "Sleeping"
                updated_disk["temp"] = "N/A"
                updated_disk["reallocated"] = "N/A"
                updated_disk["pending"] = "N/A"
            updated_disks.append(updated_disk)

        self.handle_data_update(updated_disks)
        self.status_label.setText("硬盘已深度休眠并进入黑名单")

    def _build_windows_sleep_guidance(self):
        """根据当前电源计划补充休眠建议，避免客户被 Windows 周期性唤醒硬盘。"""
        settings = Win32API.get_windows_disk_idle_settings()
        if not settings:
            return (
                "\n\n如果硬盘之后又被系统自动唤醒，请检查 Windows 电源计划中的“在此时间后关闭硬盘”设置，"
                "不要设为“从不”。建议设置为 10-20 分钟。"
            )

        warnings = []
        if settings.get("is_never_sleep_ac"):
            warnings.append("交流电源")
        if settings.get("is_never_sleep_dc"):
            warnings.append("电池电源")

        if not warnings:
            return ""

        warning_text = "、".join(warnings)
        return (
            f"\n\n检测到当前 Windows 电源计划的{warning_text}已设置为“从不关闭硬盘”。"
            "在这种情况下，系统和部分驱动可能会周期性重新唤醒已休眠的机械盘。\n"
            "建议客户在“控制面板 -> 电源选项 -> 更改计划设置 -> 更改高级电源设置 -> 硬盘 -> 在此时间后关闭硬盘”中，"
            "把对应项改为 600-1200 秒（10-20 分钟）。"
        )

    def _wake_sleeping_disk(self):
        if not hasattr(self, 'monitor_service'):
            return

        serial = self.current_disk_serial
        disk_index = self.current_disk_index

        self.spin_down_button.setEnabled(False)
        self.spin_down_button.setText("正在唤醒...")
        self.status_label.setText(f"正在发送 SMART 查询唤醒硬盘 {disk_index}（机械盘起转可能需要 10-30 秒）...")
        QApplication.processEvents()

        # 注意：暂不调用 mark_disk_awake——唤醒成功后再解除休眠黑名单，
        # 失败则保持“休眠”标记，避免后台监控反复访问起转中的硬盘。

        threading.Thread(
            target=self._wake_via_smart_thread,
            args=(disk_index, serial),
            daemon=True,
        ).start()

    def _wake_via_smart_thread(self, disk_index, serial):
        """发送 SMART 查询 = I/O 唤醒。

        深睡中的盘（sense 3A/00, MEDIUM NOT PRESENT）第一次查询往往不响应，
        需要等待马达起转后重试，最多尝试 4 次。
        """
        import pythoncom
        pythoncom.CoInitialize()
        self.monitor_service.scan_lock.acquire()
        try:
            if self.monitor_service.removal_pending.is_set():
                raise RuntimeError("正在执行安全弹出，取消唤醒")
            from src.core.disk_whitelist import disk_id, is_external_disk
            from src.core.device_manager import DeviceManager
            disk = next((d for d in self.monitor_service.cached_disks
                         if d.index == disk_index and d.serial_number == serial), None)
            if (disk is None or not is_external_disk(disk) or
                    disk_id(disk) not in self.config_manager.get_managed_disk_whitelist() or
                    not DeviceManager.is_managed_disk(disk_index)):
                raise RuntimeError("硬盘已不在当前管理白名单中，取消唤醒")
            from src.hal.asm_commander import ASMCommander

            data = None
            last_error = ""
            for attempt in range(1, 5):
                try:
                    with ASMCommander(disk_index) as cmd:
                        if attempt == 1:
                            # ATA IDLE IMMEDIATE：显式唤醒 STANDBY/SLEEP 状态
                            try:
                                cmd.idle()
                            except Exception:
                                pass
                        data = cmd.get_smart_data()
                except Exception as e:
                    last_error = str(e)
                if data:
                    break
                logging.warning(
                    f"SMART 唤醒: Disk {disk_index} 第 {attempt}/4 次未响应，"
                    f"等待起转后重试...{('错误: ' + last_error) if last_error else ''}"
                )
                time.sleep(3)

            logging.info(f"SMART 唤醒: Disk {disk_index}, SMART={'OK' if data else 'FAIL'}")
            self._wake_done_signal.emit({
                "success": bool(data),
                "serial": serial,
                "data": data,
            })
        except Exception as e:
            logging.error(f"SMART 唤醒异常: {e}")
            self._wake_done_signal.emit({"success": False, "serial": serial})
        finally:
            self.monitor_service.scan_lock.release()
            pythoncom.CoUninitialize()

    def _on_wake_complete(self, result):
        wake_serial = result.get("serial")
        success = result.get("success", False)

        if not success:
            # 唤醒失败：保持“休眠”状态，允许稍后重试或直接弹出
            self.spin_down_button.setText("唤醒并解除黑名单")
            self.spin_down_button.setEnabled(getattr(self, 'current_disk_managed', False))
            self.status_label.setText("唤醒失败，硬盘未响应")
            QMessageBox.warning(
                self,
                "唤醒失败",
                "硬盘未响应唤醒命令，可能仍在起转或处于深度休眠状态。\n\n"
                "请等待 10-20 秒后，点击「唤醒并解除黑名单」重试；"
                "也可以直接点击「安全弹出设备」移除硬盘。"
            )
            return

        # 唤醒成功：正式解除休眠黑名单
        if hasattr(self, 'monitor_service'):
            self.monitor_service.mark_disk_awake(wake_serial)

        updated_disks = []
        for disk in (self.disk_data if hasattr(self, 'disk_data') and self.disk_data else []):
            d = dict(disk)
            if d.get("serial") == wake_serial and d.get("status") == "Sleeping":
                d["status"] = "Healthy"
                d["temp"] = "---"
                d["reallocated"] = "---"
                d["pending"] = "---"
            updated_disks.append(d)

        if updated_disks:
            self.handle_data_update(updated_disks)

        self.spin_down_button.setText("深度休眠")
        self.spin_down_button.setEnabled(getattr(self, 'current_disk_managed', False))
        self.status_label.setText("就绪")

        threading.Thread(
            target=lambda: self.monitor_service.check_all_smart(force=True),
            daemon=True,
        ).start()

    def setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logging.error("错误: 系统托盘不可用")
            return

        self.tray_icon = QSystemTrayIcon(self)
        
        # Use custom icon if available, otherwise fallback to system icon
        icon_path = os.path.join("assets", "icon.png")
        if os.path.exists(icon_path):
            icon = QIcon(icon_path)
        else:
            icon = self.style().standardIcon(QStyle.SP_DriveHDIcon)
            
        self.tray_icon.setIcon(icon)
        self.tray_icon.setToolTip("疾风知硬盘柜管理程序")
        self.tray_icon.activated.connect(self.on_tray_activated)

        show_action = QAction("显示主界面", self)
        quit_action = QAction("退出程序", self)
        
        show_action.triggered.connect(self.showNormal)
        quit_action.triggered.connect(self.quit_app)
        
        tray_menu = QMenu()
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()
        
        logging.info("系统托盘图标已初始化")

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.showNormal()
            self.activateWindow()
            self.raise_()

    def show_notification(self, title, message):
        # This might be called from a background thread
        # Use QMessageBox for critical errors that need immediate attention
        logging.info(f"NOTIFICATION: {title} - {message}")
        self.tray_icon.showMessage(title, message, QSystemTrayIcon.Warning, 10000)
        
        # If it's very critical, we can show a blocking dialog (needs to be thread-safe)
        # For simplicity, we use the tray message first.
        
    def quit_app(self):
        logging.info("正在退出应用程序...")
        if hasattr(self, 'monitor_service'):
            self.monitor_service.stop()
        QApplication.quit()

    def closeEvent(self, event):
        if hasattr(self, 'monitor_service') and self.monitor_service.shutdown_mode:
            event.accept()
            return
        if self.tray_icon.isVisible():
            self.hide()
            event.ignore()
