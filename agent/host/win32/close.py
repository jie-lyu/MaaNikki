"""Win32 binding for closing the game and its launcher (moved from game_close.py).

game_close.py holds the close state machine and receives this binding as ``api``.
"""
from __future__ import annotations

from game_close import WM_CLOSE


class Win32Api:
    """Thin ctypes binding; every failure raises OSError carrying the Win32 code."""

    WAIT_OBJECT_0 = 0x0
    WAIT_TIMEOUT = 0x102
    TH32CS_SNAPPROCESS = 0x2
    ERROR_NO_MORE_FILES = 18

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

        self._entry_type = PROCESSENTRY32W
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def bind(dll, name, argtypes, restype):
            func = getattr(dll, name)
            func.argtypes, func.restype = argtypes, restype
            return func

        HWND, DWORD, BOOL, HANDLE = wintypes.HWND, wintypes.DWORD, wintypes.BOOL, wintypes.HANDLE
        self._is_window = bind(user32, "IsWindow", [HWND], BOOL)
        self._is_visible = bind(user32, "IsWindowVisible", [HWND], BOOL)
        self._is_hung = bind(user32, "IsHungAppWindow", [HWND], BOOL)
        self._window_pid = bind(user32, "GetWindowThreadProcessId",
                                [HWND, ctypes.POINTER(DWORD)], DWORD)
        self._class_name = bind(user32, "GetClassNameW", [HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
        self._window_text = bind(user32, "GetWindowTextW", [HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
        self._enum_windows = bind(user32, "EnumWindows", [self._callback_type, wintypes.LPARAM], BOOL)
        self._post_message = bind(user32, "PostMessageW",
                                  [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], BOOL)
        self._open_process = bind(kernel32, "OpenProcess", [DWORD, BOOL, DWORD], HANDLE)
        self._image_name = bind(kernel32, "QueryFullProcessImageNameW",
                                [HANDLE, DWORD, wintypes.LPWSTR, ctypes.POINTER(DWORD)], BOOL)
        self._process_times = bind(kernel32, "GetProcessTimes",
                                   [HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, BOOL)
        self._wait = bind(kernel32, "WaitForSingleObject", [HANDLE, DWORD], DWORD)
        self._terminate = bind(kernel32, "TerminateProcess", [HANDLE, wintypes.UINT], BOOL)
        self._close_handle = bind(kernel32, "CloseHandle", [HANDLE], BOOL)
        self._snapshot = bind(kernel32, "CreateToolhelp32Snapshot", [DWORD, DWORD], HANDLE)
        self._first = bind(kernel32, "Process32FirstW", [HANDLE, ctypes.POINTER(PROCESSENTRY32W)], BOOL)
        self._next = bind(kernel32, "Process32NextW", [HANDLE, ctypes.POINTER(PROCESSENTRY32W)], BOOL)
        self._invalid_handle = ctypes.c_void_p(-1).value
        self._get_foreground = bind(user32, "GetForegroundWindow", [], HWND)
        self._set_foreground = bind(user32, "SetForegroundWindow", [HWND], BOOL)
        self._bring_to_top = bind(user32, "BringWindowToTop", [HWND], BOOL)
        self._attach_thread_input = bind(user32, "AttachThreadInput", [DWORD, DWORD, BOOL], BOOL)
        self._show_window = bind(user32, "ShowWindow", [HWND, ctypes.c_int], BOOL)
        self._is_iconic = bind(user32, "IsIconic", [HWND], BOOL)
        self._current_thread = bind(kernel32, "GetCurrentThreadId", [], DWORD)

    def _error(self, code=None):
        return self._ctypes.WinError(self._ctypes.get_last_error() if code is None else code)

    def is_window(self, hwnd):
        return bool(self._is_window(hwnd))

    def is_visible(self, hwnd):
        return bool(self._is_visible(hwnd))

    def is_hung(self, hwnd):
        return bool(self._is_hung(hwnd))

    def window_pid(self, hwnd):
        pid = self._wintypes.DWORD()
        if not self._window_pid(hwnd, self._ctypes.byref(pid)):
            return 0
        return int(pid.value)

    def window_thread(self, hwnd):
        return int(self._window_pid(hwnd, None))

    def window_class(self, hwnd):
        buffer = self._ctypes.create_unicode_buffer(257)
        return buffer.value if self._class_name(hwnd, buffer, len(buffer)) else ""

    def window_title(self, hwnd):
        buffer = self._ctypes.create_unicode_buffer(257)
        return buffer.value if self._window_text(hwnd, buffer, len(buffer)) else ""

    def enum_windows(self):
        found = []

        @self._callback_type
        def visit(hwnd, _):
            found.append(int(hwnd))
            return True

        if not self._enum_windows(visit, 0):
            raise self._error()
        return found

    def list_processes(self):
        snapshot = self._snapshot(self.TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == self._invalid_handle:
            raise self._error()
        try:
            entry = self._entry_type()
            entry.dwSize = self._ctypes.sizeof(entry)
            found = []
            ok = self._first(snapshot, self._ctypes.byref(entry))
            while ok:
                found.append((int(entry.th32ProcessID), entry.szExeFile))
                ok = self._next(snapshot, self._ctypes.byref(entry))
            code = self._ctypes.get_last_error()
            if code != self.ERROR_NO_MORE_FILES:
                raise self._error(code)
            return found
        finally:
            self._close_handle(snapshot)

    def open_process(self, pid, access):
        handle = self._open_process(access, False, int(pid))
        if not handle:
            raise self._error()
        return handle

    def process_image(self, handle):
        buffer = self._ctypes.create_unicode_buffer(32768)
        length = self._wintypes.DWORD(len(buffer))
        if not self._image_name(handle, 0, buffer, self._ctypes.byref(length)):
            raise self._error()
        return buffer.value

    def process_creation_time(self, handle):
        times = [self._wintypes.FILETIME() for _ in range(4)]
        if not self._process_times(handle, *(self._ctypes.byref(item) for item in times)):
            raise self._error()
        return (int(times[0].dwHighDateTime) << 32) | int(times[0].dwLowDateTime)

    def wait_process(self, handle, seconds):
        result = self._wait(handle, max(0, int(seconds * 1000)))
        if result == self.WAIT_OBJECT_0:
            return True
        if result == self.WAIT_TIMEOUT:
            return False
        raise self._error()

    def post_close(self, hwnd):
        # PostMessage never blocks on an unresponsive window.
        if not self._post_message(hwnd, WM_CLOSE, 0, 0):
            raise self._error()

    def terminate(self, handle):
        if not self._terminate(handle, 1):
            raise self._error()

    def close_handle(self, handle):
        if handle and not self._close_handle(handle):
            raise self._error()

    def foreground_window(self):
        return int(self._get_foreground())

    def set_foreground(self, hwnd):
        return bool(self._set_foreground(hwnd))

    def bring_to_top(self, hwnd):
        return bool(self._bring_to_top(hwnd))

    def attach_thread_input(self, first, second, attach):
        return bool(self._attach_thread_input(int(first), int(second), bool(attach)))

    def show_window(self, hwnd, command):
        return bool(self._show_window(hwnd, int(command)))

    def is_iconic(self, hwnd):
        return bool(self._is_iconic(hwnd))

    def current_thread_id(self):
        return int(self._current_thread())
