"""macOS target: identity and focus of the connected Infinity Nikki window.

Controller.info comes from MaaNikki's patched control unit (native/macos-control-unit/):
type "macos", the CGWindowID, the display-window capture and hybrid input methods, and
maanikki_patch. The official unit captures the wrong area and cannot send clicks the game accepts,
so a connection without the patch, or with other methods, is refused here before any task runs.

The game process owns many windows; the target is the one whose owner's bundle id is the game's
and whose trimmed title is the game's title.
"""
from __future__ import annotations

import re

from navigation.models import GameWindowClosed, NavigationError

GAME_BUNDLE_ID = "com.papegames.infinitynikki"
GAME_TITLE = re.compile(r"\s*(Infinity\s*Nikki|无限暖暖|無限暖暖)\s*")
SCREENCAP_DISPLAY_WINDOW = 1 << 1
INPUT_HYBRID = (1 << 0) | (1 << 1)  # GlobalEvent | PostToPid
MIN_PATCH_LEVEL = 2  # 2: Controller.info carries the geometry_changed latch


def check_controller_info(info) -> int:
    """The CGWindowID of a connection the macOS backend can drive; raises NavigationError otherwise."""
    window_id = info.get("window_id") if isinstance(info, dict) else None
    if not isinstance(info, dict) or info.get("type") != "macos" or type(window_id) is not int or not 0 < window_id < 2**32:
        raise NavigationError("当前连接没有有效的 Mac 游戏窗口，导航已停止。")
    patch = info.get("maanikki_patch")
    if type(patch) is not int:
        raise NavigationError("当前使用的是官方 Mac 控制模块，截图与点击不适用于本游戏；请使用 MaaNikki 自带的控制模块。")
    if patch < MIN_PATCH_LEVEL:
        raise NavigationError("MaaNikki 自带的 Mac 控制模块版本过旧，请更新后重新连接游戏。")
    if info.get("screencap_method") != SCREENCAP_DISPLAY_WINDOW or info.get("input_method") != INPUT_HYBRID:
        raise NavigationError("Mac 控制器的截图或输入方式不受支持，请在 MaaNikki 中重新连接游戏。")
    if not isinstance(info.get("geometry_changed"), bool):
        raise NavigationError("Mac 控制模块没有报告窗口几何状态，请更新 MaaNikki 自带的控制模块后重新连接。")
    return window_id


class MacOSTarget:
    """The game window from Controller.info, verified by owner bundle id and title."""

    # Display-window capture renders the game window itself: other windows on top do not matter,
    # but it must be on screen (not minimized, not on another Space).
    capture_requires_foreground = False
    # The patched unit posts releases to the game process without activating anything, and it
    # tracks what it pressed: releasing around it would leave it believing a key is still down.
    release_ignores_focus = True

    def __init__(self, info: dict):
        self.handle = self.window_id = check_controller_info(info)
        from host.macos import quartz
        self.quartz = quartz
        window = quartz.window(self.window_id)
        if window is None:
            raise GameWindowClosed("连接的游戏窗口已经关闭，请重新连接。")
        if quartz.bundle_identifier(window.pid) != GAME_BUNDLE_ID:
            raise NavigationError("当前连接的窗口不是游戏客户端，请重新连接无限暖暖。")
        if not GAME_TITLE.fullmatch(window.title or ""):
            raise NavigationError("当前连接的窗口不是无限暖暖，导航已停止，请检查连接对象。")
        self.pid = window.pid

    def _window(self):
        window = self.quartz.window(self.window_id)
        # A reused window id under another process is not the game.
        return window if window is not None and window.pid == self.pid else None

    def input(self):
        from host.macos.input import MacOSInput
        return MacOSInput(self)

    def geometry(self):
        return MacOSGeometry(self)

    def exists(self) -> bool:
        return self._window() is not None

    def minimized(self) -> bool:
        window = self._window()
        return window is None or not window.on_screen

    def foreground(self) -> bool:
        window = self._window()
        return bool(window is not None and window.on_screen and self.quartz.front_pid() == self.pid)

    def focus_details(self) -> dict:
        front = self.quartz.front_pid()
        path = self.quartz.process_path(front) if front > 0 else ""
        return {"game_window_id": self.window_id, "game_pid": self.pid, "front_pid": front,
                "front_app": path.rsplit("/", 1)[-1]}

    def hung(self) -> bool:
        # No public API reports an unresponsive app; the control unit times out its own calls.
        return False

    def bring_to_foreground(self, controller) -> bool:
        """The preview never activates the game itself; the user brings it back."""
        return self.foreground()


class MacOSGeometry:
    """The game window on the desktop, in global points (top-left origin)."""

    def __init__(self, target: MacOSTarget):
        self.target = target

    def client_size(self) -> tuple[int, int] | None:
        window = self.target._window()
        return (round(window.bounds[2]), round(window.bounds[3])) if window else None

    def client_screen_rect(self) -> tuple[int, int, int, int] | None:
        window = self.target._window()
        if window is None:
            return None
        x, y, width, height = window.bounds
        return round(x), round(y), round(x + width), round(y + height)

    def monitor_work_area(self) -> tuple[int, int, int, int] | None:
        # The control unit refuses clicks that the Dock or menu bar would receive at click time.
        return None
