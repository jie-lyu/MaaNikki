"""Win32 GDI and window primitives shared by the Windows overlays (moved from overlay_style)."""
from __future__ import annotations

from functools import lru_cache
import ctypes
from ctypes import wintypes as w
import math

import numpy as np

from overlay_style import (
    FONT_FAMILY,
    OverlayTheme,
    _blend_rect,
    _blend_stamp,
    hud_max_width,
    hud_scale,
    hud_top_center_anchor,
    resolve_overlay_theme,
    role_colors,
    tinted_sparkle,
)


PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class BitmapInfo(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG),
                ("planes", w.WORD), ("bits", w.WORD), ("compression", w.DWORD),
                ("image_size", w.DWORD), ("x_resolution", w.LONG),
                ("y_resolution", w.LONG), ("colors", w.DWORD),
                ("important_colors", w.DWORD), ("color_table", w.DWORD)]


class Blend(ctypes.Structure):
    _fields_ = [("operation", w.BYTE), ("flags", w.BYTE),
                ("alpha", w.BYTE), ("format", w.BYTE)]


class WindowClass(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("proc", PROC), ("extra", ctypes.c_int),
                ("window_extra", w.INT), ("instance", w.HINSTANCE), ("icon", w.HICON),
                ("cursor", w.HANDLE), ("background", w.HBRUSH), ("menu", w.LPCWSTR),
                ("name", w.LPCWSTR)]


class Paint(ctypes.Structure):
    _fields_ = [("dc", w.HDC), ("erase", w.BOOL), ("rect", w.RECT),
                ("restore", w.BOOL), ("update", w.BOOL), ("reserved", w.BYTE * 32)]


class TextSize(ctypes.Structure):
    _fields_ = [("cx", w.LONG), ("cy", w.LONG)]


def bind(library, name, result, *args):
    function = getattr(library, name)
    function.restype, function.argtypes = result, list(args)
    return function


def checked(result, operation):
    if not result:
        raise OSError(f"{operation}失败（Win32={ctypes.get_last_error()}）。")
    return result


