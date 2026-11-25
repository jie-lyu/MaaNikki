"""美鸭梨挖掘 (MaaNikki_Dig) on the Android client, first version: open the dig page, read the queue, report, go back.

HUD -> 美鸭梨 menu (a strict tap on the duck) -> the menu's 「美鸭梨挖掘」 entry -> the dig page, recognised by its own
title (exactly 「美鸭梨挖掘」) and the 「挖掘队列」 header with a readable counter -> read the queue on two fresh frames at
least SETTLE_SECONDS apart -> back to the HUD (strict back-arrow taps). Nothing on the dig page is ever tapped except its
back arrow: never 再次挖掘, the page's 挖掘 button, 选择物资, the hour pills or 消耗. The PC flow (Gameplay.dig) is not
used: it taps the gather check mark and, by default, 再次挖掘, and reports the dig targets to the statistics service;
none of that runs here (this module does not import the statistics code). The PC's options (DigRepeat) are not read:
an Android start submits empty options.

What the saved frames show (1280x720; phase A survey, 2026-11-25, never run through this code on the device): on the
menu (page 1 of 2) the entry's label 「美鸭梨挖掘」 reads at ~[326,347,108,25] under its icon (the survey opened the page
by a tap on the icon at (381,335); this flow taps the label, as the PC does: not seen on the device yet). The dig page:
back arrow ~(56,41), title 「美鸭梨挖掘」 [92,25,137,33] read as one OCR row; right column 「挖掘队列」 ~[1153,88,80,19],
the counter 「(0/4)」 ~[1162,107,62,23], two empty slots and two locked ones (centres x 1197, y 197/292/387/482); the
centre holds 「！选择物资」 [780,406,109,32], 「消耗」 [809,533,52,26], hour pills (OCR reads 「小时」 under two of them) and
the 「挖掘」 button, its label read as a row of its own at [797,639,73,39]. That frame's queue was empty.

The queue counts as empty only when, on both frames, the counter reads 0 of 4 (the surveyed four-slot layout), no
harvest-like word (收获, 领取, 再次挖掘, ...) is read anywhere on the frame and every slot looks plain (dark, or the grey
lock: no icon); a dig-page frame on the way back with a counter above 0 or harvest-like text overturns that (Later).
Anything else (a running or finished dig, never seen on the device yet) is reported without any tap: harvesting is
not implemented on Android, the slots must be collected by hand, and the frame is saved for the later survey.

Every tap goes through the shared strict helpers (pages.tap_duck for the duck, pages.tap_on_page with retry=False for
the menu entry and the back arrows: decided and confirmed on fresh focus-checked frames, the shared forbidden words)
and also this task's own words (DIG_WORDS: 再次挖掘, 选择物资, 消耗, the button's 「挖掘」 matched as a whole row so the
title and 「挖掘队列」 do not trip it, 小时, ...): one of them within pages.FORBIDDEN_MARGIN of the target (the whole duck
for the duck) on either tap frame refuses the tap and stops all input. A positively recognised unexpected screen (the
quit dialog, an unknown popup, another page) stops with that frame saved; waits forgive only frames on which nothing
is recognised yet (briefly) and frames of the state being left. Pages are compared by their whole title, whitespace
aside (pages.is_exactly). User messages are fixed texts: what OCR read (a refusing label, a page title, harvest-like
words) goes to the saved diagnostics only. No BACK key.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import time

import cv2

from host.android import actions, pages

MENU, DIG = "美鸭梨", "美鸭梨挖掘"
MENU_DIG = "MaaNikki_Android_MenuDig"
QUEUE_HEADER = "MaaNikki_Android_DigQueueHeader"
QUEUE_COUNT = "MaaNikki_Android_DigQueueCount"
DIG_WORDS = "MaaNikki_Android_DigWords"
HARVEST_LIKE = "MaaNikki_Android_DigHarvestLike"
NODES = (MENU_DIG, QUEUE_HEADER, QUEUE_COUNT, DIG_WORDS, HARVEST_LIKE)
# The shared 「恭喜获得」 layer's texts (android_monthly.json): positive evidence that a frame is no mere transition.
LAYER_TITLE, LAYER_HINT = "MaaNikki_Android_RewardLayerTitle", "MaaNikki_Android_RewardLayerHint"

COUNT_TEXT = re.compile(r"^[(（]?\s*(\d+)\s*[/／]\s*(\d+)\s*[)）]?$")
# The queue slots (centres on the saved frame) and how much of a SLOT_HALF*2 square around each may be bright for the
# slot to look plain: empty slots 0.000, locked ones (the grey lock) 0.107 and 0.112; material icons on the same frame
# 0.77-0.87 (pale ones included, so brightness, not saturation).
SLOTS = ((1197, 197), (1197, 292), (1197, 387), (1197, 482))
SLOT_HALF = 22
SLOT_BRIGHT_V = 150
SLOT_PLAIN = .25
SETTLE_SECONDS = 1.0
OPEN_WAIT = 5
TRANSITION_ALLOWANCE = 1.5
BACK_STEPS = 4


class Stop(Exception):
    """Something unexpected: report it and send nothing more (not even the way back). ``frames`` are saved."""

    def __init__(self, reason, frames=()):
        super().__init__(reason)
        self.frames = [frame for frame in frames if frame is not None]


# ---- this task's danger words ----

def dig_word_near(recognize, box) -> str | None:
    """A DIG_WORDS label (its full OCR box, read on the whole frame) within pages.FORBIDDEN_MARGIN of ``box``; a
    check that could not run counts as one."""
    result = recognize(DIG_WORDS)
    if result is None:
        return "检查不了要点的地方附近有没有危险按钮"
    if not getattr(result, "hit", False):
        return None
    for row in pages._rows(result):
        if pages._within_margin([int(v) for v in getattr(row, "box", None) or []], [int(v) for v in box]):
            return str(getattr(row, "text", "") or "危险按钮")
    return None


def guarded(node, check=None):
    """``accept(frame, recognize)`` for pages.tap_on_page: no DIG_WORDS label near the target ``node`` (refused as a
    guard, with that frame) and, when given, ``check(frame, recognize)``."""
    def accept(frame, recognize):
        result = recognize(node)
        if not actions._hit(result):
            return False
        word = dig_word_near(recognize, actions._box(result))
        if word:  # the label read goes to the diagnostic only (pages.diagnostic_reason)
            raise pages.GuardRefusal(pages.GUARD_TAP, frame, word)
        return check is None or bool(check(frame, recognize))
    return accept


# ---- the dig page ----

def _bright(frame, centre) -> float:
    x, y = centre
    patch = frame[y - SLOT_HALF:y + SLOT_HALF, x - SLOT_HALF:x + SLOT_HALF]
    value = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)[..., 2]
    return float((value > SLOT_BRIGHT_V).mean())


@dataclass(frozen=True)
class View:
    """The dig page on one frame: the queue counter (None: not read, only on the way back), the harvest-like texts
    read anywhere, the slots (1-based) that do not look plain."""
    count: int | None
    total: int | None
    harvest: tuple
    busy_slots: tuple

    @property
    def empty(self) -> bool:
        # Only the surveyed layout counts as empty: four slots (SLOTS), so a total other than 4 is never empty.
        return self.count == 0 and self.total == len(SLOTS) and not self.harvest and not self.busy_slots

    def _counter(self) -> list:
        return [] if self.count is None else [f"队列 {self.count}/{self.total}"]

    def describe(self) -> str:
        """For the diagnostic: with the harvest-like texts as OCR read them."""
        parts = self._counter()
        if self.harvest:
            parts.append("读到「" + "」「".join(self.harvest) + "」")
        if self.busy_slots:
            parts.append("第 " + "、".join(map(str, self.busy_slots)) + " 格不是空的")
        return "，".join(parts)

    def summary(self) -> str:
        """For the user: the counter and the slots, never the OCR'd texts."""
        parts = self._counter()
        if self.harvest:
            parts.append("读到像收获的文字")
        if self.busy_slots:
            parts.append("第 " + "、".join(map(str, self.busy_slots)) + " 格不是空的")
        return "，".join(parts)


