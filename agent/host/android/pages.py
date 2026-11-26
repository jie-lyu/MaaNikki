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
FORBIDDEN_WORDS = "删除|购买|充值|共鸣|确认退出|解锁.*颂歌|恢复默认|¥|￥|兑换钻石|获取|开通|退出登录|切换账号|注销"
HUD_LABELS = tuple(actions.HUD_ROW_TEXT.split("|"))


class PageMismatch(actions.ScreenUnrecognised):
    """The frame is not the page the step expects (or its target is not on it). ``frame``: the frame that refused,
    when known (the caller saves it)."""
    frame = None


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
    ``frame``: the frame the word was read on, when known (the caller saves it). The message is fixed (it reaches
    the user); ``row``, the OCR'd line that refused, belongs in the diagnostic only (``detail``)."""

    def __init__(self, reason, frame=None, row=None):
        super().__init__(reason)
        self.frame, self.row = frame, row

    @property
    def detail(self) -> str:
        return f"{self}（读到「{self.row}」）" if self.row else str(self)


GUARD_TAP = "要点的地方旁边有危险按钮，没有点，任务停止"
GUARD_SWIPE = "要滑动的地方旁边有危险按钮，没有滑动，任务停止"


def diagnostic_reason(error) -> str:
    """What a failure scene records for ``error``: a guard refusal's row too (never shown to the user)."""
    return getattr(error, "detail", None) or str(error)


def screen_now(rt):
    """``(screen, frame, recognize)`` on a fresh focus-confirmed frame; frame is None when the run stopped."""
    frame = rt._fresh_device_frame()
    if frame is None:
        return None, None, None
    recognize = rt._recognizer_on(frame)
    return actions.read_screen(recognize), frame, recognize


same_title = actions.same_title


def _squash(text: str) -> str:
    return re.sub(r"\s", "", text or "")


def is_page(screen, title: str) -> bool:
    """A page whose title starts with ``title`` (a page named in code, e.g. 「邮件」 for 「邮件 7/300 i」), whitespace
    aside. A page observed on screen is left by its whole title instead (``exact``, same_title)."""
    return screen is not None and screen.kind == "page" and _squash(screen.title).startswith(_squash(title))


def is_exactly(screen, title: str) -> bool:
    """The page titled ``title`` (whitespace aside), e.g. the one observed before its back arrow."""
    return screen is not None and screen.kind == "page" and same_title(screen.title, title)


def wait_screen(rt, what: str, done, source, seconds: float, step: float = .4):
    """Poll fresh valid frames until ``done(screen, frame, recognize)`` holds and return ``(screen, frame,
    recognize)``. Frames that ``source`` takes (the screen being left) and frames on which nothing at all is
    recognised yet (a transition) are waited through up to ``seconds``; any other frame (another page, the quit
    dialog, a popup, ...) raises FrameMismatch with that frame at once, and so does the time running out (with the
    last frame). None when the run stopped."""
    import time
    deadline = time.monotonic() + seconds
    while not rt.stopped:
        screen, frame, recognize = screen_now(rt)
        if frame is None:
            return None
        if rt._frame_still_valid(frame):
            if done(screen, frame, recognize):
                return screen, frame, recognize
            if not (source(screen, frame, recognize) or nothing_recognised(screen, recognize)):
                raise FrameMismatch(f"{what}时出现了不该出现的画面", screen, frame, recognize)
            if time.monotonic() >= deadline:
                raise FrameMismatch(f"{what}超时", screen, frame, recognize)
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


def is_modal(screen, title: str) -> bool:
    return screen is not None and screen.kind == "modal" and screen.title.startswith(title)


SCREEN_WORDS = {"page": "页面", "modal": "弹窗"}


def _on_screen(screen, title, exact=False, kind="page") -> bool:
    if kind != "page":
        return is_modal(screen, title)
    return is_exactly(screen, title) if exact else is_page(screen, title)


