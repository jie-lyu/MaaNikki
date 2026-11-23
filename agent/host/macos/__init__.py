"""macOS backend for the foreground preview. Imported only on macOS, through the accessors in host/__init__.py."""
from __future__ import annotations

from host import Capabilities

# Foreground only: no background input, overlays, window resizing, launching or closing the game,
# clipboard or credential store yet. Click targets are bounded by the frame; the patched control
# unit refuses a click that the Dock, the menu bar or another window would receive.
CAPABILITIES = Capabilities("macos", foreground_input=True)


# Pipeline entries of the tasks the preview runs, checked by the agent before any task code runs
# (agent/registration.py), so saved presets, scheduled runs and feature entries are refused alike.
# Each is added only after it passed on a real Mac (docs/macos-support-plan.md, task order).
PREVIEW_TASK_ENTRIES = frozenset({
    "MaaNikki_Mailbox",
})

# interface.json names of the patched control unit's methods (client/src/types/maa.ts maps them).
SCREENCAP_NAME = "DisplayWindow"
INPUT_NAME = "Hybrid"


def supports_foreground_inputs(controller) -> bool:
    """interface.json MacOS controllers that use the patched unit's display-window capture and hybrid input."""
    if not isinstance(controller, dict) or controller.get("type") != "MacOS":
        return False
    macos = controller.get("macos")
    return isinstance(macos, dict) and macos.get("screencap") == SCREENCAP_NAME and macos.get("input") == INPUT_NAME
