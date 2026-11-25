"""Android target: whether the game's activity can take input on the emulator.

There is no desktop window. "Exists" means the game's process runs on the device, "foreground" that its window has
input focus there (mCurrentFocus; mFocusedApp still names the game behind a system dialog or the launcher, so it
does not count). Both are read through the controller's own ADB shell, so they describe the device the controller
is connected to.

Desktop windows on top of the emulator do not matter for capture (2026-11-25 survey), but which Android app is in
front does: with the launcher or another app in front, the frame is not the game. So a frame counts only while the
game has focus (capture_requires_foreground).

Input arrives in a later step. Until then input() gives primitives that send nothing, so the session and
navigation code can hold a target, wait for focus and release (nothing) without any key reaching the device.
"""
from __future__ import annotations

import re
import threading

from navigation.models import NavigationError

GAME_PACKAGE = "com.papegames.infinitynikki"
_FOCUS = re.compile(r"\su\d+\s+([\w.]+)/")
# The controller keeps one shell output: a second request before the first result is read would replace it.
# Every shell request of this process holds this lock from posting through reading the result.
_SHELL_LOCK = threading.Lock()


def focused_package(output: str | None) -> str | None:
    """The package of the window with input focus from `dumpsys window | grep mCurrentFocus`, or None."""
    packages = set()
    for line in (output or "").splitlines():
        if "mCurrentFocus" in line:
            match = _FOCUS.search(line)
            packages.add(match.group(1) if match else None)
    return packages.pop() if len(packages) == 1 else None


def process_state(output: str | None) -> bool | None:
    """From `pidof <package>; echo __exit=$?`: True when a numeric pid is reported, False when pidof confirmed
    there is none, None when the result cannot be read (it then says nothing about the game)."""
    if output is None:
        return None
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    status = next((line for line in lines if line.startswith("__exit=")), None)
    pids = [line for line in lines if not line.startswith("__exit=")]
    if status == "__exit=0" and pids and all(part.isdigit() for line in pids for part in line.split()):
        return True
    if status == "__exit=1" and not pids:
        return False
    return None


def check_controller_info(info) -> str:
    """The ADB serial of a connection the Android backend can drive; raises NavigationError otherwise."""
    serial = info.get("adb_serial") if isinstance(info, dict) else None
    if not isinstance(info, dict) or info.get("type") != "adb" or not isinstance(serial, str) or not serial:
        raise NavigationError("当前连接不是安卓模拟器，导航已停止。")
    return serial


class NoInput:
    """Input primitives for a target without a keyboard, mouse or relative pointer: presses are refused before
    anything counts as held (UnsupportedInput), releases send nothing."""

    # ForegroundInput.mouse_down asks this before it marks a button held, so no touch-up is ever sent for it.
    supports_scene_buttons = False

    def force_key_up(self, key: int) -> None:
        return None

    def force_mouse_up(self, button: int) -> None:
        return None

    def press_mouse(self, button: int) -> bool:
        from host import UnsupportedInput
        raise UnsupportedInput("安卓模拟器上没有电脑鼠标按键，这一步暂不支持。")

    def post_window_key(self, key: int, *, down: bool) -> bool:
        return False

    def relative_move(self, dx: int, dy: int) -> bool:
        from host import UnsupportedInput
        raise UnsupportedInput("安卓模拟器上没有相对鼠标移动，镜头需要用滑动来转，这一步暂不支持。")

    def last_error(self) -> int:
        return 0

    def physical_keys(self):
        raise NavigationError("安卓模拟器没有电脑键盘输入。")


class AndroidTarget:
    """The game on the emulator the controller is connected to."""

    capture_requires_foreground = True
    # Nothing is pressed yet; releases never wait for focus.
    release_ignores_focus = True
    handle = 0

    def __init__(self, info: dict, controller):
        self.serial = check_controller_info(info)
        self.controller = controller

    def _shell(self, command: str) -> str | None:
        with _SHELL_LOCK:
            try:
                job = self.controller.post_shell(command, 5000).wait()
                return job.get() if getattr(job, "succeeded", False) else None
            except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
                return None

    def focus_package(self) -> str | None:
        return focused_package(self._shell("dumpsys window | grep mCurrentFocus"))

    def exists(self) -> bool:
        # Only a confirmed absence ends the task as "game closed"; an unreadable answer is not one. Whether the
        # task then runs or pauses is the focus check's decision: positive game focus is enough to continue.
        return process_state(self._shell(f"pidof {GAME_PACKAGE}; echo __exit=$?")) is not False

    def minimized(self) -> bool:
        return False  # the emulator window's own state does not stop ADB capture or input (minimised: unverified)

    def foreground(self) -> bool:
        return self._read_focus() == GAME_PACKAGE

    def _read_focus(self) -> str | None:
        """The focused package, with a negative reading recorded in the same locked step: no tap can slip in
        between seeing another app in front and making earlier frames stale."""
        from host import device_input
        state = device_input()
        with state.lock:
            package = self.focus_package()
            if package != GAME_PACKAGE:
                state.observed_loss()
            return package

    def hung(self) -> bool:
        return False

    def bring_to_foreground(self, controller) -> bool:
        return False  # never start, resume or switch apps on the device

    def focus_details(self) -> dict:
        return {"backend": "android", "serial": self.serial, "focused_package": self._read_focus()}

    def input(self) -> NoInput:
        return NoInput()

    def geometry(self):
        raise NavigationError("安卓模拟器没有电脑上的游戏窗口。")