def _target(screen, recognize, title, node, override, exact=False, kind="page", place=None):
    place = place or f"「{title}」{SCREEN_WORDS[kind]}"
    if not _on_screen(screen, title, exact, kind):
        raise PageMismatch(f"当前画面不是{place}")
    result = recognize(node, override) if override else recognize(node)
    if not actions._hit(result):
        raise PageMismatch(f"{place}上找不到要点的东西（{node}）")
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
                retry: bool = True, exact: bool = False, kind: str = "page", place: str | None = None) -> bool:
    """Tap ``node`` on the page titled ``title`` (decided and confirmed on fresh frames; ``kind="modal"``: on the
    recognised modal named ``title`` instead, e.g. 兑换奖励, with the same rules). ``verify(frame)`` and
    ``accept(frame, recognize)``, when given, must hold on both of them too (e.g. a button that is enabled, the whole
    expected state of the page). False when the run stopped; GuardRefusal when the point is next to a forbidden word.

    The deciding frame must be that page with the target (and ``verify``/``accept``): any other frame raises
    FrameMismatch with it, nothing sent. A confirming frame that shows another screen (another page or title, the
    quit dialog, a popup, anything recognised) raises FrameMismatch with it too and is never decided past. With
    ``retry``, a confirming frame on which nothing is recognised yet (a transition), or the same page with its target
    moved or its state not yet right, means deciding again; without it those raise FrameMismatch as well. Only a
    frame voided by a focus change is always decided again. ``exact``: the page's whole title must be ``title``
    (whitespace aside) on both frames, not only start with it (美鸭梨 and 美鸭梨挖掘; a page observed on screen and
    left by its own title). ``place``: how messages name the page (e.g. a page observed on screen, whose OCR'd title
    is not for the user)."""
    from task_control import ForegroundChanged
    place = place or f"「{title}」{SCREEN_WORDS[kind]}"
    last = None
    for _attempt in range(rt.TOUCH_KEY_ATTEMPTS):
        screen, decided, recognize = screen_now(rt)
        if decided is None:
            return False
        try:
            target = _target(screen, recognize, title, node, override, exact, kind, place)
        except PageMismatch as error:
            raise FrameMismatch(str(error), screen, decided, recognize) from None
        word = _forbidden_near(recognize, target)
        if word:
            raise GuardRefusal(GUARD_TAP, decided, word)
        if (verify is not None and not verify(decided)) or (accept is not None and not accept(decided, recognize)):
            raise FrameMismatch(f"{place}上要点的东西状态不对", screen, decided, recognize)
        if not rt._frame_still_valid(decided):
            continue
        fresh, confirming, recognize_b = screen_now(rt)
        if confirming is None:
            return False
        if not rt._frame_still_valid(confirming):
            continue
        last = fresh, confirming, recognize_b
        if not (_on_screen(fresh, title, exact, kind) and same_title(fresh.title, screen.title)):
            if retry and nothing_recognised(fresh, recognize_b):
                continue
            raise FrameMismatch(f"确认时画面已经不是{place}", fresh, confirming, recognize_b)
        result = recognize_b(node, override) if override else recognize_b(node)
        confirmed = actions._box(result) if actions._hit(result) else None
        if confirmed is None or not actions._near(confirmed, target):
            if retry:
                continue
            raise FrameMismatch(f"确认时{place}上要点的东西不在原处", fresh, confirming, recognize_b)
        word = _forbidden_near(recognize_b, confirmed)
        if word:
            raise GuardRefusal(GUARD_TAP, confirming, word)
        if (verify is not None and not verify(confirming)) or \
                (accept is not None and not accept(confirming, recognize_b)):
            if retry:
                continue
            raise FrameMismatch(f"确认时{place}上要点的东西状态不对", fresh, confirming, recognize_b)
        if not (rt._frame_still_valid(decided) and rt._frame_still_valid(confirming)):
            continue
        try:
            if not rt.action("Click", target=confirmed, source_frame=confirming):
                return False
        except ForegroundChanged:
            continue
        return rt.pause(delay)
    reason = f"在{place}上连续 {rt.TOUCH_KEY_ATTEMPTS} 次没能在稳定的画面上确认要点的位置"
    if last is not None:
        raise FrameMismatch(reason, *last)
    raise PageMismatch(reason)


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
                raise GuardRefusal(GUARD_SWIPE, decided, word)
        if not accept(decided, recognize):
            raise SwipeMismatch(f"「{title}」页面的状态不对，没有滑动", screen, decided, recognize)
        fresh, confirming, recognize_b = screen_now(rt)
        if confirming is None:
            return False
        if not (rt._frame_still_valid(decided) and rt._frame_still_valid(confirming)):
            continue
        if not is_page(fresh, title) or not same_title(fresh.title, screen.title):
            raise SwipeMismatch(f"确认滑动时画面已经不是「{title}」页面", fresh, confirming, recognize_b)
        for box in points:
            word = _forbidden_near(recognize_b, box)
            if word:
                raise GuardRefusal(GUARD_SWIPE, confirming, word)
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


