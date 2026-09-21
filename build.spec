import os
import sys

# 获取当前脚本所在目录的绝对路径
current_dir = os.path.abspath(os.getcwd())

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[current_dir],
    binaries=[],
    datas=[('assets', 'assets'), ('src', 'src')],
    hiddenimports=[
        'src',
        'src.ui',
        'src.ui.main_window',
        'src.core',
        'src.core.monitor_service',
        'src.core.device_manager',
        'src.core.device_renamer',
        'src.core.config_manager',
        'src.core.history_manager',
        'src.core.shutdown_guard',
        'src.core.shutdown_service',
        'src.hal',
        'src.hal.win32_api',
        'src.hal.asm_commander',
        'src.utils',
        'src.utils.admin',
        'src.utils.smart_parser',
        'src.utils.paths',
        'PySide6',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'PySide6.QtNetwork',
        'wmi',
        'pythoncom',
        'pywintypes',
        'win32api',
        'win32com',
        'win32service',
        'win32serviceutil',
        'win32event',
        'servicemanager',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='疾风知硬盘柜管理',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets\\icon.png',
    uac_admin=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='疾风知硬盘柜管理_v1.3.78',
)
