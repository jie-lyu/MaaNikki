"""Win32 window and process operations for starting the game.

Moved from start_game.py (the start-game pretask: finding, restoring and
focusing the game and launcher windows) and from action/start_game.py (the
in-agent startup checks for a live, responsive game window).
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import time


def ensure_unminimized(hwnd) -> bool:
    """Ensure a window is not minimized without stealing foreground focus."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    if not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        time.sleep(0.15)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE fallback
            time.sleep(0.15)
    return bool(user32.IsWindow(hwnd) and not user32.IsIconic(hwnd))


def focus_window(hwnd):
    """Activate once at startup, then verify without repeatedly stealing focus."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    if not user32.IsWindow(hwnd):
        return False
    if user32.GetForegroundWindow() == hwnd and not user32.IsIconic(hwnd):
        return True

    current_thread = kernel32.GetCurrentThreadId()

    def activate():
        foreground_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        attached = []
        try:
            # The helper is launched in the background. Share the foreground input
            # queue during activation so Windows can grant it foreground access.
            for thread in dict.fromkeys((foreground_thread, target_thread)):
                if thread and thread != current_thread and user32.AttachThreadInput(current_thread, thread, True):
                    attached.append(thread)
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE; preserve maximized windows.
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            for thread in reversed(attached):
                user32.AttachThreadInput(current_thread, thread, False)

    activate()
    deadline, stable_since, last_attempt = time.monotonic()+3, None, time.monotonic()
    while time.monotonic() < deadline:
        if not user32.IsWindow(hwnd):
            return False
        now = time.monotonic()
        if user32.GetForegroundWindow() == hwnd and not user32.IsIconic(hwnd):
            stable_since = now if stable_since is None else stable_since
            if now-stable_since >= .15:
                return True
        else:
            stable_since = None
            if now-last_attempt >= .5:
                activate()
                last_attempt = now
        time.sleep(.05)
    return False


def game_window():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    found = []

    @callback_type
    def visit(hwnd, _):
        title = ctypes.create_unicode_buffer(257)
        class_name = ctypes.create_unicode_buffer(257)
        if (user32.IsWindowVisible(hwnd)
                and user32.GetClassNameW(hwnd, class_name, len(class_name))
                and class_name.value == "UnrealWindow"
                and user32.GetWindowTextW(hwnd, title, len(title))
                and re.fullmatch(r"\s*(Infinity\s*Nikki|无限暖暖|無限暖暖)\s*", title.value)):
            rect = wintypes.RECT()
            if user32.IsIconic(hwnd):
                # Minimized Unreal windows may report an empty client area.
                found.append(int(hwnd))
            elif user32.GetClientRect(hwnd, ctypes.byref(rect)):
                width, height = rect.right-rect.left, rect.bottom-rect.top
                if width >= 640 and height >= 360:
                    found.append(int(hwnd))
        return True

    if not user32.EnumWindows(visit, 0):
        raise OSError("无法枚举游戏窗口。")
    return found[0] if found else None


class LauncherWindows:
    def __init__(self, folder):
        self.folder = folder.resolve()
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.GetClientRect.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsIconic.restype = wintypes.BOOL
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

    def find(self):
        found = []

        @self.callback_type
        def visit(hwnd, _):
            rect = wintypes.RECT()
            if not (self.user32.IsWindowVisible(hwnd) and self.user32.GetClientRect(hwnd, ctypes.byref(rect))):
                return True
            width, height = rect.right-rect.left, rect.bottom-rect.top
            if width < 640 or height < 360:
                return True
            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = self.kernel32.OpenProcess(0x1000, False, pid.value)
            if process:
                try:
                    name, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
                    if self.kernel32.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(length)):
                        path = Path(name.value)
                        if (path.name.lower() in {"xstarter.exe", "launcher.exe"}
                                and path.is_relative_to(self.folder)):
                            found.append((width*height, int(hwnd)))
                finally:
                    self.kernel32.CloseHandle(process)
            return True

        if not self.user32.EnumWindows(visit, 0):
            raise OSError("无法查找官方启动器窗口。")
        return max(found)[1] if found else None

    def foreground(self, hwnd):
        return self.user32.GetForegroundWindow() == hwnd and not self.user32.IsIconic(hwnd)

    def focus(self, hwnd):
        if not focus_window(hwnd):
            raise RuntimeError("未能自动将官方启动器切到前台，请检查窗口状态和权限后重试。")


_WM_NULL = 0x0000


_SMTO_BLOCK = 0x0001


_SMTO_ABORTIFHUNG = 0x0002


_ERROR_TIMEOUT = 1460


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


_STILL_ACTIVE = 259


_U32 = None


_K32 = None


def _startup_user32():
    global _U32
    if os.name != "nt":
        return None
    if _U32 is None:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.IsWindow.argtypes = [wintypes.HWND]
        u32.IsWindow.restype = wintypes.BOOL
        u32.IsHungAppWindow.argtypes = [wintypes.HWND]
        u32.IsHungAppWindow.restype = wintypes.BOOL
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        u32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        u32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        _U32 = u32
    return _U32


def _startup_kernel32():
    global _K32
    if os.name != "nt":
        return None
    if _K32 is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        _K32 = k32
    return _K32


def _is_window_process_alive(u32, hwnd) -> bool:
    if u32 is None or not hwnd or not u32.IsWindow(hwnd):
        return False
    k32 = _startup_kernel32()
    if k32 is None:
        return True
    pid = wintypes.DWORD(0)
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return True
    proc = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not proc:
        return bool(u32.IsWindow(hwnd))
    try:
        code = wintypes.DWORD(0)
        if k32.GetExitCodeProcess(proc, ctypes.byref(code)):
            return code.value == _STILL_ACTIVE and bool(u32.IsWindow(hwnd))
        return bool(u32.IsWindow(hwnd))
    finally:
        k32.CloseHandle(proc)


def _is_window_unresponsive(u32, hwnd) -> bool:
    if u32 is None or not hwnd:
        return False
    if u32.IsHungAppWindow(hwnd):
        return True
    ctypes.set_last_error(0)
    msg_res = ctypes.c_size_t(0)
    ok = u32.SendMessageTimeoutW(
        hwnd,
        _WM_NULL,
        0,
        0,
        _SMTO_BLOCK | _SMTO_ABORTIFHUNG,
        80,
        ctypes.byref(msg_res),
    )
    if ok != 0:
        return False
    return ctypes.get_last_error() == _ERROR_TIMEOUT and bool(u32.IsWindow(hwnd))