LEAVING = "要离开的页面"  # how messages name a page observed on screen (its OCR'd title is not for the user)


def to_hud(rt, attempts: int = 4, *, rejected=None, single: bool = False) -> bool:
    """Back to the open-world HUD before a task: the back arrow of the page that is showing, tapped through
    tap_on_page on that page left by its whole observed title (``exact``: whitespace aside, both tap frames, with the
    forbidden-label check). A modal, the quit dialog, an unrecognised screen or a tap frame that is not that page
    (FrameMismatch, nothing sent) stops the task; ``rejected(frame, reason)``, when given, receives the frame it
    stopped on (the refusing tap frame when there is one). A guard refusal propagates. Reasons never quote an OCR'd
    title.

    ``single``: each back arrow is one attempt (tap_on_page with retry=False: a tap frame that is not that page in
    the same state stops, with that frame), and no other back arrow is decided until fresh frames show that page
    gone (left_page): the HUD (done, also after the last allowed back arrow) or a page with another title. That page
    staying up for LEAVE_PAGE_WAIT, or anything else positively recognised meanwhile (the quit dialog, a modal such
    as 兑换奖励, an unknown popup), stops with that frame instead of being tapped again."""
    for _ in range(attempts):
        screen, frame, _ = screen_now(rt)
        if frame is None:
            return False
        if not rt._frame_still_valid(frame):
            reason = "焦点变了，这张截图作废"
        elif screen.kind == "hud":
            return True
        elif screen.kind == "page":
            title = screen.title
            try:
                if not tap_on_page(rt, title, actions.BACK_ARROW, retry=not single, exact=True, place=LEAVING):
                    return False
            except GuardRefusal:
                raise
            except PageMismatch as error:  # nothing sent
                frame, reason = (error.frame if error.frame is not None else frame), str(error)
            else:
                if not single:
                    continue
                state, frame, after = left_page(rt, title)
                if state is None:
                    return False
                if state == "left":
                    if after.kind == "hud":
                        return True
                    continue
                reason = ("点了返回箭头之后，页面一直没有离开" if state == "timeout" else
                          f"点了返回箭头之后出现了{after.reason or SCREEN_KINDS.get(after.kind, after.kind)}")
        else:
            reason = screen.reason or f"当前画面是{SCREEN_KINDS.get(screen.kind, screen.kind)}"
        if rejected is not None:
            rejected(frame, reason)
        return False
    return False


SCREEN_KINDS = {"hud": "大世界", "page": "另一个页面", "modal": "弹窗", "quit_dialog": "退出确认框", "unknown": "认不出的画面"}


