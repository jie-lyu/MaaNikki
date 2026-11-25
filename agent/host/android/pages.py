"""Taps on a recognised page of the Android client, with the same rules as the named actions (actions.py):

decided on one focus-confirmed frame, refused unless that frame is positively the expected page and the target is
recognised on it, refused when the point lies on or next to text that spends, deletes or commits (删除, 购买, ...),
confirmed on a fresh frame (same page, same title, target within a few pixels) and sent with that fresh frame as its
source. A screen that is not the expected one sends nothing.
"""
from __future__ import annotations

import re

from host.android import actions

FORBIDDEN = "MaaNikki_Android_Forbidden"
FORBIDDEN_MARGIN = 40  # pixels around the tap point that may not hold a forbidden word
# The words of MaaNikki_Android_Forbidden's ``expected`` (a test keeps the two equal), to locate a word inside a row.
FORBIDDEN_WORDS = "删除|购买|充值|共鸣|确认退出|解锁.*颂歌|恢复默认|¥|￥|兑换钻石|获取|开通"
HUD_LABELS = tuple(actions.HUD_ROW_TEXT.split("|"))


class PageMismatch(actions.ScreenUnrecognised):
    """The frame is not the page the step expects (or its target is not on it)."""


class LayerMismatch(PageMismatch):
    """A tap frame of tap_on_layer is not the layer: the frame itself (``screen``, ``frame``, ``recognize``) goes to
    the caller to decide on; nothing was sent."""

    def __init__(self, reason, screen, frame, recognize):
        super().__init__(reason)
        self.screen, self.frame, self.recognize = screen, frame, recognize


class GuardRefusal(PageMismatch):
    """A forbidden word lies next to the point: the task stops all input (no navigation afterwards either)."""


def screen_now(rt):
    """``(screen, frame, recognize)`` on a fresh focus-confirmed frame; frame is None when the run stopped."""
    frame = rt._fresh_device_frame()
    if frame is None:
        return None, None, None
    recognize = rt._recognizer_on(frame)
    return actions.read_screen(recognize), frame, recognize


def is_page(screen, title: str) -> bool:
    return screen is not None and screen.kind == "page" and screen.title.startswith(title)


def wait_for(rt, accept, seconds: float, step: float = .4):
    """The first screen within ``seconds`` that ``accept(screen)`` takes, else None (polls fresh frames)."""
    import time
    deadline = time.monotonic() + seconds
    while not rt.stopped:
        screen, frame, _ = screen_now(rt)
        if frame is None:
            return None
        if accept(screen) and rt._frame_still_valid(frame):
            return screen
        if time.monotonic() >= deadline:
            return None
        rt.pause(step)
    return None


def _target(screen, recognize, title, node, override):
    if not is_page(screen, title):
        raise PageMismatch(f"当前画面不是「{title}」页面")
    result = recognize(node, override) if override else recognize(node)
    if not actions._hit(result):
        raise PageMismatch(f"「{title}」页面上找不到要点的东西（{node}）")
    return actions._box(result)


def _rows(result):
    return list(getattr(result, "filtered_results", None) or []) or [getattr(result, "best_result", None)]


def _within_margin(found, box) -> bool:
    x, y, w, h = box
    left, top, right, bottom = x - FORBIDDEN_MARGIN, y - FORBIDDEN_MARGIN, x + w + FORBIDDEN_MARGIN, y + h + FORBIDDEN_MARGIN
    return len(found) == 4 and found[0] < right and left < found[0] + found[2] and found[1] < bottom \
        and top < found[1] + found[3]


