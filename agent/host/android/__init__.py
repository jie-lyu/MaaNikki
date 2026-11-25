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

CAPABILITIES = Capabilities("android", frames_follow_device_focus=True)


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


def task_allowed(entry) -> bool:
    """Whether the Android list lets ``entry`` run (verified or experimental); an unreadable list allows nothing."""
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
