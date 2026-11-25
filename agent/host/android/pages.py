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


def _forbidden_near(recognize, box) -> str | None:
    """A forbidden label whose box comes within FORBIDDEN_MARGIN of ``box``, read whole on the full frame (a region
    cropped around the point could cut 删除 off a label that ends just inside the margin)."""
    result = recognize(FORBIDDEN)
    if result is None:
        return "检查不了要点的地方附近有没有危险按钮"
    if not getattr(result, "hit", False):
        return None
    rows = list(getattr(result, "filtered_results", None) or []) or [getattr(result, "best_result", None)]
    x, y, w, h = box
    left, top, right, bottom = x - FORBIDDEN_MARGIN, y - FORBIDDEN_MARGIN, x + w + FORBIDDEN_MARGIN, y + h + FORBIDDEN_MARGIN
    for row in rows:
        found = [int(v) for v in getattr(row, "box", None) or []]
        if len(found) == 4 and found[0] < right and left < found[0] + found[2] and found[1] < bottom \
                and top < found[1] + found[3]:
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
    PageMismatch (nothing sent) when the deciding frame is not the layer; GuardRefusal next to a forbidden word."""
    from task_control import ForegroundChanged
    box = [int(point[0]), int(point[1]), 1, 1]
    for _attempt in range(rt.TOUCH_KEY_ATTEMPTS):
        screen, decided, recognize = screen_now(rt)
        if decided is None:
            return False
        if not accept(screen, recognize):
            raise PageMismatch(f"当前画面不是{what}")
        word = _forbidden_near(recognize, box)
        if word:
            raise GuardRefusal(f"要点的地方旁边有「{word}」，没有点，任务停止")
        if not rt._frame_still_valid(decided):
            continue
        fresh, confirming, recognize_b = screen_now(rt)
        if confirming is None:
            return False
        if not accept(fresh, recognize_b):
            continue
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
    raise PageMismatch(f"连续 {rt.TOUCH_KEY_ATTEMPTS} 次没能在稳定的画面上确认{what}")
