"""SMART 计数器探针 —— 用于实验/核对"不安全关机数"等计数器的真实语义。

背景：WD/HGST 硬盘的 0xC0(Power-off Retract Count) 与 0xC1(Load/Unload Cycle)
经常报同一个原始值，需要实测确认：
  - 一次"命令式停转"(STANDBY IMMEDIATE) 会不会让 C0 增加？
  - 一次"起转/唤醒"(读取 SMART) 会不会让 C0 增加？
  - 断电时的紧急回收增加多少？

用法（需要管理员权限才能打开 PhysicalDriveN）：
    python tools/smart_counter_probe.py <disk_index> [标签]
    python tools/smart_counter_probe.py <disk_index> --park     # 读取 → 停转 → 再读取
    python tools/smart_counter_probe.py <disk_index> --park --wait 15

结果同时打印并追加写入 tools/smart_counter_log.jsonl。
"""

import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.hal.asm_commander import ASMCommander
from src.utils.smart_parser import SmartParser

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smart_counter_log.jsonl")

# 关注的计数器
WATCH = {
    4: "Start/Stop Count",
    9: "Power-On Hours",
    12: "Power Cycle Count",
    192: "0xC0 Power-off Retract",
    193: "0xC1 Load/Unload Cycle",
}


def read_counters(disk_index, model=None, serial=None):
    """读取 SMART 并返回关注计数器的原始值。

    注意：读取 SMART 本身会产生 I/O，会把已停转的盘重新唤醒。
    """
    try:
        with ASMCommander(disk_index, model_hint=model, serial_hint=serial) as cmd:
            data = cmd.get_smart_data()
    except Exception as e:
        return {"error": str(e)}

    if not data:
        return {"error": "SMART 读取失败"}

    attrs = SmartParser.parse_512(data)
    values = {a.id: a.raw for a in attrs}
    return {name: values.get(attr_id) for attr_id, name in WATCH.items()}


def park(disk_index, model=None, serial=None):
    """发送 FLUSH CACHE + STANDBY IMMEDIATE（Windows 对内置盘的停转方式）。"""
    with ASMCommander(disk_index, model_hint=model, serial_hint=serial) as cmd:
        flush_ok = cmd.flush_cache(timeout=5)
        standby_ok = cmd.standby_immediate(timeout=8)
    return flush_ok, standby_ok


def emit(record):
    print(json.dumps(record, ensure_ascii=False))
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"写入日志失败: {e}", file=sys.stderr)


def main():
    args = [a for a in sys.argv[1:]]
    if not args:
        print(__doc__)
        return 1

    disk_index = int(args[0])
    do_park = "--park" in args
    wait_seconds = 12
    if "--wait" in args:
        wait_seconds = int(args[args.index("--wait") + 1])
    samples = []
    if "--samples" in args:
        samples = [int(x) for x in args[args.index("--samples") + 1].split(",")]
    label = "probe"
    for a in args[1:]:
        if not a.startswith("--") and not a.isdigit():
            label = a

    base = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "disk": disk_index, "label": label}

    before = read_counters(disk_index)
    emit({**base, "step": "before", **before})

    if do_park:
        flush_ok, standby_ok = park(disk_index)
        emit({**base, "step": "park",
              "flush_ok": flush_ok, "standby_ok": standby_ok})

        if samples:
            # 多次采样：判断"命令式停转"是否会在之后某个时刻才计入计数器
            prev = time.time()
            for offset in samples:
                time.sleep(max(0, offset - (time.time() - prev)))
                after = read_counters(disk_index)
                rec = {**base, "step": f"sample+{offset}s", **after}
                emit(rec)
        else:
            time.sleep(wait_seconds)
            after_park = read_counters(disk_index)
            emit({**base, "step": f"after_park+{wait_seconds}s", **after_park})

    return 0


if __name__ == "__main__":
    sys.exit(main())
