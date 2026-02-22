# 疾风知硬盘柜管理程序 (JiFengZhi HDD Manager)

[![Gitee Release](https://img.shields.io/badge/Gitee-Download-red)](https://gitee.com/stormforge/JiFengZhiHDDManager/releases)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

## 📥 下载与安装 (Download & Install)

**方式一：直接下载成品 (推荐)**
1.  访问本项目的 [Gitee 发行版页面 (Releases)](https://gitee.com/stormforge/JiFengZhiHDDManager/releases)。
2.  下载最新版本的压缩包（例如 `疾风知硬盘柜管理_v1.3.26.zip`）。
3.  解压到任意文件夹。
4.  右键以**管理员身份运行** `疾风知硬盘柜管理.exe`。

**方式二：源码运行**
```bash
git clone https://gitee.com/stormforge/JiFengZhiHDDManager.git
cd JiFengZhiHDDManager
pip install -r requirements.txt
python main.py
```

---

## 1. 项目概述 (Project Overview)
本项目是一个专为 Windows 平台设计的硬盘柜管理工具，专门针对 **ASM2074 (USB 3.0 Hub)** + **ASM1153E (USB-SATA Bridge)** 芯片方案的硬盘柜。
主要功能包括硬盘健康监控 (SMART)、系统文件系统错误监控、异常弹窗报警、硬盘休眠管理以及**安全停转弹出 (Safe Spin-down & Eject)** 功能。

### 1.1 核心目标
- **数据安全**: 在断电/拔出前确保磁头归位并停止盘片旋转。
- **静默守护**: 极低频次的 SMART 检测，避免打扰硬盘休眠。
- **实时监控**: 通过系统日志及时发现 NTFS/ReFS 文件系统错误。
- **用户友好**: 托盘运行，异常时弹窗打断，可视化管理。

---

## 2. 技术栈 (Tech Stack)

为了兼顾底层硬件操作和 UI 开发效率，推荐使用以下技术栈：

- **编程语言**: Python 3.10+ (或 C# .NET 6+)
  - *注：本文档以 Python 方案为例，因其 ctypes 调用 Win32 API 灵活且开发迅速。*
- **GUI 框架**: PySide6 (Qt for Python) - 现代化界面，支持系统托盘。
- **底层交互**: 
  - `ctypes` / `pywin32`: 调用 Windows API (`DeviceIoControl`, `CreateFile`, `EventLog`).
  - `wmi`: 获取基础磁盘信息。
- **打包工具**: Nuitka 或 PyInstaller (生成单文件 exe)。

---

## 3. 系统架构 (Architecture)

程序分为三层架构：

1.  **硬件抽象层 (HAL - Hardware Abstraction Layer)**
    - 负责与 ASM1153E 通信。
    - 封装 SCSI Pass-Through (SAT) 指令。
    - 实现 SMART 数据读取、电源管理指令发送。
2.  **核心服务层 (Core Service)**
    - **MonitorService**: 负责定时任务调度。
      - **SMART Monitor**: 低频任务 (24h/次)，读取物理磁盘健康数据。
      - **EventLog Monitor**: 高频任务 (5min/次)，读取系统日志中的磁盘错误。
    - **DeviceManager**: 维护当前在线磁盘列表，处理热插拔事件 (`WM_DEVICECHANGE`)。
    - **ConfigManager**: 管理休眠时间设置、报警阈值。
3.  **用户界面层 (UI Layer)**
    - **TrayIcon**: 系统托盘图标，右键菜单（打开主界面、安全弹出）。
    - **Dashboard**: 显示硬盘列表、温度、健康度、日志。
    - **Notification**: 异常弹窗 (Toast 或 MessageBox)。

---

## 4. 核心功能实现细节 (Implementation Details)

### 4.1 硬盘识别与 SMART 监控
由于经过了 USB Hub (ASM2074) 和 Bridge (ASM1153E)，直接读取 SMART 需要使用 **SCSI ATA Translation (SAT)**。

- **API**: `CreateFile` (打开物理驱动器 `\\.\PhysicalDriveX`), `DeviceIoControl`.
- **IOCTL Code**: `IOCTL_SCSI_PASS_THROUGH_DIRECT` (推荐) 或 `IOCTL_ATA_PASS_THROUGH`.
- **监控策略 (关键更新)**:
  - **SMART 检测频率**: **每 24 小时一次**。
  - **设计考量**: 频繁读取 SMART 指令会强制唤醒硬盘，导致休眠失效，长期频繁启停会缩短硬盘寿命。
  - **触发机制**: 仅在以下情况读取 SMART：
    1. 程序启动时（如果距离上次检测超过24小时）。
    2. 到达定时周期（24小时）。
    3. 用户手动点击“刷新健康度”。
    4. 系统日志检测到严重磁盘错误时（触发一次紧急检查）。

### 4.2 系统内核日志监控
监控 Windows 记录的文件系统错误，这通常比 SMART 更早发现逻辑坏道或连接不稳定。

- **API**: Windows Event Log API (`EvtQuery`, `EvtNext`).
- **优势**: 读取 Event Log 是查询操作系统自身的数据库，**不会产生物理磁盘 I/O**，因此**不会唤醒休眠的硬盘**。
- **监控频率**: **每 5 分钟** (高频)。
- **过滤条件**:
  - **Log Name**: `System`
  - **Sources**: `Disk`, `Ntfs`, `ReFS`, `btrfs` (如果安装了驱动).
  - **Level**: `Error` (1), `Warning` (2).
- **逻辑**: 轮询最近产生的日志，匹配盘符或物理磁盘号，发现新错误即报警。

### 4.3 硬盘休眠管理 (Hibernation)
管理硬盘的 APM (Advanced Power Management) 和 Standby Timer。

- **ATA 命令**:
  - `STANDBY IMMEDIATE (0xE0)`: 立即休眠。
  - `IDLE (0xE3)`: 设置空闲计时器（自动休眠时间）。
  - `SET FEATURES (0xEF)`: 设置 APM Level (子指令 `0x05`, 值 `0x01`-`0xFE`, `0x80`以下允许停转)。

### 4.4 安全停转弹出 (Spin Down & Eject)
这是本程序的特色功能。Windows 默认的“安全删除”有时不会发送停转命令，导致硬盘在高速旋转时断电（伤硬盘）。

- **操作流程**:
  1. 用户在软件界面点击“弹出硬盘柜”或特定硬盘。
  2. **Step 1 (Sync)**: 调用系统 `FlushFileBuffers` 确保缓存写入。
  3. **Step 2 (Spin Down)**: 发送 SCSI 命令 `START STOP UNIT`。
     - **Opcode**: `0x1B`
     - **Immed**: `0` (等待命令完成)
     - **LoEj**: `1` (Load/Eject)
     - **Start**: `0` (Stop the motor)
  4. **Step 3 (Eject)**: 调用 Windows API `CM_Request_Device_Eject` 安全移除 USB 设备。
  5. **反馈**: 弹出成功提示“硬盘已停转，可安全关闭电源”。

---

## 5. 开发路线图 (Roadmap)

### Phase 1: 基础框架与硬件通信 (Week 1)
- [ ] 搭建 Python/PySide6 项目结构。
- [ ] 实现 `DiskInfo` 类：获取物理磁盘号、Model、Serial Number。
- [ ] **关键**: 实现 `ASM1153E_Commander` 类，封装 `DeviceIoControl` 发送 SCSI/ATA 命令。
- [ ] 验证：能够读取到 USB 硬盘的 SMART 原始数据。

### Phase 2: 监控与报警服务 (Week 2)
- [ ] 实现 SMART 解析器，将原始数据转换为人类可读格式。
- [ ] 实现 Event Log 监听器 (每5分钟)。
- [ ] **关键**: 实现定时任务调度器，确保 SMART 检测仅每24小时执行一次。
- [ ] 实现异常弹窗逻辑 (PySide6 Dialog)。

### Phase 3: 电源管理与弹出 (Week 3)
- [ ] 实现“休眠时间设置”功能 (发送 ATA IDLE 指令)。
- [ ] 实现“安全停转”逻辑 (`START STOP UNIT` -> `Eject`)。
- [ ] 绑定 UI 上的“安全移除”按钮。

### Phase 4: UI 完善与打包 (Week 4)
- [ ] 美化 Dashboard，显示硬盘温度曲线。
- [ ] 实现系统托盘最小化、开机自启。
- [ ] 使用 Nuitka 打包为 EXE。

---

## 6. 关键代码片段参考 (Reference)

### SMART 监控周期逻辑 (Python)
```python
import time

class SmartMonitor:
    def __init__(self):
        self.last_check_time = {} # {disk_serial: timestamp}
        self.check_interval = 24 * 3600 # 24 hours

    def should_check(self, disk_serial):
        current_time = time.time()
        last_time = self.last_check_time.get(disk_serial, 0)
        
        # 如果从未检测过，或者距离上次检测超过24小时
        if (current_time - last_time) > self.check_interval:
            return True
        return False
        
    def check_smart(self, disk):
        if not self.should_check(disk.serial):
            return
            
        # 执行 SMART 读取 IOCTL
        # ...
        
        self.last_check_time[disk.serial] = time.time()
```

### SCSI Start/Stop Unit Command Structure
```python
# 伪代码结构，用于发送停转指令
scsi_cdb = [
    0x1B, # Operation Code: START STOP UNIT
    0x01, # Immed=1 (不等待) 或 0x00 (等待)
    0x00, # Reserved
    0x00, # Reserved
    0x02, # LoEj=1, Start=0 (Eject and Stop) -> 0x02 | Start=0
    0x00  # Control
]
```
