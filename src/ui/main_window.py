from PySide6.QtWidgets import (QMainWindow, QLabel, QVBoxLayout, QHBoxLayout, QWidget, 
                             QMessageBox, QSystemTrayIcon, QMenu, QApplication, QStyle, 
                             QTableWidget, QTableWidgetItem, QHeaderView, QListWidget, 
                             QListWidgetItem, QFrame, QScrollArea, QPushButton, QSlider,
                             QDialog, QTextEdit, QFileDialog, QCheckBox, QInputDialog)
from PySide6.QtGui import QIcon, QAction, QColor, QFont, QPalette, QPixmap
from PySide6.QtCore import Qt, Slot, Signal, QSize, QThread, QTimer
import logging
import os
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
from src.utils.paths import get_resource_path
from src.utils.log_reporter import LogReporter

logging.info("Win32API 导入成功")

# Win32 Constants
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004

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
        # 移除自动上传选项，因为没有后端服务器支持
        # settings_layout = QHBoxLayout()
        # self.auto_report_cb = QCheckBox("以后遇到严重错误自动发送报告 (需配置服务器)")
        # self.auto_report_cb.setChecked(self.report_config["auto_report"])
        # settings_layout.addWidget(self.auto_report_cb)
        # layout.addLayout(settings_layout)
        
        # 4. Actions
        btn_layout = QHBoxLayout()
        
        self.export_btn = QPushButton("仅导出到本地...")
        self.export_btn.clicked.connect(self.do_export)
        
        self.send_btn = QPushButton("通过邮件发送报告 (推荐)")
        self.send_btn.setStyleSheet("background-color: #76b900; color: black; font-weight: bold; border: none;")
        self.send_btn.clicked.connect(self.do_email_report)
        
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
        
    def do_email_report(self):
        """生成日志并打开邮件客户端"""
        self.send_btn.setEnabled(False)
        self.send_btn.setText("正在生成...")
        QApplication.processEvents()
        
        # 1. Pack logs to a temp location
        zip_path = self.reporter.pack_logs()
        if not zip_path:
            QMessageBox.warning(self, "错误", "无法生成日志包")
            self.send_btn.setEnabled(True)
            self.send_btn.setText("通过邮件发送报告 (推荐)")
            return
            
        # 2. Open folder with file selected
        try:
            subprocess.Popen(f'explorer /select,"{zip_path}"')
        except Exception as e:
            logging.error(f"Failed to open explorer: {e}")
            
        # 3. Construct mailto link
        recipient = "278715262@qq.com"
        subject = f"疾风知硬盘柜-错误报告 ({datetime.now().strftime('%Y-%m-%d')})"
        
        # 4. Show custom guide dialog
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("发送错误报告")
        msg_box.setText(
            "请按以下步骤发送报告：\n\n"
            "1. 包含日志的文件夹已自动打开 (选中了 .zip 文件)\n"
            "2. 请手动发送邮件给开发者\n\n"
            f"收件人: {recipient}\n"
            f"主　题: {subject}\n"
            "附　件: (请拖入刚才生成的 zip 文件)\n\n"
            "点击“确定”关闭此窗口。"
        )
        msg_box.setIcon(QMessageBox.Information)
        
        # Add a "Copy Email" button
        copy_btn = msg_box.addButton("复制邮箱地址", QMessageBox.ActionRole)
        msg_box.addButton("确定", QMessageBox.AcceptRole)
        
        msg_box.exec()
        
        if msg_box.clickedButton() == copy_btn:
            QApplication.clipboard().setText(recipient)
            QMessageBox.information(self, "提示", "邮箱地址已复制")
        
        self.accept()
            
    def accept(self):
        # Save checkbox state if it existed
        # self.config_manager.set_report_config(self.auto_report_cb.isChecked())
        super().accept()