def left_page(rt, title: str, seconds: float | None = None, step: float = .3):
    """After a back arrow on the page titled exactly ``title``: ``("left", frame, screen)`` once a fresh valid frame
    shows the HUD or a page with another title (whitespace aside: OCR spacing alone is the same page); ``("other",
    frame, screen)`` on the first one that shows anything else recognised (the quit dialog, a modal, an unknown
    screen with a close mark, duck or back arrow: not proof that the page went); ``("timeout", frame, screen)`` when the time runs out first (the last valid frame); frames
    of that page and frames on which nothing at all is recognised yet are waited through. ``(None, None, None)``
    when the run stopped."""
    import time
    seconds = LEAVE_PAGE_WAIT if seconds is None else seconds
    deadline, last = time.monotonic() + seconds, (None, None)
    while not rt.stopped:
        screen, frame, recognize = screen_now(rt)
        if frame is None:
            return None, None, None
        if rt._frame_still_valid(frame):
            last = frame, screen
            if screen.kind == "hud" or (screen.kind == "page" and not same_title(screen.title, title)):
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
            raise GuardRefusal(f"「{name}」快捷入口旁边有危险按钮，没有点，任务停止", decided, word)
        fresh, confirming, recognize_b = screen_now(rt)
        if confirming is None:
            return False
        if not (rt._frame_still_valid(decided) and rt._frame_still_valid(confirming)):
            continue
        # The confirming frame's first reading decides: a quit dialog or any other screen read on it stops here,
        # before confirm_tap reads the same image again (a second reading must not overrule a positive refusal).
        if fresh is None or fresh.kind != "hud":
            raise FrameMismatch(f"确认时画面已经不是大世界，没有点「{name}」快捷入口", fresh, confirming, recognize_b)
        target = actions.confirm_tap(plan, screen, recognize_b)
        if target is None:
            raise FrameMismatch(f"确认时画面已经不是大世界，或「{name}」快捷入口不在原处", fresh, confirming,
                                recognize_b)
        word = forbidden_word_near(recognize_b, target)
        if word:
            raise GuardRefusal(f"「{name}」快捷入口旁边有危险按钮，没有点，任务停止", confirming, word)
        if not (rt._frame_still_valid(decided) and rt._frame_still_valid(confirming)):
            continue
        try:
            if not rt.action("Click", target=target, source_frame=confirming):
                return False
        except ForegroundChanged:
            continue
        return rt.pause(delay)
    raise PageMismatch(f"连续 {rt.TOUCH_KEY_ATTEMPTS} 次没能在稳定的画面上确认「{name}」快捷入口（焦点在变）")


def tap_duck(rt, *, words=None, check=None) -> bool:
    """The 美鸭梨 menu from the HUD by one strict tap on the duck (tap_on_layer: the HUD with the duck in the same
    place on both tap frames, no forbidden label within FORBIDDEN_MARGIN of the whole duck on either, read as the
    named action reads it, forbidden_word_near). Unlike the shared named action (Runtime.key("menu"), which
    cancels a quit dialog and decides again, architecture decision 2), a quit dialog or any other screen on any
    frame stops with that frame (FrameMismatch); there is no second attempt. False when the run stopped.
    ``words(recognize, box)``, when given, is a task's own danger-word check, run around the whole duck on both
    frames after the shared one; the label it returns refuses the tap (GuardRefusal) as a forbidden one does.
    ``check(screen, recognize)``, when given, must also hold on both frames (else LayerMismatch with that frame)."""
    from host.android import touch_point
    screen, frame, recognize = screen_now(rt)
    if frame is None:
        return False
    if screen is None or screen.kind != "hud":
        raise FrameMismatch("要打开美鸭梨菜单时画面不是大世界", screen, frame, recognize)
    point = touch_point(screen.targets["duck"])

    def accept(screen_, recognize_):
        return screen_.kind == "hud" and actions._near(screen_.targets.get("duck"), point) \
            and (check is None or bool(check(screen_, recognize_)))

    def guard(screen_, recognize_):  # around the whole duck as recognised on that frame, as the named action
        duck = screen_.targets["duck"]
        return forbidden_word_near(recognize_, duck) or (words(recognize_, duck) if words is not None else None)
    return tap_on_layer(rt, "鸭子图标在原处的大世界", accept, point, guard=guard)


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


