# 疾风知硬盘柜管理程序 — 弹出/关机休眠机制技术笔记

> 本文档记录 2026-08-22 排障日验证并固化的全部技术细节，目的是防止后续修改代码时
> 把已验证可行的正确方法改坏。修改 SafeRemovalPatcher / ASMCommander /
> 关机保护 / 自启动相关代码前，请先通读本文档。

---

## 1. 核心功能链路（当前已验证可行）

### 1.1 系统托盘弹出 → 硬盘休眠（主链路）

用户在系统托盘点"安全删除硬件" → 弹出 USB 硬盘柜
  → Windows 向持有设备句柄的窗口发 WM_DEVICECHANGE / DBT_DEVICEQUERYREMOVE (0x8001)
       → nativeEvent 收到 → spindown_patcher.handle_wm_devicechange(wParam, lParam)
            → _on_query_remove(lparam)：
                 1. 解析 DEV_BROADCAST_HDR → devicetype
                 2. devicetype == DBT_DEVTYP_HANDLE(6) → 用 dbch_handle 反查磁盘号
                 3. 发送 ATA SLEEP (sleep_only，复用已注册句柄)
                 4. _release_handle_for_disk() 释放句柄通知
                 5. 返回 reply（允许移除）
       → nativeEvent 返回 (True, 1)  ← 关键！
  → Windows 继续 REMOVEPENDING(0x8003) → REMOVECOMPLETE(0x8004)，弹出完成

### 1.2 关键代码位置

| 模块 | 位置 | 职责 |
|------|------|------|
| src/hal/win32_api.py | SafeRemovalPatcher | 拦截 WM_DEVICECHANGE、注册通知、发 SLEEP |
| src/hal/win32_api.py | _register_handle_notifications() | 打开外置盘句柄 + 注册 DEV_BROADCAST_HANDLE |
| src/hal/win32_api.py | _on_query_remove() | QUERYREMOVE 处理：反查磁盘 → SLEEP → 释放句柄 |
| src/hal/win32_api.py | _release_handle_for_disk() | 注销句柄通知并关闭句柄 |
| src/hal/asm_commander.py | sleep_only() / sleep() / spin_down() | 发送 ATA 命令 |
| src/ui/main_window.py | nativeEvent() | 返回 (True, 1) 允许移除 |

---

## 2. 硬件/系统环境（本机验证基准）

- Windows 11 25H2（注意：设备通知行为与旧版有差异）
- 外置硬盘柜：ASMT ASMT105x（USB\VID_174C&PID_55AA），多槽位
- 外置盘：WDC WUH721818ALE6L4（18T）、WUH721816ALE6L4（16T×2）、Teclast SSD 等
- Python 3.11.9（本机 ctypes 是精简版，缺 ctypes.offsetof，见 §5）
- 程序以管理员运行（PyInstaller uac_admin=True）

---

## 3. 设备通知机制（排障核心结论）

### 3.1 三种通知方式对比（实测结论）

| 方式 | 注册结构 | 能否收到 QUERYREMOVE(0x8001) | 备注 |
|------|---------|------------------------------|------|
| 接口通知 | DEV_BROADCAST_DEVICEINTERFACE (GUID_DEVINTERFACE_DISK/VOLUME/USB_DEV) | 收不到（Win11 25H2 只收到 REMOVECOMPLETE 0x8004） | 无句柄占用，但不触发休眠 |
| 卷通知 | DEV_BROADCAST_VOLUME (devicetype=2) | 注册即失败 err=13（Windows 11 25H2 限制） | 不可用 |
| 句柄通知 | DEV_BROADCAST_HANDLE (devicetype=6) | 能收到（需先打开物理盘句柄） | 当前方案 |

结论：必须用 DEV_BROADCAST_HANDLE 句柄通知。打开 \\.\PhysicalDriveN 句柄并注册
RegisterDeviceNotificationW(..., DEVICE_NOTIFY_WINDOW_HANDLE) 后才能收到 QUERYREMOVE。

### 3.2 DEV_BROADCAST_HANDLE 结构（win32_api.py 中定义）

    class DEV_BROADCAST_HANDLE(ctypes.Structure):
        _fields_ = [
            ("dbch_size", wintypes.DWORD),        # 偏移 0
            ("dbch_devicetype", wintypes.DWORD),  # 偏移 4  = DBT_DEVTYP_HANDLE (6)
            ("dbch_reserved", wintypes.DWORD),    # 偏移 8
            ("dbch_handle", wintypes.HANDLE),     # 偏移 12 设备句柄
            ("dbch_hdevnotify", ctypes.c_void_p), # 偏移 20 注册后回填
            ("dbch_eventguid", ctypes.c_ubyte * 16),
            ("dbch_nameoffset", wintypes.LONG),
            ("dbch_data", ctypes.c_ubyte * 1),
        ]

### 3.3 打开句柄的权限（关键坑！）

    h = kernel32.CreateFileW(path, 0x80000000 | 0x40000000, 1|2, None, 3, 0, None)
    #                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    #                         必须 GENERIC_READ|WRITE！

- 错误 desiredAccess=0：句柄能注册通知，但 DeviceIoControl 报错误 5（拒绝访问），SLEEP 发不出
- 错误 GENERIC_READ (0x80000000)：同样 DeviceIoControl 报错误 5（IOCTL_SCSI_PASS_THROUGH 需要 RW）
- 正确 GENERIC_READ|WRITE (0xC0000000)：正常发 ATA 命令（实测 TEST UNIT READY ok）

### 3.4 注册代码注意

- RegisterDeviceNotificationW 必须用位置参数调用（ctypes WinDLL 不支持关键字参数）：

      notify = user32.RegisterDeviceNotificationW(self._hwnd, ctypes.byref(dbh), DEVICE_NOTIFY_WINDOW_HANDLE)

  错误写法 RegisterDeviceNotificationW(hwnd=..., lpFilter=..., flags=...) 会 TypeError。

---

## 4. nativeEvent 返回值语义（最容易改错的点）

    elif msg.message == WM_DEVICECHANGE:
        self.spindown_patcher.handle_wm_devicechange(msg.wParam, msg.lParam)
        return True, 1     # 必须是 (True, 1)！

DBT_DEVICEQUERYREMOVE 的返回值语义与普通消息相反：
- 返回 TRUE (result=1) = 允许移除设备
- 返回 FALSE/0 (result=0) = 否决移除 → Windows 显示"设备正在使用中"，弹出失败！

之前版本返回 (True, 0) 导致系统事件日志 225 记录"疾风知硬盘柜管理.exe stopped the removal"，
用户看到"磁盘正在被占用"。不要改回 (True, 0)。

同时注意：handle_wm_devicechange() 内部返回的 reply 值不影响 Windows 判定，
真正的判定结果是 nativeEvent 的第二个返回值。

---

## 5. ctypes.offsetof 缺失问题（本机 Python 精简版）

本机 Python 3.11.9 的 ctypes 是精简版（__init__.py 约 18KB，标准版约 35KB），
没有 offsetof：

    import ctypes
    hasattr(ctypes, 'offsetof')  # False

