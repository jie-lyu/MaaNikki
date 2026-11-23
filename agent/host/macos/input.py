"""macOS input primitives behind ForegroundInput's guaranteed release.

Keys arrive as the agent's Win32 virtual-key ids and are translated once, in host.macos.keys.
Releases are posted straight to the game process, so they work without the agent channel and
never touch the user's front application. The preview has no scene mouse buttons, window-message
keys, raw relative moves or route recording on macOS: those primitives refuse.
"""
from __future__ import annotations

from host.macos.keys import UnmappedKey, translate
from navigation.models import NavigationError


class MacOSInput:
    def __init__(self, target):
        self.target = target
        self.quartz = target.quartz
        self.pid = target.pid

    def force_key_up(self, key: int) -> None:
        # Cleanup only: posting to the game process needs neither the agent channel nor focus.
        try:
            self.quartz.post_key(self.pid, translate(key), down=False)
        except UnmappedKey:
            pass  # never pressed through this backend either

    def force_mouse_up(self, button: int) -> None:
        # press_mouse never presses on macOS, so there is nothing to release here; clicks the
        # control unit pressed are released by the unit itself.
        return None

    def press_mouse(self, button: int) -> bool:
        return False

    def post_window_key(self, key: int, *, down: bool) -> bool:
        return False

    def relative_move(self, dx: int, dy: int) -> bool:
        return False

    def last_error(self) -> int:
        return 0

    def physical_keys(self):
        raise NavigationError("Mac 预览版暂不支持路线录制。")