def sync_overlay_zorder(
    user,
    hwnd,
    game_hwnd,
    *,
    want_topmost: bool,
    stay_above_game: bool = False,
) -> None:
    """Place the overlay topmost when active, or right above game_hwnd in background."""
    get_window = bind(user, "GetWindow", w.HWND, w.HWND, w.UINT)
    get_style = bind(user, "GetWindowLongPtrW", ctypes.c_ssize_t, w.HWND, ctypes.c_int)
    get_class = bind(user, "GetClassNameW", ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
    is_visible = bind(user, "IsWindowVisible", w.BOOL, w.HWND)
    move = bind(user, "SetWindowPos", w.BOOL, w.HWND, w.HWND, *([ctypes.c_int] * 4), w.UINT)
    swp_flags = 0x0013  # SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE
    is_topmost = bool(get_style(hwnd, -20) & 0x00000008)

    if want_topmost:
        if not is_topmost:
            move(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, swp_flags)
            return
        # Even when WS_EX_TOPMOST is already set, a borderless/fullscreen game
        # re-activating can place game_hwnd above hwnd within the topmost band.
        cur = get_window(hwnd, 3)  # GW_HWNDPREV (walk upward from hwnd)
        hops = 0
        while cur and hops < 64:
            hops += 1
            if int(cur) == int(game_hwnd):
                move(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, swp_flags)
                return
            cur = get_window(cur, 3)
        return

    if not stay_above_game:
        return

    game_topmost = bool(get_style(game_hwnd, -20) & 0x00000008)
    class_buf = ctypes.create_unicode_buffer(128)
    if game_topmost:
        if not is_topmost:
            move(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, swp_flags)
        anchor = None
        cur = get_window(game_hwnd, 3)  # GW_HWNDPREV
        hops = 0
        while cur and hops < 128:
            hops += 1
            if int(cur) == int(hwnd):
                return
            if is_visible(cur) and (get_style(cur, -20) & 0x00000008):
                length = get_class(cur, class_buf, len(class_buf))
                if not (length > 0 and class_buf.value.startswith("MaaNikki")):
                    anchor = cur
                    break
            cur = get_window(cur, 3)
        move(hwnd, anchor if anchor else ctypes.c_void_p(-1), 0, 0, 0, 0, swp_flags)
        return

    if is_topmost:
        move(hwnd, ctypes.c_void_p(-2), 0, 0, 0, 0, swp_flags)

    # Walk upward from game_hwnd to find the nearest visible foreign non-topmost
    # window above it, ignoring hidden system/IME/tooltip windows in between so
    # an already-positioned overlay does not re-trigger SetWindowPos on every tick.
    anchor = None
    cur = get_window(game_hwnd, 3)  # GW_HWNDPREV
    hops = 0
    while cur and hops < 256:
        hops += 1
        if int(cur) == int(hwnd):
            return
        if is_visible(cur) and not (get_style(cur, -20) & 0x00000008):
            length = get_class(cur, class_buf, len(class_buf))
            if not (length > 0 and class_buf.value.startswith("MaaNikki")):
                anchor = cur
                break
        cur = get_window(cur, 3)

    if anchor is None and int(get_window(hwnd, 3) or 0) == 0:
        return
    insert_after = anchor if anchor else ctypes.c_void_p(0)  # HWND_TOP within non-topmost band
    move(hwnd, insert_after, 0, 0, 0, 0, swp_flags)


def _gdi_text_extent(dc, gdi, text: str) -> tuple[int, int]:
    if not text:
        return 0, 0
    extent_fn = bind(gdi, "GetTextExtentPoint32W", w.BOOL, w.HDC, w.LPCWSTR, ctypes.c_int, ctypes.POINTER(TextSize))
    sz = TextSize()
    checked(extent_fn(dc, text, len(text), ctypes.byref(sz)), "测量遮罩文字宽度")
    return int(sz.cx), int(sz.cy)


def _fit_text(dc, gdi, text: str, max_px: int) -> str:
    if not text or max_px <= 16:
        return ""
    w_px, _ = _gdi_text_extent(dc, gdi, text)
    if w_px <= max_px:
        return text
    lo, hi, best = 0, len(text), ""
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = text[:mid].rstrip() + ("…" if mid < len(text) else "")
        cw, _ = _gdi_text_extent(dc, gdi, cand)
        if cw <= max_px:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best or "…"


def _wrap_text_px(dc, gdi, text: str, max_px: int, max_lines: int = 2) -> list[str]:
    clean = " ".join((text or "").split())
    if not clean or max_px <= 24:
        return []
    lines: list[str] = []
    rest = clean
    while rest and len(lines) < max_lines:
        if _gdi_text_extent(dc, gdi, rest)[0] <= max_px:
            lines.append(rest)
            rest = ""
            break
        if len(lines) == max_lines - 1:
            lines.append(_fit_text(dc, gdi, rest, max_px))
            rest = ""
            break
        lo, hi, cut = 1, len(rest), 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if _gdi_text_extent(dc, gdi, rest[:mid])[0] <= max_px:
                cut = mid
                lo = mid + 1
            else:
                hi = mid - 1
        chunk = rest[:cut].rstrip()
        if not chunk:
            chunk = rest[:1]
            cut = 1
        lines.append(chunk)
        rest = rest[cut:].lstrip()
    return lines


def measure_hud_card(
    dc,
    gdi,
    select_fn,
    delete_fn,
    client_w: int,
    client_h: int,
    *,
    badge: str = "",
    title: str = "",
    subtitle: str = "",
    max_width: int | None = None,
) -> dict:
    """Measure exact pixel dimensions & wrapped lines for the unified Top-Center HUD."""
    _, _, s = hud_scale(client_w, client_h)
    font_fn = bind(gdi, "CreateFontW", w.HFONT, *([ctypes.c_int] * 5), *([w.DWORD] * 8), w.LPCWSTR)
    f_title_h = max(12, round(14 * s))
    f_body_h = max(11, round(12 * s))
    f_chip_h = max(10, round(11 * s))
    f_title = f_body = f_chip = None
    old_font = None
    try:
        f_title = checked(font_fn(-f_title_h, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 5, 0, FONT_FAMILY), "创建标题字体")
        f_body = checked(font_fn(-f_body_h, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, FONT_FAMILY), "创建正文字体")
        f_chip = checked(font_fn(-f_chip_h, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 5, 0, FONT_FAMILY), "创建标签字体")
        cap_w = max(180, min(client_w - round(20 * s), max_width or hud_max_width(client_w, client_h)))
        pad_l = round(14 * s)
        pad_r = round(12 * s)
        pad_y = round(8 * s)
        spark_sz = max(12, round(14 * s))
        spark_gap = round(7 * s)
        chip_pad_x = round(7 * s)
        chip_gap = round(8 * s)
        row_gap = round(4 * s)

        content_x_off = pad_l + spark_sz + spark_gap
        max_inner_w = max(80, cap_w - content_x_off - pad_r)

        chip_w = chip_h = chip_text_h = 0
        if badge:
            old_font = checked(select_fn(dc, f_chip), "选择测量字体")
            bw, bh = _gdi_text_extent(dc, gdi, badge)
            chip_w = bw + chip_pad_x * 2
            chip_text_h = bh
            chip_h = max(round(18 * s), bh + round(3 * s))

        h_first = select_fn(dc, f_title)
        if old_font is None:
            old_font = checked(h_first, "选择测量字体")
        title_avail = max(48, max_inner_w - (chip_w + chip_gap if badge else 0))
        fit_title = _fit_text(dc, gdi, " ".join((title or "").split()), title_avail)
        tw, th = _gdi_text_extent(dc, gdi, fit_title)
        header_row_w = (chip_w + (chip_gap if badge and fit_title else 0) + tw)
        header_row_h = max(chip_h, th, spark_sz)

        sub_lines: list[str] = []
        sub_h_line = 0
        sub_max_w = 0
        if subtitle:
            select_fn(dc, f_body)
            sub_lines = _wrap_text_px(dc, gdi, subtitle, max_inner_w, max_lines=2)
            for line in sub_lines:
                lw, lh = _gdi_text_extent(dc, gdi, line)
                sub_max_w = max(sub_max_w, lw)
                sub_h_line = max(sub_h_line, lh)

        inner_w = max(header_row_w, sub_max_w, round(120 * s))
        box_w = min(cap_w, content_x_off + inner_w + pad_r)
        sub_block_h = len(sub_lines) * (sub_h_line + round(2 * s)) - (round(2 * s) if sub_lines else 0)
        box_h = pad_y * 2 + header_row_h + ((row_gap + sub_block_h) if sub_lines else 0)

        return {
            "width": int(box_w),
            "height": int(box_h),
            "s": s,
            "badge": badge,
            "title": fit_title,
            "sub_lines": sub_lines,
            "pad_l": pad_l,
            "pad_y": pad_y,
            "spark_sz": spark_sz,
            "spark_gap": spark_gap,
            "chip_w": chip_w,
            "chip_h": chip_h,
            "chip_text_h": chip_text_h,
            "chip_pad_x": chip_pad_x,
            "chip_gap": chip_gap,
            "header_row_h": header_row_h,
            "row_gap": row_gap,
            "title_h": th,
            "chip_font_h": f_chip_h,
            "title_font_h": f_title_h,
            "body_font_h": f_body_h,
            "sub_h_line": sub_h_line,
        }
    finally:
        if old_font:
            select_fn(dc, old_font)
        for font_handle in (f_title, f_body, f_chip):
            if font_handle:
                delete_fn(font_handle)


def draw_hud_card(
    pixels: np.ndarray,
    uint32_view: np.ndarray,
    dc,
    gdi,
    select_fn,
    delete_fn,
    x0: int,
    y0: int,
    layout: dict,
    theme: OverlayTheme,
    *,
    role: str = "accent",
    emphasized: bool = False,
    opacity: float = 0.88,
) -> tuple[int, int, int, int]:
    """Paint the measured unified HUD banner onto a BGRA buffer at (x0, y0)."""
    s = layout["s"]
    box_w = layout["width"]
    box_h = layout["height"]
    x1, y1 = x0 + box_w, y0 + box_h
    colors = role_colors(theme, role, emphasized=emphasized)
    radius = max(6, round(10 * s))

    if emphasized:
        glow_pad = max(2, round(2.5 * s))
        _blend_rect(
            pixels, x0 - glow_pad, y0 - glow_pad, x1 + glow_pad, y1 + glow_pad,
            colors["stripe"], radius=radius + glow_pad, opacity=0.22,
        )
    sh_dx, sh_dy = round(2 * s), round(3 * s)
    _blend_rect(
        pixels, x0 + sh_dx, y0 + sh_dy, x1 + sh_dx, y1 + sh_dy,
        (10, 9, 12), radius=radius, opacity=0.30,
    )
    _blend_rect(
        pixels, x0, y0, x1, y1,
        theme.hud_bg_bgr, radius=radius, opacity=opacity,
        border_bgr=colors["border"], border_px=max(1.0, (1.6 if emphasized else 1.2) * s),
    )

    stripe_x = x0 + round(5 * s)
    stripe_w = max(2, round(3 * s))
    stripe_pad = max(6, round(8 * s))
    if y1 - stripe_pad > y0 + stripe_pad:
        _blend_rect(
            pixels, stripe_x, y0 + stripe_pad, stripe_x + stripe_w, y1 - stripe_pad,
            colors["stripe"], radius=stripe_w / 2.0, opacity=0.95,
        )

    spark_sz = layout["spark_sz"]
    spark_x = x0 + layout["pad_l"]
    spark_y = y0 + layout["pad_y"] + max(0, (layout["header_row_h"] - spark_sz) // 2)
    spark = tinted_sparkle(spark_sz, colors["sparkle_outer"], colors["sparkle_inner"])
    _blend_stamp(pixels, spark, spark_x, spark_y)

    content_x = spark_x + spark_sz + layout["spark_gap"]
    header_top = y0 + layout["pad_y"]
    if layout["badge"]:
        chip_y0 = header_top + max(0, (layout["header_row_h"] - layout["chip_h"]) // 2)
        chip_r = max(4, round(6 * s))
        _blend_rect(
            pixels, content_x, chip_y0, content_x + layout["chip_w"], chip_y0 + layout["chip_h"],
            colors["chip_bg"], radius=chip_r, opacity=0.96,
            border_bgr=colors["stripe"] if role != "forced" else None,
            border_px=max(0.8, 1.0 * s),
        )

    font_fn = bind(gdi, "CreateFontW", w.HFONT, *([ctypes.c_int] * 5), *([w.DWORD] * 8), w.LPCWSTR)
    text_out = bind(gdi, "TextOutW", w.BOOL, w.HDC, ctypes.c_int, ctypes.c_int, w.LPCWSTR, ctypes.c_int)
    set_bk = bind(gdi, "SetBkMode", ctypes.c_int, w.HDC, ctypes.c_int)
    set_color = bind(gdi, "SetTextColor", w.DWORD, w.HDC, w.DWORD)
    flush = bind(gdi, "GdiFlush", w.BOOL)

    bx0 = max(0, x0 - round(4 * s))
    by0 = max(0, y0 - round(3 * s))
    bx1 = min(pixels.shape[1], x1 + round(5 * s))
    by1 = min(pixels.shape[0], y1 + round(6 * s))
    sub_px = pixels[by0:by1, bx0:bx1]
    alpha_before = sub_px[..., 3].copy()
    rgb_slice = uint32_view[by0:by1, bx0:bx1]
    rgb_before = (rgb_slice & 0x00FFFFFF).copy()

    created = []
    old_font = None
    try:
        def make_font(height_px, weight):
            h = checked(font_fn(-height_px, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, FONT_FAMILY), "创建遮罩文字字体")
            created.append(h)
            return h

        f_chip = make_font(layout["chip_font_h"], 700) if layout["badge"] else None
        f_title = make_font(layout["title_font_h"], 700) if layout["title"] else None
        f_body = make_font(layout["body_font_h"], 400) if layout["sub_lines"] else None
        set_bk(dc, 1)

        title_x = content_x
        if layout["badge"] and f_chip:
            old_font = checked(select_fn(dc, f_chip), "选择标签字体")
            set_color(dc, colors["chip_ref"])
            chip_y0 = header_top + max(0, (layout["header_row_h"] - layout["chip_h"]) // 2)
            chip_ty = chip_y0 + max(0, (layout["chip_h"] - (layout.get("chip_text_h") or layout["chip_font_h"])) // 2)
            checked(text_out(dc, content_x + layout["chip_pad_x"], chip_ty, layout["badge"], len(layout["badge"])),
                    "绘制遮罩标签")
            title_x = content_x + layout["chip_w"] + layout["chip_gap"]

        if layout["title"] and f_title:
            h_sel = select_fn(dc, f_title)
            if old_font is None:
                old_font = checked(h_sel, "选择标题字体")
            set_color(dc, theme.hud_title_ref)
            title_ty = header_top + max(0, (layout["header_row_h"] - layout["title_h"]) // 2)
            checked(text_out(dc, title_x, title_ty, layout["title"], len(layout["title"])), "绘制遮罩标题")

        if layout["sub_lines"] and f_body:
            h_sel = select_fn(dc, f_body)
            if old_font is None:
                old_font = checked(h_sel, "选择正文字体")
            set_color(dc, colors["sub_ref"])
            sy_cur = header_top + layout["header_row_h"] + layout["row_gap"]
            step_y = layout["sub_h_line"] + round(2 * s)
            for line in layout["sub_lines"]:
                checked(text_out(dc, content_x, sy_cur, line, len(line)), "绘制遮罩正文")
                sy_cur += step_y

        checked(flush(), "提交遮罩绘制")
        changed = (rgb_slice & 0x00FFFFFF) != rgb_before
        sub_px[..., 3] = np.where(changed, 255, alpha_before)
    finally:
        if old_font:
            select_fn(dc, old_font)
        for handle in created:
            delete_fn(handle)

    return (bx0, by0, bx1, by1)


def render_top_center_banner(
    pixels: np.ndarray,
    uint32_view: np.ndarray,
    dc,
    gdi,
    select_fn,
    delete_fn,
    client_w: int,
    client_h: int,
    *,
    badge: str = "",
    title: str = "",
    subtitle: str = "",
    role: str = "accent",
    emphasized: bool = False,
    opacity: float = 0.88,
    anchor_y: int | None = None,
    theme: OverlayTheme | None = None,
) -> tuple[int, int, int, int]:
    """Measure and draw one unified Top-Center HUD card onto a full-client surface."""
    active_theme = theme or resolve_overlay_theme()
    layout = measure_hud_card(
        dc, gdi, select_fn, delete_fn, client_w, client_h,
        badge=badge, title=title, subtitle=subtitle,
    )
    x0, y0 = hud_top_center_anchor(client_w, client_h, layout["width"], anchor_y=anchor_y)
    return draw_hud_card(
        pixels, uint32_view, dc, gdi, select_fn, delete_fn,
        x0, y0, layout, active_theme, role=role, emphasized=emphasized, opacity=opacity,
    )


_QS_ALLINPUT = 0x04FF


_MWMO_INPUTAVAILABLE = 0x0004


_WAIT_OBJECT_0 = 0


_WAIT_TIMEOUT = 0x00000102


@lru_cache(maxsize=1)
def _win32_wake_api():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    return {
        "create_event": bind(
            kernel, "CreateEventW", wintypes.HANDLE,
            ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR,
        ),
        "set_event": bind(kernel, "SetEvent", wintypes.BOOL, wintypes.HANDLE),
        "reset_event": bind(kernel, "ResetEvent", wintypes.BOOL, wintypes.HANDLE),
        "close_handle": bind(kernel, "CloseHandle", wintypes.BOOL, wintypes.HANDLE),
        "wait_single": bind(
            kernel, "WaitForSingleObject", wintypes.DWORD,
            wintypes.HANDLE, wintypes.DWORD,
        ),
        "msg_wait": bind(
            user, "MsgWaitForMultipleObjectsEx", wintypes.DWORD,
            wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ),
        "queue_status": bind(user, "GetQueueStatus", wintypes.DWORD, wintypes.UINT),
        "handle_array_type": wintypes.HANDLE * 1,
    }


class Win32WakeEvent:
    """Manual-reset wake event that dispatches Win32 thread messages without polling delay."""

    def __init__(self):
        import sys
        import threading

        self._lock = threading.Lock()
        self._handle = None
        self._api = None
        self._wait_handles = None
        self._fallback = None
        if sys.platform == "win32":
            api = _win32_wake_api()
            handle = api["create_event"](None, True, False, None)
            if handle:
                self._api = api
                self._handle = handle
                self._wait_handles = api["handle_array_type"](handle)
                return
        self._fallback = threading.Event()

    def set(self):
        with self._lock:
            if self._fallback is not None:
                self._fallback.set()
            elif self._handle:
                self._api["set_event"](self._handle)

    def clear(self):
        with self._lock:
            if self._fallback is not None:
                self._fallback.clear()
            elif self._handle:
                self._api["reset_event"](self._handle)

    def is_set(self) -> bool:
        with self._lock:
            if self._fallback is not None:
                return self._fallback.is_set()
            if self._handle:
                return self._api["wait_single"](self._handle, 0) == _WAIT_OBJECT_0
            return False

    def close(self):
        with self._lock:
            handle = self._handle
            self._handle = None
            self._wait_handles = None
            api = self._api
        if handle and api is not None:
            api["close_handle"](handle)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def wait_or_pump(self, timeout_sec: float, *, pump_fn, stop_event=None) -> bool:
        """Wait until signaled, stop_event set, or timeout, pumping queued messages immediately."""
        import time

        if self._fallback is not None:
            pump_fn()
            return self._fallback.wait(max(0.0, timeout_sec))

        handle = self._handle
        api = self._api
        wait_handles = self._wait_handles
        if not handle or api is None or wait_handles is None:
            pump_fn()
            return False

        msg_wait = api["msg_wait"]
        wait_single = api["wait_single"]
        queue_status = api["queue_status"]

        if timeout_sec <= 0.0:
            pump_fn()
            return wait_single(handle, 0) == _WAIT_OBJECT_0

        deadline = time.monotonic() + timeout_sec
        stuck_spins = 0
        while stop_event is None or not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False
            wait_ms = max(1, min(0x7FFFFFFF, int(math.ceil(remaining * 1000.0))))
            res = msg_wait(1, wait_handles, wait_ms, _QS_ALLINPUT, _MWMO_INPUTAVAILABLE)
            if res == _WAIT_OBJECT_0:
                return True
            if res == _WAIT_OBJECT_0 + 1:
                drained = pump_fn()
                if drained:
                    stuck_spins = 0
                elif (queue_status(_QS_ALLINPUT) >> 16) != 0:
                    # Only throttle if Windows reports unconsumed queue bits right after PeekMessageW.
                    # Inter-thread SendMessage (e.g. WM_NCHITTEST) clears its bit during PeekMessageW
                    # even when PeekMessageW returns 0, so genuine high-rate cursor probes stay <1ms.
                    stuck_spins += 1
                    if stuck_spins >= 4:
                        rem_ms = max(1, min(8, int((deadline - time.monotonic()) * 1000.0)))
                        if wait_single(handle, rem_ms) == _WAIT_OBJECT_0:
                            return True
                        stuck_spins = 0
                else:
                    stuck_spins = 0
            elif res == _WAIT_TIMEOUT:
                return False
            else:
                slice_ms = min(16, wait_ms)
                if wait_single(handle, slice_ms) == _WAIT_OBJECT_0:
                    return True
        return False
