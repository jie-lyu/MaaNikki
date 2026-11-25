"""Taps and swipes on a recognised page of the Android client, with the same rules as the named actions (actions.py):

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


class FrameMismatch(PageMismatch):
    """A tap frame is not what the tap needs and the tap is not tried again: the frame itself (``screen``,
    ``frame``, ``recognize``) goes to the caller to decide on or keep as evidence; nothing was sent."""

    def __init__(self, reason, screen, frame, recognize):
        super().__init__(reason)
        self.screen, self.frame, self.recognize = screen, frame, recognize


class LayerMismatch(FrameMismatch):
    """A tap frame of tap_on_layer is not the layer."""


class GuardRefusal(PageMismatch):
    """A forbidden word lies next to the point: the task stops all input (no navigation afterwards either).
    ``frame``: the frame the word was read on, when known (the caller saves it)."""

    def __init__(self, reason, frame=None):
        super().__init__(reason)
        self.frame = frame


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


def nothing_recognised(screen, recognize) -> bool:
    """An unrecognised screen on which nothing at all was recognised (no quit title or 取消, no modal close mark, no
    duck, no back arrow, and every one of those checks ran): what a screen in transition looks like. A screen with
    any of them is something else and is not waited through."""
    if screen is None or screen.kind != "unknown":
        return False
    for node in (actions.QUIT_TITLE, actions.QUIT_CANCEL, actions.MODAL_CLOSE, actions.HUD_DUCK, actions.BACK_ARROW):
        result = recognize(node)
        if result is None or actions._hit(result):
            return False
    return True


def _target(screen, recognize, title, node, override, exact=False):
    if not is_page(screen, title) or (exact and screen.title != title):
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


def hud_label_row(text: str) -> bool:
    """Whether an OCR line holds nothing but HUD shortcut labels (the forbidden words in it, 共鸣, among them): OCR
    merges the densely packed label row (「共鸣 奇迹之旅活动大厅…」) and pads a lone 「共鸣」 into its neighbour, so its
    box says little about where the word is. Any other line (购买等级, ¥6, a standalone 「解锁悠远颂歌」) counts whole."""
    if not text.strip():
        return False
    if any(match.group(0) not in HUD_LABELS for match in re.finditer(FORBIDDEN_WORDS, text)):
        return False
    rest = text
    for label in sorted(HUD_LABELS, key=len, reverse=True):
        rest = rest.replace(label, "")
    return not re.sub(r"\s", "", rest)


WORD_EDGE = 6  # a word read in a window counts only when its box stays this far inside the window (not clipped)


def locate_word(recognize, line_text: str, line_box, start: int, word: str):
    """The box of ``word`` (at character ``start`` of an OCR line of HUD labels) read on its own by OCR in windows
    of the line, or None when no window reads it whole. A window counts only when it reads exactly the word and the
    word's box stays WORD_EDGE inside the window's right side (and its left side, for a word inside the line), so a
    glyph cut by the window cannot make the word look shorter than it is. A word at the start of the line keeps the
    line's left edge."""
    x, y, w, h = [int(v) for v in line_box]
    glyph = max(1, h)
    estimate = round(x + w * start / max(1, len(line_text)))
    lefts = (x - 4, x) if start == 0 else (estimate - glyph, estimate - WORD_EDGE - 4)
    for left in (max(0, value) for value in lefts):
        for width in range(round(glyph * (len(word) + .5)), round(glyph * (len(word) + 2.5)) + 1, 4):
            result = recognize(FORBIDDEN, {"roi": [left, max(0, y - 4), width, h + 8], "expected": ".+"})
            if result is None or not getattr(result, "hit", False):
                continue
            for row in _rows(result):
                found = [int(v) for v in getattr(row, "box", None) or []]
                if str(getattr(row, "text", "") or "").strip() != word or len(found) != 4:
                    continue
                if found[0] + found[2] > left + width - WORD_EDGE:
                    continue  # touches the window's right side: maybe cut
                if start == 0:
                    right = found[0] + found[2]
                    return [min(found[0], x), found[1], right - min(found[0], x), found[3]]
                if found[0] >= left + WORD_EDGE:
                    return found
    return None


