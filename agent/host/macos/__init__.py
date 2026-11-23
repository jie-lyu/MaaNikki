"""macOS backend for the foreground preview. Imported only on macOS, through the accessors in host/__init__.py."""
from __future__ import annotations

import json
from pathlib import Path

from host import Capabilities

# Foreground only: no background input, overlays, window resizing, launching or closing the game,
# clipboard or credential store yet. Click targets are bounded by the frame; the patched control
# unit refuses a click that the Dock, the menu bar or another window would receive.
CAPABILITIES = Capabilities("macos", foreground_input=True)


# Pipeline entries of the tasks the preview runs (preview_tasks.json beside this file). The agent
# checks the running task's entry before any task code (agent/registration.py); the client checks
# every entry before submitting it, which also covers MXU's built-in tasks. Each entry is added only
# after the task passed on a real Mac (docs/macos-support-plan.md, task order).
PREVIEW_TASK_ENTRIES = frozenset(
    json.loads((Path(__file__).with_name("preview_tasks.json")).read_text(encoding="utf-8"))["entries"])

# interface.json names of the patched control unit's methods (client/src/types/maa.ts maps them).
SCREENCAP_NAME = "DisplayWindow"
INPUT_NAME = "Hybrid"


def supports_foreground_inputs(controller) -> bool:
    """interface.json MacOS controllers that use the patched unit's display-window capture and hybrid input."""
    if not isinstance(controller, dict) or controller.get("type") != "MacOS":
        return False
    macos = controller.get("macos")
    return isinstance(macos, dict) and macos.get("screencap") == SCREENCAP_NAME and macos.get("input") == INPUT_NAME
