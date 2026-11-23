"""Win32 credential storage: the PearPal login is protected with DPAPI (moved from map_mask/pearpal.py)."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json

from map_mask.pearpal import DataError


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def credentials(path):
    """DPAPI is tied to the current Windows user; never log the decrypted value."""
    raw = path.read_bytes()
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, target = Blob(len(raw), buffer), Blob()
    decrypt = ctypes.windll.crypt32.CryptUnprotectData
    decrypt.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    decrypt.restype = wintypes.BOOL
    free = ctypes.windll.kernel32.LocalFree
    free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
    if not decrypt(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise DataError("美鸭梨登录信息无法读取，请重新登录。")
    try:
        value = json.loads(ctypes.string_at(target.data, target.size))
        if not all(isinstance(value.get(key), str) and value[key] for key in ("token", "openid")):
            raise DataError("美鸭梨登录信息无效，请重新登录。")
        return value
    finally:
        ctypes.memset(target.data, 0, target.size)
        free(target.data)
