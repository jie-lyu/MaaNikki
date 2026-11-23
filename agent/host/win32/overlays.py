"""Win32 overlay windows: task HUD, route minimap overlay and big-map overlay."""
from host.win32.hud import GameHudOverlay
from host.win32.map_overlay import OverlayWindow
from host.win32.route_overlay import RouteMinimapOverlay

__all__ = ["GameHudOverlay", "OverlayWindow", "RouteMinimapOverlay", "capture_is_window_only"]


def capture_is_window_only(info) -> bool:
    """Whether the controller captures the game window alone (FramePool/PrintWindow), never the desktop.

    An independent overlay is then outside the game's capture and may stay visible to desktop
    streaming and recording; otherwise it must be excluded from capture.
    """
    # Imported here, not at module level: importing maa opens the MaaFramework libraries, which
    # fails wherever MAAFW_BINARY_PATH is not set (the packaged runtime ships no maa/bin).
    from maa.define import MaaWin32ScreencapMethodEnum
    capture_method = info.get("screencap_method", 0) if isinstance(info, dict) else 0
    background = int(MaaWin32ScreencapMethodEnum.Background)
    return type(capture_method) is int and capture_method > 0 and not capture_method & ~background
