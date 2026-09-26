# Release 1.3.89

主题：**修复"程序启动后新插入硬盘不显示"（热插拔即时发现）**

## 问题（两个独立缺陷叠加）

1. **发现慢**：物理盘清单只在 `now - last_inventory_scan_time >= device_inventory_interval`
   时重新枚举（原值 **300 秒**），而 `WM_DEVICECHANGE` 分支只是 `return super().nativeEvent(...)`，
   不使缓存失效 → 插入后最长 5 分钟才"看见"。
2. **看见了也不显示（主因）**：侧栏完整列表只在 `target_disks is None` 的全量路径生成
   （含"未加入白名单（不管理）"行）；每 10 秒的定时路径对非白名单盘直接 `continue`，
   随后 `_merge_ui_data` **只从 updates 追加新条目** → 新插入、尚未勾选"纳入管理"的盘
   **永远进不了侧栏**，只有重启或点"刷新硬盘状态"才会出现。

实测对照（同一份代码、不接触硬件）：

| 路径 | 推送条数 | 含新盘 |
| --- | --- | --- |
| 定时路径（每 10 秒） | 0 | ❌（这就是缺陷 2 的对照组） |
| 全量路径（启动 / 手动刷新） | 1 | ✅ 状态"未加入白名单（不管理）" |

## 修复

1. **事件驱动重扫**：`WM_DEVICECHANGE` 的 `0x8000 (ARRIVAL)` / `0x8004 (REMOVECOMPLETE)` /
   `0x0007 (DEVNODES_CHANGED)` → **1500 ms 去抖**（USB 柜上电的事件风暴合并成一次）→
   工作线程执行 `MonitorService.rescan_devices()`。
   **只加副作用，返回值语义不变**（QUERYREMOVE 仍必须返回 1 才允许移除）。
2. **`rescan_devices()`**：`scan_lock` 下强制重新枚举 → 记录 diff（新增/移除/解除屏蔽）→
   用设备缓存**重建完整列表**并推送。
   **重建时不发任何 SMART/IDENTIFY**（`_rows_for_devices_without_probe`）：0x0007 是
   "任何设备变化"都会发的通知，若每次事件都跑 SMART，会把空闲停转但未标记休眠的盘
   反复唤醒。已知盘沿用上一次的行（保留温度/健康），新盘给占位行
   （白名单盘="待检测"，非白名单="未加入白名单（不管理）"），真实数据由按盘调度下一轮补上。
   `removal_pending` / `shutdown_mode` 时自动跳过（弹出与关机事务让路）。
3. **兜底轮询可配置**：`app_config.json` → `device_inventory_interval_seconds`（默认 **120**，0 = 只靠事件）。
   用于兜住"广播丢失 / 事件被事务跳过 / 多盘位柜与网络盘不发广播"。
4. **屏蔽自愈**：同 pnp_id 的盘在新鲜枚举里**再次出现**时，自动解除 `ejected_disks` /
   `eject_quarantine`（即文案承诺的"请重新连接设备后恢复"）；没枚举到就不解除。

## 实测数据（回答"轮询会不会导致其他盘被占用而弹不出去"）

| 指标 | 实测 |
| --- | --- |
| 一次全量枚举（7 个盘） | **0.44 秒** |
| 8 次枚举后进程句柄增量 | **0**（268 → 268，零残留） |
| 磁盘 I/O | 无（WMI + PnP 父链 + `desiredAccess=0` 的卷号映射句柄，即用即关） |
| 与弹出事务 | 串行化（`scan_lock` + `removal_pending`），事务期间重扫自动跳过 |

⇒ **不会**造成"其他盘被占用"。占用必须靠常驻句柄，枚举不留句柄；历史上真正的自占用
来源是 `SafeRemovalPatcher` 的常驻 `\\.\PhysicalDriveN` 句柄（已是死代码）。

## 测试

- 新增 `tools/test_device_arrival.py`（11 项）：事件触发集合过滤（QUERYREMOVE 系列不触发）、
  事件风暴去抖、重扫把"未纳入管理"的新盘加入列表、拔出盘从列表消失、
  事务/关机期间跳过重扫且不枚举、重新插入后解除隔离、
  **休眠盘只显示 `Sleeping` 且绝不构造 ASMCommander（不唤醒）**、
  间隔为 0 时不会每次调用都重新枚举。
- 回归：全量 77 项测试中，本次新增 32 项全绿；仅剩 6 项既有失败
  （`test_eject_integration` 4 项 + `test_safe_shutdown` 2 项，已在未改动的 v1.3.87/v1.3.88 上
  用干净 worktree 做 A/B 确认与本次改动无关）。
- 真机：`rescan_devices()` 在真机跑通（7 个盘 → 7 行完整列表，无 SMART 写入白名单外的盘）。