因此 _extract_interface_path() 中手动计算偏移（不能用 ctypes.offsetof）：

    # DEV_BROADCAST_DEVICEINTERFACE 的 dbcc_name 偏移：
    #   dbcc_size(4) + dbcc_devicetype(4) + dbcc_reserved(4) + dbcc_classguid(GUID 16) = 28
    offset = 4 + 4 + 4 + ctypes.sizeof(GUID)   # = 28
    return ctypes.wstring_at(base + offset)

修改此函数时不要重新引入 ctypes.offsetof，否则在用户机器上会 AttributeError → 返回 None
→ 无法反查磁盘号 → 休眠永不触发。

---

## 6. SLEEP 命令发送（ASMCommander）

### 6.1 sleep_only()（弹出场景用）

    def sleep_only(self):
        # 只发 ATA SLEEP (0xE6)，不 FLUSH CACHE，失败立即返回，绝不回退！
        sleep_cdb = [0]*16
        sleep_cdb[0] = 0x85          # ATA PASS-THROUGH (16)
        sleep_cdb[1] = (3 << 1)      # PROTOCOL=3 (Non-data)
        sleep_cdb[14] = 0xE6         # Command - SLEEP
        success, _ = self.send_scsi_command(sleep_cdb, None, data_direction=SCSI_IOCTL_DATA_OUT, timeout=3)
        return success   # 失败也直接返回 False，不回退 spin_down()

为什么不能回退：QUERYREMOVE 是同步窗口，多次 I/O 尝试（STANDBY → START STOP UNIT）
会拖慢响应 → Windows 弹出超时被否决。且设备一旦开始移除，后续 I/O 报 1117。

### 6.2 sleep()（关机/系统休眠场景用）

    def sleep(self):
        # 1. FLUSH CACHE (0xE7)
        # 2. SLEEP (0xE6)，失败回退 spin_down()
        # 3. spin_down()：STANDBY IMMEDIATE (0xE0) → 失败再 SCSI START STOP UNIT (0x1B)

### 6.3 SCSI Status 判定（send_scsi_command）

- ScsiStatus == 0 → 成功
- ScsiStatus == 2（CHECK CONDITION）+ sense 00/1D（RECOVERED/NO SENSE）→ SAT 成功
  （ATA 命令经 USB 桥返回寄存器时就是这种"错误"形态，实际命令已执行成功）
- ScsiStatus == 2 + sense 3A/00（MEDIUM NOT PRESENT）→ 盘已休眠/离线，不是失败
  （关机时对已休眠盘发命令会得到这个）
- 其他 → 失败

---

## 7. 弹出时序与句柄释放（正确顺序）

收到 QUERYREMOVE (devicetype=6)
  → 用 dbch_handle 反查磁盘号（self._handle_disk_map）
  → 发送 SLEEP（复用已注册的 RW 句柄）
  → _release_handle_for_disk(disk_index)：注销通知 + CloseHandle
  → 返回 reply

必须 SLEEP 之后再释放句柄。如果先释放句柄再 SLEEP，重新打开设备会报
1117 (ERROR_IO_DEVICE) 失败（设备已进入移除流程）。

必须释放句柄。如果不释放，Windows 在 QUERYREMOVE 阶段检查到句柄仍打开，
判定设备被占用 → 弹出被否决（事件日志 225）。

---

## 8. 关机/系统休眠保护（2026-09-19 重写：对齐 Windows 内置盘做法）

> 旧实现（下方“历史实现”段落）用 ATA SLEEP + 阻塞 20 秒，是**错误**的，已删除。
> 新实现在 `src/core/shutdown_guard.py`。

### 8.1 旧策略为什么错

旧流程：WM_QUERYENDSESSION → 并发发 `DeviceManager.spin_down_disk`（FLUSH CACHE + **ATA SLEEP 0xE6**）
→ 阻塞等待最多 20 秒 → 标记休眠。

1. **SLEEP 是深睡**，必须 COMRESET/重新上电才能退出。QES 之后系统还要停止服务、
   flush 文件系统、卸载卷、断电；其中任何一次 I/O 落到已 SLEEP 的盘上都会挂起十几秒，
   控制器超时后复位硬盘 → 盘被重新转起来 → 断电瞬间盘仍在旋转 →
   磁头紧急回收 → **SMART C0（断电磁头缩回计数 / 不安全关机数）+1**。
2. **20 秒阻塞**超过 Windows 给应用的关机等待预算，可能被强杀在命令中途。
3. **自家补丁又把盘唤醒**：SafeRemovalPatch 在关机期间收到 DEVNODES_CHANGED 会对所有
   外置盘做 TUR 探测（探测即起转），甚至补发 SLEEP，把刚停转的盘再折腾一遍。

### 8.2 Windows 是怎么做的（内置硬盘）

电源计划“在此时间后关闭硬盘”以及存储设备 D3 断电，用的是 **ATA STANDBY IMMEDIATE (0xE0)**：
卸载磁头 + 停转马达，但**可恢复**（下一条命令自动起转，不需复位）；硬盘收到该命令时会先落盘。
Windows **从不对内置盘发 SLEEP**，断电时盘已停转、磁头已卸载 → 不会紧急回收 → C0 不增长。

### 8.3 新策略（ShutdownGuard）

触发点（`main_window.nativeEvent`）：

| 消息 | 动作 |
|---|---|
| WM_QUERYENDSESSION | `prepare_disks_for_shutdown("关机", 预算 4s)` |
| WM_ENDSESSION wParam=1 | `prepare_disks_for_shutdown("关机收尾", 预算 2.5s)` —— 断电前最后一次 |
| WM_ENDSESSION wParam=0 | 关机被取消 → `thaw_background_access()` 恢复后台 |
| PBT_APMSUSPEND | `prepare_disks_for_shutdown("系统休眠")` |
| PBT_APMRESUMESUSPEND / AUTOMATIC | `thaw_background_access()` |

每块外置盘（`is_removable` 或 PnP 含 USB/UASP；内置 SATA/NVMe 不碰，作为对照组）：

1. `DeviceManager.flush_volumes_for_disk()` —— FlushFileBuffers 刷卷（不锁卷，避免干扰关机流程）
2. `DeviceManager.prepare_disk_for_shutdown()` —— **ATA FLUSH CACHE(0xE7) → ATA STANDBY IMMEDIATE(0xE0)**
   （`ASMCommander.flush_cache()` / `standby_immediate()`，回退 SCSI START STOP UNIT）

并发执行、总耗时不超过预算；命令返回即代表磁头已卸载（**不需要**等马达物理停转）。

关机前必须冻结后台（`freeze_background_access()`）：

- `monitor_service.shutdown_mode = True`（监控主循环改为 1 秒轮询暂停，**不再 break 退出线程**）
- `SafeRemovalPatcher.shutdown_in_progress = True`（`handle_wm_devicechange` 直接返回，
  不再 TUR 探测 / 补发 SLEEP）

