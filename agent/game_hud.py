"""Task HUD labels, settings and per-tasker registry; the Win32 window is in host/win32/hud.py."""
from __future__ import annotations

import atexit
import json
import threading
from typing import TYPE_CHECKING

from app_paths import RESOURCE_ROOT
from client_config import node_action_params
from overlay_style import MXU_CONFIG_FILE, should_exclude_from_capture

if TYPE_CHECKING:
    from host.win32.hud import GameHudOverlay


STATIC_NODE_LABELS = {
    "MaaNikki_StartGame": "启动游戏",
    "MaaNikki_Assist": "探索辅助",
    "MaaNikki_Exploration": "探索辅助",
    "MaaNikki_RouteRecord": "录制路线",
    "MaaNikki_RouteRun": "自定义路线",
    "MaaNikki_OpenWorldCollect": "大世界材料采集",
    "MaaNikki_RouteTest": "路线测试",
    "MaaNikki_MinigameBling": "小游戏刷噗灵",
    "MaaNikki_Wardrobe": "读取衣柜",
    "MaaNikki_WardrobeRead": "读取衣柜",
    "MaaNikki_RenewalRead": "读取焕新",
    "MaaNikki_BackpackRead": "读取背包",
    "MaaNikki_GameKeys": "读取游戏键位",
    "MaaNikki_GameKeysRead": "读取游戏键位",
    "MaaNikki_Bless": "祝福闪光幻境",
    "MaaNikki_Jihua": "素材激化幻境",
    "MaaNikki_Monster": "魔物试炼幻境",
    "MaaNikki_RealmWeekly": "心之突破幻境",
    "MaaNikki_Fishing": "当前点位钓鱼",
    "MaaNikki_RunCoordination": "任务协调",
}

NODE_PREFIX_LABELS = (
    ("MaaNikki_Zhaoxi", "朝夕心愿"),
    ("MaaNikki_ClaimZhaoxi", "朝夕心愿"),
    ("MaaNikki_Xinghai", "星海拾光"),
    ("MaaNikki_ClaimXinghai", "星海拾光"),
    ("MaaNikki_Crown", "奇迹之冠巅峰赛"),
    ("MaaNikki_MonthlyPass", "奇迹之旅奖励"),
    ("MaaNikki_MonthlyCrystal", "每月五抽兑换"),
    ("MaaNikki_Monthly", "奇迹之旅"),
    ("MaaNikki_Mailbox", "邮箱领取"),
    ("MaaNikki_RedeemCodes", "兑换码领取"),
    ("MaaNikki_Redeem", "兑换码领取"),
    ("MaaNikki_WeeklyFreeGifts", "每周免费礼包"),
    ("MaaNikki_Shop", "商店免费礼包"),
    ("MaaNikki_Pursuit", "祝福闪光收集"),
    ("MaaNikki_Blessing", "祝福闪光收集"),
    ("MaaNikki_Bless", "祝福闪光幻境"),
    ("MaaNikki_Jihua", "素材激化幻境"),
    ("MaaNikki_Monster", "魔物试炼幻境"),
    ("MaaNikki_Weekly", "心之突破幻境"),
    ("MaaNikki_Dig", "美鸭梨挖掘"),
    ("MaaNikki_Home", "家园日常"),
    ("MaaNikki_CollectStarlightCrystals", "收集星光结晶"),
    ("MaaNikki_Realm", "幻境挑战"),
    ("MaaNikki_Renewal", "读取焕新"),
    ("MaaNikki_Rejuvenate", "读取焕新"),
    ("MaaNikki_Backpack", "读取背包"),
    ("MaaNikki_Wardrobe", "读取衣柜"),
    ("MaaNikki_Assist", "探索辅助"),
    ("MaaNikki_StartGame", "启动游戏"),
)

_GENERIC_HELPER_NODES = {
    "",
    "MaaNikki_Daily_Action",
    "MaaNikki_Daily_Message",
    "MaaNikki_ReturnMain",
    "MaaNikki_ReturnMainSecond",
    "MaaNikki_ReturnMainThird",
    "MaaNikki_FocusLost",
    "MaaNikki_NavigationSteering",
    "MaaNikki_RouteOverlay",
    "MaaNikki_TaskHud",
    "MaaNikki_AutoAdjustResolution",
}

_TASK_ENTRY_LABELS: dict[str, str] | None = None
_HUD_CASE_TO_MODE = {
    "Adaptive": "adaptive",
    "Always": "always",
    "ReasonOnly": "reason_only",
    "adaptive": "adaptive",
    "always": "always",
    "reason_only": "reason_only",
}
_HUD_REGISTRY_LOCK = threading.Lock()
_HUD_BY_TASKER: dict[int, "GameHudOverlay"] = {}
_CAPTURE_INCOMPATIBLE = False


