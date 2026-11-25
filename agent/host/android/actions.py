"""Named game actions on the Android client, as taps decided on recognised, focus-confirmed frames.

The phone client has no keyboard shortcuts. A named action becomes at most one tap, chosen from what is positively
recognised on one frame and confirmed again on a fresh frame right before it is sent (the tap carries that fresh
frame as its source; docs/android-support-plan.md decisions 2 and 7):

- the 退出游戏 confirmation is a guard: whenever it is seen, the only tap is its 取消, whatever was asked;
- a modal (any dialog with the game's four-point close mark at its top-right corner, e.g. the 兑换码 modal and the
  quit confirmation itself) is only ever closed by that mark;
- ``menu`` opens the 美鸭梨 menu and only on the open-world HUD (duck icon); ``leave`` leaves the current screen: a
  modal by its close mark, a page by its top-left back arrow, and on the HUD there is nothing to leave;
- ``map`` taps the minimap, and the HUD shortcuts (商城, 奇迹之旅, 奇想日历, 换装) tap the icon above their label, found
  by OCR on the label row, only on the HUD; the HUD layout can be customised, so there is no fallback position:
  a label that cannot be read refuses the action (共鸣, the gacha, sits between them);
- anything else (an unrecognised screen, a known screen where the action means nothing) sends nothing.

The open-world HUD and a page each need text read by OCR as well as their icon (the shortcut labels; the page title
beside the arrow), so a failing OCR engine, which reports ordinary misses, cannot turn the quit confirmation (the
duck still shows behind it) into the HUD.

BACK (KEYCODE_BACK) is never sent: in the open world it opens the quit confirmation and on a dropdown it closes the
whole settings page, and every screen it would close here has a tappable close of its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field

QUIT_TITLE = "MaaNikki_Android_QuitDialog"
QUIT_CANCEL = "MaaNikki_Android_QuitCancel"
MODAL_CLOSE = "MaaNikki_Android_ModalClose"
HUD_DUCK = "MaaNikki_Android_HudDuck"
BACK_ARROW = "MaaNikki_Android_BackArrow"
HUD_LABEL = "MaaNikki_Android_HudLabel"
PAGE_TITLE = "MaaNikki_Android_PageTitle"
NODES = (QUIT_TITLE, QUIT_CANCEL, MODAL_CLOSE, HUD_DUCK, BACK_ARROW, HUD_LABEL, PAGE_TITLE)

# Observed on BlueStacks Air at 1280x720 (phase A survey): the big map opened from the minimap at (160,105).
MINIMAP = [130, 75, 60, 60]

HUD_SHORTCUTS = {"shop": "商城", "monthly": "奇迹之旅", "calendar": "奇想日历", "dress": "换装"}
ACTIONS = frozenset({"menu", "leave", "map", *HUD_SHORTCUTS})
ACTION_NAMES = {"menu": "打开美鸭梨菜单", "leave": "离开当前页面", "map": "地图", **HUD_SHORTCUTS}
HUD_ROW_TEXT = "|".join(("商城", "共鸣", "奇迹之旅", "活动大厅", "奇想日历", "无限之心", "任务", "设计图", "换装"))

# The label row under the HUD shortcut icons. OCR reads the densely packed labels as one box, so each label is
# looked for in narrow overlapping windows; the windows that read it must agree on where it is.
LABEL_ROW = (600, 55, 680, 40)
LABEL_WINDOW = 90
LABEL_STEP = 10
LABEL_AGREEMENT = 25
ICON_ABOVE_LABEL = 15
# How far a target may move between the deciding frame and the confirming frame.
CONFIRM_TOLERANCE = 8


class ScreenUnrecognised(Exception):
    """Nothing on the frame was positively recognised as a screen this action may tap on."""


@dataclass(frozen=True)
class Screen:
    kind: str  # "quit_dialog", "modal", "hud", "page" or "unknown"
    targets: dict = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class Plan:
    """The tap for an action: ``target`` (None: nothing to tap, the action is already done), ``key`` naming what
    is tapped (a target of the screen, "minimap" or "label"), and whether the tap is the asked action itself
    (False for the quit guard's 取消 when something else was asked)."""
    target: list | None
    key: str
    completes: bool
    label: str = ""


def _hit(result) -> bool:
    return bool(result is not None and getattr(result, "hit", False) and getattr(result, "box", None))


def _box(result):
    box = getattr(result, "box", None)
    return [int(v) for v in box] if box is not None else None


def _text(result) -> str:
    return str(getattr(getattr(result, "best_result", None), "text", "") or "")


def _cjk(text: str) -> int:
    return sum("一" <= ch <= "鿿" for ch in text)


def read_screen(recognize) -> Screen:
    """Classify one frame. ``recognize(node, override=None)`` runs a node on that same frame and returns a result
    with ``hit``, ``box`` and ``best_result`` (None when the recognition could not run: the screen is then
    unknown)."""
    title, cancel = recognize(QUIT_TITLE), recognize(QUIT_CANCEL)
    if title is None or cancel is None:
        return Screen("unknown", reason="退出确认框的检查没能完成")
    if _hit(title) and _hit(cancel):
        return Screen("quit_dialog", {"cancel": _box(cancel)})
    if _hit(title) or _hit(cancel):
        return Screen("unknown", reason="画面上像是有确认框，但不是认得的退出确认框")
    close = recognize(MODAL_CLOSE)
    if close is None:
        return Screen("unknown", reason="弹窗的检查没能完成")
    if _hit(close):
        return Screen("modal", {"close": _box(close)})
    duck, arrow = recognize(HUD_DUCK), recognize(BACK_ARROW)
    if duck is None or arrow is None:
        return Screen("unknown", reason="左上角图标的检查没能完成")
    if _hit(duck) and not _hit(arrow):
        if not _hit(recognize(HUD_LABEL, {"expected": HUD_ROW_TEXT})):
            return Screen("unknown", reason="看到了大世界的鸭子图标，但读不出快捷栏的文字（文字识别可能不可用）")
        return Screen("hud", {"duck": _box(duck)})
    if _hit(arrow) and not _hit(duck):
        page_title = recognize(PAGE_TITLE)
        if not _hit(page_title) or _cjk(_text(page_title)) < 2:
            return Screen("unknown", reason="看到了返回箭头，但读不出页面标题（文字识别可能不可用）")
        return Screen("page", {"back": _box(arrow)})
    return Screen("unknown", reason="认不出当前画面（既不是大世界也不是带返回箭头的页面）")


def _label_window(recognize, label, x):
    """Whether a window about one label wide, centred on ``x``, reads ``label``."""
    x0, y, _width, height = LABEL_ROW
    width = 18 * len(label) + 24
    return _hit(recognize(HUD_LABEL, {"roi": [max(0, x - width // 2), y, width, height], "expected": label}))


def locate_shortcut(recognize, label: str):
    """The tap target of the HUD shortcut whose label is ``label``, or None when the label row does not show it
    consistently. Only meaningful on a frame already recognised as the HUD."""
    x0, y, width, height = LABEL_ROW
    centres, tops = [], []
    for x in range(x0, x0 + width - LABEL_WINDOW + 1, LABEL_STEP):
        result = recognize(HUD_LABEL, {"roi": [x, y, LABEL_WINDOW, height], "expected": label})
        if not _hit(result):
            continue
        text = _text(result)
        at = text.find(label)
        if at < 0:
            continue
        bx, by, bw, _bh = _box(result)
        centres.append(bx + bw * (at + len(label) / 2) / len(text))
        tops.append(by)
    if len(centres) < 2:
        return None
    centres.sort()
    median = centres[len(centres) // 2]
    if any(abs(c - median) > LABEL_AGREEMENT for c in centres):
        return None
    x = round(median)
    # Confirm on a window centred on the chosen point: it must read this label, so a point that drifted towards a
    # neighbour (共鸣 sits next to 商城 and 奇迹之旅) is refused instead of tapped.
    if not _label_window(recognize, label, x):
        return None
    return [x, max(0, min(tops) - ICON_ABOVE_LABEL)]


def plan_tap(action: str, screen: Screen, recognize) -> Plan:
    """The tap for ``action`` on ``screen``. Raises ScreenUnrecognised when nothing may be tapped."""
    if action not in ACTIONS:
        raise ValueError(action)
    if screen.kind == "quit_dialog":
        return Plan(screen.targets["cancel"], "cancel", action == "leave")
    if screen.kind == "unknown":
        raise ScreenUnrecognised(screen.reason)
    if action == "leave":
        if screen.kind == "hud":
            return Plan(None, "", True)  # already in the open world
        key = {"modal": "close", "page": "back"}[screen.kind]
        return Plan(screen.targets[key], key, True)
    if screen.kind != "hud":
        raise ScreenUnrecognised(f"「{ACTION_NAMES[action]}」只在大世界里能用，当前画面不是大世界")
    if action == "menu":
        return Plan(screen.targets["duck"], "duck", True)
    if action == "map":
        return Plan(list(MINIMAP), "minimap", True)
    label = HUD_SHORTCUTS[action]
    target = locate_shortcut(recognize, label)
    if target is None:
        raise ScreenUnrecognised(f"大世界的快捷栏上找不到「{label}」")
    return Plan(target, "label", True, label)


def confirm_tap(plan: Plan, screen: Screen, recognize):
    """The target of ``plan`` (decided on ``screen``) as seen again on a fresh frame, or None when that frame does
    not show the same screen with the same thing in the same place. ``recognize`` runs on the fresh frame."""
    fresh = read_screen(recognize)
    if fresh.kind != screen.kind:
        return None
    if plan.key == "minimap":
        return plan.target
    if plan.key == "label":
        return plan.target if _label_window(recognize, plan.label, plan.target[0]) else None
    box = fresh.targets.get(plan.key)
    if box is None or any(abs(a - b) > CONFIRM_TOLERANCE for a, b in zip(box[:2], plan.target[:2])):
        return None
    return box
