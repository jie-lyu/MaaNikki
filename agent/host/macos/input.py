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

    def scene_press_blocked(self) -> bool:
        """True only when the unit is known to refuse a scene press: the cursor is outside the window.

        The unit presses where the cursor is and refuses unless it is over the connected window (it
        never moves the cursor: in the scene that would turn the camera). Optional presses such as
        navigation's middle-button alignment check this first and take their keyboard fallback instead
        of failing the route. Unknown positions return False and leave the decision to the unit.
        """
        point = self.quartz.cursor()
        window = self.target._window()
        if point is None or window is None:
            return False
        x, y, width, height = window.bounds
        return not (x <= point[0] < x + width and y <= point[1] < y + height)

    def post_window_key(self, key: int, *, down: bool) -> bool:
        return False

    def relative_move(self, dx: int, dy: int) -> bool:
        return False

    def last_error(self) -> int:
        return 0

    def physical_keys(self):
        raise NavigationError("Mac 预览版暂不支持路线录制。")
