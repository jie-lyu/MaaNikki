"""Win32 target: identity and focus of the connected game window.

Moved from navigation/controller.py (ForegroundInput's window checks) and
task_control.py (bringing the game back to the foreground).
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import re

from navigation.models import GameWindowClosed, NavigationError


class Win32Target:
    """The HWND from Controller.info, verified to be the Infinity Nikki client."""

    def __init__(self, info: dict):
        hwnd = info.get("hwnd") if isinstance(info, dict) else None
        if (not isinstance(info, dict) or info.get("type") != "win32" or type(hwnd) is not int
                or not 0 < hwnd < 2**(ctypes.sizeof(ctypes.c_void_p)*8)):
            raise NavigationError("当前连接没有有效的 Windows 游戏窗口，导航已停止。")
        self.handle = self.hwnd = hwnd
        from maa.define import MaaWin32ScreencapMethodEnum
        capture_method = info.get("screencap_method", 0)
        screen_methods = int(MaaWin32ScreencapMethodEnum.Foreground
                             | MaaWin32ScreencapMethodEnum.DXGI_DesktopDup
                             | MaaWin32ScreencapMethodEnum.GDI)
        self.capture_requires_foreground = bool(type(capture_method) is int
                                                and capture_method & screen_methods)
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.GetForegroundWindow.argtypes = []
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.IsHungAppWindow.argtypes = [wintypes.HWND]
        self.user32.IsHungAppWindow.restype = wintypes.BOOL
        self.user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        self.user32.MapVirtualKeyW.restype = wintypes.UINT
        self.user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.PostMessageW.restype = wintypes.BOOL
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetClassNameW.restype = ctypes.c_int
        if not self.user32.IsWindow(self.hwnd):
            raise GameWindowClosed("连接的游戏窗口已经关闭，请重新连接。")
        class_name = ctypes.create_unicode_buffer(257)
        if (not self.user32.GetClassNameW(self.hwnd, class_name, len(class_name))
                or class_name.value != "UnrealWindow"):
            raise NavigationError("当前连接的窗口不是游戏客户端，请重新连接无限暖暖。")
        length = self.user32.GetWindowTextLengthW(self.hwnd)
        if not 0 < length <= 256:
            raise NavigationError("无法确认连接窗口的标题，导航已停止。")
        title = ctypes.create_unicode_buffer(length+1)
        if (not self.user32.GetWindowTextW(self.hwnd, title, len(title))
                or not re.fullmatch(r"\s*(Infinity\s*Nikki|无限暖暖|無限暖暖)\s*", title.value)):
            raise NavigationError("当前连接的窗口不是无限暖暖，导航已停止，请检查连接对象。")

    def input(self):
        """InputBackend bound to this window."""
        from host.win32.input import Win32Input
        return Win32Input(self)

    def geometry(self):
        """Capture geometry of this window."""
        from host.win32.geometry import Win32Geometry
        return Win32Geometry(self)

    def exists(self) -> bool:
        return bool(self.user32.IsWindow(self.hwnd))

    def minimized(self) -> bool:
        return bool(self.user32.IsIconic(self.hwnd))

    def foreground(self) -> bool:
        return bool(
            self.user32.IsWindow(self.hwnd)
            and not self.user32.IsIconic(self.hwnd)
            and self.user32.GetForegroundWindow() == self.hwnd
        )

    def focus_details(self) -> dict:
        hwnd = self.user32.GetForegroundWindow()
        text = ctypes.create_unicode_buffer(1024)
        if hwnd:
            self.user32.GetWindowTextW(hwnd, text, len(text))
        return {"game_hwnd": self.hwnd, "foreground_hwnd": int(hwnd or 0), "foreground_title": text.value}

    def hung(self) -> bool:
        return bool(self.user32.IsWindow(self.hwnd) and self.user32.IsHungAppWindow(self.hwnd))

    def bring_to_foreground(self, controller) -> bool:
        """Restore and activate the game; Alt key-up through Maa first, then a thread-attach fallback."""
        if self.foreground():
            return True
        user32 = self.user32
        hwnd = self.hwnd
        try:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        except Exception:
            pass
        try:
            controller.post_key_up(18).wait()
        except Exception:
            pass
        if self.foreground():
            return True
        # Fall back to thread-queue attach + Win32 activation if focus lock blocks Seize key-up.
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=False)
            kernel32.GetCurrentThreadId.argtypes = []
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
            user32.AttachThreadInput.restype = wintypes.BOOL
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.ShowWindow.restype = wintypes.BOOL
            user32.BringWindowToTop.argtypes = [wintypes.HWND]
            user32.BringWindowToTop.restype = wintypes.BOOL
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.SetForegroundWindow.restype = wintypes.BOOL
            current_thread = kernel32.GetCurrentThreadId()
            fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            target_thread = user32.GetWindowThreadProcessId(hwnd, None)
            attached = []
            try:
                for thread in dict.fromkeys((fg_thread, target_thread)):
                    if thread and thread != current_thread and user32.AttachThreadInput(current_thread, thread, True):
                        attached.append(thread)
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                for thread in reversed(attached):
                    user32.AttachThreadInput(current_thread, thread, False)
        except Exception:
            pass
        return self.foreground()
