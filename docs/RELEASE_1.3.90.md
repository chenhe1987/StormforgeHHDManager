# Release 1.3.90

主题：**修复 v1.3.89 引入的回归（SMART 被冲成"待检测"）+ 补上"同一盘位换盘"的识别**

> v1.3.89 未发布到 Gitee（当时的流程是"实机验证通过后再上传"）。本版是它的修正版，
> 请直接用 v1.3.90。

## v1.3.89 的两个问题（用户实测反馈）

### 问题 1：已管理盘的 SMART 信息消失，状态变成"待检测"（**v1.3.89 引入的回归**）

`rescan_devices()` 用 `(index, serial)` 精确匹配"沿用旧行"，然后**整表替换**
`last_ui_data`。但 UI 行里的 `serial` 是 **SATA IDENTIFY 得到的真实序列号**，
而设备缓存（`DiskInfo.serial_number`）里是**桥接芯片序列号**（如 `3740BBBBAAAA`）——
匹配必然失败 → 所有行都被换成占位行 → 已纳入管理的盘 SMART 数据（温度/待处理扇区/
属性）全部被冲掉，显示"待检测"。

> `_merge_ui_data` 的注释早就写明这个坑："UI 记录里的 serial 可能是硬盘真实序列号，
> 而设备缓存里是桥接芯片序列号，因此绝不能只按 serial 判定"。

实测证据：`release/Stormforge_DiskManager_v1.3.89/app.log`
`12:26:45,524 [Rescan] reason=WM_DEVICECHANGE:0x0007 设备清单无变化（7 个）` →
`设备变化重扫结果: {'added': 0, ..., 'rows': 7}` —— 这一行之后列表被整表替换。

### 问题 2：新插入的盘不识别（**同一盘位换盘**没覆盖）

桥接柜（ASMT / D4 DAS）的序列号是**按盘位**分配的：同一盘位换盘后 pnp_id 与序列号
都不变 → `disk_id` 不变 → 列表里该盘位"已有行" → 增量补行逻辑不会补 →
界面继续显示旧盘的型号。实测：`12:25:57` 枚举已看到新盘
（盘位3 变成 `HGST HUS728T8TALE6L4`、盘位4 变成 `Teclast 120GB S500`），
但列表没有更新。

## 修复（v1.3.90）

1. **列表只增量补行，绝不整表重建**：保留 `last_ui_data` 里所有已有行（SMART 数据、
   `_merge_ui_data` 的原生语义），只给"列表里没有的盘位"追加占位行。
   新增回归测试 `test_smart_row_survives_when_ui_serial_is_ata_serial`
   （用真实序列号 vs 桥接序列号，直接复现 v1.3.89 的 bug）。
2. **盘位指纹识别换盘**：`_sync_device_fingerprints()` 在每次枚举后记录
   `{盘位: (pnp_id, 序列号, 型号)}`，指纹变化即判定"该盘位换了盘"：
   - 丢掉该盘位的旧行（旧盘的健康数据不再有效）并立即补一行占位；
   - `disk_last_check_times.pop(桥接序列号)` → 新盘**立即到期**，下一轮（≤1 秒）就被检测。
     （不清掉的话，因为检测时间是按盘位序列号记的，新盘会沿用上一块盘的到期时间，
     最长可能几小时都不被读取——这也是"新盘信息一直是旧的"的一部分原因。）
3. 解除屏蔽（重新插入后恢复）改为在**过滤之前**完成，刚恢复的盘这一轮就出现在列表里。
4. 占位行状态区分：白名单盘 = "待检测"（≤1 秒后被真实数据替换）、休眠盘 = "Sleeping"、
   其余 = "未加入白名单（不管理）"。

## 测试

- `tools/test_device_arrival.py` 扩到 **16 项**，新增：
  - `test_smart_row_survives_when_ui_serial_is_ata_serial`（v1.3.89 回归，真实/桥接序列号）
  - `test_bay_swap_is_detected_by_model_fingerprint`（盘位不变换盘 → 指纹识别 + 清检测时间 + 新型号可见）
  - `test_unchanged_bay_keeps_its_row`（没换盘的盘位不被误伤）
- 全量 82 项：本次相关 16 + 21 + 9 全绿；仅剩 6 项历史失败
  （`test_eject_integration` 4 + `test_safe_shutdown` 2，已在未改动的版本上用干净 worktree 做 A/B 确认）。