def forbidden_word_near(recognize, target) -> str | None:
    """A forbidden word within FORBIDDEN_MARGIN of ``target`` (a point or a box), for the named HUD actions, where
    the shortcut labels sit close together and 共鸣 (the gacha) is one of them. Every line read on the full frame
    counts with its full OCR box, except a line of HUD labels within the margin: each forbidden word in it must then
    be read on its own (locate_word) and clear the margin, and a word that cannot be read on its own refuses."""
    result = recognize(FORBIDDEN)
    if result is None:
        return "检查不了要点的地方附近有没有危险按钮"
    if not getattr(result, "hit", False):
        return None
    box = [int(v) for v in target] if len(target) == 4 else [int(target[0]), int(target[1]), 1, 1]
    for row in _rows(result):
        found, text = getattr(row, "box", None), str(getattr(row, "text", "") or "")
        if found is None or len(found) != 4 or not _within_margin([int(v) for v in found], box):
            continue
        if not hud_label_row(text):
            return text or "危险按钮"
        for match in re.finditer(FORBIDDEN_WORDS, text):
            word = locate_word(recognize, text, found, match.start(), match.group(0))
            if word is None or _within_margin(word, box):
                return match.group(0)
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


def tap_on_page(rt, title: str, node: str, *, override=None, delay: float = .5, verify=None, accept=None,
                retry: bool = True, exact: bool = False) -> bool:
    """Tap ``node`` on the page titled ``title`` (decided and confirmed on fresh frames). ``verify(frame)`` and
    ``accept(frame, recognize)``, when given, must hold on both of them too (e.g. a button that is enabled, the whole
    expected state of the page). False when the run stopped; PageMismatch (nothing sent) when the page, its target,
    ``verify`` or ``accept`` fails on the deciding frame; GuardRefusal when the point is next to a forbidden word.
    A confirming frame that does not agree means deciding again, unless ``retry`` is False: then a deciding or
    confirming frame that does not agree raises FrameMismatch with that frame (only a frame voided by a focus change
    is decided again). ``exact``: the page's title must be exactly ``title`` on both frames (not only start with
    it, e.g. 美鸭梨 and 美鸭梨挖掘)."""
    from task_control import ForegroundChanged

    def mismatch(reason, screen_, frame_, recognize_):
        if retry:
            return False
        raise FrameMismatch(reason, screen_, frame_, recognize_)
    for _attempt in range(rt.TOUCH_KEY_ATTEMPTS):
        screen, decided, recognize = screen_now(rt)
        if decided is None:
            return False
        try:
            target = _target(screen, recognize, title, node, override, exact)
        except PageMismatch as error:
            mismatch(str(error), screen, decided, recognize)
            raise
        word = _forbidden_near(recognize, target)
        if word:
            raise GuardRefusal(f"要点的地方旁边有「{word}」，没有点，任务停止", decided)
        if (verify is not None and not verify(decided)) or (accept is not None and not accept(decided, recognize)):
            mismatch(f"「{title}」页面上要点的东西状态不对", screen, decided, recognize)
            raise PageMismatch(f"「{title}」页面上要点的东西状态不对")
        if not rt._frame_still_valid(decided):
            continue
        fresh, confirming, recognize_b = screen_now(rt)
        if confirming is None:
            return False
        try:
            confirmed = _target(fresh, recognize_b, title, node, override, exact)
        except PageMismatch as error:
            mismatch(f"确认时{error}", fresh, confirming, recognize_b)
            continue
        if fresh.title != screen.title or not actions._near(confirmed, target):
            mismatch(f"确认时「{title}」页面上要点的东西不在原处", fresh, confirming, recognize_b)
            continue
        word = _forbidden_near(recognize_b, confirmed)
        if word:
            raise GuardRefusal(f"要点的地方旁边有「{word}」，没有点，任务停止", confirming)
        if (verify is not None and not verify(confirming)) or \
                (accept is not None and not accept(confirming, recognize_b)):
            mismatch(f"确认时「{title}」页面上要点的东西状态不对", fresh, confirming, recognize_b)
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


def _point_box(point):
    return [int(point[0]), int(point[1]), 1, 1]


class SwipeMismatch(FrameMismatch):
    """A tap frame of swipe_on_page is not the expected state: that frame goes to the caller, to be saved; nothing
    was sent and the swipe is not decided again."""