NVIDIA_STYLE = """
QMainWindow {
    background-color: #0c0c0c;
    font-family: "OPPO Sans", "Microsoft YaHei", "Segoe UI", sans-serif;
}
QWidget {
    font-family: "OPPO Sans", "Microsoft YaHei", "Segoe UI", sans-serif;
}
QWidget#CentralWidget {
    background-color: #0c0c0c;
}
QListWidget {
    background-color: #1a1a1a;
    border: none;
    border-right: 1px solid #2d2d2d;
    outline: none;
    padding: 10px;
}
QListWidget::item {
    background-color: #262626;
    color: #ffffff;
    border-radius: 4px;
    margin-bottom: 8px;
    padding: 15px;
}
QListWidget::item:selected {
    background-color: #333333;
    border-left: 4px solid #9147ff; /* Purple accent for selection */
}
QListWidget::item:hover {
    background-color: #2d2d2d;
}
QLabel {
    color: #ffffff;
}
QLabel#TitleLabel {
    font-size: 24px;
    font-weight: bold;
    color: #9147ff; /* Changed to Purple */
    margin-bottom: 10px;
}
QLabel#StatusLabel {
    font-size: 14px;
    color: #aaaaaa;
    margin-bottom: 20px;
}
QFrame#DetailCard {
    background-color: #1a1a1a;
    border-radius: 8px;
    padding: 20px;
}
QTableWidget {
    background-color: #1a1a1a;
    color: #ffffff;
    gridline-color: #2d2d2d;
    border: none;
    selection-background-color: #333333;
}
QHeaderView::section {
    background-color: #262626;
    color: #9147ff; /* Changed to Purple */
    padding: 8px;
    border: none;
    font-weight: bold;
}
QScrollBar:vertical {
    background: #1a1a1a;
    width: 10px;
}
QScrollBar::handle:vertical {
    background: #333333;
    border-radius: 5px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QPushButton#EjectButton {
    background-color: #76b900; /* Reverted to Green */
    color: #000000;
    border: none;
    border-radius: 4px;
    padding: 10px 20px;
    font-weight: bold;
    font-size: 14px;
}
QPushButton#EjectButton:hover {
    background-color: #88d000;
}
QPushButton#EjectButton:pressed {
    background-color: #5c9100;
}
QPushButton#EjectButton:disabled {
    background-color: #333333;
    color: #666666;
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
    color: #9147ff; /* Changed to Purple */
}
QMessageBox {
    background-color: #1a1a1a;
}
QMessageBox QLabel {
    color: #ffffff;
}
QMessageBox QPushButton {
    background-color: #333333;
    color: #ffffff;
    border: 1px solid #444444;
    padding: 5px 15px;
    min-width: 80px;
}
QMessageBox QPushButton:hover {
    background-color: #444444;
}
"""

