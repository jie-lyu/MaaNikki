"""Win32 clipboard text access for redeem codes (moved from daily/redeem_codes.py)."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from task_control import task_time as time


_CF_UNICODETEXT = 13


_GMEM_MOVEABLE = 0x0002


def _setup_win32_clipboard():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    return user32, kernel32


def _open_clipboard(user32) -> bool:
    for _ in range(5):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def get_clipboard_text() -> tuple[bool, str]:
    user32, kernel32 = _setup_win32_clipboard()
    if not user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
        return False, ""
    if not _open_clipboard(user32):
        return False, ""
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return False, ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False, ""
        try:
            return True, ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    user32, kernel32 = _setup_win32_clipboard()
    encoded = (str(text) + "\0").encode("utf-16-le")
    hmem = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(encoded))
    if not hmem:
        return False
    ptr = kernel32.GlobalLock(hmem)
    if not ptr:
        kernel32.GlobalFree(hmem)
        return False
    try:
        ctypes.memmove(ptr, encoded, len(encoded))
    finally:
        kernel32.GlobalUnlock(hmem)
    if not _open_clipboard(user32):
        kernel32.GlobalFree(hmem)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(_CF_UNICODETEXT, hmem):
            kernel32.GlobalFree(hmem)
            return False
        return True
    finally:
        user32.CloseClipboard()
