"""macOS clipboard text for redeem codes: the general NSPasteboard, the same contract as host/win32/clipboard.py.

get_clipboard_text() -> (had_text, text): (False, "") when the pasteboard holds no text or cannot be
read, so the caller restores nothing. set_clipboard_text(text) -> True when the pasteboard now holds
exactly that text. Like the Win32 module, only plain text is saved and restored.

No PyObjC: the Objective-C runtime is plain C. objc_msgSend is called through one fixed prototype per
message (never as a variadic function, which arm64 passes differently), inside an autorelease pool so
the agent's task thread does not accumulate autoreleased strings. Loaded on first use, only on macOS.
"""
from __future__ import annotations

import ctypes
from functools import lru_cache

_UTF8 = 0x08000100  # kCFStringEncodingUTF8


@lru_cache(maxsize=1)
def _runtime():
    objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.objc_autoreleasePoolPush.argtypes = []
    objc.objc_autoreleasePoolPush.restype = ctypes.c_void_p
    objc.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
    objc.objc_autoreleasePoolPop.restype = None
    appkit = ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")  # registers NSPasteboard
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFStringCreateWithBytes.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32, ctypes.c_bool]
    cf.CFStringCreateWithBytes.restype = ctypes.c_void_p
    cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
    cf.CFStringGetLength.restype = ctypes.c_long
    cf.CFStringGetMaximumSizeForEncoding.argtypes = [ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    pasteboard_class = objc.objc_getClass(b"NSPasteboard")
    string_type = ctypes.c_void_p.in_dll(appkit, "NSPasteboardTypeString").value
    if not pasteboard_class or not string_type:
        raise OSError("NSPasteboard is not available")

    def message(restype, *argtypes):
        return ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(("objc_msgSend", objc))

    sel = objc.sel_registerName
    return {
        "objc": objc, "cf": cf, "class": pasteboard_class, "type": string_type,
        "general": (message(ctypes.c_void_p), sel(b"generalPasteboard")),
        "named": (message(ctypes.c_void_p, ctypes.c_void_p), sel(b"pasteboardWithName:")),
        "release_globally": (message(None), sel(b"releaseGlobally")),
        "clear": (message(ctypes.c_long), sel(b"clearContents")),
        "set": (message(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p), sel(b"setString:forType:")),
        "get": (message(ctypes.c_void_p, ctypes.c_void_p), sel(b"stringForType:")),
    }


def _python_string(cf, ref) -> str | None:
    size = cf.CFStringGetMaximumSizeForEncoding(cf.CFStringGetLength(ref), _UTF8) + 1
    buffer = ctypes.create_string_buffer(size)
    if not cf.CFStringGetCString(ref, buffer, size, _UTF8):
        return None
    return buffer.value.decode("utf-8")


def _board(rt):
    """The pasteboard the functions use: the general one (tests substitute a private named one)."""
    send, sel = rt["general"]
    return send(rt["class"], sel)


def get_clipboard_text() -> tuple[bool, str]:
    try:
        rt = _runtime()
    except (OSError, ValueError, AttributeError):
        return False, ""
    pool = rt["objc"].objc_autoreleasePoolPush()
    try:
        board = _board(rt)
        if not board:
            return False, ""
        send, sel = rt["get"]
        text = send(board, sel, rt["type"])  # autoreleased, nil when there is no text
        if not text:
            return False, ""
        value = _python_string(rt["cf"], text)
        return (False, "") if value is None else (True, value)
    finally:
        rt["objc"].objc_autoreleasePoolPop(pool)


def set_clipboard_text(text: str) -> bool:
    try:
        rt = _runtime()
    except (OSError, ValueError, AttributeError):
        return False
    encoded = str(text).encode("utf-8")
    cf = rt["cf"]
    string = cf.CFStringCreateWithBytes(None, encoded, len(encoded), _UTF8, False)
    if not string:
        return False
    pool = rt["objc"].objc_autoreleasePoolPush()
    try:
        board = _board(rt)
        if not board:
            return False
        send, sel = rt["clear"]
        send(board, sel)
        send, sel = rt["set"]
        return bool(send(board, sel, string, rt["type"]))
    finally:
        rt["objc"].objc_autoreleasePoolPop(pool)
        cf.CFRelease(string)
