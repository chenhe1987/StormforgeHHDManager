# 疾风知硬盘柜管理程序 (JiFengZhi HDD Manager)

[![Gitee Release](https://img.shields.io/badge/Gitee-Download-red)](https://gitee.com/stormforge/JiFengZhiHDDManager/releases)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

## 📥 下载与安装 (Download & Install)

**方式一：直接下载成品 (推荐)**
1.  访问本项目的 [Gitee 发行版页面 (Releases)](https://gitee.com/stormforge/JiFengZhiHDDManager/releases)。
2.  下载最新版本的压缩包（例如 `疾风知硬盘柜管理_v1.3.59.zip`）。
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
这是本程序的特色功能。针对部分 USB-SATA 桥接芯片上“安全弹出”可能长时间卡住的问题，当前版本已经调整为更接近 Windows 原生“安全删除硬件”的策略，同时保留硬盘停转增强。

- **当前策略**:
  1. 用户在软件界面点击“安全弹出”。
  2. 程序先识别当前物理磁盘对应的 Windows 设备实例 ID，并记录该磁盘关联的卷信息。
  3. 程序尝试发送短超时 `SLEEP` 指令，让硬盘优先停转，但不会把它当作长时间阻塞点。
  4. 程序沿设备父链查找最适合的原生弹出目标节点，对独立 USB-SATA 芯片调用 `CM_Request_Device_Eject`。
  5. 程序将每一步写入 `operations.log`，便于分析“卡在哪一步”。
  6. 如果桥接芯片拒绝彻底移除节点，但盘体已经停转，程序会明确提示该盘已可安全拔出。

- **设计要点**:
  - 不再依赖手动锁卷 / 手动卸载卷作为主流程，降低无读写情况下仍长时间阻塞的风险。
  - 优先使用 Windows 原生设备移除路径，兼容系统对独立 USB-SATA 芯片的处理方式。
  - 停转逻辑仍然保留，但改为“增强项”，避免它拖慢整个弹出流程。

### 4.5 关机保护 (Shutdown Protection)
当前版本的关机保护策略是“关机时自动休眠外置硬盘”，而不是“关机时自动安全弹出硬盘”。

- **当前策略**:
  1. 程序监听 `WM_QUERYENDSESSION`，在系统真正结束会话前执行关机保护。
  2. 关机阶段立即冻结后台 SMART 监控与磁盘重新枚举，避免程序自身再次唤醒硬盘。
  3. 程序只使用当前缓存的外置硬盘列表，不再为了补全列表执行新的在线扫描。
  4. 在发送 `SLEEP` 指令前，程序会先将目标硬盘标记为“休眠保护中”，阻止后台线程再次访问。
  5. 所有外置盘并发执行 `FLUSH CACHE + ATA SLEEP`，并将总等待时间控制在 4 秒内，尽量兼顾数据安全和关机速度。

- **设计要点**:
  - 重点是防止机械盘在断电前仍处于活跃状态。
  - 不把关机流程设计成“逐盘安全弹出”，避免 Windows 关机超时。
  - 通过“关机静默模式”减少程序自身在最后几秒重新触碰硬盘的概率。

---

## 2. 版本历史 (Version History)

### v1.3.60
- **安全弹出流程重构**:
  - 弹出策略改为 Shell Eject 优先（与 Windows 原生安全删除硬件路径一致）+ PnP 移除备选，大幅提高 2074+1153E 组合的弹出成功率。
  - 弹出前先发送 `FLUSH CACHE + SLEEP` 确保磁头归位并停转（使用 `ATA SLEEP (0xE6)`，非 `STANDBY IMMEDIATE`）。
- **系统弹出停转补丁 (SafeRemovalPatcher)**:
  - 新增补丁机制：拦截系统托盘的 `DBT_DEVICEQUERYREMOVE` 消息，在外置硬盘被移除前自动发送停转命令。
  - 弥补 Windows 在 Hub+桥接拓扑下不下达停转命令的缺陷。
  - 可在左侧设置栏通过「系统弹出时附加硬盘停转」复选框开启/关闭，默认开启。
- **托盘双击唤醒**: 系统托盘图标双击即可弹出主界面。
- **关机保护增强**: 修复关机保护后 `closeEvent` 仍然拦截窗口关闭的问题，确保正常关机流程。

### v1.3.59
- **关机保护修复**:
  - 修复了部分环境下“关机前硬盘被程序后台再次唤醒”的问题。
  - 新增关机静默模式：收到关机信号后，后台 SMART 监控与磁盘重新枚举会立即停止。
  - 关机阶段严格只使用当前磁盘缓存，不再回退到在线扫描，避免为了找盘而重新唤醒硬盘。
  - 在发送休眠命令前，先将目标外置盘加入休眠保护名单，降低后台线程再次访问的概率。

### v1.3.58
- **弹出稳定性修复**:
  - 在安全弹出前释放 WMI/COM 查询对象，缓解程序自身持有卷句柄导致的 `error=5` 锁卷失败问题。
  - 适度延长卷预处理超时，提升部分磁盘在短时间内完成锁卷与卸载的成功率。

### v1.3.46
- **安全弹出优化**:
  - 将安全弹出流程调整为更接近 Windows 原生“安全删除硬件”的设备节点移除策略，优先对独立 USB-SATA 芯片执行原生 eject。
  - 移除了手动锁卷 / 手动卸载卷作为主流程步骤，降低无实际读写时仍长时间阻塞的问题。
  - 保留硬盘 `SLEEP` 停转能力，但改为短超时增强步骤，避免停转逻辑拖慢整体弹出。
- **日志增强**:
  - 新增更详细的操作日志记录，安全弹出关键步骤会写入 `operations.log`。
  - 日志打包功能现在会包含操作日志，便于回收现场日志分析超时或 veto 问题。

### v1.3.45
- **修复**:
  - 修复了版本升级后“随系统启动”失效的问题。程序现在会在启动时自动校验注册表启动项，如果发现仍指向旧版本 EXE 或路径不存在，会自动修复为当前版本路径。
  - 优化了“立即休眠硬盘”后的后台监控逻辑。只要用户手动休眠硬盘，程序就不再自动重新枚举该硬盘，直到用户手动唤醒或点击刷新，避免程序自身再次唤醒硬盘。

### v1.3.44
- **修复**:
  - 修复了手动“刷新设备列表”时，程序会错误地扫描并尝试恢复所有曾经连接过但当前已拔出的设备（Phantom 设备），导致长时间卡顿（约 30 秒）并报大量 `CM_PROB_PHANTOM` 错误的问题。

### v1.3.43
- **功能增强**:
  - 提高了程序在系统关机时的优先级，确保在系统强制终止进程前有足够时间完成硬盘弹出。
  - 优化了关机信号处理逻辑，同时支持 `WM_QUERYENDSESSION` 和 `WM_ENDSESSION`，提升关机保护的可靠性。
  - 修复了配置管理器（ConfigManager）的多实例冲突问题，改用单例模式确保所有设置同步更新。
  - 修正了“关机自动弹出”选项在某些情况下无法记忆的问题。

### v1.3.42
- **优化**: 
  - 重构了关机自动弹出逻辑，采用多线程并发执行所有硬盘的安全弹出和马达停转指令。
  - 解决了多硬盘环境下，因顺序执行导致超时被 Windows 强制关机而未能成功停转的问题。
  - 严格限制了关机等待时间（最高 4 秒），确保不影响系统的正常关机速度。

### v1.3.41
- 优化：
  - 将配置文件保存位置迁移至用户目录（%LOCALAPPDATA%/StormForgeDiskManager），确保如“关机时自动弹出硬盘”等选项为持久记忆，重启后保持勾选状态。

### v1.3.40
- **修复**: 
  - 修复了因为程序路径中包含空格导致注册表开机自启动失效的问题，现在路径会自动使用双引号包裹。

### v1.3.39
- **修复与优化**:
  - 彻底移除了程序对内置及外接 NVMe 硬盘的显示和 SMART 检查，避免因驱动不兼容导致的 Error 误报和无效请求。
  - 优化了底层 WMI/PowerShell 兜底机制，增强了对空值数据的容错处理。

### v1.3.37
- **关机保护**: 新增关机时自动弹出所有移动硬盘的功能，防止强制断电导致磁头未归位。
- **自启动优化**: 将开机自启动方式从计划任务迁移至注册表，提升稳定性并方便管理。
- **反馈优化**: 移除对本地邮件客户端的强制调用，改为弹窗引导手动发送错误报告。

### v1.3.36
- **功能修复**: 修复了点击“检查软件更新”按钮后无法自动打开浏览器的问题。
- **兼容性增强**: 引入 `os.startfile` 作为备选方案，提升管理员权限下的跳转成功率。
- **交互优化**: 即使是最新版本，也提供前往项目主页的快捷链接。

### v1.3.35
- **重要修复**: 修复了 SMART 属性 9 (通电时间) 解析异常的问题（曾导致显示 169 亿年使用时间）。
- **解析优化**: 增加了通电时间原始值的合理性校验，自动屏蔽厂商自定义的高位数据。

### v1.3.34
- **布局优化**: 将功能按钮（刷新、更新、日志、自启动）移动至左侧侧边栏底部，解决按钮遮挡问题。
- **界面修正**: 修正了 USB 硬盘接口显示，由 "IDE" 改为 "USB (SATA)"。

### v1.3.33 (及以前)
- 初始版本，支持 ASM2074+ASM1153E 芯片组的硬盘柜管理。
- 支持 SMART 监控、休眠管理和安全弹出。

---

## 6. 开发与编译 (Development & Build)

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
