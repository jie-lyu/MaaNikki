"""The interface.json of the Android emulator development home.

    interface.json                         the repository's, read only (the Windows package copies it unchanged)
    build/macos/interface-overlay.json     only its agent fields: the agent runs from ./python on this Mac
    build/android/interface-overlay.json   the nikki_android controller and the read-only AndroidCheck task

The macOS controller is left out: the Android home links the stock MaaFramework runtime, which cannot drive the
native Mac game (that needs the patched control unit, scripts/macos_dev_home.py).
"""
from __future__ import annotations

import copy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from macos_interface import INTERFACE, OVERLAY as MACOS_OVERLAY, read_json, render  # noqa: E402,F401

ANDROID_OVERLAY = ROOT / "build" / "android" / "interface-overlay.json"


def apply_android_overlay(interface: dict, macos_overlay: dict, android_overlay: dict) -> dict:
    result = copy.deepcopy(interface)
    agents = result["agent"] if isinstance(result["agent"], list) else [result["agent"]]
    for agent in agents:
        agent.update(macos_overlay["agent"])
    result["controller"] = [*result["controller"], *android_overlay["controller"]]
    result["task"] = [*result.get("task", []), *android_overlay["task"]]
    return result


def android_interface() -> dict:
    return apply_android_overlay(read_json(INTERFACE), read_json(MACOS_OVERLAY), read_json(ANDROID_OVERLAY))