def dig_page(screen) -> bool:
    return pages.is_exactly(screen, DIG)


def read_count(recognize):
    result = recognize(QUEUE_COUNT)
    if not actions._hit(result):
        return None
    match = COUNT_TEXT.match(actions._text(result).strip())
    if match is None:
        return None
    count, total = int(match.group(1)), int(match.group(2))
    return (count, total) if 0 <= count <= total <= 8 and total > 0 else None


def read_view(screen, frame, recognize) -> View | None:
    """The queue on one frame, or None when it is not the settled dig page (the title not exactly 「美鸭梨挖掘」, no
    「挖掘队列」 header, a counter that does not read, a check that could not run)."""
    if not dig_page(screen) or not actions._hit(recognize(QUEUE_HEADER)):
        return None
    counted = read_count(recognize)
    harvest = recognize(HARVEST_LIKE)
    if counted is None or harvest is None:
        return None
    texts = _texts(harvest)
    busy = tuple(index + 1 for index, centre in enumerate(SLOTS) if _bright(frame, centre) > SLOT_PLAIN)
    return View(*counted, texts, busy)


def _texts(result) -> tuple:
    return tuple(str(getattr(row, "text", "") or "") for row in pages._rows(result)) \
        if getattr(result, "hit", False) else ()


class Later:
    """Queue evidence on dig-page frames seen on the way back, after the queue was read empty: the first frame whose
    counter reads above 0 or on which harvest-like text is read (``view``, ``frame``). Called with ``(frame,
    recognize)`` of frames already known to be the dig page."""

    def __init__(self):
        self.view = self.frame = None

    def __call__(self, frame, recognize):
        if self.frame is not None:
            return
        counted = read_count(recognize)
        texts = _texts(recognize(HARVEST_LIKE))
        if (counted is not None and counted[0] > 0) or texts:
            self.view = View(*(counted or (None, None)), texts, ())
            self.frame = frame


