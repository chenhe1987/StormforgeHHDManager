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

## 8. 关机/系统休眠保护

### 8.1 触发方式

- WM_QUERYENDSESSION（关机）→ eject_all_removable_disks()
- WM_POWERBROADCAST / PBT_APMSUSPEND（系统休眠）→ eject_all_removable_disks(is_system_sleep=True)

驱动级功能：无条件执行，不读 config_manager.get_shutdown_eject()（UI 已固定开启并禁用）。

### 8.2 休眠列表来源（关键！）

    # 关机阶段严格只使用缓存，避免再次在线扫描唤醒硬盘。
    # 优先使用 monitor_service.cached_disks（物理盘缓存，始终保持最新），
    # 避免依赖 UI disk_data（仅在 SMART 检测时刷新，silent 模式下可能过期）。
    cached = getattr(self.monitor_service, 'cached_disks', []) or []

    eject_list = []
    for d in cached:
        idx = getattr(d, 'index', None)
        serial = getattr(d, 'serial_number', None)
        model = getattr(d, 'model', None) or getattr(d, 'model_hint', None)
        is_removable = getattr(d, 'is_removable', False)
        if idx is not None and is_removable and serial not in already_asleep:
            eject_list.append((idx, model, serial))

不要改回 self.disk_data：silent 模式下 disk_data 只在 SMART 检测时更新，
长时间运行后会过期 → 关机时漏掉部分外置盘（实测漏掉磁盘 3）。

- cached_disks 是 DiskInfo 对象列表（src/core/device_manager.py）
- already_asleep = monitor_service.sleeping_disks | ejected_disks（跳过已休眠/已弹出盘）

### 8.3 流程

1. Phase 1：并发（线程）发送 SLEEP，join 最多 5 秒
2. Phase 2：等待 20 秒（SPIN_DOWN_WAIT_SEC）让硬盘停转
3. 标记成功盘 mark_disk_sleeping(serial)
4. ShutdownBlockReasonCreate 显示"正在为您执行硬盘关机休眠保护"

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

- build.spec：COLLECT name='疾风知硬盘柜管理_v1.3.72'，uac_admin=True
- 打包命令：python -m PyInstaller --noconfirm --clean --distpath dist --workpath build build.spec
- 打包前必须结束运行中的程序实例（否则 app.log 被锁无法删除 dist）
- 旧 dist 有 app.log/operations.log 锁定时，先 taskkill 再删
- 打包后 fc /N 对比源码与 _internal 确认一致

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
