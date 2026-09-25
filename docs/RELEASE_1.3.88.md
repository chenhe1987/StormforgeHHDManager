# Release 1.3.88

主题：**弹出失败的根因修复 + 提示可读化 + 无卷盘支持**

> **实机验收（2026-09-25 23:06，本机 ASMT105x 双盘，v1.3.88）**
>
> | 盘 | 形态 | 关键事件序列 | 结果 |
> | --- | --- | --- | --- |
> | 磁盘 4 / E: | GPT + ReFS，1 卷 | LOCK error=5 → `volume_dismounted_without_lock` → 跳过 flush/dismount → `offline_verified` → E7 → E6(1.98s, scsi=0) → PnP `cr=0 veto=0` | `ejected_sleep_accepted`（约 4.6s） |
> | 磁盘 3 | RAW，0 分区 | `volume_less_disk` → `offline_verified` → E7 → E6(0.86s) → PnP `cr=0` | `ejected_sleep_accepted`（约 4.4s） |
>
> 证据：`release/Stormforge_DiskManager_v1.3.88/operations.log` 的
> `[EjectTransaction] disk=4/3 event=...` 完整序列。技术原则已固化到
> `docs/TECH_NOTES_弹出休眠机制.md` §8.41 与修改红线第 10–15 条。

## 1. 修复：ReFS / exFAT 卷弹不出去（裸 WinError 5）

v1.3.78 之后的新弹出事务把 `FSCTL_LOCK_VOLUME` 的 `ERROR_ACCESS_DENIED(5)` 一律当致命错误，
导致本机 E:（ReFS）永远弹不出去，日志只有一行 `[WinError 5] 拒绝访问。`。

实测结论（本机，2026-09-25）：

- GUI 完全退出、只剩关机服务时，E: 卷锁定**同样返回 5**；
- 同一时刻 Restart Manager 查不到任何占用者，句柄审计确认本程序自身持有 0 个句柄；
- 与 `src/hal/win32_api.py` 旧实现留下的记录一致：**ReFS 卷锁定恒返回 5，DISMOUNT 可成功**。

因此对 **ReFS/exFAT 等已知不支持锁卷的文件系统**：锁定失败 → 跳过锁定、直接
`FSCTL_DISMOUNT_VOLUME`（与 Windows 资源管理器弹出同款）；**卸载失败仍然立即中止**
（不发 SLEEP、不离线）。非 ReFS/exFAT 的卷依旧严格执行"锁卷失败即停止"。

开关：`app_config.json` → `"eject_dismount_without_lock"`（默认 `true`；置 `false` 恢复旧行为）。

## 2. 新增：无分区表（RAW）硬盘可直接弹出

`Get-Partition` 对 RAW 盘会报错，旧预检直接抛异常 → 整块盘永远弹不出去
（本机磁盘 3 = `A000BBBBAAAA`，16 TB，无分区表）。

现在：卷枚举改为容错，`Get-Disk` 的 `PartitionStyle` 随快照返回；
`PartitionStyle=RAW` 且无卷 → 判定"没有卷需要隔离"，按**整盘直接停转 + 弹出**处理，
并记录 `volume_less_disk` 事件（GUI 状态栏显示"该盘无分区表（RAW），直接整盘停转并弹出"）。
**有分区表却枚举不到卷仍然 fail closed**，并把卷枚举错误原样带进提示。

## 3. 提示可读化：不再是一个 error=5

新增 `src/utils/volume_diag.py`：

- `describe_winerror()`：错误码翻译（5/2/21/32/1117/1167/433 …）；
- 占用诊断：Restart Manager（文件层面）+ 存储句柄审计（按
  `IOCTL_STORAGE_GET_DEVICE_NUMBER` 判定磁盘号/分区号，**包含本程序自身**）；
- 弹出失败文案示例：

```
无法隔离卷 E: (ReFS)：锁定卷失败（WinError 5 拒绝访问（被其他程序占用、权限不足，
或该文件系统不支持此操作））。
占用者（文件层面）：未发现其他程序；本程序自身持有该盘句柄：0 个；
其他程序仅做文件层面检查（Restart Manager）
建议：关闭占用该卷的程序后重试。
```

- `src/hal/eject_backend.py` 分阶段报错：打开物理盘 / 打开卷 / 锁定卷 各自有明确文案。

## 4. 顺带修复：关机停转"静默空转"

`_park_all_disks` 以前在没有目标时静默 `return 0`，日志只剩 `最终关机 SLEEP 总耗时 0.000s`。
现在会写明原因（共享清单不存在 / 白名单为空 / 命中盘都已休眠 / 清单里没有白名单外置盘）。

## 5. 测试

- 新增 `tools/test_eject_diagnostics.py`（21 项）：包含用真 `WindowsBackend` + IOCTL 打桩的
  完整事务测试 —— ReFS 直卸后跳过 flush/dismount 并继续 offline+SLEEP+弹出；
  RAW 盘无卷走完整事务；GPT 无卷仍然 fail closed；NTFS 锁卷失败在 offline 前中止。
- 新增手动诊断工具 `tools/volume_occupancy_probe.py`：
  `python tools\volume_occupancy_probe.py --disk 4 --letter E --try-lock`
- 已知遗留：`tools/test_eject_integration.py`（4 项）与 `tools/test_safe_shutdown.py`（2 项）
  在未改动的 v1.3.87 上同样失败，与本次改动无关（已用干净 worktree 做 A/B 对照）。
