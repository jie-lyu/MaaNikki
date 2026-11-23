"""Game window layout entry points: 16:9 healing, temporary tall window and capture-safe points.

The window manipulation is Win32 and lives in host/win32/window_layout.py,
loaded on first use so that importing task code loads no platform backend.
Capture sizes and the AutoAdjustResolution setting are platform-neutral and
stay here.
"""
from __future__ import annotations

from app_paths import DATA_ROOT
from client_config import mxu_config, node_action_params
from host import capabilities, platform_backend


BASE_CAPTURE_WIDTH = 1280
BASE_CAPTURE_HEIGHT = 720
TALL_PHYSICAL_WIDTH = 1440
TALL_PHYSICAL_HEIGHT = 5760
TALL_CAPTURE_WIDTH = 1280
TALL_CAPTURE_HEIGHT = 5120


def is_auto_adjust_resolution_enabled(context=None, options=None) -> bool:
    """Return whether automatic window resolution adjustment is enabled in Settings."""
    if context is not None:
        try:
            params = node_action_params(context, "MaaNikki_AutoAdjustResolution")
            if "value" in params and params["value"] not in ("", None):
                return bool(params["value"])
        except Exception:
            pass
    if isinstance(options, dict) and "AutoAdjustResolution" in options:
        opt = options.get("AutoAdjustResolution")
        if isinstance(opt, bool):
            return opt
        if isinstance(opt, dict):
            if opt.get("type") == "switch" and opt.get("value") not in ("", None):
                return bool(opt.get("value", True))
            case_name = opt.get("caseName")
            if case_name in ("Yes", "No"):
                return case_name == "Yes"
    try:
        cfg = mxu_config(DATA_ROOT) or {}
        opt = (cfg.get("globalOptionValues") or {}).get("AutoAdjustResolution")
        if isinstance(opt, dict):
            if opt.get("type") == "switch" and opt.get("value") not in ("", None):
                return bool(opt.get("value", True))
            case_name = opt.get("caseName")
            if case_name in ("Yes", "No"):
                return case_name == "Yes"
    except Exception:
        pass
    return True


def _layout():
    if not capabilities(platform_backend()).tall_window:
        raise RuntimeError("当前系统不支持调整游戏窗口。")
    from host.win32 import window_layout
    return window_layout


def window_hwnd(rt=None, controller=None) -> int | None:
    """Return the active game window handle associated with rt/controller if available."""
    return _layout().window_hwnd(rt=rt, controller=controller)


def safe_capture_y_range(hwnd, capture_height, **kwargs):
    return _layout().safe_capture_y_range(hwnd, capture_height, **kwargs)


def is_capture_point_safe(hwnd, target, **kwargs):
    return _layout().is_capture_point_safe(hwnd, target, **kwargs)


def clamp_capture_park_point(hwnd, point, **kwargs):
    return _layout().clamp_capture_park_point(hwnd, point, **kwargs)


def heal_16_9_window(controller=None, **kwargs):
    return _layout().heal_16_9_window(controller, **kwargs)


def ensure_16_9_window(controller=None, **kwargs):
    return _layout().ensure_16_9_window(controller, **kwargs)


def is_resolution_adjustment_blocked(hwnd=None):
    return _layout().is_resolution_adjustment_blocked(hwnd)


def tall_window(rt):
    """Context manager: temporarily resize the game window to the tall scan layout and restore on exit."""
    return _layout().tall_window(rt)


# Session lifecycle hooks run unconditionally at startup and exit; without the
# window layout backend nothing was resized, so there is nothing to hand off.
def adopt_session_16_9_handoff():
    if capabilities(platform_backend()).tall_window:
        return _layout().adopt_session_16_9_handoff()


def persist_session_16_9_handoff():
    if capabilities(platform_backend()).tall_window:
        return _layout().persist_session_16_9_handoff()


def restore_session_16_9_windows():
    if capabilities(platform_backend()).tall_window:
        return _layout().restore_session_16_9_windows()