class MainWindow(QMainWindow):
    # Signal to update UI from background thread
    update_data_signal = Signal(list)
    _wake_done_signal = Signal(dict)

    def __init__(self, silent_mode=False):
        logging.info("正在初始化 MainWindow...")
        super().__init__()
        self._silent_mode = silent_mode
        self.version = "1.3.72"
        self.setWindowTitle(f"疾风知硬盘柜管理程序 v{self.version}")
        self.resize(1100, 750)
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
        self._wake_done_signal.connect(self._on_wake_complete)
        
        # UI Setup
        central_widget = QWidget()
        central_widget.setObjectName("CentralWidget")
        self.setCentralWidget(central_widget)
        
        self.main_layout = QHBoxLayout(central_widget)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        
        # Left Sidebar Area (Logo + List)
        self.sidebar_container = QWidget()
        self.sidebar_container.setFixedWidth(280)
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
        self.sidebar = QListWidget()
        self.sidebar.setFrameShape(QFrame.NoFrame) # Remove border as container has it
        self.sidebar.itemClicked.connect(self.on_disk_selected)
        # Give stretch to list so it takes available space
        self.sidebar_layout.addWidget(self.sidebar, 1)

        # Settings Area (Placed in Sidebar, AT THE BOTTOM)
        self.settings_container = QWidget()
        self.settings_container.setStyleSheet("background-color: #1a1a1a; border-top: 1px solid #2d2d2d;")
        self.settings_layout = QVBoxLayout(self.settings_container)
        self.settings_layout.setContentsMargins(10, 10, 10, 10)
        self.settings_layout.setSpacing(8)

        # 1. Refresh Button (Green)
        self.refresh_btn = QPushButton("刷新设备列表")
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
        self.shutdown_eject_checkbox = QCheckBox("关机/休眠时自动休眠硬盘（驱动级，固定开启）")
        self.shutdown_eject_checkbox.setStyleSheet("color: #888888; font-size: 12px; padding: 5px;")
        self.shutdown_eject_checkbox.setChecked(True)
        self.shutdown_eject_checkbox.setEnabled(False)
        self.shutdown_eject_checkbox.setToolTip("系统关机或休眠时，自动向所有外置硬盘发送 SLEEP 停转保护。")
        self.settings_layout.addWidget(self.shutdown_eject_checkbox)

        # 6. Safe Removal Spin-Down Patch Checkbox — 驱动级功能：固定启用
        self.spindown_patch_checkbox = QCheckBox("系统弹出时附加硬盘停转（驱动级，固定开启）")
        self.spindown_patch_checkbox.setStyleSheet("color: #888888; font-size: 12px; padding: 5px;")
        self.spindown_patch_checkbox.setChecked(True)
        self.spindown_patch_checkbox.setEnabled(False)
        self.spindown_patch_checkbox.setToolTip(
            "当您通过 Windows 系统托盘安全删除硬件时，\n"
            "程序会自动发送 SLEEP 停转命令，确保磁头归位、盘片停转。\n"
            "该功能为驱动级常驻，无需手动开启。"
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
        
        self.spin_down_button = QPushButton("立即休眠硬盘")
        self.spin_down_button.setObjectName("SpinDownButton")
        self.spin_down_button.setFixedWidth(180)
        self.spin_down_button.setStyleSheet("""
            QPushButton#SpinDownButton {
                background-color: #2d2d2d;
                color: #ffffff;
                border: 1px solid #3d3d3d;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton#SpinDownButton:hover {
                background-color: #3d3d3d;
                border-color: #4d4d4d;
            }
            QPushButton#SpinDownButton:pressed {
                background-color: #262626;
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
        self.actions_layout.addStretch()
        self.detail_layout.addLayout(self.actions_layout)
        
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

        # Tray Icon Setup
        logging.info("正在设置系统托盘...")
        self.setup_tray()

        self.current_disk_serial = None # Track currently selected disk
        
        # Start Monitor Service
        logging.info("正在启动监控服务线程...")
        self.monitor_service = MonitorService(
            callback_notify=self.show_notification,
            callback_update_ui=self.emit_update_signal
        )
        self.monitor_service.start()

        # Safe Removal Spin-Down Patcher — always register to intercept removal events
        # and pause monitor service to release device refs. _enabled only controls FLUSH+SLEEP.
        self.spindown_patcher = SafeRemovalPatcher()
        self.spindown_patcher.monitor_service = self.monitor_service
        # 驱动级功能：系统弹出停转始终启用，不受 UI 设置影响。
        self.spindown_patcher.enabled = True
        QTimer.singleShot(1000, self._register_spindown_patcher)

        # 驱动级常驻：启动即注册开机自启动（--silent 静默运行），
        # 并强制固化配置，确保关机/休眠保护与系统弹出停转始终生效。
        try:
            if not self.config_manager.is_autostart_enabled():
                if self.config_manager.set_autostart(True):
                    logging.info("已自动注册开机自启动（驱动级常驻）")
            self.config_manager.set_shutdown_eject(True)
            self.config_manager.set_safe_remove_spindown(True)
            logging.info("驱动级保护配置已固化：开机自启 + 关机休眠 + 弹出停转")
        except Exception as e:
            logging.warning(f"驱动级常驻配置失败: {e}")
        
        self.status_label.setText("监控服务运行中 (系统日志实时监控)")
        logging.info("MainWindow 初始化完成")

    def emit_update_signal(self, data):
        self.update_data_signal.emit(data)

    @Slot(list)
    def handle_data_update(self, data):
        logging.info(f"UI 收到数据更新: {len(data)} 条记录")
        
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
        for disk in data:
            model = disk.get("model", "Unknown")
            status = disk.get("status", "Unknown")
            is_removable = disk.get("is_removable", False)
            
            # 简化显示，如果型号太长则截断
            display_name = f"{model[:25]}..." if len(model) > 25 else model
            display_text = f"{display_name} ({status})"
            
            item = QListWidgetItem(display_text)
            item.setData(Qt.UserRole, disk)
            
            # 创建一个小圆点图标来区分内外置
            # 外置使用青色 (#00d4ff)，内置使用深灰色 (#555555)
            dot_color = QColor("#00d4ff") if is_removable else QColor("#555555")
            pixmap = QPixmap(12, 12)
            pixmap.fill(Qt.transparent)
            from PySide6.QtGui import QPainter, QBrush
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setBrush(QBrush(dot_color))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(2, 2, 8, 8)
            painter.end()
            item.setIcon(QIcon(pixmap))
            
            # 设置基本颜色（根据健康状态）
            if status == "Healthy":
                base_color = QColor("#76b900") # Green
            elif status == "Warning":
                base_color = QColor("orange")
            elif "Error" in status or "Failed" in status or status == "Critical":
                base_color = QColor("#ffaa00") # Orange-Yellow
            else:
                base_color = QColor("#ffffff")
                
            item.setForeground(base_color)
            item.setToolTip(f"{'外置/移动设备' if is_removable else '内置硬盘'} - {model}")
                
            self.sidebar.addItem(item)
            
            # 恢复选中
            if selected_serial and disk.get("serial") == selected_serial:
                self.sidebar.setCurrentItem(item)
                
        # 如果没有选中的，默认选第一个
        if not self.sidebar.currentItem() and self.sidebar.count() > 0:
            self.sidebar.setCurrentRow(0)
            
        # 更新详情面板（如果当前有选中的）
        if self.sidebar.currentItem():
            self.on_disk_selected(self.sidebar.currentItem())

    def on_interval_changed(self, value):
        minutes = value
        if minutes < 60:
            text = f"{minutes}分钟"
        else:
            hours = minutes / 60
            text = f"{hours:.1f}小时"
        self.interval_label.setText(f"SMART 检测频率: {text}/次")
        
    def on_interval_set(self):
        if not self.current_disk_serial:
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
        if not self.current_disk_serial or not hasattr(self, 'current_disk_index'):
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
        """处理刷新按钮点击：重新扫描硬件并更新列表"""
        # 增加提醒弹窗
        reply = QMessageBox.question(
            self, 
            "深度硬件扫描确认",
            "深度扫描将执行以下操作：\n"
            "1. 强制系统重新枚举所有硬件总线\n"
            "2. 尝试重置并唤醒处于异常状态的存储控制器\n"
            "3. 深度恢复已安全删除但未拔出的硬盘\n\n"
            "注意：此过程可能需要 1-3 分钟，期间界面可能会有短暂无响应，属于正常现象。\n"
            "是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.No:
            return

        self.refresh_btn.setText("正在扫描硬件...")
        self.refresh_btn.setEnabled(False)
        QApplication.processEvents()
        
        # 准备日志捕获
        captured_logs = []
        log_handler = ListHandler(captured_logs)
        log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s: %(message)s', datefmt='%H:%M:%S'))
        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)
        # 确保日志级别足够低以捕获 INFO
        original_level = root_logger.level
        root_logger.setLevel(logging.INFO)
        
        has_issue = False
        recovery_success = False
        
        # 获取扫描锁，防止手动刷新时后台扫描线程也在运行，导致日志交织
        lock_acquired = False
        if hasattr(self, 'monitor_service'):
            lock_acquired = self.monitor_service.scan_lock.acquire(blocking=True)
            
        try:
            # 1. 清除所有硬盘的休眠黑名单，允许重新检测
            if hasattr(self, 'monitor_service'):
                self.monitor_service.clear_disk_exclusions()
            
            # 2. 尝试重启处于“准备安全删除”状态的设备，以及其他异常状态设备
            logging.info(">>> 开始设备恢复流程 <<<")
            recovery_success = Win32API.force_recover_problem_devices()
            logging.info(">>> 设备恢复流程结束 <<<")
            
            # 3. 强制系统重新扫描总线
            logging.info(">>> 执行系统硬件重新扫描 <<<")
            Win32API.rescan_hardware()
            
            # 4. 等待一下让系统识别
            for _ in range(10): # Wait 1s
                time.sleep(0.1)
                QApplication.processEvents()
            
            # 5. 强制软件重新获取磁盘信息
            if hasattr(self, 'monitor_service'):
                # 注意：这里调用的是内部实现，因为我们已经持有了锁
                self.monitor_service._check_all_smart_impl(force=True)
                
            # 检查是否有值得展示的日志
            full_log = "\n".join(captured_logs)
            if "ERROR" in full_log or "WARNING" in full_log or "发现异常设备" in full_log:
                has_issue = True
                
        except Exception as e:
            logging.error(f"刷新失败: {e}")
            has_issue = True
        finally:
            if lock_acquired:
                self.monitor_service.scan_lock.release()
                
            self.refresh_btn.setText("刷新设备列表")
            self.refresh_btn.setEnabled(True)
            
            root_logger.removeHandler(log_handler)
            root_logger.setLevel(original_level)
            
            # 显示结果
            self.show_refresh_result(has_issue, recovery_success, captured_logs)

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

    def nativeEvent(self, eventType, message):
        """Handle Windows native events to detect shutdown"""
        try:
            if eventType.data() == b"windows_generic_MSG":
                msg = MSG.from_address(int(message))

                if msg.message == WM_QUERYENDSESSION:
                    logging.info("收到系统关机信号 (WM_QUERYENDSESSION)")
                    # 驱动级功能：无论 UI 设置如何，关机时始终休眠外置硬盘。
                    hwnd = int(self.winId())
                    reason = "正在为您执行硬盘关机休眠保护，请稍候..."
                    ctypes.windll.user32.ShutdownBlockReasonCreate(hwnd, ctypes.c_wchar_p(reason))
                    self.eject_all_removable_disks()
                    return True, 1

                elif msg.message == WM_ENDSESSION:
                    logging.info(f"系统会话结束 (WM_ENDSESSION, wParam={msg.wParam})")
                    if msg.wParam:
                        pass

                elif msg.message == WM_POWERBROADCAST:
                    if msg.wParam == PBT_APMSUSPEND:
                        logging.info("收到系统休眠信号 (PBT_APMSUSPEND)")
                        # 驱动级功能：系统休眠时始终休眠外置硬盘。
                        self.eject_all_removable_disks(is_system_sleep=True)
                    return True, 0

                elif msg.message == WM_DEVICECHANGE:
                    # 已处理。注意 DBT_DEVICEQUERYREMOVE 的返回值语义与普通消息相反：
                    #   返回 TRUE (result=1) = 允许移除设备
                    #   返回 FALSE/0 (result=0) = 否决移除（Windows 显示"设备正在使用中"）
                    # 程序从不否决弹出，因此统一返回 (True, 1)。
                    self.spindown_patcher.handle_wm_devicechange(msg.wParam, msg.lParam)
                    return True, 1
        except Exception as e:
            logging.error(f"nativeEvent error: {e}")

        return super().nativeEvent(eventType, message)

    def eject_all_removable_disks(self, is_system_sleep=False):
        """关机/系统休眠时休眠所有外置硬盘：SLEEP → 等待盘停转(~12s) → 断电"""
        event_type = "系统休眠" if is_system_sleep else "关机"
        logging.info(f"正在执行{event_type}保护(休眠所有硬盘)...")

        # 硬盘柜实际停转需要约 10 秒，设 20 秒余量（覆盖停转较慢的盘）
        SPIN_DOWN_WAIT_SEC = 20

        hwnd = int(self.winId()) if not is_system_sleep else None
        if not is_system_sleep and hwnd is not None:
            try:
                reason = "正在为您执行硬盘关机休眠保护，请稍候..."
                ctypes.windll.user32.ShutdownBlockReasonCreate(hwnd, ctypes.c_wchar_p(reason))
            except Exception as e:
                logging.warning(f"设置关机阻塞提示失败: {e}")

        try:
            if hasattr(self, 'monitor_service'):
                self.monitor_service.shutdown_mode = True

            # 关机阶段严格只使用缓存，避免再次在线扫描唤醒硬盘。
            # 优先使用 monitor_service.cached_disks（物理盘缓存，始终保持最新），
            # 避免依赖 UI disk_data（仅在 SMART 检测时刷新，silent 模式下可能过期）。
            cached = []
            if hasattr(self, 'monitor_service') and self.monitor_service:
                cached = getattr(self.monitor_service, 'cached_disks', []) or []
            if not cached:
                logging.warning("关机保护缺少磁盘缓存，跳过自动休眠以避免重新扫描唤醒硬盘")
                return

            # 跳过已休眠/已弹出的硬盘——向它们发送 FLUSH CACHE 等任何 SCSI 命令都会唤醒硬盘，
            # 导致"唤醒再休眠"的重复动作。它们已在 sleeping_disks 中确认休眠，无需再发命令。
            already_asleep = set()
            if hasattr(self, 'monitor_service'):
                already_asleep = (
                    self.monitor_service.sleeping_disks
                    | self.monitor_service.ejected_disks
                )

            eject_list = []
            for d in cached:
                idx = getattr(d, 'index', None)
                serial = getattr(d, 'serial_number', None)
                model = getattr(d, 'model', None) or getattr(d, 'model_hint', None) or ('Disk' + str(idx))
                is_removable = getattr(d, 'is_removable', False)
                if idx is not None and is_removable and serial not in already_asleep:
                    eject_list.append((idx, model, serial))

            if not eject_list:
                logging.info(
                    f"没有需要休眠的外置硬盘"
                    f"（已确认休眠/弹出的盘会跳过，共 {len(already_asleep)} 块）"
                )
                return

            # Phase 1: 并发发送 SLEEP 命令
            results = []
            sleep_sent_time = time.time()

            def sleep_worker(disk_index, model, serial):
                try:
                    logging.info(f"{event_type}保护: 正在休眠硬盘 {model} (Index: {disk_index})...")
                    success, msg = DeviceManager.spin_down_disk(
                        disk_index, model=model, serial=serial
                    )
                    results.append((disk_index, model, serial, success, msg))
                    if success:
                        logging.info(f"{event_type}保护: 硬盘 {disk_index} SLEEP 已发送（桥已接受）")
                    else:
                        logging.warning(f"{event_type}保护: 硬盘 {disk_index} SLEEP 失败 — {msg}")
                except Exception as e:
                    logging.error(f"磁盘 {disk_index} {event_type}休眠异常: {e}")
                    results.append((disk_index, model, serial, False, str(e)))

            threads = []
            for disk_index, model, serial in eject_list:
                t = threading.Thread(target=sleep_worker, args=(disk_index, model, serial))
                t.start()
                threads.append(t)

            for t in threads:
                remaining = 5.0 - (time.time() - sleep_sent_time)
                if remaining > 0:
                    t.join(timeout=remaining)

            # Phase 2: 等待硬盘实际停转（不发送任何 I/O，纯等待）
            elapsed = time.time() - sleep_sent_time
            remaining_wait = max(0, SPIN_DOWN_WAIT_SEC - elapsed)
            if remaining_wait > 0:
                logging.info(
                    f"{event_type}保护: SLEEP 已全部发送，等待 {remaining_wait:.1f} 秒让硬盘停转..."
                )
                time.sleep(remaining_wait)

            # 汇总：标记 SLEEP 成功的盘
            slept_serials = []
            for _, _, serial, success, msg in results:
                if success:
                    slept_serials.append(serial)
                else:
                    logging.error(f"{event_type}保护: {serial} SLEEP 失败，不标记为 Sleeping")

            if hasattr(self, 'monitor_service'):
                for serial in slept_serials:
                    self.monitor_service.mark_disk_sleeping(serial)

            total_wait = time.time() - sleep_sent_time
            logging.info(
                f"{event_type}保护完成: {len(slept_serials)}/{len(eject_list)} 块硬盘已休眠"
                f"（总等待 {total_wait:.1f} 秒），可安全断电"
            )
        except Exception as e:
            logging.error(f"{event_type}休眠保护执行异常: {e}")
        finally:
            if not is_system_sleep and hwnd is not None:
                try:
                    ctypes.windll.user32.ShutdownBlockReasonDestroy(hwnd)
                except:
                    pass

    def on_disk_selected(self, item):
        if not item:
            self.spin_down_button.setEnabled(False)
            return
        
        disk = item.data(Qt.UserRole)
        self.title_label.setText(disk.get("model", "未知型号"))
        
        status = disk.get("status", "")
        
        if status == "Sleeping":
            self.spin_down_button.setText("唤醒/刷新硬盘")
            self.spin_down_button.setEnabled(True)
        else:
            self.spin_down_button.setText("立即休眠硬盘")
            self.spin_down_button.setEnabled(True)
            
        self.current_disk_index = disk.get("index")
        self.current_disk_serial = disk.get("serial")
        self.current_disk_model = disk.get("model")

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
        if not hasattr(self, 'current_disk_index'):
            return

        current_item = self.sidebar.currentItem()
        if current_item:
            disk = current_item.data(Qt.UserRole)
            if disk.get("status") == "Sleeping":
                self._wake_sleeping_disk()
                return

        reply = QMessageBox.question(
            self, '确认休眠', 
            f"确定要让磁盘 {self.current_disk_index} 立即进入休眠吗？\n\n注意：如果磁盘正在读写数据，此操作可能会被立即唤醒或导致数据传输中断。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            logging.info(f"用户触发磁盘 {self.current_disk_index} 休眠")
            if hasattr(self, 'monitor_service') and self.current_disk_serial:
                self.monitor_service.mark_disk_sleeping(self.current_disk_serial)

            self.spin_down_button.setEnabled(False)
            self.spin_down_button.setText("正在休眠...")
            self.status_label.setText("正在发送休眠命令并验证停转...")
            QApplication.processEvents()
            
            threading.Thread(
                target=self._spin_down_and_verify_thread,
                args=(self.current_disk_index, self.current_disk_model, self.current_disk_serial),
                daemon=True,
            ).start()

    def _spin_down_and_verify_thread(self, disk_index, model, serial):
        result = {}
        try:
            success, message = DeviceManager.spin_down_disk(
                disk_index, model=model, serial=serial
            )
            result = {"success": success, "message": message, "serial": serial}
        except Exception as e:
            result = {"success": False, "message": str(e), "serial": serial}
        QTimer.singleShot(0, lambda: self._on_spin_down_complete(result))

    def _on_spin_down_complete(self, result):
        success = result.get("success", False)
        message = result.get("message", "")
        serial = result.get("serial")

        if success:
            self._mark_current_disk_sleeping_in_ui()
            full_message = message + self._build_windows_sleep_guidance()
            QMessageBox.information(self, "休眠成功", full_message)
            self.status_label.setText("硬盘已进入休眠状态")
        else:
            if hasattr(self, 'monitor_service') and serial:
                self.monitor_service.mark_disk_awake(serial)
            QMessageBox.warning(self, "休眠失败", message)
            self.spin_down_button.setText("立即休眠硬盘")
            self.spin_down_button.setEnabled(True)
            self.status_label.setText("休眠失败")

    def _mark_current_disk_sleeping_in_ui(self):
        """本地直接把当前磁盘标记为休眠，避免立即触发一次全盘扫描导致再次唤醒。"""
        if not hasattr(self, "disk_data") or not self.disk_data:
            self.status_label.setText("硬盘已进入休眠状态")
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
        self.status_label.setText("硬盘已进入休眠状态")

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
        self.status_label.setText(f"正在解除休眠黑名单并发送 SMART 查询唤醒硬盘 {disk_index}...")
        QApplication.processEvents()

        self.monitor_service.mark_disk_awake(serial)

        threading.Thread(
            target=self._wake_via_smart_thread,
            args=(disk_index, serial),
            daemon=True,
        ).start()

    def _wake_via_smart_thread(self, disk_index, serial):
        """移出黑名单后立刻发送 SMART 查询 = I/O 唤醒"""
        import pythoncom
        pythoncom.CoInitialize()
        try:
            from src.hal.asm_commander import ASMCommander

            with ASMCommander(disk_index) as cmd:
                data = cmd.get_smart_data()
                logging.info(f"SMART 唤醒: Disk {disk_index}, SMART={'OK' if data else 'FAIL'}")
                self._wake_done_signal.emit({
                    "success": True,
                    "serial": serial,
                    "data": data,
                })
        except Exception as e:
            logging.error(f"SMART 唤醒异常: {e}")
            self._wake_done_signal.emit({"success": False, "serial": serial})
        finally:
            pythoncom.CoUninitialize()

    def _on_wake_complete(self, result):
        wake_serial = result.get("serial")

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

        self.spin_down_button.setText("立即休眠硬盘")
        self.spin_down_button.setEnabled(True)
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
