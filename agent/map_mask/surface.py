"""Pixel helpers for the big-map overlay; the Win32 surface is in host/win32/layered_surface.py."""
from __future__ import annotations

from functools import lru_cache
import math
import numpy as np

from overlay_style import _blend_stamp, mix_bgr


def split_exploration_hint(hint: str) -> tuple[str, str, str]:
    clean = " ".join((hint or "").split())
    if not clean:
        return ("探索辅助", "", "accent")
    if "登录" in clean:
        return ("探索辅助", clean, "paused")
    for prefix in ("地图标记：", "地图标记:"):
        if clean.startswith(prefix):
            return ("地图标记", clean[len(prefix):].strip() or clean, "accent")
    return ("探索辅助", clean, "accent")


def _wrap_lines(text, width_chars=21, max_lines=3):
    lines = []
    for raw_line in (text or "").splitlines() or [""]:
        line = raw_line.strip()
        while len(line) > width_chars:
            lines.append(line[:width_chars])
            line = line[width_chars:]
            if len(lines) >= max_lines:
                lines[-1] = lines[-1][:-1] + "…"
                return lines
        if line:
            lines.append(line)
            if len(lines) >= max_lines:
                return lines
    return lines or ["暂无文字说明"]


def _over_layer(dst: np.ndarray, bgr: tuple[int, int, int] | np.ndarray, alpha_map: np.ndarray) -> None:
    alpha = np.clip(alpha_map, 0.0, 1.0)[..., None]
    if not np.any(alpha > 1e-4):
        return
    cur = dst.astype(np.float32)
    col = np.asarray(bgr, dtype=np.float32)
    out_rgb = col * alpha + cur[..., :3] * (1.0 - alpha)
    out_a = alpha * 255.0 + cur[..., 3:] * (1.0 - alpha)
    dst[:] = np.clip(np.rint(np.concatenate([out_rgb, out_a], axis=-1)), 0, 255).astype(np.uint8)


@lru_cache(maxsize=8)
def _outer_ring_stamp(
    rx: int,
    ry: int,
    frac_x: float,
    frac_y: float,
    r_in: float,
    r_out: float,
    sx: float,
    sy: float,
    s: float,
    hud_bg_bgr: tuple[int, int, int],
    accent_bgr: tuple[int, int, int],
) -> np.ndarray:
    w_box, h_box = rx * 2 + 1, ry * 2 + 1
    stamp = np.zeros((h_box, w_box, 4), dtype=np.uint8)
    yy, xx = np.ogrid[:h_box, :w_box]
    dist = np.hypot((xx - (rx + frac_x)) / max(1e-3, sx), (yy - (ry + frac_y)) / max(1e-3, sy))
    annulus = np.clip((dist - (r_in + 0.5)) / 2.0, 0.0, 1.0) * np.clip(r_out + 0.5 - dist, 0.0, 1.0)
    _over_layer(stamp, hud_bg_bgr, annulus * 0.05)
    px_err = np.abs(dist - r_out) * s
    shadow_alpha = np.clip((2.2 * s - px_err) / max(1.0, 1.4 * s), 0.0, 1.0) * 0.38
    _over_layer(stamp, (16, 15, 22), shadow_alpha)
    ring_col = mix_bgr(accent_bgr, (245, 248, 255), 0.60)
    core_alpha = np.clip((1.15 * s - px_err) / max(0.8, 0.75 * s), 0.0, 1.0) * 0.56
    _over_layer(stamp, ring_col, core_alpha)
    return stamp


def _draw_outer_ring(pixels: np.ndarray, cx: float, cy: float, r_in: float, r_out: float,
                     sx: float, sy: float, s: float, theme) -> None:
    cx_px, cy_px = cx * sx, cy * sy
    ix, iy = round(cx_px), round(cy_px)
    rx = math.ceil(r_out * sx) + 4
    ry = math.ceil(r_out * sy) + 4
    stamp = _outer_ring_stamp(
        rx, ry, round(cx_px - ix, 2), round(cy_px - iy, 2),
        r_in, r_out, round(sx, 4), round(sy, 4), round(s, 4),
        theme.hud_bg_bgr, theme.accent_bgr,
    )
    _blend_stamp(pixels, stamp, ix - rx, iy - ry)


def _clamp_ray_to_viewport(cx: float, cy: float, px: float, py: float,
                           pad_l: float, pad_t: float, pad_r: float, pad_b: float) -> tuple[float, float]:
    dx, dy = px - cx, py - cy
    t = 1.0
    if dx < 0.0 and px < pad_l:
        t = min(t, (pad_l - cx) / dx)
    elif dx > 0.0 and px > pad_r:
        t = min(t, (pad_r - cx) / dx)
    if dy < 0.0 and py < pad_t:
        t = min(t, (pad_t - cy) / dy)
    elif dy > 0.0 and py > pad_b:
        t = min(t, (pad_b - cy) / dy)
    t = max(0.0, min(1.0, t))
    return cx + dx * t, cy + dy * t


def _blend_premul_stamp(pixels: np.ndarray, stamp: np.ndarray, left: int, top: int, opacity: float = 1.0) -> None:
    if opacity >= 0.999:
        _blend_stamp(pixels, stamp, left, top)
        return
    if opacity <= 0.0:
        return
    scaled = np.clip(np.rint(stamp.astype(np.float32) * opacity), 0, 255).astype(np.uint8)
    _blend_stamp(pixels, scaled, left, top)