def word_boxes(text: str, box) -> list:
    """Where the forbidden words of one OCR row are. A row that holds nothing but forbidden words and HUD shortcut
    labels (OCR merges the densely packed label row, e.g. 「共鸣 奇迹之旅活动大厅…」, and pads a lone 「共鸣」 into its
    neighbour) gives each word its own box: its start from its position in the row, its width from the glyph width
    (the row's width per character, at most the row height: the HUD glyphs measure 18-19 px in rows 15-23 px high,
    while OCR pads a lone label's box up to 60 px). Any other row gives its whole box: the word belongs to a label or
    button whose extent is not known (购买等级, ¥6)."""
    rest = re.sub(FORBIDDEN_WORDS, "", text)
    for label in HUD_LABELS:
        rest = rest.replace(label, "")
    x, y, w, h = [int(v) for v in box]
    if re.sub(r"\s", "", rest) or not text:
        return [[x, y, w, h]]
    glyph = min(w / len(text), h)
    return [[round(x + w * m.start() / len(text)), y, max(1, round(glyph * (m.end() - m.start()))), h]
            for m in re.finditer(FORBIDDEN_WORDS, text)]


def forbidden_word_near(recognize, target) -> str | None:
    """A forbidden word within FORBIDDEN_MARGIN of ``target`` (a point or a box), each word located on its own
    (word_boxes) in the rows read on the full frame. For the named HUD actions, where the shortcut labels sit close
    together and 共鸣 (the gacha) is one of them; elsewhere it is as strict as _forbidden_near."""
    result = recognize(FORBIDDEN)
    if result is None:
        return "检查不了要点的地方附近有没有危险按钮"
    if not getattr(result, "hit", False):
        return None
    box = [int(v) for v in target] if len(target) == 4 else [int(target[0]), int(target[1]), 1, 1]
    for row in _rows(result):
        found, text = getattr(row, "box", None), str(getattr(row, "text", "") or "")
        if found is None or len(found) != 4:
            continue
        if any(_within_margin(word, box) for word in word_boxes(text, found)):
            return text or "危险按钮"
    return None


def _forbidden_near(recognize, box) -> str | None:
    """A forbidden label whose box comes within FORBIDDEN_MARGIN of ``box``, read whole on the full frame (a region
    cropped around the point could cut 删除 off a label that ends just inside the margin)."""
    result = recognize(FORBIDDEN)
    if result is None:
        return "检查不了要点的地方附近有没有危险按钮"
    if not getattr(result, "hit", False):
        return None
    for row in _rows(result):
        if _within_margin([int(v) for v in getattr(row, "box", None) or []], box):
            return str(getattr(row, "text", "") or "危险按钮")
    return None


def tap_on_page(rt, title: str, node: str, *, override=None, delay: float = .5, verify=None, accept=None) -> bool:
    """Tap ``node`` on the page titled ``title`` (decided and confirmed on fresh frames). ``verify(frame)`` and
    ``accept(frame, recognize)``, when given, must hold on both of them too (e.g. a button that is enabled, the whole
    expected state of the page). False when the run stopped; PageMismatch (nothing sent) when the page, its target,
    ``verify`` or ``accept`` fails on the deciding frame; GuardRefusal when the point is next to a forbidden word."""
    from task_control import ForegroundChanged
    for _attempt in range(rt.TOUCH_KEY_ATTEMPTS):
        screen, decided, recognize = screen_now(rt)
        if decided is None:
            return False
        target = _target(screen, recognize, title, node, override)
        word = _forbidden_near(recognize, target)
        if word:
            raise GuardRefusal(f"要点的地方旁边有「{word}」，没有点，任务停止")
        if (verify is not None and not verify(decided)) or (accept is not None and not accept(decided, recognize)):
            raise PageMismatch(f"「{title}」页面上要点的东西状态不对")
        if not rt._frame_still_valid(decided):
            continue
        fresh, confirming, recognize_b = screen_now(rt)
        if confirming is None:
            return False
        try:
            confirmed = _target(fresh, recognize_b, title, node, override)
        except PageMismatch:
            continue
        if fresh.title != screen.title or not actions._near(confirmed, target):
            continue
        word = _forbidden_near(recognize_b, confirmed)
        if word:
            raise GuardRefusal(f"要点的地方旁边有「{word}」，没有点，任务停止")
        if verify is not None and not verify(confirming):
            continue
        if accept is not None and not accept(confirming, recognize_b):
            continue
        if not (rt._frame_still_valid(decided) and rt._frame_still_valid(confirming)):
            continue
        try:
            if not rt.action("Click", target=confirmed, source_frame=confirming):
                return False
        except ForegroundChanged:
            continue
        return rt.pause(delay)
    raise PageMismatch(f"在「{title}」页面上连续 {rt.TOUCH_KEY_ATTEMPTS} 次没能在稳定的画面上确认要点的位置")