def swipe_on_page(rt, title: str, begin, end, *, duration: int, accept, delay: float = .5) -> bool:
    """One swipe from the point ``begin`` to the point ``end`` on the page titled ``title``, with the rules of
    tap_on_page: decided on a fresh focus-confirmed frame that is that page and that ``accept(frame, recognize)``
    takes (the caller's whole expected state, e.g. the list it read, no price band near either point), neither point
    within FORBIDDEN_MARGIN of a forbidden label; confirmed on a fresh frame (same title, ``accept``, guard); both
    frames still valid; sent as a Swipe carrying the confirming frame, through the same reserved, logged and
    device-confirmed path as a tap (the device must show this gesture's own lift afterwards). False when the run
    stopped; SwipeMismatch (nothing sent, no second attempt) with the first tap frame that is not the expected state;
    GuardRefusal next to a forbidden word. Only a frame voided by a focus change is decided again."""
    from task_control import ForegroundChanged
    points = (_point_box(begin), _point_box(end))
    for _attempt in range(rt.TOUCH_KEY_ATTEMPTS):
        screen, decided, recognize = screen_now(rt)
        if decided is None:
            return False
        if not rt._frame_still_valid(decided):
            continue
        if not is_page(screen, title):
            raise SwipeMismatch(f"要滑动时画面不是「{title}」页面", screen, decided, recognize)
        for box in points:
            word = _forbidden_near(recognize, box)
            if word:
                raise GuardRefusal(f"要滑动的地方旁边有「{word}」，没有滑动，任务停止", decided)
        if not accept(decided, recognize):
            raise SwipeMismatch(f"「{title}」页面的状态不对，没有滑动", screen, decided, recognize)
        fresh, confirming, recognize_b = screen_now(rt)
        if confirming is None:
            return False
        if not (rt._frame_still_valid(decided) and rt._frame_still_valid(confirming)):
            continue
        if not is_page(fresh, title) or fresh.title != screen.title:
            raise SwipeMismatch(f"确认滑动时画面已经不是「{title}」页面", fresh, confirming, recognize_b)
        for box in points:
            word = _forbidden_near(recognize_b, box)
            if word:
                raise GuardRefusal(f"要滑动的地方旁边有「{word}」，没有滑动，任务停止", confirming)
        if not accept(confirming, recognize_b):
            raise SwipeMismatch(f"确认滑动时「{title}」页面的状态变了，没有滑动", fresh, confirming,
                                recognize_b)
        if not (rt._frame_still_valid(decided) and rt._frame_still_valid(confirming)):
            continue
        try:
            if not rt.action("Swipe", begin=points[0][:2], end=points[1][:2], duration=int(duration),
                             source_frame=confirming):
                return False
        except ForegroundChanged:
            continue
        return rt.pause(delay)
    raise PageMismatch(f"在「{title}」页面上连续 {rt.TOUCH_KEY_ATTEMPTS} 次没能在稳定的画面上确认要滑动的位置（焦点在变）")


LEAVE_PAGE_WAIT = 5  # seconds a page may stay on screen after its back arrow was tapped (single-attempt to_hud)


def to_hud(rt, attempts: int = 4, *, rejected=None, single: bool = False) -> bool:
    """Back to the open-world HUD before a task: the back arrow, tapped through tap_on_page on the page that is
    showing (both tap frames, with the forbidden-label check); a modal, the quit dialog or an unrecognised screen
    stops the task without a tap. ``rejected(frame, reason)``, when given, receives the frame it stopped on.

    ``single``: each back arrow is one attempt on the page with exactly the title it was decided for (tap_on_page
    with retry=False, both tap frames that exact page; a tap frame that is not stops, with that frame), and no other
    back arrow is decided until fresh frames show that page gone (left_page): the HUD (done, also after the last
    allowed back arrow) or a page with another title. That page staying up for LEAVE_PAGE_WAIT, or anything else
    positively recognised meanwhile (the quit dialog, a modal such as 兑换奖励, an unknown popup), stops with that
    frame instead of being tapped again."""
    for _ in range(attempts):
        screen, frame, _ = screen_now(rt)
        if frame is None:
            return False
        if not rt._frame_still_valid(frame):
            reason = "焦点变了，这张截图作废"
        elif screen.kind == "hud":
            return True
        elif screen.kind == "page" and not single:
            if tap_on_page(rt, screen.title, actions.BACK_ARROW):
                continue
            return False
        elif screen.kind == "page":
            title = screen.title
            try:
                if not tap_on_page(rt, title, actions.BACK_ARROW, retry=False, exact=True):
                    return False
            except FrameMismatch as error:
                frame, reason = error.frame, str(error)
            else:
                state, frame, after = left_page(rt, title)
                if state is None:
                    return False
                if state == "left":
                    if after.kind == "hud":
                        return True
                    continue
                reason = (f"点了「{title}」的返回箭头之后，页面一直没有离开" if state == "timeout" else
                          f"点了「{title}」的返回箭头之后出现了{after.reason or after.title or after.kind}")
        else:
            reason = screen.reason or f"当前画面是{screen.kind}"
        if rejected is not None:
            rejected(frame, reason)
        return False
    return False


