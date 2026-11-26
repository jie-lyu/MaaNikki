"""Dev-only Android entry MaaNikki_AndroidWalkProbe: walk to one nearby point with host.android.move on the agent's
Runtime, so a supervised device session exercises the real Runtime.gesture path (reservation, touch marker, intent
log, device-confirmed lift) end to end. Never a user task:

- its pipeline node exists only in resource_android/pipeline/android_walk_probe.json (the Windows package ships
  neither that directory nor host/android);
- it is not in agent/host/android/tasks.json, interface.json or any interface overlay, so no client offers it, and the
  agent refuses it unless the runner names it in MAANIKKI_ANDROID_DEV_ENTRIES (host.android.dev_entries; only
  scripts/android/runner.py sets that, and only for this entry);
- it is run by nikki.gameplay only when both the task entry and the parameter's kind say so.

The parameters come from the runner's --option values (custom_action_param): map, hint [x, y], target [x, y], timeout,
leash, radius. Every step's frame and numbers go to DATA_ROOT/debug/android_walk/<time>/ (walk.jsonl; the frames
with the UID masked, as every diagnostic image); failure scenes go to debug/on_error as usual.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import time

from host.android.move import RuntimeIo

ENTRY = "MaaNikki_AndroidWalkProbe"
KIND = "android_walk_probe"
MAX_TIMEOUT_S = 120.0
MAX_LEASH_U = 20.0
RADIUS_RANGE = (0.5, 3.0)


class Params:
    def __init__(self, options: dict):
        def point(name):
            value = options.get(name)
            if not isinstance(value, (list, tuple)) or len(value) != 2 or any(
                    type(v) not in (int, float) or not math.isfinite(v) for v in value):
                raise ValueError(f"走路探针的参数 {name} 必须是两个数（x, y）")
            return float(value[0]), float(value[1])

        def number(name, default, low, high):
            value = options.get(name, default)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"走路探针的参数 {name} 必须在 {low}–{high} 之内")
            return float(value)

        self.map = options.get("map")
        if not isinstance(self.map, str) or not self.map:
            raise ValueError("走路探针缺少地图参数 map")
        self.hint, self.target = point("hint"), point("target")
        self.timeout = number("timeout", 60.0, 1.0, MAX_TIMEOUT_S)
        self.leash = number("leash", 12.0, 1.0, MAX_LEASH_U)
        self.radius = number("radius", 1.8, *RADIUS_RANGE)
        if math.dist(self.hint, self.target) > self.leash:
            raise ValueError("走路探针的目标超出了允许的范围（leash）")


class DiagnosticIo(RuntimeIo):
    """move.RuntimeIo whose step records (and their frames) are kept in one folder of the data root's debug dir."""

    def __init__(self, rt, folder):
        super().__init__(rt)
        self.folder, self.count = folder, 0
        folder.mkdir(parents=True, exist_ok=True)

    def record(self, kind, record, frame):
        path = None
        if frame is not None:
            from diagnostics import save_image
            path = save_image(self.folder / f"{self.count:03d}-{kind}.png", frame).name
            self.count += 1
        line = {"time": datetime.now().astimezone().isoformat(timespec="milliseconds"), "kind": kind,
                "frame": path, **record}
        with (self.folder / "walk.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        return path


def run(rt, options: dict) -> bool:
    """The walk; True when the target was reached. Android only (refused before anything else elsewhere)."""
    from host import capabilities, game_backend
    if not capabilities(game_backend()).touch_actions_only:
        rt.log("走路探针只在安卓模拟器上运行，没有执行。")
        return False
    from app_paths import DATA_ROOT
    from host.android.minimap import MinimapReader
    from host.android.move import Goal, Walker
    try:
        params = Params(options)
    except ValueError as error:
        rt.log(f"{error}，没有执行。")
        return False
    folder = DATA_ROOT / "debug" / "android_walk" / datetime.now().strftime("%Y%m%d-%H%M%S")
    io = DiagnosticIo(rt, folder)
    io.record("start", {"map": params.map, "hint": params.hint, "target": params.target, "timeout": params.timeout,
                        "leash": params.leash, "radius": params.radius}, None)
    walker = Walker(io, MinimapReader(params.map), leash=(*params.hint, params.leash))
    result = walker.walk([Goal(*params.target, params.radius)], params.hint, params.timeout)

    def outcome():  # the loop's own stop check and its deadline, asked again after every blocking write
        if io.stopped():
            return "stopped", "已停止。"
        if result.ok and walker.deadline is not None and time.monotonic() > walker.deadline:
            return "timeout", "移动超时（写完记录时已经超过了限时），不算到达。"
        return result.outcome, result.message

    def terminal(decided, corrects=None):
        io.record("result", {"outcome": decided[0], "message": decided[1], "position": result.position,
                             "attempted": result.attempted, "confirmed": result.confirmed,
                             **({"corrects": corrects} if corrects else {})}, None)

    decided = outcome()
    terminal(decided)
    for _write in range(2):  # after the result write, then after the message file's copy of the report
        now = outcome()
        if now != decided:  # walk.jsonl's last record always says what is returned and shown
            terminal(now, corrects=decided[0])
            decided = now
        if decided[0] == "stopped":
            return False
        text = f"走路探针：{decided[1]}（发出 {result.attempted} 步，完成 {result.confirmed} 步）"
        if _write == 0:
            # The message file's copy (blocking I/O), not shown yet. Not Runtime.log: when its file append fails it
            # displays the text at once; here a failed append is followed by the same rechecks before any display.
            from message_log import write_message
            write_message(text, None)
    _show(rt, text)  # shown only now: both rechecks passed immediately before it
    return decided[0] == "arrived" and not io.stopped()


def _show(rt, text):
    """Display an already logged report (Runtime.log's display step, without writing the message file again)."""
    from task_control import message_override
    rt.context.run_task("MaaNikki_Daily_Message", message_override({"Node.Action.Starting": text}))
