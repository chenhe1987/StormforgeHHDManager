# Release 1.3.91

主题：**修复"弹出后换到另一个盘位插入 → 先能看到、随后又消失"**

> v1.3.90 未发布到 Gitee（流程是"实机验证通过后再上传"）。本版是它的修正版，请用 v1.3.91。

## 现象与日志证据（用户实测 v1.3.90）

用户在盘位 6 弹出 `ShineDisk M667 64G`，然后把它插到盘位 5：

| 时间 | 日志 | 结果 |
| --- | --- | --- |
| 12:36:11 | `将硬盘标记为已弹出: YC20160507A000000221 (index=6)` | 真实 ATA 序列号进"已弹出"黑名单 |
| 12:36:27 | `[Device] 盘位指纹变化: [5]` → `[Rescan] 换盘盘位=[5] 补行=[5]` → UI 6 条 | **能看到** ✓ |
| 12:36:32 | `SATA 识别到真实型号: ShineDisk M667 64G` → 行里 `serial` = `YC20160507A000000221` | 该值正好命中黑名单 |
| 12:36:34 | `合并后 5 条` | **消失** ✗ |

## 根因

`ejected_disks` 是按**序列号字符串**记的，而 `mark_disk_ejected()` 把**真实 ATA 序列号**
也写了进去（`_serials_for()` 同时收集桥接序列号和 UI 行的真实序列号，这是为了防止弹出时
后台还去访问该盘，本身是对的）。

换盘位后：
- 新盘位的**桥接序列号**与黑名单里的不同 → `_filter_excluded_disks` 不过滤 → 占位行先显示出来；
- 但 SMART 检测读出的**真实序列号**与黑名单里的**相同**（同一块物理盘）→ 行里的 `serial`
  变成真实序列号 → `_merge_ui_data()` 的 `if serial in self.ejected_disks: continue`
  在下一轮把这一行丢掉 → 消失。

`_heal_reappeared_devices()` 只按"枚举里出现的序列号"解除屏蔽，而枚举里只有**桥接**序列号，
解除不到真实序列号这一条。

## 修复

1. **读到真实序列号 = 这块盘现在就在位** → 立即调用新增的 `_heal_serial_exclusion()`
   解除它的"已弹出/休眠"屏蔽（并同步清理持久化的 `sleeping_disks`）。
   真正被弹出的盘不在总线上、读不出序列号，因此不会被误解除。
   位置：`monitor_service._check_all_smart_impl()` 的 SATA IDENTIFY 分支。
2. **`_merge_ui_data` 丢弃"已弹出"记录时补日志**
   （`DEBUG: 合并时丢弃已弹出硬盘的记录: ... (index=..., serial=...)`）——
   这次就是因为该分支静默丢行，只能靠条数从 6 变 5 才发现。

## 测试

`tools/test_device_arrival.py` 扩到 **20 项**，新增 `MovedDiskTests`（用日志里的真实序列号）：

- `test_row_whose_serial_is_blacklisted_would_be_dropped` —— 固化这个陷阱本身（复现 v1.3.90 的消失）
- `test_reading_real_serial_heals_the_blacklist` —— 读到真实序列号后解除屏蔽、行保留
- `test_genuinely_ejected_disk_stays_hidden` —— 真被弹出（不在总线上）的盘不会被误解除
- `test_full_scan_clears_blacklist_when_disk_is_readable` —— 端到端：走完整的
  `_check_all_smart_impl`（打桩 IDENTIFY/SMART），验证屏蔽被解除且行被推送

全量 86 项：本次相关 20 + 21 + 9 全绿；其余 6 项为历史失败（早前已用干净 worktree 做 A/B 确认）。