**绝不在关机路径使用 ATA SLEEP。**

### 8.35 实测结论（2026-09-19，tools/smart_counter_probe.py）

用真实硬件跑过对照实验（disk 5，WUH721816ALE6L4）：

| 操作 | C0 (0xC0) | C1 (0xC1) |
|---|---|---|
| 读取基线 | 1388 | 1388 |
| FLUSH CACHE + **STANDBY IMMEDIATE** | 1388 | 1388 |
| 15 秒后再读取（把盘重新起转） | 1388 | 1388 |

结论：

1. **ASMT 105x/1153E 桥会正常传递 STANDBY IMMEDIATE** ——
   `SafeRemovalPatcher` 老注释里"桥会过滤 STANDBY，只能用 SLEEP"的说法是**错的**，
   不要据此回退到 SLEEP。（当年误判很可能是因为当时自家监控每 10 秒一次
   SMART/TUR 探测不停地吵醒硬盘，看起来像"没停转"。）
2. **命令式停转、正常起转都不计入 C0/C1** → 这两个计数器只统计
   "断电时磁头仍在盘上"造成的紧急回收。策略目标因此非常明确：
   **断电瞬间盘必须是停的**。
3. **TUR (TEST UNIT READY) 不能用来判断盘是否已停转**：桥会代答 TUR，
   实测停转 1 秒后 TUR 仍返回成功。旧版据此打印的"桥可能未传递停转命令"是误报，已删除。

### 8.36 最终架构（2026-09-20，七轮实测 + 社区调研后确定）

七轮实测的演化（每一步都被真实关机验证过）：

| 版本 | 做法 | 结果 |
|---|---|---|
| v1 | QES 里同步停转 + 每盘 1s TUR 验证（耗时 4.7s） | 外置 +3、内置 +1；用户听到"停了又转" |
| v2/v3 | QES 立刻返回，停转交给守护循环（等卷卸载） | **完全没停转**：进程在 QES 后 38ms 就被结束 |
| v4 | 同上（卷映射改 Win32，0ms） | 循环启动了，但进程仍只活了 38ms |
| v5 | QES 内同步停转，无探测 | 停了又转；+2；卸载卷失败（应用还活着，error=5） |
| v6 | QES 同步"锁卷→卸载卷→停转"，保持锁句柄 | 锁失败（error=5：应用还占着卷）→ 停在转的盘被 flush 唤醒 → +2 |
| v7 | 关机停转服务 PRESHUTDOWN：锁卷→卸载→STANDBY | 锁卷 error=5 依旧（PRESHUTDOWN 时仍有占用），卸载做不到 |
| v8/v9 | 服务里 Windows 离线(黑名单)+STANDBY | 16 字节结构 → error=24 离线失败；离线前置失败后整条流程作废，盘没停 |
| v10 | **纯深睡（ATA SLEEP 0xE6）+移入黑名单，不锁卷不卸载**（用户方案："无法卸载≠无法休眠"） | **生效**：盘深睡保持到断电，三块盘 0xC0 全部 Δ+0；但内核关机 flush 卷撞深睡盘 → UASPStor 复位卡 58 秒 |
| v11 | 离线(40字节结构修复)→等卷卸载→深睡 | 回归 v9 的问题：卸载被当成深睡前提（用户否决） |
| **v12（当前，定稿）** | **刷卷缓存 → 尽力离线(纯加速,不等不阻塞) → 无条件深睡(重试3次) → 移入黑名单** | **第 11 轮实测通过：4 块外置盘 3.1s 完成离线+深睡+黑名单，关机不再卡顿；开机核对三块盘 0xC0 增量与内置对照盘完全一致（+1=+1），判据达标，用户确认成功** |

**关键实测与调研结论：**

1. WM_QUERYENDSESSION 之后约 38ms 进程就被结束（日志实证）。
2. QES 阶段应用还活着，外置盘的卷被资源管理器等占用（FSCTL_LOCK_VOLUME
   实测 error=5），**在 GUI 进程里永远无法完成"先卸载卷再停转"**。