def dig_ready(frame, recognize) -> bool:
    """For a tap's ``check``: the dig page's own landmarks on that tap frame (the title is checked by tap_on_page)."""
    return actions._hit(recognize(QUEUE_HEADER)) and read_count(recognize) is not None


# ---- waiting ----

def stopped(rt) -> bool:
    """The run was stopped, also between the client publishing stop.request and the tasker stopping (rt.stopped
    reads only the tasker): nothing more is reported then, neither success nor failure."""
    from host import android_user_stop
    return rt.stopped or android_user_stop()


def evidence(recognize) -> bool:
    """Something positive on a frame that is therefore no mere transition: the 「恭喜获得」 layer's title or hint, or
    harvest-like text (确认收获, 再次挖掘, ...); a check that could not run counts as evidence."""
    for node in (LAYER_TITLE, LAYER_HINT, HARVEST_LIKE):
        result = recognize(node)
        if result is None or actions._hit(result):
            return True
    return False


def wait_until(rt, what, done, source=lambda s, f, r: False, transitional=lambda s, f, r: False,
               seconds=OPEN_WAIT):
    """Poll fresh frames until ``done(screen, frame, recognize)`` returns something other than None/False and return
    ``(value, screen, frame, recognize)``. Frames for which ``source`` holds (the state being left) are waited through
    up to ``seconds``; a frame on which nothing at all is recognised (pages.nothing_recognised), or that
    ``transitional`` takes, only within TRANSITION_ALLOWANCE and only without positive evidence on it (``evidence``:
    a reward layer, harvest-like text); any other frame, or the time running out, saves that frame and stops. None when the run stopped. Stricter than pages.wait_screen (kept for that): a transition is
    forgiven only briefly, not for the whole wait, and ``transitional`` names the unsettled dig page."""
    start = time.monotonic()
    while not stopped(rt):
        screen, frame, recognize = pages.screen_now(rt)
        if frame is None:
            return None
        if rt._frame_still_valid(frame):
            value = done(screen, frame, recognize)
            if value is not None and value is not False:
                return value, screen, frame, recognize
            elapsed = time.monotonic() - start
            transition = elapsed <= TRANSITION_ALLOWANCE and (
                pages.nothing_recognised(screen, recognize) or transitional(screen, frame, recognize)) \
                and not evidence(recognize)
            if not (transition or source(screen, frame, recognize)):
                raise Stop(f"{what}时出现了不该出现的画面", [frame])
            if elapsed >= seconds:
                raise Stop(f"{what}超时", [frame])
        rt.pause(.4)
    return None


