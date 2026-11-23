"""Shared MaaNikki overlay theme tokens, typography metrics and pixel helpers (no platform calls).

Win32 window and GDI primitives live in host/win32/gdi.py."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import math
import time

import cv2
import numpy as np

from app_paths import DATA_ROOT


MXU_CONFIG_FILE = DATA_ROOT / "config/mxu-MaaNikki.json"
FONT_FAMILY = "Microsoft YaHei"

MODE_THEMES = {
    "light": {
        "bg_primary": "#faf6f0",
        "bg_secondary": "#fffdfa",
        "bg_tertiary": "#f4eee5",
        "text_primary": "#352c27",
        "text_secondary": "#5f534b",
        "text_muted": "#786b60",
        "border": "#e8dfd2",
        "border_strong": "#cfc2b0",
    },
    "dark": {
        "bg_primary": "#1f1c1d",
        "bg_secondary": "#2a2627",
        "bg_tertiary": "#332e2f",
        "text_primary": "#f6f0ea",
        "text_secondary": "#d3c9c0",
        "text_muted": "#ab9f96",
        "border": "#433c3c",
        "border_strong": "#655b59",
    },
}

ACCENT_PRESETS = {
    "rosegold": {"default": "#b4536f", "secondary": "#8f6a1f", "light": "#f8e9ec", "lightDark": "#3b2a30", "dark_default": "#f0a8bd", "dark_secondary": "#e0bd72"},
    "gold": {"default": "#8f6a1f", "secondary": "#b4536f", "light": "#f5ecd8", "lightDark": "#3a3122", "dark_default": "#e0bd72", "dark_secondary": "#f0a8bd"},
    "celeste": {"default": "#2e7d7a", "secondary": "#8f6a1f", "light": "#e3f1f0", "lightDark": "#233332", "dark_default": "#8bd3cf", "dark_secondary": "#e0bd72"},
    "meadow": {"default": "#3f7a5c", "secondary": "#8f6a1f", "light": "#e4f0e8", "lightDark": "#25332b", "dark_default": "#8fcfac", "dark_secondary": "#e0bd72"},
    "starsea": {"default": "#4a5a9c", "secondary": "#7a5aa6", "light": "#e8ebf6", "lightDark": "#272c40", "dark_default": "#a9b6ec", "dark_secondary": "#c6aeea"},
    "dream": {"default": "#7a5aa6", "secondary": "#b4536f", "light": "#efe9f6", "lightDark": "#32293f", "dark_default": "#c6aeea", "dark_secondary": "#f0a8bd"},
    "slate": {"default": "#5a6476", "secondary": "#8f6a1f", "light": "#e9ebef", "lightDark": "#2b2e34", "dark_default": "#b0bac9", "dark_secondary": "#e0bd72"},
}


def hex_to_bgr(value: str, fallback: tuple[int, int, int] = (189, 168, 240)) -> tuple[int, int, int]:
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        return fallback
    try:
        r = int(text[0:2], 16)
        g = int(text[2:4], 16)
        b = int(text[4:6], 16)
        return (b, g, r)
    except ValueError:
        return fallback


def bgr_to_colorref(bgr: tuple[int, int, int]) -> int:
    b, g, r = int(bgr[0]), int(bgr[1]), int(bgr[2])
    return ((b & 0xFF) << 16) | ((g & 0xFF) << 8) | (r & 0xFF)


def mix_bgr(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    u = max(0.0, min(1.0, float(t)))
    return (
        round(a[0] * (1.0 - u) + b[0] * u),
        round(a[1] * (1.0 - u) + b[1] * u),
        round(a[2] * (1.0 - u) + b[2] * u),
    )


def on_color_ref(bgr: tuple[int, int, int]) -> int:
    b, g, r = bgr[0] / 255.0, bgr[1] / 255.0, bgr[2] / 255.0
    lum = 0.0722 * b + 0.7152 * g + 0.2126 * r
    return 0xEAF0F6 if lum < 0.42 else 0x1D1C1F


@dataclass(frozen=True)
class OverlayTheme:
    mode: str
    accent_name: str
    accent_bgr: tuple[int, int, int]
    accent_ref: int
    secondary_bgr: tuple[int, int, int]
    secondary_ref: int
    hud_bg_bgr: tuple[int, int, int]
    hud_elevated_bgr: tuple[int, int, int]
    hud_border_bgr: tuple[int, int, int]
    hud_title_ref: int
    hud_body_ref: int
    hud_muted_ref: int
    success_bgr: tuple[int, int, int]
    success_bg_bgr: tuple[int, int, int]
    warning_bgr: tuple[int, int, int]
    warning_bg_bgr: tuple[int, int, int]
    error_bgr: tuple[int, int, int]
    accent_bg_bgr: tuple[int, int, int]
    card_bg_bgr: tuple[int, int, int]
    card_surface_bgr: tuple[int, int, int]
    card_border_bgr: tuple[int, int, int]
    card_pill_bgr: tuple[int, int, int]
    card_title_ref: int
    card_body_ref: int
    card_muted_ref: int
    card_accent_ref: int
    card_glyph_bgr: tuple[int, int, int]
    card_glyph_active_bgr: tuple[int, int, int]

    @property
    def card_accent_bgr(self) -> tuple[int, int, int]:
        return (
            (self.card_accent_ref >> 16) & 0xFF,
            (self.card_accent_ref >> 8) & 0xFF,
            self.card_accent_ref & 0xFF,
        )


_SYSTEM_DARK_CACHE: tuple[float, bool] | None = None
_THEME_CACHE: tuple[int, bool, OverlayTheme] | None = None


def _system_prefers_dark() -> bool:
    global _SYSTEM_DARK_CACHE
    now = time.monotonic()
    if _SYSTEM_DARK_CACHE is not None and now - _SYSTEM_DARK_CACHE[0] < 5.0:
        return _SYSTEM_DARK_CACHE[1]
    dark = False
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            dark = int(value) == 0
    except OSError:
        dark = False
    _SYSTEM_DARK_CACHE = (now, dark)
    return dark


def resolve_overlay_theme() -> OverlayTheme:
    """Resolve current MaaNikki mode + accent tokens from config/mxu-MaaNikki.json."""
    global _THEME_CACHE
    try:
        mtime = MXU_CONFIG_FILE.stat().st_mtime_ns
    except OSError:
        mtime = 0
    sys_dark = _system_prefers_dark()
    if _THEME_CACHE is not None and _THEME_CACHE[0] == mtime and _THEME_CACHE[1] == sys_dark:
        return _THEME_CACHE[2]
    mode = "dark" if sys_dark else "light"
    accent_name = "rosegold"
    custom_entry = None
    if mtime:
        try:
            cfg = json.loads(MXU_CONFIG_FILE.read_text(encoding="utf-8-sig"))
            settings = cfg.get("settings") if isinstance(cfg, dict) else None
            if isinstance(settings, dict):
                raw_mode = settings.get("theme")
                if raw_mode in {"light", "dark"}:
                    mode = raw_mode
                elif raw_mode == "system":
                    mode = "dark" if sys_dark else "light"
                raw_accent = str(settings.get("accentColor") or "").strip()
                if raw_accent:
                    accent_name = raw_accent
                for item in settings.get("customAccents") or ():
                    if isinstance(item, dict) and item.get("name") == accent_name and isinstance(item.get("colors"), dict):
                        custom_entry = item["colors"]
                        break
        except Exception:
            pass
    theme = _build_theme(mode, accent_name, custom_entry)
    _THEME_CACHE = (mtime, sys_dark, theme)
    return theme


def _build_theme(mode: str, accent_name: str, custom_colors: dict | None = None) -> OverlayTheme:
    preset = ACCENT_PRESETS.get(accent_name, ACCENT_PRESETS["rosegold"])
    if custom_colors:
        accent_light_hex = custom_colors.get("default") or preset["default"]
        accent_dark_hex = custom_colors.get("darkDefault") or accent_light_hex
        secondary_hex = custom_colors.get("secondary") or preset["dark_secondary"]
        tint_light_hex = custom_colors.get("light") or preset["light"]
        tint_dark_hex = custom_colors.get("lightDark") or preset["lightDark"]
    else:
        accent_light_hex = preset["default"]
        accent_dark_hex = preset["dark_default"]
        secondary_hex = preset["dark_secondary"] if mode == "dark" else preset["secondary"]
        tint_light_hex = preset["light"]
        tint_dark_hex = preset["lightDark"]

    dark_tokens = MODE_THEMES["dark"]
    light_tokens = MODE_THEMES["light"]
    accent_bgr = hex_to_bgr(accent_dark_hex, (189, 168, 240))
    custom_sec = (custom_colors.get("darkSecondary") or custom_colors.get("secondary")) if custom_colors else preset["dark_secondary"]
    secondary_bgr = hex_to_bgr(custom_sec or preset["dark_secondary"], (114, 189, 224))
    accent_dark_tint = hex_to_bgr(tint_dark_hex, (48, 42, 59))
    hud_base = hex_to_bgr(dark_tokens["bg_primary"], (29, 28, 31))
    hud_sec = hex_to_bgr(dark_tokens["bg_secondary"], (39, 38, 42))
    hud_border_base = hex_to_bgr(dark_tokens["border_strong"], (89, 91, 101))
    hud_bg = mix_bgr(hud_base, accent_dark_tint, 0.24)
    hud_elevated = mix_bgr(hud_sec, accent_dark_tint, 0.28)
    hud_border = mix_bgr(hud_border_base, accent_bgr, 0.42)

    is_dark = mode == "dark"
    mt = dark_tokens if is_dark else light_tokens
    card_accent_hex = accent_dark_hex if is_dark else accent_light_hex
    card_accent_bgr = hex_to_bgr(card_accent_hex, accent_bgr)
    card_sec_hex = secondary_hex if is_dark else (preset["secondary"] if not custom_colors else secondary_hex)
    card_bg = hex_to_bgr(mt["bg_primary"])
    card_surface = hex_to_bgr(mt["bg_tertiary"])
    card_border = mix_bgr(hex_to_bgr(mt["border_strong"]), card_accent_bgr, 0.22)
    card_pill = hex_to_bgr(tint_dark_hex if is_dark else tint_light_hex, card_surface)

    return OverlayTheme(
        mode=mode,
        accent_name=accent_name,
        accent_bgr=accent_bgr,
        accent_ref=bgr_to_colorref(accent_bgr),
        secondary_bgr=secondary_bgr,
        secondary_ref=bgr_to_colorref(secondary_bgr),
        hud_bg_bgr=hud_bg,
        hud_elevated_bgr=hud_elevated,
        hud_border_bgr=hud_border,
        hud_title_ref=bgr_to_colorref(hex_to_bgr(dark_tokens["text_primary"])),
        hud_body_ref=bgr_to_colorref(hex_to_bgr(dark_tokens["text_secondary"])),
        hud_muted_ref=bgr_to_colorref(hex_to_bgr(dark_tokens["text_muted"])),
        success_bgr=hex_to_bgr("#9ed8b8"),
        success_bg_bgr=(44, 53, 36),
        warning_bgr=hex_to_bgr("#f0ca8b"),
        warning_bg_bgr=(34, 49, 58),
        error_bgr=hex_to_bgr("#ffa4b0"),
        accent_bg_bgr=accent_dark_tint,
        card_bg_bgr=card_bg,
        card_surface_bgr=card_surface,
        card_border_bgr=card_border,
        card_pill_bgr=card_pill,
        card_title_ref=bgr_to_colorref(hex_to_bgr(mt["text_primary"])),
        card_body_ref=bgr_to_colorref(hex_to_bgr(mt["text_secondary"])),
        card_muted_ref=bgr_to_colorref(hex_to_bgr(mt["text_muted"])),
        card_accent_ref=bgr_to_colorref(hex_to_bgr(card_accent_hex, card_accent_bgr)),
        card_glyph_bgr=mix_bgr(hex_to_bgr(mt["text_muted"]), card_accent_bgr, 0.25),
        card_glyph_active_bgr=hex_to_bgr(card_sec_hex, card_accent_bgr),
    )


def should_exclude_from_capture(controller) -> bool:
    """Always exclude MaaNikki overlays from capture so fallback screen/DXGI captures never occlude UI or minimap."""
    _ = controller
    return True


def _premul(bgr: tuple[int, int, int], alpha: float) -> tuple[int, int, int, int]:
    a = max(0.0, min(1.0, float(alpha)))
    return (
        int(round(bgr[0] * a)),
        int(round(bgr[1] * a)),
        int(round(bgr[2] * a)),
        int(round(255.0 * a)),
    )


def _rounded_mask(h, w_px, radius):
    yy, xx = np.ogrid[:h, :w_px]
    r = max(1.0, float(radius))
    dx = np.maximum(np.maximum(r - xx, xx - (w_px - 1 - r)), 0.0)
    dy = np.maximum(np.maximum(r - yy, yy - (h - 1 - r)), 0.0)
    dist = np.hypot(dx, dy)
    return np.clip(r + 0.5 - dist, 0.0, 1.0).astype(np.float32)


def _blend_rect(pixels, x0, y0, x1, y1, color_bgr, radius=0, opacity=1.0, border_bgr=None, border_px=1.5):
    height, width = pixels.shape[:2]
    cx0, cy0 = max(0, round(x0)), max(0, round(y0))
    cx1, cy1 = min(width, round(x1)), min(height, round(y1))
    if cx0 >= cx1 or cy0 >= cy1:
        return
    w_box, h_box = max(1, round(x1 - x0)), max(1, round(y1 - y0))
    yy, xx = np.ogrid[cy0 - round(y0):cy1 - round(y0), cx0 - round(x0):cx1 - round(x0)]
    r = max(1.0, min(float(radius), min(w_box, h_box) / 2.0))
    dx = np.maximum(np.maximum(r - xx, xx - (w_box - 1 - r)), 0.0)
    dy = np.maximum(np.maximum(r - yy, yy - (h_box - 1 - r)), 0.0)
    dist = np.hypot(dx, dy)
    outer = np.clip(r + 0.5 - dist, 0.0, 1.0).astype(np.float32) * float(opacity)
    alpha = outer[..., None]
    fill = np.asarray(color_bgr, dtype=np.float32)
    if border_bgr is not None:
        inner = np.clip(r - border_px + 0.5 - dist, 0.0, 1.0).astype(np.float32)[..., None]
        b_col = np.asarray(border_bgr, dtype=np.float32)
        col = fill * inner + b_col * (1.0 - inner)
    else:
        col = fill
    target = pixels[cy0:cy1, cx0:cx1].astype(np.float32)
    src_pre = np.empty_like(target)
    src_pre[..., :3] = col * alpha
    src_pre[..., 3:] = alpha * 255.0
    pixels[cy0:cy1, cx0:cx1] = np.clip(
        np.rint(src_pre + target * (1.0 - alpha)), 0, 255).astype(np.uint8)


def _blend_stamp(pixels, stamp, left, top, circle=None, sx=1.0, sy=1.0):
    height, width = pixels.shape[:2]
    sh, sw = stamp.shape[:2]
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(width, left + sw), min(height, top + sh)
    if x0 >= x1 or y0 >= y1:
        return
    source = stamp[y0 - top:y1 - top, x0 - left:x1 - left].astype(np.uint16)
    if circle:
        cx, cy, radius = circle
        yy, xx = np.ogrid[y0:y1, x0:x1]
        inside = ((xx / sx - cx) ** 2 + (yy / sy - cy) ** 2 <= radius ** 2)
        source = source * inside[..., None]
    target = pixels[y0:y1, x0:x1]
    target[:] = source + (target.astype(np.uint16) * (255 - source[..., 3:]) + 127) // 255


@lru_cache(maxsize=64)
def tinted_sparkle(size: int, outer_bgr: tuple[int, int, int], inner_bgr: tuple[int, int, int]):
    extent = max(24, size * 4)
    unit = extent / 64.0
    canvas = np.zeros((extent, extent, 4), dtype=np.float32)

    def poly(rx, ry, inner_r):
        pts = []
        for idx in range(8):
            r = (rx if idx % 2 == 0 else inner_r) if idx % 4 == 0 else (ry if idx % 2 == 0 else inner_r)
            ang = idx * math.pi / 4.0
            pts.append(((32.0 + math.sin(ang) * r) * unit, (32.0 - math.cos(ang) * r) * unit))
        mask = np.zeros((extent, extent), dtype=np.uint8)
        cv2.fillPoly(mask, [np.round(pts).astype(np.int32)], 255, lineType=cv2.LINE_AA)
        return mask.astype(np.float32) / 255.0

    for mask, col in ((poly(25.0, 25.0, 6.2), outer_bgr), (poly(13.0, 13.0, 3.2), inner_bgr)):
        a = mask[..., None]
        canvas[..., :3] = np.asarray(col, dtype=np.float32) * a + canvas[..., :3] * (1.0 - a)
        canvas[..., 3:] = a * 255.0 + canvas[..., 3:] * (1.0 - a)
    result = cv2.resize(canvas, (size, size), interpolation=cv2.INTER_AREA)
    return np.clip(np.rint(result), 0, 255).astype(np.uint8)


def hud_scale(client_w: int, client_h: int) -> tuple[float, float, float]:
    sx = max(0.35, client_w / 1280.0)
    sy = max(0.35, client_h / 720.0)
    s = max(0.82, min(sx, sy))
    return sx, sy, s


def hud_top_center_anchor(client_w: int, client_h: int, box_w: int, anchor_y: int | None = None) -> tuple[int, int]:
    sx, sy, s = hud_scale(client_w, client_h)
    y = max(6, round(14 * sy)) if anchor_y is None else anchor_y
    margin = max(6, round(10 * s))
    x = max(margin, min(max(margin, client_w - box_w - margin), (client_w - box_w) // 2))
    return x, y


def hud_max_width(client_w: int, client_h: int) -> int:
    sx, _, s = hud_scale(client_w, client_h)
    if client_w >= 900:
        return max(round(280 * s), min(client_w - round(450 * sx), round(620 * s)))
    return max(180, client_w - round(24 * s))


def role_colors(theme: OverlayTheme, role: str, *, emphasized: bool = False) -> dict:
    if role == "forced":
        stripe = theme.warning_bgr
        chip_bg = theme.warning_bgr
        chip_ref = 0x1D1C1F
        border = mix_bgr(theme.hud_border_bgr, theme.warning_bgr, 0.78 if emphasized else 0.58)
        sub_ref = bgr_to_colorref(mix_bgr(hex_to_bgr("#f6f0ea"), theme.warning_bgr, 0.35))
        return {"stripe": stripe, "chip_bg": chip_bg, "chip_ref": chip_ref, "border": border,
                "sparkle_outer": theme.warning_bgr, "sparkle_inner": theme.accent_bgr, "sub_ref": sub_ref}
    if role == "paused":
        stripe = theme.warning_bgr
        chip_bg = theme.warning_bg_bgr
        chip_ref = bgr_to_colorref(theme.warning_bgr)
        border = mix_bgr(theme.hud_border_bgr, theme.warning_bgr, 0.48)
        return {"stripe": stripe, "chip_bg": chip_bg, "chip_ref": chip_ref, "border": border,
                "sparkle_outer": theme.warning_bgr, "sparkle_inner": theme.secondary_bgr, "sub_ref": theme.hud_body_ref}
    if role == "recovered":
        stripe = theme.success_bgr
        chip_bg = theme.success_bg_bgr
        chip_ref = bgr_to_colorref(theme.success_bgr)
        border = mix_bgr(theme.hud_border_bgr, theme.success_bgr, 0.56)
        return {"stripe": stripe, "chip_bg": chip_bg, "chip_ref": chip_ref, "border": border,
                "sparkle_outer": theme.success_bgr, "sparkle_inner": theme.secondary_bgr, "sub_ref": theme.hud_body_ref}
    stripe = theme.accent_bgr
    chip_bg = theme.accent_bg_bgr
    chip_ref = theme.accent_ref
    border = mix_bgr(theme.hud_border_bgr, theme.accent_bgr, 0.65 if emphasized else 0.32)
    return {"stripe": stripe, "chip_bg": chip_bg, "chip_ref": chip_ref, "border": border,
            "sparkle_outer": theme.accent_bgr, "sparkle_inner": theme.secondary_bgr, "sub_ref": theme.hud_body_ref}


