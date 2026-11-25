"""Taps on a recognised page of the Android client, with the same rules as the named actions (actions.py):

decided on one focus-confirmed frame, refused unless that frame is positively the expected page and the target is
recognised on it, refused when the point lies on or next to text that spends, deletes or commits (删除, 购买, ...),
confirmed on a fresh frame (same page, same title, target within a few pixels) and sent with that fresh frame as its
source. A screen that is not the expected one sends nothing.
"""
from __future__ import annotations

from host.android import actions

FORBIDDEN = "MaaNikki_Android_Forbidden"
FORBIDDEN_MARGIN = 40  # pixels around the tap point that may not hold a forbidden word


class PageMismatch(actions.ScreenUnrecognised):
    """The frame is not the page the step expects (or its target is not on it)."""


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


def _forbidden_near(recognize, box) -> str | None:
    x, y, w, h = box
    roi = [max(0, x - FORBIDDEN_MARGIN), max(0, y - FORBIDDEN_MARGIN), w + 2 * FORBIDDEN_MARGIN, h + 2 * FORBIDDEN_MARGIN]
    roi[2] = min(roi[2], 1280 - roi[0])
    roi[3] = min(roi[3], 720 - roi[1])
    result = recognize(FORBIDDEN, {"roi": roi})
    if result is None:
        return "检查不了要点的地方附近有没有危险按钮"
    return actions._text(result) if actions._hit(result) else None


def tap_on_page(rt, title: str, node: str, *, override=None, delay: float = .5) -> bool:
    """Tap ``node`` on the page titled ``title`` (decided and confirmed on fresh frames). False when the run stopped;
    PageMismatch (nothing sent) when the page or its target is not there or the point is next to a forbidden word."""
    from task_control import ForegroundChanged
    for _attempt in range(rt.TOUCH_KEY_ATTEMPTS):
        screen, decided, recognize = screen_now(rt)
        if decided is None:
            return False
        target = _target(screen, recognize, title, node, override)
        word = _forbidden_near(recognize, target)
        if word:
            raise PageMismatch(f"要点的地方旁边有「{word}」，没有点")
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
        if _forbidden_near(recognize_b, confirmed):
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