def _titled(title):
    return lambda s, f, r: pages.is_exactly(s, title)


def _unsettled_dig(screen, frame, recognize) -> bool:
    return dig_page(screen) and read_view(screen, frame, recognize) is None


# ---- the way in and out ----

def tap_duck(rt) -> bool:
    """The 美鸭梨 menu from the HUD by the shared strict duck tap (pages.tap_duck: the duck in the same place on both
    tap frames, no forbidden label near the whole duck, a quit dialog or any other screen on any frame stops with
    that frame, no second attempt), with this task's words checked around the whole duck on both frames too."""
    return pages.tap_duck(rt, words=dig_word_near)


# Where a known page's back arrow may lead: the dig page back to the menu (or straight to the HUD), the menu to the HUD.
LEADS_TO = {DIG: (MENU,), MENU: ()}


def to_hud(rt, what, *, any_page=False, watch=None) -> bool:
    """Back to the HUD one strict step at a time: the back arrow of 美鸭梨挖掘 (only with its queue header and counter
    on both tap frames) or 美鸭梨 (with ``any_page``, the start of a run: of any page), tapped once (the whole
    observed title, whitespace aside, on both tap frames; no second attempt; this task's words checked), each step
    waited out before the next. The quit dialog, a modal, an unknown screen, a tap frame that is not that page or a
    page that does not go stops with that frame. The very frame that shows a page gone decides the next step: after
    美鸭梨挖掘 it must be 美鸭梨 or the HUD, after 美鸭梨 the HUD (LEADS_TO), else it stops with that frame. Reaching
    the HUD after the last allowed step succeeds. ``watch(frame, recognize)``, when given, sees every dig-page frame
    on the way (the frame deciding the step, both tap frames, the frames waited through: Later).

    Kept instead of pages.to_hud(single=True), which is looser here: it backs out of any page at every step, takes
    no task words or page check on the back arrow, forgives a transition for the whole wait and does not look once
    more after its last back arrow (plan risk 83)."""
    def on_dig(frame_, recognize_):  # the tap frames: tap_on_page has checked the title
        if watch is not None:
            watch(frame_, recognize_)
        return dig_ready(frame_, recognize_)

    frame, carried, left_known = None, None, None
    for step in range(BACK_STEPS + 1):
        if carried is not None:  # the frame the wait returned: decided on itself, not on a later one
            (screen, frame, recognize), carried = carried, None
        else:
            screen, frame, recognize = pages.screen_now(rt)
            if frame is None:
                return False
            if not rt._frame_still_valid(frame):
                rt.pause(.4)
                continue
        if screen.kind == "hud":
            return True
        if left_known is not None and not any(pages.is_exactly(screen, name) for name in LEADS_TO[left_known]):
            raise Stop(f"{what}：点了返回箭头之后出现了不该出现的画面", [frame])
        if step == BACK_STEPS:
            break
        known = next((name for name in (DIG, MENU) if pages.is_exactly(screen, name)), None)
        if screen.kind != "page" or not (known or any_page):
            kind = "另一个页面" if screen.kind == "page" else pages.SCREEN_KINDS.get(screen.kind, screen.kind)
            raise Stop(f"{what}时画面不是能安全退回的页面（{screen.reason or kind}）", [frame])
        if known == DIG and watch is not None:
            watch(frame, recognize)
        title = screen.title  # as observed: the page is left by its whole title, which messages never quote
        place = f"「{known}」页面" if known else pages.LEAVING
        if not pages.tap_on_page(rt, title, actions.BACK_ARROW, retry=False, exact=True, place=place,
                                 accept=guarded(actions.BACK_ARROW, on_dig if known == DIG else None)):
            return False

        def staying(s, f, r, t=title, dig=known == DIG):
            if not pages.is_exactly(s, t):
                return False
            if dig and watch is not None:
                watch(f, r)
            return True
        left = wait_until(rt, f"{what}：点了返回箭头之后等{place}离开",
                          lambda s, f, r, t=title: s.kind == "hud" or (
                              s.kind == "page" and not pages.same_title(s.title, t)),
                          staying)
        if left is None:
            return False
        carried, left_known = left[1:], known
    raise Stop(f"{what}：退了 {BACK_STEPS} 步还没回到大世界", [frame])


