"""Win32 input primitives behind ForegroundInput's guaranteed release (moved from navigation/controller.py).

Keys are Win32 virtual-key integers, the agent's internal key id.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes


class MouseEvent(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]


class InputPayload(ctypes.Union):
    # Windows x64 INPUT's largest member is MOUSEINPUT (32 bytes).
    _fields_ = [("mouse", MouseEvent), ("alignment", ctypes.c_byte*32)]


class MouseInput(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("payload", InputPayload)]


def send_mouse(user32, flags, *, dx=0, dy=0, data=0):
    event = MouseInput(type=0, payload=InputPayload(mouse=MouseEvent(
        dx=dx, dy=dy, mouseData=data, dwFlags=flags)))
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(MouseInput), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    return user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) == 1


def _window_key_spec(user32, key: int):
    scan = user32.MapVirtualKeyW(key, 4)  # MAPVK_VK_TO_VSC_EX
    extended = (scan >> 8) in (0xE0, 0xE1)
    flags = 1 | ((scan & 0xFF) << 16) | (int(extended) << 24)
    # Alt uses WM_SYSKEY*, other configured keys use WM_KEY*.
    down, up = (0x0104, 0x0105) if key in (18, 164, 165) else (0x0100, 0x0101)
    return down, up, flags


class Win32Input:
    """InputBackend for a Win32Target: releases without the agent channel or refocusing."""

    def __init__(self, target):
        self.target = target
        self.user32 = target.user32
        self.hwnd = target.hwnd

    def force_key_up(self, key: int) -> None:
        # Cleanup only: do not require a working Agent channel or refocus.
        extended = key in {33, 34, 35, 36, 37, 38, 39, 40, 45, 46, 163, 165}
        self.user32.keybd_event(key, 0, 0x0002 | (0x0001 if extended else 0), 0)

    def force_mouse_up(self, button: int) -> None:
        self.user32.mouse_event({0: 0x0004, 1: 0x0010, 2: 0x0040, 3: 0x0100, 4: 0x0100}[button],
                                0, 0, button-2 if button >= 3 else 0, 0)

    def press_mouse(self, button: int) -> bool:
        # Maa Seize touch_down relocates the cursor before pressing. Scene
        # buttons must only press, otherwise that warp can turn the camera.
        flags = {0: 0x0002, 1: 0x0008, 2: 0x0020, 3: 0x0080, 4: 0x0080}[button]
        return send_mouse(self.user32, flags, data=button-2 if button >= 3 else 0)

    def post_window_key(self, key: int, *, down: bool) -> bool:
        down_msg, up_msg, flags = _window_key_spec(self.user32, key)
        msg = down_msg if down else up_msg
        lparam = flags if down else (flags | 0xC0000000)
        return bool(self.user32.PostMessageW(self.hwnd, msg, key, lparam))

    def relative_move(self, dx: int, dy: int) -> bool:
        return send_mouse(self.user32, 0x0001, dx=dx, dy=dy)

    def last_error(self) -> int:
        return ctypes.get_last_error()

    # Raw helpers for BackgroundInput until it moves into this backend.
    def send_mouse(self, flags, *, dx=0, dy=0, data=0) -> bool:
        return send_mouse(self.user32, flags, dx=dx, dy=dy, data=data)

    def window_key_spec(self, key: int):
        return _window_key_spec(self.user32, key)