def _entry_labels() -> dict[str, str]:
    global _TASK_ENTRY_LABELS
    if _TASK_ENTRY_LABELS is not None:
        return _TASK_ENTRY_LABELS
    labels: dict[str, str] = {}
    tasks_dir = RESOURCE_ROOT / "tasks"
    if tasks_dir.is_dir():
        for path in tasks_dir.glob("*.json"):
            try:
                doc = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            for section in ("task", "pretask"):
                for item in doc.get(section, ()) or ():
                    if isinstance(item, dict) and item.get("entry") and item.get("label"):
                        labels[str(item["entry"])] = str(item["label"])
    _TASK_ENTRY_LABELS = labels
    return _TASK_ENTRY_LABELS


def resolve_task_label(node_name: str, current: str = "") -> str:
    """Resolve a human-readable MaaNikki task label from a pipeline node name."""
    name = str(node_name or "").strip()
    if name in _GENERIC_HELPER_NODES:
        return current
    entries = _entry_labels()
    if name in entries:
        return entries[name]
    if name in STATIC_NODE_LABELS:
        return STATIC_NODE_LABELS[name]
    for prefix, label in NODE_PREFIX_LABELS:
        if name.startswith(prefix):
            return label
    return current


def _mxu_mtime_ns() -> int:
    try:
        return MXU_CONFIG_FILE.stat().st_mtime_ns
    except OSError:
        return 0


def _read_hud_mode(context=None, fallback: str = "adaptive", *, prefer_global: bool = False) -> str:
    if context is not None and not prefer_global:
        try:
            raw = str(node_action_params(context, "MaaNikki_TaskHud").get("mode") or "").strip()
            if raw in _HUD_CASE_TO_MODE:
                return _HUD_CASE_TO_MODE[raw]
        except Exception:
            pass
    try:
        if MXU_CONFIG_FILE.is_file():
            cfg = json.loads(MXU_CONFIG_FILE.read_text(encoding="utf-8-sig"))
            opt = (cfg.get("globalOptionValues") or {}).get("TaskHudMode") if isinstance(cfg, dict) else None
            if isinstance(opt, dict):
                case = str(opt.get("caseName") or "").strip()
                if case in _HUD_CASE_TO_MODE:
                    return _HUD_CASE_TO_MODE[case]
    except Exception:
        pass
    return fallback


def _compact_step(text: str, *, task_label: str = "") -> str:
    line = " ".join(str(text or "").splitlines()).strip().rstrip("。.;；")
    if not line:
        return ""
    if task_label and line.startswith(task_label + "："):
        line = line[len(task_label) + 1:].strip()
    elif task_label and line.startswith(task_label + " · "):
        line = line[len(task_label) + 3:].strip()
    return line


def acquire_game_hud(
    tasker_key: int,
    game_hwnd: int,
    controller=None,
    context=None,
    *,
    exclude_from_capture: bool | None = None,
    mode_setting: str | None = None,
) -> "GameHudOverlay | None":
    """Return or create the per-tasker capture-excluded GameHudOverlay."""
    from host import overlay_backend
    overlays = overlay_backend()
    if _CAPTURE_INCOMPATIBLE or overlays is None or not game_hwnd:
        return None
    exclude = should_exclude_from_capture(controller) if exclude_from_capture is None else bool(exclude_from_capture)
    mode = _read_hud_mode(context) if mode_setting is None else mode_setting
    with _HUD_REGISTRY_LOCK:
        existing = _HUD_BY_TASKER.get(tasker_key)
        if existing is not None:
            if existing.alive() and existing.game == int(game_hwnd) and existing.exclude_from_capture == exclude:
                existing.retain_scope(mode_setting=mode)
                return existing
            _HUD_BY_TASKER.pop(tasker_key, None)
            existing.close()
        # The window lives in the host backend's overlay adapter.
        hud = overlays.GameHudOverlay(int(game_hwnd), exclude_from_capture=exclude, mode_setting=mode, tasker_key=tasker_key)
        if not hud.alive():
            return None
        _HUD_BY_TASKER[tasker_key] = hud
        return hud


def release_game_hud(tasker_key: int, *, immediate: bool = False) -> None:
    with _HUD_REGISTRY_LOCK:
        hud = _HUD_BY_TASKER.get(tasker_key)
        if hud is None:
            return
        if immediate:
            _HUD_BY_TASKER.pop(tasker_key, None)
    if hud is None:
        return
    if immediate:
        hud.close()
    else:
        hud.schedule_idle_close(0.75)


def close_all_huds() -> None:
    with _HUD_REGISTRY_LOCK:
        huds = list(_HUD_BY_TASKER.values())
        _HUD_BY_TASKER.clear()
    for hud in huds:
        try:
            hud.close()
        except Exception:
            pass


atexit.register(close_all_huds)


