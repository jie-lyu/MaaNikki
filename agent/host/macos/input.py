"""macOS input primitives behind ForegroundInput's guaranteed release.

Keys arrive as the agent's Win32 virtual-key ids and are translated once, in host.macos.keys.
Releases are posted straight to the game process, so they work without the agent channel and
never touch the user's front application. Scene mouse buttons (left, right, middle) are pressed by
the control unit where the cursor already is (patch level 3), which also tracks and releases them;
the extra buttons, window-message keys, raw relative moves and route recording refuse on macOS.
"""
from __future__ import annotations

from host.macos.keys import UnmappedKey, translate
from host.macos.target import SCENE_BUTTONS_PATCH_LEVEL, SCENE_PRESS_PRESSURE
from navigation.models import NavigationError

SCENE_BUTTONS = (0, 1, 2)  # left, right, middle: the unit's contacts


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
        # Cleanup only: the unit holds the press and releases it on its own at the end of the task
        # and when the controller goes away, so a failed request here leaves nothing pressed for long.
        controller = self.target.controller
        if controller is None or button not in SCENE_BUTTONS:
            return None
        try:
            controller.post_touch_up(contact=button).wait()
        except Exception:
            pass
        return None

    def press_mouse(self, button: int) -> bool:
        # A press only, where the cursor already is: the unit refuses unless the game is in front
        # and the cursor is over the connected window. Released through post_touch_up(button).
        controller = self.target.controller
        if (controller is None or button not in SCENE_BUTTONS
                or self.target.patch_level < SCENE_BUTTONS_PATCH_LEVEL):
            return False
        # The binding's order is (x, y, contact, pressure): name them, a positional slip presses another button.
        return bool(controller.post_touch_down(x=0, y=0, contact=button,
                                               pressure=SCENE_PRESS_PRESSURE).wait().succeeded)

    def post_window_key(self, key: int, *, down: bool) -> bool:
        return False

    def relative_move(self, dx: int, dy: int) -> bool:
        return False

    def last_error(self) -> int:
        return 0

    def physical_keys(self):
        raise NavigationError("Mac 预览版暂不支持路线录制。")
