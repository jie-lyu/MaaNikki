"""ctypes bindings for the few macOS C APIs the backend needs: window list, keyboard events, front process.

Loaded on first use, only on macOS. No PyObjC: CoreFoundation, CoreGraphics, the Process Manager
and libproc are plain C.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_FRAMEWORKS = "/System/Library/Frameworks"
_UTF8 = 0x08000100  # kCFStringEncodingUTF8
_CF_NUMBER_SINT64 = 4  # kCFNumberSInt64Type
_CF_NUMBER_DOUBLE = 13  # kCFNumberDoubleType
_INCLUDING_WINDOW = 1 << 3  # kCGWindowListOptionIncludingWindow


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


@dataclass(frozen=True)
class WindowInfo:
    window_id: int
    pid: int
    title: str
    bounds: tuple[float, float, float, float]  # x, y, width, height in global points
    on_screen: bool


@lru_cache(maxsize=1)
def _cf():
    cf = ctypes.CDLL(f"{_FRAMEWORKS}/CoreFoundation.framework/CoreFoundation")
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFDictionaryGetValue.restype = ctypes.c_void_p
    cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    cf.CFNumberGetValue.restype = ctypes.c_bool
    cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
    cf.CFBooleanGetValue.restype = ctypes.c_bool
    cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
    cf.CFStringGetLength.restype = ctypes.c_long
    cf.CFStringGetMaximumSizeForEncoding.argtypes = [ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFURLCreateFromFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
    cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
    cf.CFBundleCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFBundleCreate.restype = ctypes.c_void_p
    cf.CFBundleGetIdentifier.argtypes = [ctypes.c_void_p]
    cf.CFBundleGetIdentifier.restype = ctypes.c_void_p
    return cf


@lru_cache(maxsize=1)
def _cg():
    cg = ctypes.CDLL(f"{_FRAMEWORKS}/CoreGraphics.framework/CoreGraphics")
    cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    cg.CGRectMakeWithDictionaryRepresentation.argtypes = [ctypes.c_void_p, ctypes.POINTER(CGRect)]
    cg.CGRectMakeWithDictionaryRepresentation.restype = ctypes.c_bool
    cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    cg.CGEventPostToPid.argtypes = [ctypes.c_int32, ctypes.c_void_p]
    cg.CGEventPostToPid.restype = None
    return cg


@lru_cache(maxsize=1)
def _hi():
    hi = ctypes.CDLL(f"{_FRAMEWORKS}/ApplicationServices.framework/ApplicationServices")
    hi.GetFrontProcess.argtypes = [ctypes.c_void_p]
    hi.GetFrontProcess.restype = ctypes.c_int16
    hi.GetProcessPID.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32)]
    hi.GetProcessPID.restype = ctypes.c_int16
    return hi


@lru_cache(maxsize=1)
def _libproc():
    lib = ctypes.CDLL("/usr/lib/libproc.dylib")
    lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    lib.proc_pidpath.restype = ctypes.c_int
    return lib


@lru_cache(maxsize=None)
def _cg_key(name: str) -> int:
    """A CFStringRef dictionary key exported by CoreGraphics (kCGWindowOwnerPID, ...)."""
    return ctypes.c_void_p.in_dll(_cg(), name).value


def _string(ref) -> str:
    if not ref:
        return ""
    cf = _cf()
    size = cf.CFStringGetMaximumSizeForEncoding(cf.CFStringGetLength(ref), _UTF8) + 1
    buffer = ctypes.create_string_buffer(size)
    return buffer.value.decode("utf-8") if cf.CFStringGetCString(ref, buffer, size, _UTF8) else ""


def _number(ref, *, floating=False):
    if not ref:
        return None
    value = ctypes.c_double() if floating else ctypes.c_int64()
    if not _cf().CFNumberGetValue(ref, _CF_NUMBER_DOUBLE if floating else _CF_NUMBER_SINT64, ctypes.byref(value)):
        return None
    return value.value


def window(window_id: int) -> WindowInfo | None:
    """One window from the window server's list, or None when it no longer exists."""
    cf, cg = _cf(), _cg()
    array = cg.CGWindowListCopyWindowInfo(_INCLUDING_WINDOW, window_id)
    if not array:
        return None
    try:
        if cf.CFArrayGetCount(array) < 1:
            return None
        entry = cf.CFArrayGetValueAtIndex(array, 0)

        def get(name):
            return cf.CFDictionaryGetValue(entry, _cg_key(name))

        if _number(get("kCGWindowNumber")) != window_id:
            return None
        rect = CGRect()
        bounds_ref = get("kCGWindowBounds")
        if not bounds_ref or not cg.CGRectMakeWithDictionaryRepresentation(bounds_ref, ctypes.byref(rect)):
            return None
        on_screen_ref = get("kCGWindowIsOnscreen")
        return WindowInfo(
            window_id=window_id,
            pid=int(_number(get("kCGWindowOwnerPID")) or 0),
            title=_string(get("kCGWindowName")),
            bounds=(rect.origin.x, rect.origin.y, rect.size.width, rect.size.height),
            on_screen=bool(on_screen_ref and cf.CFBooleanGetValue(on_screen_ref)),
        )
    finally:
        cf.CFRelease(array)


def front_pid() -> int:
    """pid of the front (active) application, -1 when unknown.

    GetFrontProcess is deprecated but answers without a run loop, unlike NSWorkspace, and works
    for applications that do not answer accessibility requests. The patched control unit uses the
    same call before sending input.
    """
    psn = (ctypes.c_uint32 * 2)()
    pid = ctypes.c_int32(-1)
    hi = _hi()
    if hi.GetFrontProcess(ctypes.byref(psn)) != 0 or hi.GetProcessPID(ctypes.byref(psn), ctypes.byref(pid)) != 0:
        return -1
    return pid.value


def process_path(pid: int) -> str:
    buffer = ctypes.create_string_buffer(4096)
    length = _libproc().proc_pidpath(pid, buffer, len(buffer))
    return buffer.value[:length].decode("utf-8", "replace") if length > 0 else ""


def bundle_identifier(pid: int) -> str:
    """CFBundleIdentifier of the .app that contains the process's executable, or ""."""
    path = Path(process_path(pid))
    bundle = next((parent for parent in (path, *path.parents) if parent.suffix == ".app"), None)
    if bundle is None:
        return ""
    cf = _cf()
    raw = str(bundle).encode("utf-8")
    url = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), True)
    if not url:
        return ""
    try:
        ref = cf.CFBundleCreate(None, url)
        if not ref:
            return ""
        try:
            return _string(cf.CFBundleGetIdentifier(ref))
        finally:
            cf.CFRelease(ref)
    finally:
        cf.CFRelease(url)


def post_key(pid: int, keycode: int, *, down: bool) -> bool:
    """Post a keyboard event to one process, the path the game accepts in the foreground and background."""
    cf, cg = _cf(), _cg()
    event = cg.CGEventCreateKeyboardEvent(None, keycode, down)
    if not event:
        return False
    try:
        cg.CGEventPostToPid(pid, event)
        return True
    finally:
        cf.CFRelease(event)
