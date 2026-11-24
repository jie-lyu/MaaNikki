"""macOS backend for the foreground preview. Imported only on macOS, through the accessors in host/__init__.py."""
from __future__ import annotations

import json

from host import Capabilities

# Foreground only: no background input, overlays, window resizing, launching or closing the game or
# credential store yet. Click targets are bounded by the frame; the patched control unit refuses a
# click that the Dock, the menu bar or another window would receive. Clipboard text (redeem codes)
# goes through the general NSPasteboard (host/macos/clipboard.py).
CAPABILITIES = Capabilities("macos", foreground_input=True, clipboard=True)


# Pipeline entries of the tasks the preview runs, from agent/host/macos/preview_tasks.json under
# the resource root, the same file the client reads before submitting tasks (which also covers MXU's
# built-in tasks). The agent checks the running task's entry before any task code
# (agent/registration.py). Each entry is added only after the task passed on a real Mac
# (docs/macos-support-plan.md, task order).
PREVIEW_TASKS_FILE = ("agent", "host", "macos", "preview_tasks.json")


# Development switch: comma-separated task entries allowed in supervised device tests on top of the
# verified preview list. A release client removes it from the agent's environment.
DEV_EXTRA_TASKS_ENV = "MAANIKKI_MACOS_EXTRA_TASKS"


def dev_extra_task_entries() -> frozenset[str]:
    import os
    return frozenset(item.strip() for item in os.environ.get(DEV_EXTRA_TASKS_ENV, "").split(",") if item.strip())


VERIFIED, EXPERIMENTAL = "verified", "experimental"


def parse_task_tiers(data) -> tuple[frozenset[str], frozenset[str], dict[str, str]]:
    """(verified, experimental, excluded) from the list file's JSON; ValueError unless every field is well formed.

    entries is required; experimental and excluded are optional (an older list means no experimental
    tasks). An entry may appear in only one of the three. The client applies the same rules
    (client/src-tauri/src/commands/utils.rs, parse_task_tiers).
    """
    if not isinstance(data, dict):
        raise ValueError("任务清单不是对象")

    def names(key, required):
        value = data.get(key, None if required else [])
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ValueError(f"任务清单的 {key} 无效")
        if len(set(value)) != len(value):
            raise ValueError(f"任务清单的 {key} 有重复")
        return frozenset(value)
    verified, experimental = names("entries", True), names("experimental", False)
    excluded = data.get("excluded", {})
    if (not isinstance(excluded, dict)
            or not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in excluded.items())):
        raise ValueError("任务清单的 excluded 无效")
    if verified & experimental or (verified | experimental) & set(excluded):
        raise ValueError("任务清单里同一入口出现在多个档位")
    return verified, experimental, dict(excluded)


def load_task_list(text: str):
    """The list file's JSON, refusing what Python alone would accept: duplicate keys (the last would silently win)
    and NaN/Infinity constants. The client's reader refuses the same."""
    def no_duplicates(pairs):
        keys = [key for key, _ in pairs]
        if len(set(keys)) != len(keys):
            raise ValueError("任务清单有重复的键")
        return dict(pairs)

    def no_constants(name):
        raise ValueError(f"任务清单含有无效的数值 {name}")
    return json.loads(text, object_pairs_hook=no_duplicates, parse_constant=no_constants)


def _task_list():
    from app_paths import RESOURCE_ROOT
    path = RESOURCE_ROOT.joinpath(*PREVIEW_TASKS_FILE)
    return load_task_list(path.read_text(encoding="utf-8"))


def task_tiers() -> tuple[frozenset[str], frozenset[str], dict[str, str]]:
    return parse_task_tiers(_task_list())


EXPERIMENTAL_STARTED = "本任务尚未在 Mac 上验证（试用）"


def start_notice(entry) -> str | None:
    """The line an experimental task writes to its own log when it starts, with the list's note for it
    (what it may spend or change); None for verified, refused or unknown entries and an unreadable list."""
    try:
        data = _task_list()
        experimental = parse_task_tiers(data)[1]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if entry not in experimental:
        return None
    notes = data.get("notes")
    note = notes.get(entry) if isinstance(notes, dict) else None
    return f"{EXPERIMENTAL_STARTED}：{note}。" if isinstance(note, str) and note else f"{EXPERIMENTAL_STARTED}。"


def refusal_reason(entry) -> str:
    """What to tell the user when the preview refuses ``entry``."""
    try:
        reason = task_tiers()[2].get(entry)
    except (OSError, ValueError, KeyError, TypeError):
        return "无法读取 Mac 任务清单，已跳过。"
    return reason or "Mac 测试预览版暂不开放这个任务，已跳过。"


def task_tier(entry) -> str | None:
    """VERIFIED, EXPERIMENTAL, or None (excluded or unknown); an unreadable list raises."""
    verified, experimental, _ = task_tiers()
    return VERIFIED if entry in verified else EXPERIMENTAL if entry in experimental else None


def preview_task_entries() -> frozenset[str]:
    """Entries verified on a real Mac."""
    return task_tiers()[0]


# interface.json names of the patched control unit's methods (client/src/types/maa.ts maps them).
SCREENCAP_NAME = "DisplayWindow"
INPUT_NAME = "Hybrid"


def supports_foreground_inputs(controller) -> bool:
    """interface.json MacOS controllers that use the patched unit's display-window capture and hybrid input."""
    if not isinstance(controller, dict) or controller.get("type") != "MacOS":
        return False
    macos = controller.get("macos")
    return isinstance(macos, dict) and macos.get("screencap") == SCREENCAP_NAME and macos.get("input") == INPUT_NAME
