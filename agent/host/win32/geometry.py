"""Win32 capture geometry of the connected window, over the helpers in window_layout.py."""
from __future__ import annotations


class Win32Geometry:
    """Client area and monitor work area of the target window, in physical screen pixels."""

    def __init__(self, target):
        self.hwnd = target.hwnd

    @staticmethod
    def _layout():
        from host.win32 import window_layout
        return window_layout

    def client_size(self) -> tuple[int, int] | None:
        layout = self._layout()
        u32 = layout._user32()
        with layout._per_monitor_dpi(u32):
            return layout._client_size(u32, self.hwnd)

    def client_screen_rect(self) -> tuple[int, int, int, int] | None:
        layout = self._layout()
        u32 = layout._user32()
        with layout._per_monitor_dpi(u32):
            return layout._client_screen_rect(u32, self.hwnd)

    def monitor_work_area(self) -> tuple[int, int, int, int] | None:
        layout = self._layout()
        u32 = layout._user32()
        with layout._per_monitor_dpi(u32):
            info = layout._monitor_info(u32, self.hwnd)
        if info is None:
            return None
        work = info.rcWork
        return int(work.left), int(work.top), int(work.right), int(work.bottom)