def open_page(rt):
    """HUD -> 美鸭梨 -> 美鸭梨挖掘; returns ``(view, frame)`` of the first settled dig-page frame, None when stopped."""
    if not tap_duck(rt):
        return None
    if wait_until(rt, "打开美鸭梨菜单", _titled(MENU), lambda s, f, r: s.kind == "hud") is None:
        return None
    # The entry must be on the page shown (page 1); page 2 has no entry and is not swiped to: it stops, frame saved.
    if not pages.tap_on_page(rt, MENU, MENU_DIG, retry=False, exact=True, accept=guarded(MENU_DIG)):
        return None
    found = wait_until(rt, "打开美鸭梨挖掘页面", read_view, _titled(MENU), _unsettled_dig)
    return None if found is None else (found[0], found[2])


def inspect(rt, first):
    """``(view, frame)`` deciding the outcome: a first frame that does not read empty decides at once; an empty one
    must be read empty again on a fresh frame at least SETTLE_SECONDS later, else that second frame decides."""
    if not first[0].empty:
        return first
    if not rt.pause(SETTLE_SECONDS):
        return None
    found = wait_until(rt, "再读一次挖掘队列", read_view, transitional=_unsettled_dig)
    return None if found is None else (found[0], found[2])


def attempt(rt, step):
    """``step()`` with this task's stops: a Stop (its frames), a tap frame that did not agree, a guard refusal or an
    unrecognised screen (the refusing frame when it carries one) is saved and reported; None after any of them, and
    nothing more is sent. The user message is the error's fixed text; a guard refusal's OCR'd label goes to the
    diagnostic only (pages.diagnostic_reason)."""
    from navigation.models import NavigationError
    try:
        return step()
    except (Stop, actions.ScreenUnrecognised, NavigationError) as error:  # PageMismatch, FrameMismatch, guard too
        frames = error.frames if isinstance(error, Stop) else [getattr(error, "frame", None)]
        for index, frame in enumerate(frame for frame in frames if frame is not None):
            rt.diagnostic(f"android_dig_stop_{index}", pages.diagnostic_reason(error), image=frame)
        fail(rt, str(error))
    return None


def save_queue(rt, view, frame) -> bool:
    """The non-empty queue's frame for the survey; True only when the frame was saved."""
    reference = rt.diagnostic("android_dig_queue", f"挖掘队列不是空的：{view.describe()}", image=frame)
    return isinstance(reference, dict) and reference.get("image_status") == "saved"


def run(rt) -> bool:
    def look():
        if not to_hud(rt, "开始前回到大世界", any_page=True):
            return None
        first = open_page(rt)
        return first and inspect(rt, first)
    seen = attempt(rt, look)
    # The way back only after the page was read: after a stop, a guard refusal or a fatal error nothing more is sent.
    if not seen or stopped(rt):
        return False
    view, frame = seen
    # Saved before anything else is sent: a non-empty queue on this client, never seen yet, for the survey.
    saved = False if view.empty else save_queue(rt, view, frame)
    later = Later() if view.empty else None
    back = attempt(rt, lambda: to_hud(rt, "返回大世界", watch=later))
    # A stop (also one only requested so far) reports nothing: neither the outcome nor a failure.
    if back is None or stopped(rt):
        return False
    if not back:
        return fail(rt, "没能回到大世界")
    if later is not None and later.frame is not None:  # the way back saw a queue that is not empty after all
        view, saved = later.view, save_queue(rt, later.view, later.frame)
    if stopped(rt):
        return False
    if view.empty:
        rt.log("美鸭梨挖掘：没有可收获的挖掘。")
        return True
    rt.log(f"美鸭梨挖掘：挖掘队列不是空的（{view.summary()}）。安卓上还没在设备上见过收获的画面，这一版不收获，"
           f"请先在游戏里手动收获；{'画面已保存' if saved else '画面没能保存'}。本项未完成。")
    return False


def fail(rt, reason) -> bool:
    from host import android_user_stop
    if not rt.stopped and not android_user_stop():  # a user stop is no failure
        rt.log(f"美鸭梨挖掘：{reason}，本项未完成。")
    return False
