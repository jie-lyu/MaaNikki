"""Named game actions on the Android client, as taps decided on one recognised frame.

The phone client has no keyboard shortcuts. A named action (``Runtime.key("menu")`` and the HUD shortcuts) becomes
one tap, chosen from what is positively recognised on a single focus-confirmed frame (the per-decision rule,
docs/android-support-plan.md decision 7), and sent with that frame as its source:

- the 退出游戏 confirmation is a guard: whenever it is seen, the only tap is its 取消, whatever was asked;
- ``menu`` keeps the PC Esc meaning: on the open-world HUD it opens the 美鸭梨 menu (duck icon); on a page with the
  top-left back arrow it goes back one level; on the 兑换码 modal it taps the modal's close button;
- ``map`` taps the minimap, and the HUD shortcuts (商城, 奇迹之旅, 奇想日历, 换装) tap the icon above their label, found
  by OCR on the label row, only on the HUD; the HUD layout can be customised, so there is no fallback position:
  a label that cannot be read refuses the action (共鸣, the gacha, sits between them);
- anything else (an unrecognised screen, a known screen where the action means nothing) sends nothing.

BACK (KEYCODE_BACK) is never sent: in the open world it opens the quit confirmation and on a dropdown it closes the
whole settings page, and every page that BACK would close here has a tappable close of its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field

QUIT_TITLE = "MaaNikki_Android_QuitDialog"
QUIT_CANCEL = "MaaNikki_Android_QuitCancel"
REDEEM_TITLE = "MaaNikki_Android_RedeemModal"
HUD_DUCK = "MaaNikki_Android_HudDuck"
BACK_ARROW = "MaaNikki_Android_BackArrow"
HUD_LABEL = "MaaNikki_Android_HudLabel"
NODES = (QUIT_TITLE, QUIT_CANCEL, REDEEM_TITLE, HUD_DUCK, BACK_ARROW, HUD_LABEL)

# Observed on BlueStacks Air at 1280x720 (phase A survey): the 兑换码 modal closed at (945,193), the big map opened
# from the minimap at (160,105).
REDEEM_CLOSE = [925, 173, 40, 40]
MINIMAP = [130, 75, 60, 60]

HUD_SHORTCUTS = {"shop": "商城", "monthly": "奇迹之旅", "calendar": "奇想日历", "dress": "换装"}
ACTIONS = frozenset({"menu", "map", *HUD_SHORTCUTS})
ACTION_NAMES = {"menu": "美鸭梨菜单／返回", "map": "地图", **HUD_SHORTCUTS}

# The label row under the HUD shortcut icons. OCR reads the densely packed labels as one box, so each label is
# looked for in narrow overlapping windows; the windows that read it must agree on where it is.
LABEL_ROW = (600, 55, 680, 40)
LABEL_WINDOW = 90
LABEL_STEP = 10
LABEL_AGREEMENT = 25
ICON_ABOVE_LABEL = 15


class ScreenUnrecognised(Exception):
    """Nothing on the frame was positively recognised as a screen this action may tap on."""


@dataclass(frozen=True)
class Screen:
    kind: str  # "quit_dialog", "redeem_modal", "hud", "page" or "unknown"
    targets: dict = field(default_factory=dict)
    reason: str = ""


def _hit(result) -> bool:
    return bool(result is not None and getattr(result, "hit", False) and getattr(result, "box", None))


def _box(result):
    box = getattr(result, "box", None)
    return [int(v) for v in box] if box is not None else None


def read_screen(recognize) -> Screen:
    """Classify one frame. ``recognize(node, override=None)`` runs a node on that same frame and returns a result
    with ``hit`` and ``box`` (None when the recognition could not run, which counts as "not seen" except for the
    quit guard, which fails closed)."""
    title, cancel = recognize(QUIT_TITLE), recognize(QUIT_CANCEL)
    if title is None or cancel is None:
        return Screen("unknown", reason="退出确认框的检查没能完成")
    if _hit(title) and _hit(cancel):
        return Screen("quit_dialog", {"cancel": _box(cancel)})
    if _hit(title) or _hit(cancel):
        return Screen("unknown", reason="画面上像是有确认框，但不是认得的退出确认框")
    if _hit(recognize(REDEEM_TITLE)):
        return Screen("redeem_modal", {"close": list(REDEEM_CLOSE)})
    duck, arrow = recognize(HUD_DUCK), recognize(BACK_ARROW)
    if _hit(duck) and not _hit(arrow):
        return Screen("hud", {"duck": _box(duck)})
    if _hit(arrow) and not _hit(duck):
        return Screen("page", {"back": _box(arrow)})
    return Screen("unknown", reason="认不出当前画面（既不是大世界也不是带返回箭头的页面）")


def locate_shortcut(recognize, label: str):
    """The tap target of the HUD shortcut whose label is ``label``, or None when the label row does not show it
    consistently. Only meaningful on a frame already recognised as the HUD."""
    x0, y, width, height = LABEL_ROW
    centres, tops = [], []
    for x in range(x0, x0 + width - LABEL_WINDOW + 1, LABEL_STEP):
        result = recognize(HUD_LABEL, {"roi": [x, y, LABEL_WINDOW, height], "expected": label})
        if not _hit(result):
            continue
        best = getattr(result, "best_result", None)
        text = str(getattr(best, "text", "") or "")
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
    # Confirm on a window centred on the chosen point, about one label wide: it must read this label, so a point
    # that drifted towards a neighbour (共鸣 sits next to 商城 and 奇迹之旅) is refused instead of tapped.
    width = 18 * len(label) + 24
    check = recognize(HUD_LABEL, {"roi": [max(0, x - width // 2), y, width, height], "expected": label})
    if not _hit(check):
        return None
    icon_y = max(0, min(tops) - ICON_ABOVE_LABEL)
    return [x, icon_y]


def plan_tap(action: str, screen: Screen, recognize):
    """``(target, completes)``: the tap for ``action`` on ``screen`` and whether that tap is the action itself
    (False for the quit guard's 取消). Raises ScreenUnrecognised when nothing may be tapped."""
    if action not in ACTIONS:
        raise ValueError(action)
    if screen.kind == "quit_dialog":
        return screen.targets["cancel"], False
    if screen.kind == "unknown":
        raise ScreenUnrecognised(screen.reason)
    if action == "menu":
        target = {"redeem_modal": "close", "hud": "duck", "page": "back"}[screen.kind]
        return screen.targets[target], True
    if screen.kind != "hud":
        raise ScreenUnrecognised(f"「{ACTION_NAMES[action]}」只在大世界里能用，当前画面不是大世界")
    if action == "map":
        return list(MINIMAP), True
    label = HUD_SHORTCUTS[action]
    target = locate_shortcut(recognize, label)
    if target is None:
        raise ScreenUnrecognised(f"大世界的快捷栏上找不到「{label}」")
    return target, True
