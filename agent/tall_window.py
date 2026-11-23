"""Game window layout entry points: 16:9 healing, temporary tall window and capture-safe points.

The window manipulation is Win32 and lives in host/win32/window_layout.py,
loaded on first use so that importing task code loads no platform backend.
Capture sizes and the AutoAdjustResolution setting are platform-neutral and
stay here.

Capture-safe points have their own capability: where the backend clips click
targets to the monitor work area (Win32: taskbar and off-screen parts), the
window layout backend answers; elsewhere the frame itself is the bound, as the
Win32 code already does when no window handle is known. The patched Mac
control unit refuses a click that something else would receive.
"""
from __future__ import annotations

import math

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


def _work_area_layout():
    """The window layout backend when it clips click targets to the work area, else None."""
    if capabilities(platform_backend()).work_area_bounds:
        from host.win32 import window_layout
        return window_layout
    return None


def _frame_bounds(capture_size) -> tuple[int, int, int, int]:
    """Inclusive (min_x, min_y, max_x, max_y): the whole frame, minus a tall frame's rows below the 16:9 base.

    Same result as window_layout.safe_capture_bounds without a window handle.
    """
    cap_w = max(1, int(capture_size[0]))
    cap_h = max(1, int(capture_size[1]))
    max_y = min(cap_h, BASE_CAPTURE_HEIGHT) - 1 if cap_h > BASE_CAPTURE_HEIGHT else cap_h - 1
    return 0, 0, cap_w - 1, max(0, max_y)


# Where the backend clips to the work area, calls pass through exactly as given.

def safe_capture_y_range(hwnd, capture_height, **kwargs):
    layout = _work_area_layout()
    if layout is not None:
        return layout.safe_capture_y_range(hwnd, capture_height, **kwargs)
    _, min_y, _, max_y = _frame_bounds((kwargs.get("capture_width", BASE_CAPTURE_WIDTH), capture_height))
    return min_y, max_y


def is_capture_point_safe(hwnd, target, **kwargs):
    layout = _work_area_layout()
    if layout is not None:
        return layout.is_capture_point_safe(hwnd, target, **kwargs)
    if not isinstance(target, (list, tuple)) or len(target) not in (2, 4):
        return False
    try:
        px = float(target[0]) + (float(target[2]) / 2.0 if len(target) == 4 else 0.0)
        py = float(target[1]) + (float(target[3]) / 2.0 if len(target) == 4 else 0.0)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(px) and math.isfinite(py)):
        return False
    min_x, min_y, max_x, max_y = _frame_bounds(kwargs.get("capture_size", (BASE_CAPTURE_WIDTH, BASE_CAPTURE_HEIGHT)))
    return min_x <= round(px) <= max_x and min_y <= round(py) <= max_y


def clamp_capture_park_point(hwnd, point, **kwargs):
    layout = _work_area_layout()
    if layout is not None:
        return layout.clamp_capture_park_point(hwnd, point, **kwargs)
    min_x, min_y, max_x, max_y = _frame_bounds(kwargs.get("capture_size", (BASE_CAPTURE_WIDTH, BASE_CAPTURE_HEIGHT)))
    return [max(min_x, min(max_x, round(float(point[0])))), max(min_y, min(max_y, round(float(point[1]))))]


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