def left_page(rt, title: str, seconds: float = LEAVE_PAGE_WAIT, step: float = .3):
    """After a back arrow on the page titled exactly ``title``: ``("left", frame, screen)`` once a fresh valid frame
    shows the HUD or a page with another title; ``("other", frame, screen)`` on the first one that shows anything
    else recognised (the quit dialog, a modal, an unknown screen with a close mark, duck or back arrow: not proof
    that the page went); ``("timeout", frame, screen)`` when the time runs out first (the last valid frame); frames
    of that page and frames on which nothing at all is recognised yet are waited through. ``(None, None, None)``
    when the run stopped."""
    import time
    deadline, last = time.monotonic() + seconds, (None, None)
    while not rt.stopped:
        screen, frame, recognize = screen_now(rt)
        if frame is None:
            return None, None, None
        if rt._frame_still_valid(frame):
            last = frame, screen
            if screen.kind == "hud" or (screen.kind == "page" and screen.title != title):
                return "left", frame, screen
            if not (screen.kind == "page" or nothing_recognised(screen, recognize)):
                return "other", frame, screen
        if time.monotonic() >= deadline:
            return ("timeout", *last) if last[0] is not None else ("timeout", frame, screen)
        rt.pause(step)
    return None, None, None


def tap_shortcut(rt, action: str, *, delay: float = .35) -> bool:
    """One tap on the HUD shortcut ``action`` (actions.HUD_SHORTCUTS), only from the HUD: decided on a fresh frame
    that is the HUD with the shortcut found (actions.plan_tap), confirmed on a fresh frame (actions.confirm_tap), no
    forbidden word near the point on either (forbidden_word_near, as the named actions), and sent with the
    confirming frame. Unlike the named action (Runtime.key), it never cancels a quit dialog and never decides again
    on a frame that disagrees: a tap frame that is not the HUD with the shortcut in place raises FrameMismatch with
    that frame (nothing sent). False when the run stopped; GuardRefusal next to a forbidden word. Only a frame
    voided by a focus change is decided again."""
    from task_control import ForegroundChanged
    name = actions.ACTION_NAMES[action]
    if action not in actions.HUD_SHORTCUTS:
        raise ValueError(action)
    for _attempt in range(rt.TOUCH_KEY_ATTEMPTS):
        screen, decided, recognize = screen_now(rt)
        if decided is None:
            return False
        if not rt._frame_still_valid(decided):
            continue
        if screen is None or screen.kind != "hud":
            raise FrameMismatch(f"要点「{name}」快捷入口时画面不是大世界", screen, decided, recognize)
        try:
            plan = actions.plan_tap(action, screen, recognize)
        except actions.ScreenUnrecognised as error:
            raise FrameMismatch(str(error), screen, decided, recognize) from error
        word = forbidden_word_near(recognize, plan.target)
        if word:
            raise GuardRefusal(f"「{name}」快捷入口旁边有「{word}」，没有点，任务停止", decided)
        fresh, confirming, recognize_b = screen_now(rt)
        if confirming is None:
            return False
        if not (rt._frame_still_valid(decided) and rt._frame_still_valid(confirming)):
            continue
        target = actions.confirm_tap(plan, screen, recognize_b)
        if target is None:
            raise FrameMismatch(f"确认时画面已经不是大世界，或「{name}」快捷入口不在原处", fresh, confirming,
                                recognize_b)
        word = forbidden_word_near(recognize_b, target)
        if word:
            raise GuardRefusal(f"「{name}」快捷入口旁边有「{word}」，没有点，任务停止", confirming)
        if not (rt._frame_still_valid(decided) and rt._frame_still_valid(confirming)):
            continue
        try:
            if not rt.action("Click", target=target, source_frame=confirming):
                return False
        except ForegroundChanged:
            continue
        return rt.pause(delay)
    raise PageMismatch(f"连续 {rt.TOUCH_KEY_ATTEMPTS} 次没能在稳定的画面上确认「{name}」快捷入口（焦点在变）")


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
            raise GuardRefusal(f"要点的地方旁边有「{word}」，没有点，任务停止", decided)
        if not rt._frame_still_valid(decided):
            continue
        fresh, confirming, recognize_b = screen_now(rt)
        if confirming is None:
            return False
        if not accept(fresh, recognize_b):
            raise LayerMismatch(f"确认时画面已经不是{what}", fresh, confirming, recognize_b)
        word = _forbidden_near(recognize_b, box)
        if word:
            raise GuardRefusal(f"要点的地方旁边有「{word}」，没有点，任务停止", confirming)
        if not (rt._frame_still_valid(decided) and rt._frame_still_valid(confirming)):
            continue
        try:
            if not rt.action("Click", target=box[:2], source_frame=confirming):
                return False
        except ForegroundChanged:
            continue
        return rt.pause(delay)
    raise PageMismatch(f"连续 {rt.TOUCH_KEY_ATTEMPTS} 次没能在稳定的画面上确认{what}（焦点在变）")
