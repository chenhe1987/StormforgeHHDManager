"""关机/系统休眠停转策略。

Windows 发送关机或系统休眠通知后，程序冻结后台探测，只对已加入白名单的
USB/UASP 硬盘执行与“立即休眠”按钮相同的流程：刷可访问的卷缓存、发送
FLUSH CACHE 和 ATA SLEEP，然后把成功的硬盘加入本次运行的休眠集合。界面在
关机前持续显示数据保存提示；内置盘和未加入白名单的硬盘完全跳过。
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime

from src.core.device_manager import DeviceManager
from src.core.disk_whitelist import disk_id

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
        """从物理盘缓存里挑出白名单内需要处理的外置盘。"""
        already_stopped = set()
        allowed = (self.config_manager.get_managed_disk_whitelist()
                   if self.config_manager is not None else set())
        if self.monitor_service is not None:
            already_stopped = (
                set(getattr(self.monitor_service, "sleeping_disks", set()) or set())
                | set(getattr(self.monitor_service, "ejected_disks", set()) or set())
            )

        targets = []
        for d in cached_disks or []:
            idx = getattr(d, "index", None)
            if idx is None or not self._is_external(d) or disk_id(d) not in allowed:
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
        """Use the proven deep-sleep path when Windows begins shutdown/suspend.

        The operation is intentionally the same as the UI's immediate sleep:
        flush available volume buffers, send FLUSH CACHE + ATA SLEEP, then add
        the disk to the in-process sleeping set.  Only allow-listed external
        disks returned by collect_external_disks() are eligible.
        """
        self._shutdown_runs += 1
        run_no = self._shutdown_runs
        self.freeze_background_access()

        targets = self.collect_external_disks(cached_disks)
        if not targets:
            logging.info(f"[ShutdownGuard] {event_type}#{run_no}: 没有需要休眠的白名单硬盘")
            return 0

        by_disk = {}
        resolved = [
            {
                "index": disk_index,
                "model": model,
                "serial": serial,
                "volumes": by_disk.get(disk_index, []),
            }
            for disk_index, model, serial in targets
        ]
        logging.info(
            f"[ShutdownGuard] {event_type}#{run_no}: 按立即休眠逻辑处理 "
            f"{len(resolved)} 块白名单硬盘（直接 ATA SLEEP）"
        )
        return self._park_round(resolved, event_type, run_no)

    def _park_one(self, target, event_type, run_no, reason):
        disk_index = target["index"]
        model = target["model"]
        serial = target["serial"]
        volumes = target.get("volumes") or []
        started = time.time()
        msg = ""
        try:
            from src.hal.asm_commander import ASMCommander
            with ASMCommander(disk_index, model_hint=model, serial_hint=serial) as cmd:
                ok = cmd.sleep_only()
            msg = "ATA SLEEP 命令已接受" if ok else "ATA SLEEP 命令失败"

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
