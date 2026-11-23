"""Rules about Win32 controller definitions and connected controllers; pure, importable anywhere."""
from __future__ import annotations

from maa.define import MaaWin32ScreencapMethodEnum


def supports_foreground_inputs(controller) -> bool:
    """interface.json Win32 controllers whose mouse and keyboard both seize the input (SendInput)."""
    if not isinstance(controller, dict) or controller.get("type") != "Win32":
        return False
    win32 = controller.get("win32")
    allowed = {"Seize", "SeizeWithBlockInput"}
    return (isinstance(win32, dict) and win32.get("mouse") in allowed
            and win32.get("keyboard") in allowed)


def capture_is_window_only(info) -> bool:
    """Whether the controller captures the game window alone (FramePool/PrintWindow), never the desktop.

    An independent overlay is then outside the game's capture and may stay visible to desktop
    streaming and recording; otherwise it must be excluded from capture.
    """
    capture_method = info.get("screencap_method", 0) if isinstance(info, dict) else 0
    background = int(MaaWin32ScreencapMethodEnum.Background)
    return type(capture_method) is int and capture_method > 0 and not capture_method & ~background