def tap_on_layer(rt, what: str, accept, point, *, delay: float = .5, verify=None, guard=None) -> bool:
    """Tap the fixed ``point`` on a screen that is not a page (e.g. a reward layer that closes on a tap on blank
    space), with the rules of tap_on_page: ``accept(screen, recognize)`` must recognise the layer on the deciding
    frame and again on a fresh confirming frame, no forbidden label may come within FORBIDDEN_MARGIN of the point on
    either, both frames must still be valid, and the tap carries the confirming frame. False when the run stopped;
    LayerMismatch (nothing sent, no second attempt) with the first tap frame that is not the layer; GuardRefusal next
    to a forbidden word. Only a frame voided by a focus change is decided again. ``verify(frame)``, when given, must
    hold on both frames too (e.g. no input strip across the point), else LayerMismatch with that frame.
    ``guard(screen, recognize)``, when given, replaces the forbidden-label check around the point on both frames
    (e.g. around the whole recognised icon); it returns the label that refuses, or None."""
    from task_control import ForegroundChanged
    box = [int(point[0]), int(point[1]), 1, 1]
    if guard is None:
        def guard(_screen, recognize_):
            return _forbidden_near(recognize_, box)
    for _attempt in range(rt.TOUCH_KEY_ATTEMPTS):
        screen, decided, recognize = screen_now(rt)
        if decided is None:
            return False
        if not accept(screen, recognize) or (verify is not None and not verify(decided)):
            raise LayerMismatch(f"当前画面不是{what}", screen, decided, recognize)
        word = guard(screen, recognize)
        if word:
            raise GuardRefusal(GUARD_TAP, decided, word)
        if not rt._frame_still_valid(decided):
            continue
        fresh, confirming, recognize_b = screen_now(rt)
        if confirming is None:
            return False
        if not accept(fresh, recognize_b) or (verify is not None and not verify(confirming)):
            raise LayerMismatch(f"确认时画面已经不是{what}", fresh, confirming, recognize_b)
        word = guard(fresh, recognize_b)
        if word:
            raise GuardRefusal(GUARD_TAP, confirming, word)
        if not (rt._frame_still_valid(decided) and rt._frame_still_valid(confirming)):
            continue
        try:
            if not rt.action("Click", target=box[:2], source_frame=confirming):
                return False
        except ForegroundChanged:
            continue
        return rt.pause(delay)
    raise PageMismatch(f"连续 {rt.TOUCH_KEY_ATTEMPTS} 次没能在稳定的画面上确认{what}（焦点在变）")


TEXT_ACTION = "InputText"  # how a text input is named in logs/android-input.jsonl


def held_contact(rt):
    """Before a text input: the source frame showed a contact held (or no readable touch state): all input halts."""
    from host import InputUnconfirmed
    rt._halt_unconfirmed()
    raise InputUnconfirmed("安卓模拟器上还有按住的触点（或读不出设备的触摸状态），文字没有输入，已停止所有输入；"
                           "请检查模拟器后重新启动 MaaNikki。")


def input_text_on_screen(rt, text: str, what: str, accept) -> bool:
    """Type ``text`` into the focused field of a recognised screen through the framework's text input (MaaTouch: it
    puts the text on the device's clipboard and sends a paste key; never the host clipboard, `input text` or an
    in-game paste button). Text input is not a touch, but it is input and follows the same rules: decided on a fresh
    focus-confirmed frame that ``accept(screen, frame, recognize)`` takes (the caller's whole expected state, e.g. the
    modal recognised, its field focused and empty), confirmed on a second fresh frame that it also takes, both frames
    still valid, and sent with the confirming frame as its source through send_text (logged intent first, refused
    when a stop is requested or input is halted). False when the run stopped or the input was not sent;
    FrameMismatch (nothing sent, no second attempt) with the first frame that ``accept`` refuses. Only a frame voided
    by a focus change is decided again. Whether the field then holds ``text`` is for the caller to read back."""
    from task_control import ForegroundChanged
    for _attempt in range(rt.TOUCH_KEY_ATTEMPTS):
        screen, decided, recognize = screen_now(rt)
        if decided is None:
            return False
        if not rt._frame_still_valid(decided):
            continue
        if not accept(screen, decided, recognize):
            raise FrameMismatch(f"要输入文字时画面不是{what}", screen, decided, recognize)
        fresh, confirming, recognize_b = screen_now(rt)
        if confirming is None:
            return False
        if not (rt._frame_still_valid(decided) and rt._frame_still_valid(confirming)):
            continue
        if not accept(fresh, confirming, recognize_b):
            raise FrameMismatch(f"确认输入文字时画面已经不是{what}", fresh, confirming, recognize_b)
        try:
            return send_text(rt, text, confirming)
        except ForegroundChanged:
            continue
    raise PageMismatch(f"连续 {rt.TOUCH_KEY_ATTEMPTS} 次没能在稳定的画面上确认{what}（焦点在变）")