3. Windows 关机流程（[gfody/OnShutdown](https://github.com/gfody/OnShutdown)）：
   `WM_QUERYENDSESSION → 应用退出 → 服务 PRESHUTDOWN（可推迟关机约 125s）
   → 其他服务停止 → 文件系统 flush → 设备断电`。
   **PRESHUTDOWN 是唯一"应用已退出、卷可卸载、断电之前"的钩子**，
   其余方案（GPO 脚本 / WMI 事件 / 任务计划事件）都会被提前终止。
4. 社区标准顺序（[Debian 用户列表](https://lists.debian.org/debian-user/2026/05/msg00005.html)）：
   `sync → umount → 停转 → 断电`；[微软问答](https://learn.microsoft.com/en-us/answers/questions/2622701/external-hard-drive-safe-shutdown)
   确认 Windows 从不会替 USB 桥接硬盘做关机停转（磁头撞击声=紧急回收）。

**v7 架构：**

- **GUI**：启动时确保服务已安装（sc create，start=auto，LocalSystem）并运行；
  QES 阶段只冻结自身后台（不再停转）；每 60 秒把物理盘清单+休眠名单写入
  `shutdown_disks.json`（服务在 SYSTEM 会话里 WMI 枚举不可靠，实测返回 0 设备）。
- **服务**（同 exe 的 `--shutdown-service` 模式，`src/core/shutdown_service.py`）：
  接收 SERVICE_CONTROL_PRESHUTDOWN → 锁卷（应用已退出，应成功）→ 卸载卷 →
  FLUSH CACHE → STANDBY IMMEDIATE → 报告 STOPPED。
  之后文件系统 flush 会跳过已卸载的卷 → 盘不会被再转起来 → 断电时盘是停的。
- 本环境 pywin32 注意：win32service 不导出 StartServiceCtrlDispatcher，
  必须用 `win32serviceutil.ServiceFramework` + `servicemanager.PrepareToHostSingle(cls)`
  + `servicemanager.StartServiceCtrlDispatcher()`（无参）；
  PRESHUTDOWN 通过重写 `GetAcceptedControls()`（+0x100）和 `SvcOther(15)` 接收；
  `servicemanager.Initialize()` 是设事件源用的，不是注册服务类。
- 服务停转同样**每块盘只停一次**；已休眠的盘跳过（0 成本）。

### 8.38 技术原则（定稿）—— 关机深度休眠策略

> 本条为**固定技术原则**，第 11 轮实测通过后定稿。后续任何改动不得违反：
> 未经用户确认，不得重新引入"卸载/锁卷/离线成功才休眠"的前置依赖。

**一、深睡就是黑名单（用户方案，v9/v10 定稿）**

1. 关机时给每块外置盘发送**深度休眠**：ATA FLUSH CACHE(0xE7) + **ATA SLEEP(0xE6)**。
   盘进入最低功耗，**不响应任何程序**；ASMT 桥接芯片对后续访问立刻回
   sense 3A/00（MEDIUM NOT PRESENT），不会真把盘唤醒。
2. **无条件执行**——不管卷能不能卸载、盘是否被程序占用，都先深睡：
   "外置硬盘被程序占用无法卸载，但**无法被卸载不代表无法被休眠**"。
   深睡成功后把盘移入本程序黑名单（sleeping_disks），本程序与设备补丁
   从此不再访问它。
3. 用户要再次访问硬盘，必须在程序里点击「唤醒并解除黑名单」把盘从
   黑名单移除（GUI 上会明确提醒）。
4. 深睡保持到断电；重新上电后盘自动苏醒。开机时服务兜底把所有外置盘
   恢复上线（Persist=False，离线状态不会持久化）。

**二、离线只是可选加速（消除关机卡顿），绝不成为深睡前提**

- 第 10 轮实测：深睡后卷仍挂载，内核关机阶段的卷 flush 会向深睡盘发命令
  超时 → UASPStor 复位设备，关机卡 58 秒。
- 因此 PRESHUTDOWN 里先**尽力**把盘设为 Windows 离线（
  IOCTL_DISK_SET_DISK_ATTRIBUTES，40 字节结构；盘还在转时卷同步卸载）。
- 离线失败/卷被占用：直接跳过，**不等待、不阻塞**，照常深睡
  （该分支等价于第 10 轮已确认生效的纯深睡方案）。
- 若离线成功却导致深睡失败（ATA 透传受离线影响）：恢复在线再重试深睡，
  **深睡优先于离线**。

**三、执行阶段与载体**

- 唯一可靠的钩子是服务的 `SERVICE_CONTROL_PRESHUTDOWN`
  （QES 后约 38ms 应用进程就被结束；GPO/WMI/任务计划触发器更早失效）。
- 服务 `JiFengZhiShutdownSvc`：LocalSystem、自动启动，与 GUI 同 exe
  （`--shutdown-service` 模式）；GUI 每 60 秒把物理盘清单+休眠名单写入
  `shutdown_disks.json` 供服务读取（SYSTEM 会话 WMI 枚举不可靠）。
- 内部直连盘（SATA/NVMe）**绝不干预**，由 Windows 自己管理，作为对照组。

**四、验证判据**

- SMART **0xC0 不安全关机数**（不是磁头归位/加载数，概念不得混淆）：
  每次开机对比"上次关机前基线"，**外置盘增量 ≤ 且等于内置对照盘增量即达标**
  （内置盘由 Windows 管理，代表"正常关机"的基准）。
- 深睡/STANDBY 命令本身会让 0xC0 延迟 1-2 分钟 +1（命令式磁头卸载入账），
  这是任何命令式停转的固有成本，与内置对照盘持平即为正常。
- 实测历程：第 10 轮纯深睡 Δ+0；第 11 轮（v12）三块盘全部 Δ+1 且
  与内置对照盘完全一致（+1=+1）→ **判据达标**，关机卡顿消失，用户确认成功。

### 8.39 日志反馈云端方案（技术原则）

> 客户端一键上传 → 云主机固定目录 → 网页/密钥随时调取，
> 按「版本 + 时间」判断报告有效性。**云主机 IP、凭据、访问密钥绝不进入公开仓库**
> —— 客户端通过程序目录 `report_server.json` 读取（随发行包分发，不入源码），
> 服务端源码与部署文档保存在本地私有资料（`venv/私有资料/`）。

- **客户端（一键上传）**：错误报告对话框「一键上传服务器」→ 打包
  `app.log / operations.log / smart_history.json / config.json / 系统信息` →
  POST 原始字节流；请求头 `X-Upload-Token`（密钥）、`X-Version`（版本）、
  `X-Filename`、`X-Description`。未配置 `report_server.json` 时自动降级为
  「仅导出到本地」。
- **服务端（固定存放）**：纯 Python3 标准库 `http.server` 接收程序，systemd
  托管、**只监听 127.0.0.1**，由 nginx 在 80 端口反代 `/logs/` 路径（无需改
  安全组）；报告落盘 `…/reports/{版本}/{时间_文件名}`，索引 `index.json`。
- **调取**：网页浏览（`<browse_url>?key=<密钥>`，版本过滤 + 时间倒序）；
  直接下载（`<download_url>?file=<文件名>&key=<密钥>`，供本地调试）。
- **有效性（版本 + 时间）**：上传 7 天内且为最新版本 → 有效；
  7 天内但低于最新版本 → 旧版本；超过 7 天 → 过期。
- **访问控制**：上传/浏览/下载共用同一密钥（请求头 / `?key=` 参数），
  错误密钥一律 403；单包上限 50MB、索引最多 500 条。

### 8.40 弹出场景铁律（v1.3.72 验证过的弹出停转方案）

> 弹出停转方案以 **v1.3.72 验证过的行为为准**（用户明确指定），不得再改动其核心逻辑。

1. **GUI「安全弹出设备」按钮（v1.3.72 原样）**：
   ① 弹出前先发 **FLUSH CACHE + SLEEP(0xE6) 深睡**（`cmd.sleep()`），磁头归位+停转；
   ② Shell Eject（Windows 原生路径）；
   ③ 锁定/卸载卷（ReFS 不支持锁定时直接 DISMOUNT；锁失败不中断流程）；
   ④ PnP 设备节点移除。成功提示含「硬盘已停转」；
   移除失败但已停转时明确提示「硬盘已成功停转，可稍后通过系统托盘完成弹出」。
2. **补丁 QUERYREMOVE（v1.3.72 原样）**：同步发送 `sleep_only()`（SLEEP 0xE6）
   + 标记休眠（sleeping_disks）+ 释放句柄；先 SLEEP、后释放，顺序不可颠倒。
3. **补丁 REMOVECOMPLETE 补发 SLEEP（v1.3.72 原样 + 保护项）**：对剩余已注册
   外置盘补发 SLEEP；仅增加「有挂载卷的盘跳过」与「内部盘不注册句柄通知」
   两个防误伤保护（不改变被弹出盘的停转行为）。
4. 收到 `DEVICEQUERYREMOVEFAILED(0x8002)` 时恢复监控状态并清空去重标记
   （新增保护项，保证可以再次弹出）。
5. 关机深度休眠策略（§8.38）与「深度休眠」按钮不受本条影响。

### 8.41 弹出场景铁律（v1.3.88 验证过的方案 —— GUI「停转并安全弹出」当前生效）

> 2026-09-25 实机双盘验收通过（记录见本节末）。**本节描述当前代码的真实行为**，
> 与 §8.40 不同：GUI 按钮走 `src/core/eject_protocol.py` 事务
> （预检 → 隔离卷 → 离线 → FLUSH → SLEEP → PnP 移除），**不调用 Shell Eject，
> 也不是"先深睡再锁卷"**。§8.40 的托盘接管补丁当前没有调用点（`_register_spindown_patcher`
> 无调用、`spindown_patcher` 从未赋值、日志里没有 `[NativeEject] registered`），
> 属死代码，**不要据 §8.40 推断现行行为**。

**事务顺序（不可颠倒）**

1. `inventory()` 预检：盘符/物理盘号/桥序列号/USB 总线 + 卷清单（卷枚举容错，见第 4 条）。
2. `validate()`：仅允许"在线、非启动/系统盘、有介质（Size≠0）"的白名单 USB 盘；
   核对句柄序列号与 PnP；确认弹出桥只对应目标盘。
3. 开物理盘句柄 → 逐个开卷句柄 → **隔离卷**：
   - 普通卷：`FSCTL_LOCK_VOLUME` → `FlushFileBuffers` → `FSCTL_DISMOUNT_VOLUME`；
   - **ReFS/exFAT 等不支持锁卷的卷**：LOCK 返回 5 → **跳过锁定，直接 `FSCTL_DISMOUNT_VOLUME`**
     （Windows 资源管理器弹出同款），并**跳过后续 flush/dismount**（已卸载，重复调用会失败并中止事务）；
   - **无分区表（RAW）的盘**：没有卷，直接进入下一步（整盘弹出）。
4. 置 OFFLINE（Persist=0）并读回属性确认（`offline_verified`）。
5. 用预开句柄发 ATA FLUSH CACHE (E7)，SCSI status 必须为 0。
6. 关全部卷句柄 → **先落日志 `sleep_dispatch`** → 只发一次 ATA SLEEP (E6)。
7. 关物理盘句柄 → `CM_Request_Device_EjectW` 请求一次（禁止回退 Hub/子树）。
8. 结果以 PnP 返回值 + 事件序列判定；E6 之后**不再** TUR/SMART/读盘验证。

**为什么 ReFS 必须跳过锁定（实测，禁止回退）**

- 本机 E:（ReFS）对 `FSCTL_LOCK_VOLUME` **恒返回 ERROR_ACCESS_DENIED(5)**；
- GUI 完全退出、只剩关机服务时**同样返回 5**；Restart Manager 查不到占用者；
  句柄审计确认本程序自身持有 0 个句柄 ⇒ **不是占用问题**，是文件系统语义；
- 旧实现 `win32_api.py::prepare_volume_for_safe_removal` 早已记录同一结论
  （"实测 ReFS 卷锁定恒返回 5，而 DISMOUNT 可成功"）。

⇒ **任何"把 LOCK error 5 当致命错误直接中止"的写法都是 bug**
（v1.3.78～v1.3.87 就是这么让 E: 永远弹不出去的）。
fail-closed 的边界只有一个：**DISMOUNT 也失败**时立即中止，不发 SLEEP、不离线，
并把占用诊断写进提示。

**无分区表（RAW）盘必须能弹**

`Get-Partition` 对 RAW 盘会报错；预检若不做容错，整块盘永远弹不出去
（本机磁盘 3 = `A000BBBBAAAA` 16 TB）。判定依据是 `Get-Disk` 的 `PartitionStyle=RAW`
且卷数为 0 ⇒ "没有卷需要隔离"，整盘停转 + 弹出，并记 `volume_less_disk` 事件。
**有分区表却枚举不到卷仍然 fail closed**，并把卷枚举错误原样带进提示。

**提示必须可读（禁止只回一个 error=5）**

失败文案必须含：卷标识 + 文件系统 + 错误码中文解释 + 占用者（Restart Manager）
+ **本程序自身持有该盘句柄数量** + 处理建议。实现：`src/utils/volume_diag.py`。

**开关**：`app_config.json` → `eject_dismount_without_lock`（默认 `true`）。
置 `false` 会退回"锁卷失败即停止"（ReFS 盘将再次弹不出去），仅排查时使用。

#### 实测验收记录（2026-09-25 23:06，v1.3.88，本机 ASMT105x 双盘）

| 盘 | 形态 | 关键事件序列 | 结果 |
| --- | --- | --- | --- |
| 磁盘 4 / E: | GPT + ReFS，1 卷 | LOCK error=5 → `volume_dismounted_without_lock` → 跳过 flush/dismount → `offline_verified` → E7(0.0s) → E6(1.98s, scsi_status=0) → PnP `cr=0 veto=0` | `ejected_sleep_accepted`，约 4.6s |
| 磁盘 3 | RAW，0 分区 | `volume_less_disk` → `offline_verified` → E7(1.47s) → E6(0.86s) → PnP `cr=0` | `ejected_sleep_accepted`，约 4.4s |

日志位置：`operations.log` 的 `[EjectTransaction] disk=4/3 event=...` 序列。
`physical_stop_verified` 恒为 false 是设计（不做事后探测，避免把刚停转的盘唤醒）。

### 8.37 计数成本实测（2026-09-19）


结论：

1. 任何"等某个时机再执行"的设计（守护循环 / 等卷卸载 / 定时器 / 后台线程）
   **都不可能执行** —— 进程活不到那一刻。用户"完全听不到硬盘停转"就是这么来的。
2. 停转必须在 **QES 处理函数内同步完成**。命令很快（每盘 FLUSH CACHE +
   STANDBY IMMEDIATE，共约 1-2 秒），且由 USB-SATA 桥接芯片转交硬盘，
   本进程随后消失不影响停转。
3. **停转路径里不能再做任何探测/验证**：v1 多出来的成本正是 TUR 验证造成的
   （桥会代答 TUR，实测停转 1 秒后 TUR 仍返回成功），
   探测会把刚停转的盘重新唤醒，随后又要再停一次。
4. 严格保证**每块盘只停一次**；已休眠（sleeping_disks）的盘直接跳过，0 成本。

### 8.37 计数成本实测（2026-09-19）


对同一块盘连续做两次"读取基线 → FLUSH CACHE + STANDBY IMMEDIATE → 多次采样"：

| 采样点 | C0 | C1 |
|---|---|---|
| before | 1388 | 1388 |
| park（发出命令） | 1388 | 1388 |
| +5s / +20s / +60s | 1388 | 1388 |
| **+120s** | **1389** | **1389** |
| +180s | 1389 | 1389 |

第二次实验同样：1389 → 1390（+1 出现在 60~120 秒之间）。

**结论（决定性）**：这些 WD/HGST 盘的 0xC0/0xC1 把**每一次磁头卸载**都计入，
包括命令式 STANDBY IMMEDIATE（延迟 1-2 分钟入账），断电时的紧急回收同样 +1。
即"停转次数 = 成本"，与是否断电无关。

所以 v3 的设计原则是**把停转次数压到最低**：

1. 关机时已经停着的盘（sleeping_disks 名单）→ 完全跳过，**0 成本**；
2. 还在转的盘 → **只停一次**，且要停在最接近断电的时刻
   （卷卸载完成之后，因为那时文件系统不可能再写）；
3. 绝不在 QES/ENDSESSION 提前停转 —— 那会被后续 flush 重新转起来，
   Windows 设备断电时还会再停一次，实测变成 **+3**；
4. 兜底：卷 45 秒仍未卸载 → 停一次（避免盘一直转到断电）。

代价对比（同一台机器、同一次关机）：

| 策略 | 外置盘 C0 增量 | 内置对照盘 |
|---|---|---|
| 旧策略（SLEEP + 阻塞 20s） | 未执行（盘卡在休眠名单里被跳过） | +0 |
| v1（QES 同步停转 + TUR 验证） | **+3** | +1 |
| v3（卷卸载后只停一次） | 待实测 | 待实测 |

### 8.4 不安全关机计数核对（重启验证）

- 每块盘第一次读到 SMART 时，把 **C0(0xC0) / C1(0xC1)** 与上次保存的基线对比：
  增量 0 = 上次关机是安全关机；随后持续刷新基线（关机时不读盘，避免唤醒）。
- 基线持久化在 `app_config.json` 的 `shutdown_counters`；
  报告写到 `%LOCALAPPDATA%\StormForgeDiskManager\shutdown_verification.json`。
- 内置直连盘是**对照组**：若内置盘增量为 0 而外置盘 >0，说明问题只在外置盘关机处理。

### 8.5 开机清空过期休眠标记

开机后首次启动（`GetTickCount64 < 180s`）时清空持久化的 sleeping_disks：
硬盘柜随主机断电重新上电，盘必然是转的，旧标记一定是错的。
（否则会出现“开机后全部显示休眠、拿不到 SMART 基线、用户要手动唤醒”）

### 8.6 历史实现（已废弃，仅作参考）

- WM_QUERYENDSESSION → `eject_all_removable_disks()` → 并发 SLEEP + 20 秒等待
- 磁盘列表来源：`monitor_service.cached_disks`（不要用 `self.disk_data`，silent 模式下会过期漏盘）
- 跳过 `sleeping_disks | ejected_disks` 中的盘

---

## 9. 驱动级常驻（2026-08-22 新增）

- 开机自启：MainWindow.__init__ 中调用 config_manager.set_autostart(True)
  （写注册表 HKCU\Software\Microsoft\Windows\CurrentVersion\Run，命令带 --silent）
- 配置固化：set_shutdown_eject(True) + set_safe_remove_spindown(True)
- UI 复选框：三个（随系统启动/关机休眠/弹出停转）均 setChecked(True) + setEnabled(False)，
  显示"驱动级，固定开启"
- 自启动路径自修复：is_autostart_enabled() 检测路径过期会自动重写注册表

---

## 10. 环境排障（已解决的历史问题）

### 10.1 AlibabaProtect 阻止弹出（已彻底移除）

现象：弹出失败，事件日志 225 记录 AlibabaProtect.exe 阻止。
自我保护：sc stop 返回 1052、taskkill 拒绝访问、写注册表 WinError 5（AliPaladin 驱动保护）。
服务复活机制：FailureActions 配置 3x SC_ACTION_RESTART + Start=2 AUTO_START。

正确移除顺序（已验证）：
    1. fltmc unload AliPaladin          # 卸载文件系统过滤驱动（关键！否则后续操作被保护）
    2. taskkill /F /T /IM AlibabaProtect.exe
    3. reg delete HKLM\SYSTEM\CurrentControlSet\Services\AlibabaProtect /f
       reg delete HKLM\SYSTEM\CurrentControlSet\Services\AliPaladin /f
    4. ren C:\Windows\System32\drivers\AliPaladinEx64.sys AliPaladinEx64.sys.disabled
    5. ren "C:\Program Files (x86)\AlibabaProtect" AlibabaProtect.disabled

仅改 Start=4 或清 FailureActions 不够（1 分钟内被驱动恢复）。

### 10.2 Windows Search (WSearch) 占用外置盘

现象：SearchIndexer.exe 打开外置盘句柄，弹出报"设备正在使用中"。
解决：sc stop WSearch + sc config WSearch start= disabled。

### 10.3 CM_PROB_HELD_FOR_EJECT (代码 47)

现象：反复弹出失败后，USB 设备卡在"待移除"状态，之后任何弹出都报"设备正在使用中"。
解决：pnputil /restart-device <instance-id>（转为代码 14 NEED_RESTART）→ 重启系统。

### 10.4 系统其他干扰

- SearchHost、HipsDaemon（火绒）等进程可能影响弹出，排障时注意区分
- 事件日志 225 是权威依据：记录"哪个进程阻止了移除"

---

## 11. 测试验证方法（如何自测而不改坏）

| 场景 | 方法 |
|------|------|
| 验证句柄通知注册 | 启动程序看 app.log："已注册句柄通知: PhysicalDriveN (h=0x.., notify=0x..)" |
| 验证 QUERYREMOVE 收到 | 实际弹出，看 "QUERYREMOVE devicetype=6" + "检测到外置硬盘即将被移除" |
| 打桩测试链路 | monkeypatch ASMCommander 为 Stub（不真实发 SLEEP），SendMessage 模拟 QUERYREMOVE |
| 跨进程 SendMessage | 不可行（err=87 ERROR_INVALID_PARAMETER，Windows 封送限制），必须真实弹出测试 |
| 模拟设备事件 | pnputil /disable-device + /enable-device（产生 0x8004/0x8000）；/restart-device 不产生事件 |
| 验证产物一致性 | fc /N src\hal\win32_api.py dist\...\_internal\src\hal\win32_api.py |

---

## 12. 打包注意事项

- build.spec：COLLECT name='Stormforge_DiskManager_v1.3.88'，uac_admin=True
- 打包命令：python -m PyInstaller build.spec --noconfirm --distpath dist --workpath build
- 打包前必须结束运行中的 GUI 实例（否则 exe/app.log 被锁）；**关机服务占用旧版本目录
  不影响构建新版本**（新版本首次启动时会自动把关机停转服务的 binPath 重指到新 exe）
- 版本号变更要同时改 build.spec 的两处 name（EXE 与 COLLECT）
- 打包后核对：`pyi-archive_viewer -l -r -b <exe>` 应能看到 `src.utils.volume_diag`、
  `src.hal.eject_backend`、`src.core.eject_protocol`；`_internal\src` 与源码哈希一致

---

## 13. 修改红线（防止改坏已验证功能）

1. 不要用 ctypes.offsetof（本机精简版没有）
2. 不要改 nativeEvent 返回值为 (True, 0)（QUERYREMOVE 需要 1 才允许移除）
3. 不要改句柄权限为 0 或仅 GENERIC_READ（DeviceIoControl 报 5 拒绝访问）
4. 不要在 sleep_only() 里加回退（弹出会超时被否决）
5. 不要先释放句柄再发 SLEEP（报 1117）
6. 不要用 self.disk_data 构建关机休眠列表（silent 下过期漏盘）
7. 不要用关键字参数调 ctypes WinDLL 函数
8. QUERYREMOVE 必须：反查磁盘 → SLEEP → 释放句柄
9. 关机休眠：cached_disks + 无条件执行 + 跳过已休眠盘
10. **不要把 `FSCTL_LOCK_VOLUME` 的 error 5 当致命错误**（ReFS/exFAT 恒返回 5，
    与占用无关；见 §8.41 实测）。v1.3.78～v1.3.87 回退过这一点，结果两个盘都弹不出去。
11. **不要删掉"锁卷失败 → 直接 DISMOUNT"的回退**；可中止的边界只有"DISMOUNT 也失败"。
12. **不要给 `eject_backend.inventory()` 的 `Get-Partition` 去掉 try/catch**
    （RAW 盘会再次整块弹不出去）。`volume_query_error` 必须随快照返回。
13. **不要对已直卸（`volume_dismounted_without_lock`）的句柄再 flush/dismount**
    （卷已卸载，调用失败会中止整个事务；见 `volume_flush_skipped`/`volume_dismount_skipped`）。
14. **弹出失败提示不得只回 error 码**，必须带占用诊断（卷/文件系统/占用者/自身句柄/建议），
    实现见 `src/utils/volume_diag.py`；`eject_dismount_without_lock` 默认必须为 true。
15. **不要用全表跨进程句柄扫描做默认诊断**：`DeviceIoControl` 撞到不响应设备会挂住
    （实测 5 分钟不出结果）。默认只做"自身句柄 + Restart Manager"，跨进程扫描放在
    `include_others=True` / `--scan-others` 手动入口。
16. **不要删掉 `WM_DEVICECHANGE` → 去抖重扫**（`MainWindow._schedule_device_rescan` /
    `MonitorService.rescan_devices`）：热插拔可见性完全依赖它。
17. **不要把 `device_change_needs_rescan` 的触发集合扩大到 QUERYREMOVE(0x8001) 系列**：
    那些分支的**返回值语义**不允许改动（QUERYREMOVE 必须返回 1 才允许移除），重扫只能
    作为副作用挂在 0x8000/0x8004/0x0007 上。
18. **新出现的盘必须走"完整列表重建"**（`rescan_devices` → `_placeholder_rows`）：
    定时路径对非白名单盘 `continue`、`_merge_ui_data` 只从 updates 追加新条目，
    用它显示新盘会永远显示不出来（v1.3.87~v1.3.88 的实际表现）。
    重建列表时**不要**顺便跑 SMART（会把空闲停转的盘反复唤醒）：已知盘沿用旧行、
    新盘给占位行，检测交给按盘调度。
20. **列表只能增量补行，绝不能整表替换**：UI 行的 `serial` 可能是 SATA IDENTIFY 的真实
    序列号，设备缓存里是桥接序列号 —— 用 `(index, serial)` 精确匹配再整表重建，
    会把已管理盘的 SMART 数据冲成"待检测"（v1.3.89 实测发生的回归，
    见 `test_smart_row_survives_when_ui_serial_is_ata_serial`）。
21. **桥接柜换盘必须靠"盘位指纹"识别**：序列号是按盘位分配的（换盘不变），
    只有 `(pnp_id, serial, model)` 的型号变化能看出来。指纹变化时除了丢旧行，
    还必须 `disk_last_check_times.pop(桥接序列号)`，否则新盘会沿用上一块盘的到期时间。
22. **解除"已弹出/隔离"屏蔽要在过滤之前**完成，否则刚重新插入的盘这一轮不会出现在列表里。
23. **`ejected_disks` 按序列号字符串记，必须能自愈**：`mark_disk_ejected` 会把**真实 ATA
    序列号**也写进黑名单。用户把盘从 A 盘位拔下插到 B 盘位时，B 盘位的桥接序列号与黑名单
    不同（所以一开始能显示），但读出来的**真实序列号相同** → 列表里这一行的 serial 命中
    黑名单 → `_merge_ui_data` 下一轮又把它当"已弹出的盘"丢掉 —— 表现为
    "刚插入能看到、一两秒后又消失"（v1.3.90 实机复现）。
    修法：读到真实序列号时调用 `_heal_serial_exclusion()` 解除屏蔽（能读出来 = 盘在位；
    真正被弹出的盘不在总线上，读不到，也就不会被误解除）。
    同盘位重新插入由 `_heal_reappeared_devices()` 按桥接序列号解除。
24. **`_merge_ui_data` 里"丢弃已弹出记录"的分支必须留日志**：它曾经静默丢行，
    只能靠"合并后 N 条"的条数变化才发现问题。
19. **`device_inventory_interval_seconds = 0` 的语义是"只靠设备事件"**，
    不要在 `_get_cached_or_scan_disks_locked` 里写成 `now - last >= 0`（恒真 = 每次都重新枚举）。

### 17. 设备热插拔即时发现（2026-09-26，v1.3.89 引入 / v1.3.90 修正）

### 问题（用户现象：程序启动后再插入硬盘，软件不显示）

两个独立缺陷叠加：

1. **发现慢**：物理盘清单只在 `now - last_inventory_scan_time >= device_inventory_interval`
   时重新枚举（原值 300 秒），而 `WM_DEVICECHANGE` 分支只是 `return super().nativeEvent(...)`，
   不使缓存失效 → 插入后最长 5 分钟才"看见"。
2. **看见了也不显示（主因）**：侧栏完整列表只在 `target_disks is None` 的全量路径生成
   （含"未加入白名单（不管理）"行）；每 10 秒的定时路径
   （`check_disks_schedule → check_specific_disks → check_all_smart(target_disks=...)`）
   对非白名单盘直接 `continue`，随后 `_merge_ui_data` 只从 `updates` 追加新条目
   → **新插入、尚未勾选"纳入管理"的盘永远进不了侧栏**，只有重启或点"刷新硬盘状态"才出现。

实测对照（同代码、无硬件）：定时路径推送 0 条且不含新盘；全量路径推送 1 条、状态
"未加入白名单（不管理）"。

### 方案（已实现）

1. `MainWindow.nativeEvent` 的 `WM_DEVICECHANGE` 分支：`wParam ∈ {0x8000 DBT_DEVICEARRIVAL,
   0x8004 DBT_DEVICEREMOVECOMPLETE, 0x0007 DBT_DEVNODES_CHANGED}` → 1500 ms 单次定时器
   **去抖**（重复 `start()` 重新计时，USB 柜上电的事件风暴合并成一次），到点在工作线程里
   调 `MonitorService.rescan_devices(reason)`。**只加副作用，返回值语义不变**
   （§15 红线 12/13 与第 17 条）。
2. `MonitorService.rescan_devices()`：`scan_lock` 下强制重新枚举（元数据，见下）→
   记录 diff（新增/移除/换盘盘位/解除屏蔽）→ **增量补行**：
   已有行原样保留（`_merge_ui_data` 原生语义，SMART 数据不能丢），只给列表里没有的盘位
   追加占位行；绝不整表重建。
   **重建/补行时不发任何 SMART/IDENTIFY**（`_placeholder_rows`）——
   0x0007 是"任何设备变化"都会发的通知，若每次事件都跑一遍 SMART 检测，会把空闲停转
   但未标记休眠的盘反复唤醒。真实 SMART 数据由正常的按盘调度在下一轮补上
   （新盘 `disk_last_check_times` 为空 = 立即到期）。
   `removal_pending` / `shutdown_mode` / 未运行时直接跳过（弹出与关机事务自动让路）。

   **换盘识别（v1.3.90 补）**：桥接柜的序列号是按盘位分配的，同一盘位换盘后 pnp_id 与
   序列号都不变，只能靠 `_sync_device_fingerprints()` 的 `(pnp_id, serial, model)` 型号
   变化识别。指纹变化时：丢掉该盘位旧行 + `disk_last_check_times.pop(桥接序列号)`
   （否则新盘沿用上一块盘的到期时间，最长几小时不被检测）。
3. 兜底轮询 `device_inventory_interval_seconds`（默认 120 秒，0 = 只靠事件）：
   兜住"广播丢失 / 事件被事务跳过 / 多盘位柜与网络盘不发广播"的情况。
   为什么 §15 的 0x0007 不能单独承担：它是**任何设备变化**都会发的通知，只能当触发器。
4. `_heal_reappeared_devices()`：同 pnp_id 的盘在**新鲜枚举里再次出现**时，解除
   `ejected_disks` / `eject_quarantine`（这正是提示文案承诺的"请重新连接设备后恢复"）。
   仅在枚举到该设备时解除，避免误放行。

### 实测数据（枚举开销，2026-09-26）

| 指标 | 实测 |
| --- | --- |
| 一次全量枚举（7 个盘） | **0.44 秒**（连续 8 次：0.437~0.468） |
| 8 次枚举后的进程句柄增量 | **0**（268 → 268，不残留任何设备/卷句柄） |
| 磁盘 I/O | 无（WMI `Win32_DiskDrive` + PnP 父链 + `desiredAccess=0` 的卷号映射句柄） |
| 与弹出事务的关系 | 串行化：`execute_eject` 先拿 `scan_lock` + 置 `removal_pending`，重扫自动跳过 |

结论：轮询/重扫**不会**造成"其他盘被占用而弹不出去"——占用需要常驻句柄，这里没有；
历史上真正的自占用来源是 `SafeRemovalPatcher` 的常驻 `\\.\PhysicalDriveN` 句柄（红线第 3 条）。


---

## 14. 自启动方案（2026-08-23 修复：必须用计划任务）

### 问题
程序以管理员权限运行（uac_admin=True / asInvoker 提权）。HKCU Run 注册表自启动
在开机时启动的程序**无法自动提权**——Windows 弹出 UAC 确认框，开机时无人点击，
程序不启动 → 驱动级功能（弹出休眠/关机休眠）不生效，需要手动打开程序。

### 正确方案：任务计划程序（已验证）
    schtasks /Create /F /TN "JiFengZhiHDDManager" /TR "\"exe路径\" --silent" /SC ONLOGON /RL HIGHEST /DELAY 0005:00

关键参数：
- /SC ONLOGON —— 用户登录时触发
- /RL HIGHEST —— 以最高权限运行（计划任务启动提权程序不需要 UAC 交互！）
- /DELAY 0005:00 —— 延迟 5 分钟，等待系统就绪

验证要点（schtasks /Query /TN ... /XML）：
- <RunLevel>HighestAvailable</RunLevel> 存在
- <LogonTrigger> 登录触发器
- 手动 schtasks /Run 可无 UAC 启动 exe

### 代码位置（src/core/config_manager.py）
- set_autostart(True)：创建计划任务，成功后删除旧注册表项；失败回退注册表
- is_autostart_enabled()：优先检查计划任务，检测到旧注册表项自动迁移
- _fix_task_path()：计划任务指向旧路径时自动重建

### 修改红线补充
10. 不要改回 HKCU Run 注册表自启动（uac_admin 程序开机不启动）
11. 不要删除 set_autostart 里的"创建后清理注册表项"逻辑（避免双启动）


---

## 15. Windows 10 22H2 兼容：DEVNODES_CHANGED 补发 SLEEP（2026-08-27）

### 问题
Win10 22H2 上"安全删除硬件"弹出多盘位 USB 硬盘柜时，**不发送 DBT_DEVICEQUERYREMOVE**
（与 Win11 25H2 不同）。接口通知 + 句柄通知都注册了，但弹出时只收到
REMOVECOMPLETE (0x8004)，休眠逻辑（挂在 QUERYREMOVE 上）不触发。

### 事件序列（实测）
    DEVNODES_CHANGED (0x0007) → ~1秒后 → REMOVECOMPLETE (0x8004) × N

0x0007 是弹出前唯一的提前信号（比 REMOVECOMPLETE 早约 1 秒，设备尚可访问）。

### 方案：TUR 探测 + 精准 SLEEP（src/hal/win32_api.py _on_devnodes_changed）
- 收到 0x0007 时，对每个已注册句柄的盘发 TEST UNIT READY (TUR)
- TUR 失败的盘 = 正在被移除 → 只对这些盘发 SLEEP
- TUR 正常的盘不受影响（避免误休眠——0x0007 也因其他设备变化触发）
- 复用已注册的 RW 句柄发 SLEEP（避免弹出窗口内重新打开失败 1117）
- 防抖：10 秒内不重复探测

### 验证（打桩测试）
    模拟 7 个盘，盘 3 TUR 失败 → 只对盘 3 发 SLEEP，其余 6 盘不动 → 防抖生效

### 修改红线补充
12. 不要删除 _on_devnodes_changed（Win10 22H2 弹出休眠依赖它）
13. 0x0007 处理必须用 TUR 探测甄别（直接对所有盘 SLEEP 会误休眠）


---

## 16. Event ID 129（磁盘重置）与休眠标记（2026-08-27）

### ID 129 含义
- 来源：storahci/storport 存储控制器
- 内容："Reset to device, \\Device\\RaidPortX, was issued"
- 原因：磁盘在超时时间内未响应 I/O 请求（微软官方《Understanding Storage Timeouts and Event 129 Errors》）
- 与休眠的关系：磁盘进入 SLEEP 后，若有程序访问（如 SMART 检测），盘无法及时响应 → 控制器超时 → 发 Reset → ID 129

### 修复
SLEEP 成功后调用 monitor_service.mark_disk_sleeping(serial)（_on_query_remove 和 _on_devnodes_changed 两处），
监控服务检测到 sleeping_disks 中的盘会跳过 SMART 检测，不再访问已休眠盘 → 不触发 ID 129。

### Win10 关机休眠
- 关机休眠走 WM_QUERYENDSESSION（系统级消息，Win10/Win11 都发），不依赖 QUERYREMOVE
- 外置判定改为：is_removable OR pnp_id 含 USB/UASP
  （覆盖 USB 硬盘柜连接的 IDE 盘——WMI 显示 Interface: IDE 但 PnP 路径含 USB；
   内置 SATA/NVMe 直连盘的 PnP 路径不含 USB，不会被误休眠）

### 修改红线补充
14. 不要删除 SLEEP 后的 mark_disk_sleeping（否则监控访问休眠盘触发 ID 129）
15. 外置判定必须含 USB PnP（否则 Win10 多盘位硬盘柜的盘全部 is_removable=False，关机不生效）
