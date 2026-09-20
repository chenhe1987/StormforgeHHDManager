"""关机/系统休眠停转策略 —— 对齐 Windows 对内置硬盘的处理方式。

## 旧策略为什么是错的

旧实现在 WM_QUERYENDSESSION 里对每块外置盘发送 `DeviceManager.spin_down_disk`
（FLUSH CACHE + **ATA SLEEP 0xE6**），然后**阻塞关机约 20 秒**等待盘物理停转。
三个致命问题：

1. **SLEEP 是深睡**：必须靠 COMRESET/重新上电才能退出（见 asm_commander.sleep 注释）。
   而 WM_QUERYENDSESSION 之后系统还要做：停止服务 → 文件系统 flush → 卸载卷 → 设备断电。
   其中任何一次 I/O 落到已 SLEEP 的盘上都会挂起十几秒，控制器超时后复位硬盘 →
   盘被重新转起来 → 断电瞬间盘仍在旋转 → 磁头紧急回收 →
   硬盘记一次 **C0（断电磁头缩回计数，即“不安全关机数”）**。
2. **20 秒阻塞**：超过 Windows 给应用的关机等待预算，系统可能在命令发出中途强杀进程，
   结果是“部分盘停转、部分盘还在转”，状态更糟。
3. **自家补丁又把盘唤醒**：SafeRemovalPatch 在关机期间收到 DEVNODES_CHANGED 会对所有
   外置盘做 TUR 探测（探测本身就会让盘起转），甚至补发 SLEEP，把刚停转的盘再折腾一遍。

## Windows 是怎么做的（内置硬盘）

电源计划的“在此时间后关闭硬盘”，以及存储设备进入 D3 断电时，用的是
**ATA STANDBY IMMEDIATE (0xE0)**：

- 卸载磁头 + 停转马达，但**可恢复** —— 后续任何命令都会让盘自动起转，不需要复位；
- 硬盘收到该命令时会把写缓存落盘；
- Windows 从不对内置盘发 SLEEP。

断电时盘已停转、磁头已卸载，因此不会发生紧急回收，C0 不增长。

## 新策略

1. **冻结所有后台访问**：监控线程进入 shutdown_mode，设备事件补丁置 shutdown_in_progress，
   关机期间不再有任何 TUR 探测 / SLEEP 补发；
2. 每块外置盘执行：**刷卷缓存 (FlushFileBuffers) → ATA FLUSH CACHE(0xE7) → ATA STANDBY IMMEDIATE(0xE0)**；
3. 多盘并发，总耗时设上限（默认 4 秒）。命令返回即代表磁头已卸载，不需要等马达物理停转；
4. 在 **WM_QUERYENDSESSION**（关机开始）与 **WM_ENDSESSION**（会话结束，断电前最后一次）
   各执行一次，幂等；系统休眠 PBT_APMSUSPEND 同样处理；
5. 全程不长时间阻塞，让 Windows 正常走完关机流程。

## 效果验证

每次开机记录各盘 SMART C0/C1（持久化在 app_config.json 的 shutdown_counters），
并与上次开机记录对比增量：

- 增量 0    → 上次关机被硬盘认定为安全关机（新策略生效）
- 增量 > 0  → 上次关机仍被判定为异常掉电（策略无效或断电时序不对）

内置直连盘由 Windows 自己管理，可作为对照组：若内置盘增量为 0 而外置盘增量 > 0，
说明问题只出在外置盘的关机处理上。
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime

from src.core.device_manager import DeviceManager

# 关停相关的 SMART 属性
ATTR_UNSAFE_SHUTDOWN = "0xC0"   # 不安全关机数（厂商文档称 Power-off Retract Count）
ATTR_LOAD_CYCLE = "0xC1"        # Load Cycle Count / 磁头加载卸载计数


class ShutdownGuard:
    """关机/休眠停转策略 + 不安全关机计数核对。"""

    def __init__(self, config_manager=None, monitor_service=None):
        self.config_manager = config_manager
        self.monitor_service = monitor_service
        self._frozen = False
        self._verified_serials = set()
        self._shutdown_runs = 0
        # 关机时锁住并卸载的卷句柄：保持打开以阻止卷被重新挂载/写入。
        # 若关机被取消（thaw），必须全部释放，否则用户的卷一直处于卸载状态。
        self._shutdown_volume_handles = []
        self._volume_handles_lock = threading.Lock()

    # ------------------------------------------------------------------ 冻结

    def freeze_background_access(self):
        """关机/休眠开始：停掉一切可能访问硬盘的后台动作。"""
        if self._frozen:
            return
        self._frozen = True

        try:
            if self.monitor_service is not None:
                self.monitor_service.shutdown_mode = True
                logging.info("[ShutdownGuard] 监控服务已进入关机静默模式")
        except Exception as e:
            logging.warning(f"[ShutdownGuard] 停止监控服务失败: {e}")

        # 设备事件补丁：关机期间设备节点变化会触发 TUR 探测/SLEEP，必须静默，
        # 否则刚停转的盘会被自己人重新唤醒。
        try:
            from src.hal.win32_api import SafeRemovalPatcher
            patcher = SafeRemovalPatcher()
            patcher.shutdown_in_progress = True
            logging.info("[ShutdownGuard] 设备事件补丁已静默（关机期间不再探测/发命令）")
        except Exception as e:
            logging.warning(f"[ShutdownGuard] 静默设备事件补丁失败: {e}")

    def thaw_background_access(self):
        """关机被取消 / 系统从休眠恢复：恢复后台服务。

        必须实现——否则一次被取消的关机（或一次休眠唤醒）会让监控服务
        和设备事件补丁永久停摆。
        """
        if not self._frozen:
            return
        self._frozen = False

        try:
            if self.monitor_service is not None:
                self.monitor_service.shutdown_mode = False
                logging.info("[ShutdownGuard] 关机/休眠结束，监控服务已恢复")
        except Exception as e:
            logging.warning(f"[ShutdownGuard] 恢复监控服务失败: {e}")

        try:
            from src.hal.win32_api import SafeRemovalPatcher
            SafeRemovalPatcher().shutdown_in_progress = False
            logging.info("[ShutdownGuard] 关机/休眠结束，设备事件补丁已恢复")
        except Exception as e:
            logging.warning(f"[ShutdownGuard] 恢复设备事件补丁失败: {e}")

        # 释放关机时锁定的卷句柄：卷会在下次被访问时自动重新挂载
        try:
            from src.hal.win32_api import Win32API
            with self._volume_handles_lock:
                handles, self._shutdown_volume_handles = self._shutdown_volume_handles, []
            for h in handles:
                try:
                    Win32API.close_handle(h)
                except Exception:
                    pass
            if handles:
                logging.info(f"[ShutdownGuard] 已释放 {len(handles)} 个关机锁定的卷句柄")
        except Exception as e:
            logging.warning(f"[ShutdownGuard] 释放卷句柄失败: {e}")

    # -------------------------------------------------------------- 主策略

    @staticmethod
    def _is_external(disk):
        """外置判定：is_removable 或 PnP 路径含 USB/UASP。

        内置 SATA/NVMe 直连盘由 Windows 自己管理，绝不干预（作为对照组）。
        """
        if getattr(disk, "is_removable", False):
            return True
        pnp_id = (getattr(disk, "pnp_id", "") or "").upper()
        return "USB" in pnp_id or "UASP" in pnp_id

    def collect_external_disks(self, cached_disks):
        """从物理盘缓存里挑出需要处理的外置盘。"""
        already_stopped = set()
        if self.monitor_service is not None:
            already_stopped = (
                set(getattr(self.monitor_service, "sleeping_disks", set()) or set())
                | set(getattr(self.monitor_service, "ejected_disks", set()) or set())
            )

        targets = []
        for d in cached_disks or []:
            idx = getattr(d, "index", None)
            if idx is None or not self._is_external(d):
                continue
            serial = getattr(d, "serial_number", None)
            # 已确认停转/已弹出的盘不再发命令：向 SLEEP 深睡盘发命令只会挂起并触发复位
            if serial and serial in already_stopped:
                continue
            model = getattr(d, "model", None) or f"Disk{idx}"
            targets.append((idx, model, serial))
        return targets

    def prepare_all_disks(self, cached_disks, event_type="关机", budget_seconds=4.0):
        """按 Windows 内置盘的做法停转所有外置盘（同步，带预算）。

        返回 (成功数, 总数)。整个过程不超过 budget_seconds。
        """
        self._shutdown_runs += 1
        run_no = self._shutdown_runs
        self.freeze_background_access()

        targets = self.collect_external_disks(cached_disks)
        if not targets:
            logging.info(f"[ShutdownGuard] {event_type}#{run_no}: 没有需要停转的外置盘")
            return 0, 0

        logging.info(
            f"[ShutdownGuard] {event_type}#{run_no}: 开始停转 {len(targets)} 块外置盘"
            f"（FLUSH CACHE → STANDBY IMMEDIATE，不使用 SLEEP，预算 {budget_seconds:.1f}s）"
        )

        started_at = time.time()
        results = []
        lock = threading.Lock()

        def worker(disk_index, model, serial):
            ok, msg, elapsed = False, "未执行", 0.0
            try:
                # 1. 先刷文件系统缓存（不锁卷，避免干扰 Windows 关机流程）
                flushed = DeviceManager.flush_volumes_for_disk(disk_index, timeout_seconds=1.5)
                # 2. ATA FLUSH CACHE + STANDBY IMMEDIATE
                ok, msg, elapsed = DeviceManager.prepare_disk_for_shutdown(
                    disk_index, model=model, serial=serial
                )
                if flushed:
                    msg += f"，已刷卷 {','.join(flushed)}"
            except Exception as e:
                msg = f"异常: {e}"
            with lock:
                results.append((disk_index, model, serial, ok, msg, elapsed))
                logging.info(
                    f"[ShutdownGuard] {event_type}#{run_no}: 磁盘 {disk_index} ({model}) "
                    f"{'已停转' if ok else '停转失败'} - {msg} ({elapsed:.2f}s)"
                )

        threads = []
        for disk_index, model, serial in targets:
            t = threading.Thread(
                target=worker, args=(disk_index, model, serial), daemon=True
            )
            t.start()
            threads.append(t)

        # 等待，但总量不超过预算——不长时间阻塞关机
        for t in threads:
            remaining = budget_seconds - (time.time() - started_at)
            if remaining <= 0:
                break
            t.join(timeout=remaining)

        ok_count = sum(1 for r in results if r[3])
        total_elapsed = time.time() - started_at
        unfinished = len(targets) - len(results)

        summary = (
            f"[ShutdownGuard] {event_type}#{run_no} 完成: {ok_count}/{len(targets)} 块已停转，"
            f"耗时 {total_elapsed:.2f}s"
        )
        if unfinished > 0:
            summary += f"（{unfinished} 块未在预算内返回）"
        logging.info(summary)

        # 成功的盘标记为 Sleeping：后续监控/补丁不再访问它们
        if self.monitor_service is not None:
            for _, _, serial, ok, _, _ in results:
                if ok and serial:
                    try:
                        self.monitor_service.mark_disk_sleeping(serial)
                    except Exception:
                        pass

        return ok_count, len(targets)

    # ------------------------------------------- 关机停转（关键路径）

    @staticmethod
    def _resolve_volumes_fast():
        """Win32 API 卷→磁盘映射（0ms，无 WMI）。"""
        from src.hal.win32_api import Win32API
        try:
            mapping = dict(Win32API.get_volume_disk_mapping() or {})
        except Exception as e:
            logging.warning(f"[ShutdownGuard] 卷映射枚举失败: {e}")
            mapping = {}
        by_disk = {}
        for letter, idx in mapping.items():
            by_disk.setdefault(idx, []).append(f"{letter}:")
        return by_disk

    def start_shutdown_parking(self, cached_disks, event_type="关机"):
        """关机/系统休眠停转（定稿策略的服务不可用降级版）：刷卷缓存 → FLUSH CACHE → **ATA SLEEP 深睡** + 移入黑名单。

        设计依据（用户方案 + 十一轮实测，固定技术原则见
        docs/TECH_NOTES_弹出休眠机制.md §8.38「技术原则（定稿）」）：
        - 深睡本身就是黑名单：ATA SLEEP(0xE6) 后盘进入最低功耗，**不响应任何程序**；
          桥接芯片会对后续访问立刻回 3A/00（无介质），不会真把盘唤醒。
        - 因此**不需要卸载卷、不需要锁卷、不需要 Windows 离线**——卷被程序占用
          也完全不影响深睡（“无法卸载 ≠ 无法休眠”）。
        - 深睡后把盘移入本程序黑名单（sleeping_disks），本程序与补丁不再访问它；
          用户需在程序里「唤醒并解除黑名单」才能再次访问。
        - 正常情况下由关机停转服务（shutdown_service.py，PRESHUTDOWN 阶段，
          带离线加速）执行；本函数只在该服务不可用时兜底。
        """
        self._shutdown_runs += 1
        run_no = self._shutdown_runs
        self.freeze_background_access()

        targets = self.collect_external_disks(cached_disks)
        if not targets:
            logging.info(f"[ShutdownGuard] {event_type}#{run_no}: 没有需要停转的外置盘")
            return 0

        by_disk = self._resolve_volumes_fast()

        resolved = []
        for disk_index, model, serial in targets:
            resolved.append({
                "index": disk_index,
                "model": model,
                "serial": serial,
                "volumes": by_disk.get(disk_index, []),
            })

        logging.info(
            f"[ShutdownGuard] {event_type}#{run_no}: 深睡 {len(resolved)} 块外置盘"
            f"（刷卷缓存→FLUSH CACHE→ATA SLEEP 深睡→移入黑名单，无需卸载卷）: "
            + ", ".join(f"{t['index']}({t['model']})" for t in resolved)
        )

        # 必须在 QES 返回前完成（进程随时可能被结束），同步执行、不启动守护线程。
        return self._park_round(resolved, event_type, run_no)

    def _park_one(self, target, event_type, run_no, reason):
        disk_index = target["index"]
        model = target["model"]
        serial = target["serial"]
        volumes = target.get("volumes") or []
        started = time.time()
        msg = ""
        try:
            from src.hal.win32_api import Win32API

            # 1) 刷文件系统缓存（不锁卷、不卸载卷：被占用也能刷，数据安全）
            volume_note = ""
            if volumes:
                flushed = []
                for vol in volumes:
                    try:
                        handle = Win32API.open_volume(vol)
                        if not handle:
                            continue
                        try:
                            ok_f, _ = Win32API.flush_volume_buffers(handle)
                            if ok_f:
                                flushed.append(vol)
                        finally:
                            Win32API.close_handle(handle)
                    except Exception:
                        pass
                if flushed:
                    volume_note = "，已刷卷 " + ",".join(flushed)

            # 2) 深睡（与“立即休眠硬盘”按钮同机制）：FLUSH CACHE + ATA SLEEP
            ok, park_msg, _ = DeviceManager.deep_sleep_disk(
                disk_index, model=model, serial=serial
            )
            msg = park_msg + volume_note

            # 3) 移入本程序黑名单：监控与补丁从此不再访问它
            if ok and self.monitor_service is not None and serial:
                try:
                    self.monitor_service.mark_disk_sleeping(serial, disk_index)
                except Exception:
                    pass
        except Exception as e:
            ok, msg = False, f"异常: {e}"
        logging.info(
            f"[ShutdownGuard] {event_type}#{run_no} [{reason}]: 磁盘 {disk_index} ({model}) "
            f"{'已深睡' if ok else '深睡失败'} - {msg} ({time.time() - started:.2f}s)"
        )
        return ok

    def _park_round(self, resolved, event_type, run_no):
        """并发停转一轮，带预算（不超过 6 秒）。"""
        started_at = time.time()
        results = []
        lock = threading.Lock()

        def worker(target):
            ok = self._park_one(target, event_type, run_no, "深睡")
            with lock:
                results.append((target, ok))

        threads = []
        for target in resolved:
            t = threading.Thread(target=worker, args=(target,), daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            remaining = 6.0 - (time.time() - started_at)
            if remaining <= 0:
                break
            t.join(timeout=remaining)

        ok_count = sum(1 for _, ok in results if ok)
        logging.info(
            f"[ShutdownGuard] {event_type}#{run_no}: {ok_count}/{len(resolved)} 块已深睡，"
            f"耗时 {time.time() - started_at:.2f}s"
        )
        return ok_count

    # -------------------------------------------------- 不安全关机计数核对

    @staticmethod
    def _extract_attr(attributes, attr_id):
        """从 UI 的 attributes 列表里取指定属性的 raw 数值。"""
        for attr in attributes or []:
            if str(attr.get("id", "")).upper() != attr_id.upper():
                continue
            raw = attr.get("raw", "")
            match = re.match(r"\s*(\d+)", str(raw))
            if match:
                return int(match.group(1))
        return None

    def _report_path(self):
        try:
            base = os.path.dirname(self.config_manager.filename)
        except Exception:
            base = os.getcwd()
        return os.path.join(base, "shutdown_verification.json")

    def process_smart_data(self, ui_data):
        """每次 UI 拿到 SMART 数据时调用（廉价，不额外访问硬盘）。

        做两件事：
        1. 每块盘**本进程首次**读到数据时，与上次关机前保存的 C0 对比，
           判断上一次关机是否为安全关机；
        2. 持续刷新基线，保证“关机前最后一次已知值”尽可能新
           （关机时不再读盘，避免把刚停转的盘唤醒）。
        """
        if not self.config_manager:
            return

        entries = [d for d in (ui_data or []) if d.get("attributes")]
        if not entries:
            return

        previous = self.config_manager.get_shutdown_counters() or {}
        current = dict(previous)
        results = []
        lines = []
        changed = False

        for disk in entries:
            serial = disk.get("serial")
            if not serial:
                continue
            c0 = self._extract_attr(disk.get("attributes"), ATTR_UNSAFE_SHUTDOWN)
            c1 = self._extract_attr(disk.get("attributes"), ATTR_LOAD_CYCLE)
            if c0 is None and c1 is None:
                continue

            model = disk.get("model", "Unknown")
            is_external = bool(disk.get("is_removable", False))
            prev = previous.get(serial) or {}
            prev_c0 = prev.get("c0")
            prev_c1 = prev.get("c1")

            # ---- 1) 本进程首次读到这块盘：核对上一次关机 ----
            if serial not in self._verified_serials:
                self._verified_serials.add(serial)
                tag = "外置" if is_external else "内置"
                if prev_c0 is None:
                    lines.append(f"[{tag}] {model} ({serial}): 首次记录基线 C0={c0}, C1={c1}")
                else:
                    delta_c0 = c0 - prev_c0
                    delta_c1 = (c1 - prev_c1) if (c1 is not None and prev_c1 is not None) else None
                    verdict = "✅ 安全关机（未增加）" if delta_c0 == 0 else f"❌ 增加了 {delta_c0} 次"
                    lines.append(
                        f"[{tag}] {model} ({serial}): 上次关机前 C0={prev_c0} → 本次开机 C0={c0}"
                        f"（增量 {delta_c0:+d}）{verdict}"
                    )
                results.append({
                    "serial": serial,
                    "model": model,
                    "is_external": is_external,
                    "c0_before": prev_c0,
                    "c0_now": c0,
                    "c1_before": prev_c1,
                    "c1_now": c1,
                    "delta_c0": (c0 - prev_c0) if prev_c0 is not None else None,
                    "delta_c1": (c1 - prev_c1) if (c1 is not None and prev_c1 is not None) else None,
                })

            # ---- 2) 刷新基线 ----
            if prev_c0 != c0 or prev_c1 != c1 or prev.get("model") != model:
                current[serial] = {
                    "model": model,
                    "c0": c0,
                    "c1": c1,
                    "is_external": is_external,
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                changed = True

        if lines:
            logging.info("===== 关机安全性核对（不安全关机数 SMART 0xC0）=====")
            for line in lines:
                logging.info(line)
            self._write_report(results)

        if changed:
            try:
                self.config_manager.set_shutdown_counters(current)
            except Exception as e:
                logging.warning(f"[ShutdownGuard] 保存关机计数基线失败: {e}")

    def _write_report(self, results):
        """写一份独立报告，便于重启后直接查看验证结果。"""
        if not results:
            return
        try:
            path = self._report_path()
            existing = {"results": []}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        existing = json.load(f) or {"results": []}
                except Exception:
                    existing = {"results": []}

            merged = {r.get("serial"): r for r in existing.get("results", [])}
            for r in results:
                merged[r.get("serial")] = r

            report = {
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "attribute": "不安全关机数 (SMART 0xC0)",
                "note": (
                    "delta_c0 = 本次开机值 - 上次关机前值，即上一次关机造成的增量。"
                    "实测（tools/smart_counter_probe.py）：这块盘每次磁头卸载都会 +1，"
                    "包括命令式 STANDBY IMMEDIATE（延迟 1-2 分钟入账）与断电时的紧急回收；"
                    "因此只要关机时盘还在转，+1 就是下限（Windows 自己管的内置盘同样是 +1）。"
                    "只有让盘在关机前就已经处于停转状态（空闲自动休眠/手动休眠）才能做到 +0。"
                ),
                "results": list(merged.values()),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            logging.info(f"[ShutdownGuard] 关机核对报告已写入: {path}")
        except Exception as e:
            logging.warning(f"[ShutdownGuard] 写关机核对报告失败: {e}")
