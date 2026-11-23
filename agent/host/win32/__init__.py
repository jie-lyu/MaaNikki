"""Windows backend. Imported only on Windows, through the accessors in host/__init__.py."""
from __future__ import annotations

from host import Capabilities

CAPABILITIES = Capabilities(
    "win32",
    foreground_input=True,
    background_input=True,
    overlay=True,
    tall_window=True,
    work_area_bounds=True,
    pointer_hover=True,
    launch_game=True,
    close_game=True,
    clipboard=True,
    credential_store=True,
)


def supports_foreground_inputs(controller) -> bool:
    """interface.json Win32 controllers whose mouse and keyboard both seize the input (SendInput),
    which lets the agent drive foreground input itself."""
    if not isinstance(controller, dict) or controller.get("type") != "Win32":
        return False
    win32 = controller.get("win32")
    allowed = {"Seize", "SeizeWithBlockInput"}
    return (isinstance(win32, dict) and win32.get("mouse") in allowed
            and win32.get("keyboard") in allowed)