def to_hud(rt, attempts: int = 4) -> bool:
    """Back to the open-world HUD before a task: the back arrow, tapped through tap_on_page on the page that is
    showing (both tap frames, with the forbidden-label check); a modal, the quit dialog or an unrecognised screen
    stops the task without a tap."""
    for _ in range(attempts):
        screen, frame, _ = screen_now(rt)
        if frame is None or not rt._frame_still_valid(frame):
            return False
        if screen.kind == "hud":
            return True
        if screen.kind != "page" or not tap_on_page(rt, screen.title, actions.BACK_ARROW):
            return False
    return False


def back_through(rt, titles, attempts: int = 3) -> bool:
    """The way back after a task: the back arrow, tapped only on pages whose title starts with one of ``titles``
    (both tap frames, with the forbidden-label check); any other screen stops input."""
    for _ in range(attempts):
        screen, frame, _ = screen_now(rt)
        if frame is None or not rt._frame_still_valid(frame):
            return False
        if screen.kind == "hud":
            return True
        title = next((title for title in titles if is_page(screen, title)), None)
        if title is None or not tap_on_page(rt, title, actions.BACK_ARROW):
            return False
    return False


def tap_on_layer(rt, what: str, accept, point, *, delay: float = .5) -> bool:
    """Tap the fixed ``point`` on a screen that is not a page (e.g. a reward layer that closes on a tap on blank
    space), with the rules of tap_on_page: ``accept(screen, recognize)`` must recognise the layer on the deciding
    frame and again on a fresh confirming frame, no forbidden label may come within FORBIDDEN_MARGIN of the point on
    either, both frames must still be valid, and the tap carries the confirming frame. False when the run stopped;
    LayerMismatch (nothing sent, no second attempt) with the first tap frame that is not the layer; GuardRefusal next
    to a forbidden word. Only a frame voided by a focus change is decided again."""
    from task_control import ForegroundChanged
    box = [int(point[0]), int(point[1]), 1, 1]
    for _attempt in range(rt.TOUCH_KEY_ATTEMPTS):
        screen, decided, recognize = screen_now(rt)
        if decided is None:
            return False
        if not accept(screen, recognize):
            raise LayerMismatch(f"当前画面不是{what}", screen, decided, recognize)
        word = _forbidden_near(recognize, box)
        if word:
            raise GuardRefusal(f"要点的地方旁边有「{word}」，没有点，任务停止")
        if not rt._frame_still_valid(decided):
            continue
        fresh, confirming, recognize_b = screen_now(rt)
        if confirming is None:
            return False
        if not accept(fresh, recognize_b):
            raise LayerMismatch(f"确认时画面已经不是{what}", fresh, confirming, recognize_b)
        word = _forbidden_near(recognize_b, box)
        if word:
            raise GuardRefusal(f"要点的地方旁边有「{word}」，没有点，任务停止")
        if not (rt._frame_still_valid(decided) and rt._frame_still_valid(confirming)):
            continue
        try:
            if not rt.action("Click", target=box[:2], source_frame=confirming):
                return False
        except ForegroundChanged:
            continue
        return rt.pause(delay)
    raise PageMismatch(f"连续 {rt.TOUCH_KEY_ATTEMPTS} 次没能在稳定的画面上确认{what}（焦点在变）")
