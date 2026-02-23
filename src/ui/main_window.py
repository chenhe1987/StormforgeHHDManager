from PySide6.QtWidgets import (QMainWindow, QLabel, QVBoxLayout, QHBoxLayout, QWidget, 
                             QMessageBox, QSystemTrayIcon, QMenu, QApplication, QStyle, 
                             QTableWidget, QTableWidgetItem, QHeaderView, QListWidget, 
                             QListWidgetItem, QFrame, QScrollArea, QPushButton, QSlider,
                             QDialog, QTextEdit, QFileDialog, QCheckBox)
from PySide6.QtGui import QIcon, QAction, QColor, QFont, QPalette, QPixmap
from PySide6.QtCore import Qt, Slot, Signal, QSize
import logging
import os
import time
import zipfile
import subprocess
from datetime import datetime
from src.hal.win32_api import Win32API
from src.core.monitor_service import MonitorService
from src.core.device_manager import DeviceManager
from src.core.config_manager import ConfigManager
from src.utils.paths import get_resource_path

logging.info("Win32API 导入成功")

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

NVIDIA_STYLE = """
QMainWindow {
    background-color: #0c0c0c;
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

    def __init__(self):
        logging.info("正在初始化 MainWindow...")
        super().__init__()
        self.setWindowTitle("疾风知硬盘柜管理程序")
        self.resize(1100, 750)
        self.setStyleSheet(NVIDIA_STYLE)
        
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
        self.sidebar_layout.addWidget(self.sidebar)

        # Settings Area at Sidebar Bottom
        self.settings_container = QWidget()
        self.settings_container.setStyleSheet("background-color: #1a1a1a; border-top: 1px solid #2d2d2d; padding: 10px;")
        self.settings_layout = QVBoxLayout(self.settings_container)
        
        from PySide6.QtWidgets import QCheckBox
        self.autostart_checkbox = QCheckBox("随系统启动")
        self.autostart_checkbox.setStyleSheet("""
            QCheckBox {
                color: #aaaaaa;
                font-size: 13px;
                padding: 5px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
            }
            QCheckBox:hover {
                color: #ffffff;
            }
        """)
        # Initialize state from config/registry
        is_enabled = self.config_manager.is_autostart_enabled()
        self.autostart_checkbox.setChecked(is_enabled)
        self.autostart_checkbox.stateChanged.connect(self.on_autostart_changed)
        
        # Add Refresh Button
        self.refresh_btn = QPushButton("刷新设备列表")
        self.refresh_btn.setObjectName("RefreshButton")
        self.refresh_btn.setFixedHeight(45)
        self.refresh_btn.setToolTip("重新扫描系统硬件，识别新插入的硬盘")
        # 显式设置绿色样式，与安全弹出按钮一致
        self.refresh_btn.setStyleSheet("""
            QPushButton#RefreshButton {
                background-color: #76b900;
                color: #000000;
                border: none;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: bold;
                font-size: 14px;
                margin-bottom: 12px;
            }
            QPushButton#RefreshButton:hover {
                background-color: #88d000;
            }
            QPushButton#RefreshButton:pressed {
                background-color: #5c9100;
            }
        """)
        self.refresh_btn.clicked.connect(self.on_refresh_clicked)
        self.settings_layout.addWidget(self.refresh_btn)
        
        # Add Export Logs Button
        self.export_logs_btn = QPushButton("导出错误日志")
        self.export_logs_btn.setObjectName("ExportLogsButton")
        self.export_logs_btn.setFixedHeight(30)
        self.export_logs_btn.setToolTip("将程序运行日志打包导出，以便排查问题")
        self.export_logs_btn.setStyleSheet("""
            QPushButton#ExportLogsButton {
                background-color: #333333;
                color: #aaaaaa;
                border: 1px solid #444444;
                border-radius: 4px;
                font-size: 12px;
                margin-bottom: 8px;
            }
            QPushButton#ExportLogsButton:hover {
                background-color: #444444;
                color: #ffffff;
                border-color: #555555;
            }
        """)
        self.export_logs_btn.clicked.connect(self.export_logs)
        self.settings_layout.addWidget(self.export_logs_btn)

        self.settings_layout.addWidget(self.autostart_checkbox)
        self.sidebar_layout.addWidget(self.settings_container)
            }
            QPushButton#RefreshButton:hover {
                background-color: #88d000;
            }
            QPushButton#RefreshButton:pressed {
                background-color: #5c9100;
            }
            QPushButton#RefreshButton:disabled {
                background-color: #333333;
                color: #555555;
            }
        """)
        self.refresh_btn.clicked.connect(self.on_refresh_clicked)
        
        self.settings_layout.addWidget(self.refresh_btn)
        self.settings_layout.addWidget(self.autostart_checkbox)
        self.sidebar_layout.addWidget(self.settings_container)
        
        self.main_layout.addWidget(self.sidebar_container)
        
        # Right Area: Details
        self.detail_area = QWidget()
        self.detail_layout = QVBoxLayout(self.detail_area)
        self.detail_layout.setContentsMargins(30, 30, 30, 30)
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
        
        self.actions_layout.addSpacing(10)
        
        self.eject_button = QPushButton("安全弹出设备")
        self.eject_button.setObjectName("EjectButton")
        self.eject_button.setFixedWidth(180)
        self.eject_button.clicked.connect(self.on_eject_clicked)
        self.eject_button.setEnabled(False) # Default disabled
        self.actions_layout.addWidget(self.eject_button)
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
        
        self.config_manager = ConfigManager()

        # Start Monitor Service
        logging.info("正在启动监控服务线程...")
        self.monitor_service = MonitorService(
            callback_notify=self.show_notification,
            callback_update_ui=self.emit_update_signal
        )
        self.monitor_service.start()
        
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
        
        # 1. 保存到配置
        self.config_manager.set_sleep_timer(self.current_disk_serial, minutes)
        
        # 2. 立即应用到硬件
        success, message = DeviceManager.set_standby_timer(self.current_disk_index, minutes)
        
        if success:
            self.sleep_timer_label.setStyleSheet("color: #9147ff; font-size: 13px; font-weight: bold;")
            # self.tray_icon.showMessage("设置成功", f"硬盘休眠时间已设置为 {minutes} 分钟", QSystemTrayIcon.Information, 3000)
        else:
            self.sleep_timer_label.setStyleSheet("color: #ffaa00; font-size: 13px; font-weight: bold;")
            QMessageBox.warning(self, "设置失败", message)
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
                self.monitor_service.sleeping_disks.clear()
            
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
        """Export logs to a zip file for troubleshooting"""
        import sys
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            default_name = f"JiFengZhi_Logs_{timestamp}.zip"
            
            # Ask user where to save
            file_path, _ = QFileDialog.getSaveFileName(
                self, "导出错误日志", 
                os.path.join(os.path.expanduser("~"), "Desktop", default_name),
                "Zip Files (*.zip)"
            )
            
            if not file_path:
                return
                
            # Create zip
            with zipfile.ZipFile(file_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                if getattr(sys, 'frozen', False):
                    base_dir = os.path.dirname(sys.executable)
                
                # Add app.log
                log_path = os.path.join(base_dir, "app.log")
                if os.path.exists(log_path):
                    zipf.write(log_path, "app.log")
                
                # Add smart_history.json
                history_path = os.path.join(base_dir, "smart_history.json")
                if os.path.exists(history_path):
                    zipf.write(history_path, "smart_history.json")
                    
                # Add system info
                info = f"OS: Windows\nTime: {timestamp}\nApp Version: v1.3.26\n"
                zipf.writestr("system_info.txt", info)
                
            QMessageBox.information(self, "导出成功", f"日志已保存至:\n{file_path}\n\n请将此文件发送给开发者。")
            
            # Open folder
            subprocess.Popen(f'explorer /select,"{file_path}"')
            
        except Exception as e:
            logging.error(f"Failed to export logs: {e}")
            QMessageBox.critical(self, "导出失败", f"无法导出日志: {str(e)}")

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

    def on_disk_selected(self, item):
        if not item:
            self.eject_button.setEnabled(False)
            self.spin_down_button.setEnabled(False)
            return
        
        disk = item.data(Qt.UserRole)
        self.title_label.setText(disk.get("model", "未知型号"))
        
        # 如果硬盘处于休眠状态，允许点击按钮来“唤醒”或重新检测
        status = disk.get("status", "")
        is_removable = disk.get("is_removable", False)
        
        if status == "Sleeping":
            self.spin_down_button.setText("唤醒/刷新硬盘")
            self.eject_button.setEnabled(False) 
            self.eject_button.setToolTip("硬盘处于休眠状态，请先唤醒")
        else:
            self.spin_down_button.setText("立即休眠硬盘")
            if is_removable:
                self.eject_button.setEnabled(True)
                self.eject_button.setToolTip("安全弹出并停止该外置硬盘")
                self.eject_button.show()
            else:
                self.eject_button.setEnabled(False)
                self.eject_button.setToolTip("内置硬盘不支持安全弹出")
                # 也可以选择隐藏，或者只是禁用。这里选择禁用并显示提示。
                # 如果用户希望区分更明显，可以考虑隐藏
                # self.eject_button.hide()
            
        self.spin_down_button.setEnabled(True)
        self.current_disk_index = disk.get("index") # Store current index
        self.current_disk_serial = disk.get("serial")

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
            
        # 检查当前是否已经是休眠状态
        current_item = self.sidebar.currentItem()
        if current_item:
            disk = current_item.data(Qt.UserRole)
            if disk.get("status") == "Sleeping":
                # 执行唤醒逻辑
                if hasattr(self, 'monitor_service') and self.current_disk_serial:
                    self.monitor_service.mark_disk_awake(self.current_disk_serial)
                    self.monitor_service.check_all_smart(force=True)
                return

        reply = QMessageBox.question(
            self, '确认休眠', 
            f"确定要让磁盘 {self.current_disk_index} 立即进入休眠吗？\n\n注意：如果磁盘正在读写数据，此操作可能会被立即唤醒或导致数据传输中断。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            logging.info(f"用户触发磁盘 {self.current_disk_index} 休眠")
            # 1. 首先在监控服务中屏蔽该硬盘，防止刚停转就被唤醒
            if hasattr(self, 'monitor_service') and self.current_disk_serial:
                self.monitor_service.mark_disk_sleeping(self.current_disk_serial)
            
            # 2. 发送停转指令
            success, message = DeviceManager.spin_down_disk(self.current_disk_index)
            
            if success:
                QMessageBox.information(self, "操作成功", message)
                # 3. 强制刷新 UI 显示为“休眠中”
                if hasattr(self, 'monitor_service'):
                    self.monitor_service.check_all_smart()
            else:
                # 如果失败了，恢复监控
                if hasattr(self, 'monitor_service') and self.current_disk_serial:
                    self.monitor_service.mark_disk_awake(self.current_disk_serial)
                QMessageBox.warning(self, "操作失败", message)

    def on_eject_clicked(self):
        if not hasattr(self, 'current_disk_index'):
            return
            
        reply = QMessageBox.question(
            self, '确认弹出', 
            f"确定要安全弹出磁盘 {self.current_disk_index} 吗？\n所有关联的分区都将被卸载。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            logging.info(f"用户触发弹出磁盘 {self.current_disk_index}")
            # 1. 首先在监控服务中屏蔽该硬盘
            if hasattr(self, 'monitor_service') and self.current_disk_serial:
                self.monitor_service.mark_disk_sleeping(self.current_disk_serial)

            # 2. 执行弹出逻辑（内部包含停转指令）
            success, message = DeviceManager.safe_eject_disk(self.current_disk_index)
            
            if success:
                QMessageBox.information(
                    self, 
                    "弹出成功", 
                    f"{message}\n\n注意：由于硬盘已进入深度休眠，若重新插入后未识别，请点击左下角的“刷新设备列表”按钮。"
                )
                # 触发一次刷新，由于已标记为休眠，检测时会跳过实际硬件查询
                if hasattr(self, 'monitor_service'):
                    self.monitor_service.check_all_smart()
            else:
                # 弹出失败，恢复监控
                if hasattr(self, 'monitor_service') and self.current_disk_serial:
                    self.monitor_service.mark_disk_awake(self.current_disk_serial)
                QMessageBox.warning(self, "弹出失败", message)

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
        # Minimize to tray instead of closing
        if self.tray_icon.isVisible():
            self.hide()
            event.ignore()