def send_text(rt, text: str, source_frame) -> bool:
    """The last checks and the dispatch of one text input decided on ``source_frame``, in the order of a touch
    (Runtime._dispatch_touch): the frame's marks still current; under the input lock, nothing halted or stopping;
    the device's reading taken with the frame reserved as for a touch (config/android-input/touch.json, so a
    stopper waits for it and an interrupted input is left for the runner to check), the intent logged to
    logs/android-input.jsonl ("sending"; not sent when that line cannot be written), then no contact held when the
    frame was captured, the game focused on the device now, the frame still current, no stop and no stop request;
    then Controller.post_input_text and its outcome logged. A text input has no lift to confirm (it is a paste key,
    no contact): the reservation is removed once the framework reports it done. A failure or an exception halts all
    input and leaves the reservation (as an unconfirmed touch does): what reached the field is unknown. Text that is or
    holds a UID (host.android.account.uid_shaped) is refused first: never typed, never journalled."""
    from host import InputUnconfirmed, UnsupportedInput, android_user_stop, device_input
    from host.android.account import uid_shaped
    from task_control import ForegroundChanged
    if uid_shaped(text):  # account review round 2, finding 6: its raw text would land in the input journal
        raise UnsupportedInput("要输入的文字像游戏 UID，没有输入，也没有写进输入记录。")
    rt.check_focus()
    state = device_input()
    if state.marks_of(source_frame) != (rt._focus_generation, state.epoch):
        raise ForegroundChanged("依据的截图已经过时（之后焦点变过），已取消本次文字输入，等待重新核对")
    fields = {"text": text}
    with state.lock:
        if state.halted or rt.stopped:
            return False
        if state.predates_input(source_frame, gesture=False):  # captured before a gesture (Runtime._dispatch_touch)
            raise UnsupportedInput("依据的截图是在上一次手势之前截的，画面可能已经变了，这次文字输入没有发送。")
        target = rt._sessionless_target()
        before = state.touch_of(source_frame)
        booted = getattr(before, "boot_id", None)
        try:
            state.begin_touch({"serial": target.serial, "boot_id": booted} if booted else None)
        except BaseException:
            if state.halted:
                rt._halt_unconfirmed()
            raise
        sent = intent = False
        try:
            if not state.record_touch(TEXT_ACTION, fields, "sending"):
                raise UnsupportedInput("无法写入安卓模拟器的输入记录，这次文字输入没有发送。")
            intent = True
            # In the order of Runtime._dispatch_touch: a contact seen held is reported first; an unreadable touch
            # reading with the user's stop already published is cancelled as a stop; a stop published by now frees
            # the reservation before any focus/ADB I/O.
            if before is not None and before.state != "none":
                held_contact(rt)
            if state.halted or rt.stopped:
                return False
            if before is None and android_user_stop(ask_tasker=False):
                return False
            if before is None:
                held_contact(rt)
            if state.halted or rt.stopped or android_user_stop(ask_tasker=False):
                return False
            if not rt._device_focus_now():
                raise ForegroundChanged("设备上游戏不在前台，已取消本次文字输入，等待重新核对")
            if state.marks_of(source_frame) != (rt._focus_generation, state.epoch):
                raise ForegroundChanged("依据的截图已经过时（之后焦点变过），已取消本次文字输入，等待重新核对")
            if rt._stopped_now() or state.stop_requested():
                return False
            sent = True
            state.sent_input()  # a gesture after this needs a frame captured after it
            try:
                succeeded = bool(rt.controller.post_input_text(text).wait().succeeded)
            except BaseException as error:
                state.record_touch(TEXT_ACTION, fields, f"raised {type(error).__name__}")
                rt._halt_unconfirmed()
                if not isinstance(error, Exception):
                    raise
                # Always reported as an unconfirmed input, whatever was raised (as Runtime._dispatch_touch does): no
                # handler that turns an ordinary error into a failure result or a quiet stop may hide it.
                raise InputUnconfirmed(f"安卓模拟器上的文字输入发出后没能确认完成（{type(error).__name__}: {error}），"
                                       "输入框里是什么不确定，已停止所有输入；请检查模拟器后重新启动 MaaNikki。") from error
            state.record_touch(TEXT_ACTION, fields, "sent" if succeeded else "failed")
        finally:
            if not sent:
                if intent:
                    state.record_touch(TEXT_ACTION, fields, "cancelled")
                rt._release_touch(state)
        if not succeeded:
            rt._halt_unconfirmed()
            raise InputUnconfirmed("安卓模拟器上的文字输入没有报告完成，输入框里是什么不确定，已停止所有输入；"
                                   "请检查模拟器后重新启动 MaaNikki。")
        rt._release_touch(state)
    return True
