"""Android target: whether the game's activity can take input on the emulator.

There is no desktop window. "Exists" means the game's process runs on the device, "foreground" that its window has
input focus there (mCurrentFocus; mFocusedApp still names the game behind a system dialog or the launcher, so it
does not count). Both are read through the controller's own ADB shell, so they describe the device the controller
is connected to. Input arrives in a later step.
"""
from __future__ import annotations

import re

from navigation.models import NavigationError

GAME_PACKAGE = "com.papegames.infinitynikki"
_FOCUS = re.compile(r"\su\d+\s+([\w.]+)/")


def focused_package(output: str | None) -> str | None:
    """The package of the window with input focus from `dumpsys window | grep mCurrentFocus`, or None."""
    packages = set()
    for line in (output or "").splitlines():
        if "mCurrentFocus" in line:
            match = _FOCUS.search(line)
            packages.add(match.group(1) if match else None)
    return packages.pop() if len(packages) == 1 else None


def check_controller_info(info) -> str:
    """The ADB serial of a connection the Android backend can drive; raises NavigationError otherwise."""
    serial = info.get("adb_serial") if isinstance(info, dict) else None
    if not isinstance(info, dict) or info.get("type") != "adb" or not isinstance(serial, str) or not serial:
        raise NavigationError("当前连接不是安卓模拟器，导航已停止。")
    return serial


class AndroidTarget:
    """The game on the emulator the controller is connected to."""

    # ADB capture reads the emulator's display; desktop windows on top of the emulator do not matter
    # (2026-11-25: frames stayed fresh with BlueStacks fully covered by other windows).
    capture_requires_foreground = False
    # Releases go through the controller's input tool, which needs no focus.
    release_ignores_focus = True
    handle = 0

    def __init__(self, info: dict, controller):
        self.serial = check_controller_info(info)
        self.controller = controller

    def _shell(self, command: str) -> str | None:
        try:
            job = self.controller.post_shell(command, 5000).wait()
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            return None
        return job.get() if getattr(job, "succeeded", False) else None

    def focus_package(self) -> str | None:
        return focused_package(self._shell("dumpsys window | grep mCurrentFocus"))

    def exists(self) -> bool:
        output = self._shell(f"pidof {GAME_PACKAGE}")
        return bool(output and output.strip())

    def minimized(self) -> bool:
        return False  # the emulator's own window state does not stop capture or input over ADB (risk 1: minimised unverified)

    def foreground(self) -> bool:
        return self.focus_package() == GAME_PACKAGE

    def hung(self) -> bool:
        return False

    def bring_to_foreground(self, controller) -> bool:
        return False  # never start, resume or switch apps on the device

    def focus_details(self) -> dict:
        return {"backend": "android", "serial": self.serial, "focused_package": self.focus_package()}

    def input(self):
        raise NavigationError("安卓模拟器的游戏操作还在开发中，暂不发送输入。")

    def geometry(self):
        raise NavigationError("安卓模拟器没有电脑上的游戏窗口。")
