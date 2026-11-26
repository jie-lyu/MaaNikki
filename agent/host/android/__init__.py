"""Android backend: the game's Android client in an emulator, driven over ADB by MaaFramework's AdbController.

Imported only through the accessors in host/__init__.py, and only on a macOS host for now: the Windows package
does not ship this package (scripts/release.py) until phase F decides how Android support reaches Windows users.
See docs/android-support-plan.md.

There is no game window on this desktop: no overlays, window resizing, launching or closing the game, and no
desktop focus or cursor to manage. Whether the game can take input is a question for the device (host/android/
target.py). Input mapping (touch for named game actions) arrives in a later step; until then the target's input
primitives send nothing and no task that sends input is on the task list. The list parser comes from host.macos,
which the Windows package does not ship either; phase F moves it to a shared module.
"""
from __future__ import annotations

from host import Capabilities

CAPABILITIES = Capabilities("android", capture_needs_device_focus=True, touch_actions_only=True)

# Runtime.action types an Android run may dispatch (exact spelling) and the only fields each may carry; anything
# else (offsets, hover-only swipes, contacts, pressure) would change what reaches the device after the checks.
DISPATCHABLE_ACTIONS = {"Click": frozenset({"target"}), "Swipe": frozenset({"begin", "end", "duration"})}
FRAME_SIZE = (1280, 720)


def touch_point(target):
    """The integer pixel a tap on ``target`` uses: the point itself, or the middle pixel of a rectangle (always
    inside it, also for a one-pixel rectangle on the frame's edge)."""
    if len(target) == 2:
        return [target[0], target[1]]
    x, y, w, h = target
    return [x + (w - 1) // 2, y + (h - 1) // 2]


def check_touch_fields(action, fields) -> None:
    """Raise UnsupportedInput unless ``action`` is a tap or swipe with only its own fields, inside the frame."""
    from host import UnsupportedInput
    allowed = DISPATCHABLE_ACTIONS.get(action)
    if allowed is None:
        raise UnsupportedInput(f"安卓模拟器上不能发送「{action}」，这一步暂不支持。")
    extra = set(fields) - allowed
    if extra:
        raise UnsupportedInput(f"安卓模拟器上的「{action}」不支持参数 {sorted(extra)}，这一步暂不支持。")
    width, height = FRAME_SIZE
    for name in ("target", "begin", "end"):
        if name not in fields:
            continue
        value = fields[name]
        if (not isinstance(value, (list, tuple)) or len(value) not in (2, 4)
                or any(type(v) is not int for v in value)):
            raise UnsupportedInput(f"安卓模拟器上的点击位置 {name}={value!r} 无效。")
        x, y = value[0], value[1]
        if len(value) == 4:
            w, h = value[2], value[3]
            inside = w > 0 and h > 0 and 0 <= x and x + w <= width and 0 <= y and y + h <= height
        else:
            inside = 0 <= x < width and 0 <= y < height
        if not inside:
            raise UnsupportedInput(f"安卓模拟器上的点击位置 {name}={list(value)} 超出画面。")
    if action == "Click" and "target" not in fields:
        raise UnsupportedInput("安卓模拟器上的点击需要明确的位置。")
    if action == "Swipe":
        duration = fields.get("duration", 200)
        if "begin" not in fields or "end" not in fields or type(duration) is not int or not 0 < duration <= 5000:
            raise UnsupportedInput("安卓模拟器上的滑动需要起点、终点和 1–5000 毫秒的时长。")


# The Android task list, same format as the macOS preview list (host/macos/__init__.py parse_task_tiers) and read
# the same strict way: verified entries, experimental ones, excluded ones with the reason, notes.
TASK_LIST_FILE = ("agent", "host", "android", "tasks.json")
EXPERIMENTAL_STARTED = "本任务尚未在安卓模拟器上验证（试用）"


def _task_list():
    from app_paths import RESOURCE_ROOT
    from host.macos import load_task_list
    return load_task_list(RESOURCE_ROOT.joinpath(*TASK_LIST_FILE).read_text(encoding="utf-8"))


def task_tiers():
    from host.macos import parse_task_tiers
    return parse_task_tiers(_task_list())


# Dev-only entries (never in tasks.json, interface.json or an interface overlay): allowed only when the process's
# environment names them in DEV_ENTRIES_ENV, which only scripts/android/runner.py sets, for that one run's agent.
DEV_ENTRIES_ENV = "MAANIKKI_ANDROID_DEV_ENTRIES"
# host.android.walk_probe: supervised device walks; host.android.crystal_route: the 星海 route 3 end to end.
DEV_ENTRIES = frozenset({"MaaNikki_AndroidWalkProbe", "MaaNikki_AndroidCrystalRoute"})


def dev_entries() -> frozenset[str]:
    """The dev-only entries this process may run: those of DEV_ENTRIES named in DEV_ENTRIES_ENV (none by default)."""
    import os
    named = {item.strip() for item in os.environ.get(DEV_ENTRIES_ENV, "").split(",")}
    return DEV_ENTRIES & frozenset(named)


def task_allowed(entry) -> bool:
    """Whether the Android list lets ``entry`` run (verified or experimental); an unreadable list allows nothing.
    A dev-only entry is allowed only when the runner enabled it (dev_entries)."""
    if entry in dev_entries():
        return True
    try:
        verified, experimental, _excluded = task_tiers()
    except (OSError, ImportError, ValueError, KeyError, TypeError):
        return False
    return entry in verified or entry in experimental


def refusal_reason(entry) -> str:
    try:
        reason = task_tiers()[2].get(entry)
    except (OSError, ImportError, ValueError, KeyError, TypeError):
        return "无法读取安卓任务清单，已跳过。"
    return reason or "安卓模拟器暂不开放这个任务，已跳过。"


def start_notice(entry) -> str | None:
    """The line an experimental task writes to its own log when it starts; None otherwise."""
    try:
        data = _task_list()
        from host.macos import parse_task_tiers
        experimental = parse_task_tiers(data)[1]
    except (OSError, ImportError, ValueError, KeyError, TypeError):
        return None
    if entry not in experimental:
        return None
    notes = data.get("notes")
    note = notes.get(entry) if isinstance(notes, dict) else None
    return f"{EXPERIMENTAL_STARTED}：{note}。" if isinstance(note, str) and note else f"{EXPERIMENTAL_STARTED}。"
