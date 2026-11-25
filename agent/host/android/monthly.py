"""奇迹之旅奖励 (MaaNikki_MonthlyPass, "月卡") on the Android client: claim 旅行任务 and, when it shows an unmistakable
claim button, 旅行秘宝.

HUD -> 奇迹之旅 shortcut -> 旅行任务 tab -> 一键领取 if gold -> 旅行秘宝 tab -> 一键领取 if gold -> back to the HUD.
乐园构想 is not handled (locked on the surveyed account, never seen). The scope is fixed here: the PC task's options
(MonthlyPages) are not read, because an Android start submits empty options.

Every tap goes through the named actions or pages.tap_on_page (decided and confirmed on fresh focus-confirmed frames,
never next to 购买等级, 解锁悠远颂歌 or other forbidden words). What the saved frames show (1280x720): the page opens on
旅行秘宝; the left tabs 旅行秘宝 (label ~(69,212)) and 旅行任务 (~(68,330)) are gold when selected; 一键领取 at
~(790,654) is gold while 旅行任务 has something to claim (OCR drops 「一」 and reads 「键领取」); 旅行秘宝 with nothing to
claim shows no button there.

Device run 1 (2026-11-26): the 旅行任务 claim went straight back to the page with the button off; the 旅行秘宝 claim
(the task XP had levelled the pass up) brought up a 「恭喜获得」 reward layer: dimmed, title 「恭喜获得」 ~[570,165,136,42],
hint 「点击空白区域继续」 ~[569,640,140,19] at the bottom centre, no close mark. Taps on the hint did not close it; a tap
on blank space did. So a claim is a success only when fresh frames, a second apart, show the same tab again with the
button no longer gold. On the way there, the reward layer, recognised positively and over the 奇迹之旅 page, gets at
most one tap, at DISMISS_POINT (blank on the layer and on every saved 奇迹之旅 page), through pages.tap_on_layer. The
first other frame after a claim (another screen, a layer that is not over this page, a layer still there after that
one tap) stops the task, sends nothing more and saves the frame. Tab and claim taps are decided and confirmed only on
frames that show the whole expected state of the page (read_view).
"""
from __future__ import annotations

from dataclasses import dataclass
import time

import cv2

from host.android import actions, pages

TITLE = "奇迹之旅"
TREASURE, TASK = "treasure", "task"
TAB_NODES = {TREASURE: "MaaNikki_Android_MonthlyTreasureTab", TASK: "MaaNikki_Android_MonthlyTaskTab"}
# Something only that tab's content shows: the 本周任务/本期任务 sub-tabs, the 旅途序曲 row label.
CONTENT_NODES = {TREASURE: "MaaNikki_Android_MonthlyTreasureContent", TASK: "MaaNikki_Android_MonthlyTaskContent"}
TAB_NAMES = {TREASURE: "旅行秘宝", TASK: "旅行任务"}
CLAIM_ALL = "MaaNikki_Android_MonthlyClaimAll"
LAYER_TITLE = "MaaNikki_Android_RewardLayerTitle"
LAYER_HINT = "MaaNikki_Android_RewardLayerHint"
# The 奇迹之旅 page as it shows blurred and dimmed under the reward layer (templates cut from device run 1's layer
# frames): the title corner and the 解锁悠远颂歌 corner, both the same on either tab. On the 9 layer frames they score
# >= 0.997 and >= 0.952; on every saved frame of another screen at most 0.763 and 0.534.
LAYER_OVER_MONTHLY = ("MaaNikki_Android_RewardLayerOverMonthlyTitle", "MaaNikki_Android_RewardLayerOverMonthlyUnlock")

# Tab labels in the 1280x720 frame: the selected tab's cell is gold behind the label (saved frames: 0.58-0.67 of the
# label box in range when selected, 0.000 when not; same HSV bounds and threshold as the PC monthly_tab_selected).
TAB_LABELS = {TREASURE: (25, 200, 88, 24), TASK: (25, 317, 86, 27)}
TAB_SELECTED = .45
# The 一键领取 button face. Gold (claimable, seen): 0.822. No button (旅行秘宝, nothing to claim, seen): 0.000.
BUTTON_FACE = (670, 640, 210, 40)
ENABLED_GOLD, NONE_GOLD = .4, .05
# After a claim: the settled state must hold this long, on at least two fresh frames, before it counts.
SETTLE_SECONDS = 1.0
CLAIM_WAIT = 10
# The reward layer's dismissal tap. Run 1 closed it with a tap at (640,700), but on the 旅行任务 tab that point lies
# ~17 px left of and ~18 px below the 一键领取 graphic (~[657,630,235,52]). This point is in the same blank band of the
# layer and, on every saved 奇迹之旅 frame (both tabs, button lit or not) and layer frame, at least 40 px from every
# label and button (57 px from the 一键领取 graphic, 46 px below the layer's hint) with no edge within 40 px.
DISMISS_POINT = (600, 705)
AFTER_DISMISS_WAIT = 5


def _fraction(frame, box, lower, upper) -> float:
    x, y, w, h = box
    hsv = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
    return cv2.countNonZero(cv2.inRange(hsv, lower, upper)) / (w * h)


def tab_gold(frame, tab) -> float:
    return _fraction(frame, TAB_LABELS[tab], (10, 40, 100), (35, 200, 255))


def button_gold(frame) -> float:
    return _fraction(frame, BUTTON_FACE, (10, 85, 130), (35, 255, 255))


@dataclass(frozen=True)
class View:
    """One fresh frame of the 奇迹之旅 page: which tab is selected and what the 一键领取 button shows ("enabled": the
    label and a gold face; "none": no gold face, the button gone or greyed; "unknown": anything in between)."""
    tab: str
    button: str
    gold: float


def read_view(frame, recognize) -> View | None:
    """The page's state on one frame already recognised as the 奇迹之旅 page, or None when it is not settled on
    exactly one tab with that tab's content showing (a layer over it, a tab switch in progress, an unseen layout)."""
    selected = [tab for tab, node in TAB_NODES.items()
                if actions._hit(recognize(node)) and tab_gold(frame, tab) >= TAB_SELECTED]
    if len(selected) != 1 or not actions._hit(recognize(CONTENT_NODES[selected[0]])):
        return None
    gold, label = button_gold(frame), actions._hit(recognize(CLAIM_ALL))
    button = "enabled" if label and gold >= ENABLED_GOLD else "none" if gold <= NONE_GOLD else "unknown"
    return View(selected[0], button, round(gold, 3))


def is_reward_layer(screen, recognize) -> bool:
    """The 「恭喜获得」 reward layer over the 奇迹之旅 page, positively: no page, HUD, modal or quit dialog was
    recognised, the quit and modal checks ran and found nothing, the layer's title and its bottom-centre hint are read,
    and the 奇迹之旅 page is recognised beneath the dim (title corner and 解锁悠远颂歌 corner)."""
    if screen is None or screen.kind != "unknown":
        return False
    for node in (actions.QUIT_TITLE, actions.QUIT_CANCEL, actions.MODAL_CLOSE):
        result = recognize(node)
        if result is None or actions._hit(result):
            return False
    return all(actions._hit(recognize(node)) for node in (LAYER_TITLE, LAYER_HINT, *LAYER_OVER_MONTHLY))


def look(rt):
    """``(kind, view, frame)`` from one fresh frame. kind: "view" (the settled 奇迹之旅 page, ``view`` set), "layer"
    (the reward layer over that page), "invalid" (the frame was voided by a focus change: it shows nothing), "other"
    (anything else) or None (the run stopped; frame None)."""
    screen, frame, recognize = pages.screen_now(rt)
    if frame is None:
        return None, None, None
    if not rt._frame_still_valid(frame):
        return "invalid", None, frame
    if pages.is_page(screen, TITLE):
        view = read_view(frame, recognize)
        return ("view" if view is not None else "other"), view, frame
    return ("layer" if is_reward_layer(screen, recognize) else "other"), None, frame


def view_now(rt):
    """``(view, frame)`` from one fresh valid frame: view is None when the frame is not the settled 奇迹之旅 page."""
    _kind, view, frame = look(rt)
    return view, frame


def expected(tab, button=None):
    """``accept(frame, recognize)`` for pages.tap_on_page: the frame is the settled 奇迹之旅 page on ``tab`` (its
    content showing, the only tab selected) and, when given, with the button in state ``button``."""
    def accept(frame, recognize):
        view = read_view(frame, recognize)
        return view is not None and view.tab == tab and (button is None or view.button == button)
    return accept


class Stop(Exception):
    """Something unexpected: report it and send nothing more (not even the way back). ``frames`` are saved."""

    def __init__(self, reason, frames=()):
        super().__init__(reason)
        self.frames = [frame for frame in frames if frame is not None]


def wait_view(rt, accept, seconds: float):
    """The first fresh view within ``seconds`` that ``accept(view)`` takes, else ``(None, last frame)``."""
    deadline, frame = time.monotonic() + seconds, None
    while not rt.stopped:
        view, latest = view_now(rt)
        if latest is None:
            return None, None
        frame = latest
        if view is not None and accept(view):
            return view, frame
        if time.monotonic() >= deadline:
            break
        rt.pause(.4)
    return None, frame


def open_tab(rt, tab) -> bool:
    """Select ``tab``: only from a recognised view of the page; a guarded tap on its label (both tap frames must be
    the settled page on the other tab) unless it is already selected, then wait for it to settle."""
    view, frame = wait_view(rt, lambda v: True, 5)
    if frame is None or rt.stopped:
        return False
    if view is None:
        raise Stop(f"切换到「{TAB_NAMES[tab]}」之前没有认出奇迹之旅页面停在哪个页签", [frame])
    if view.tab == tab:
        return True
    if not pages.tap_on_page(rt, TITLE, TAB_NODES[tab], accept=expected(view.tab)):
        return False
    view, frame = wait_view(rt, lambda v: v.tab == tab, 5)
    if frame is None or rt.stopped:
        return False
    if view is None:
        raise Stop(f"点了「{TAB_NAMES[tab]}」之后没有认出这个页签", [frame])
    return True


def dismiss_layer(rt, tab) -> bool:
    """One guarded tap on blank space of the reward layer (pages.tap_on_layer: the layer over the 奇迹之旅 page on
    both tap frames). When the layer is no longer there to decide on, only a positively recognised return to the
    expected tab is accepted (nothing was sent); anything else stops."""
    try:
        return pages.tap_on_layer(rt, "奇迹之旅页面上的「恭喜获得」奖励层", is_reward_layer, DISMISS_POINT)
    except pages.GuardRefusal:
        raise
    except pages.PageMismatch:
        if rt.stopped:
            return False
    kind, view, frame = look(rt)
    if kind is None:
        return False
    if kind == "view" and view.tab == tab:
        return True
    raise Stop(f"「恭喜获得」奖励层在点之前变了，但画面没有回到「{TAB_NAMES[tab]}」页签", [frame])


def claim(rt, tab) -> bool:
    """Claim on the selected ``tab`` when 一键领取 is gold. True when there was nothing to claim or the claim was seen
    to land (the same tab, settled, button no longer gold)."""
    name = TAB_NAMES[tab]
    view, frame = view_now(rt)
    if frame is None:
        return False
    if view is None or view.tab != tab:
        raise Stop(f"「{name}」页签没有停稳，认不出页面", [frame])
    if view.button == "none":
        rt.log(f"{name}没有可领取的奖励。")
        return True
    if view.button != "enabled":
        raise Stop(f"「{name}」页签的「一键领取」按钮状态认不出（金色占比 {view.gold}）", [frame])
    # Both tap frames must show the whole expected state: this tab, settled, the button labelled and gold.
    if not pages.tap_on_page(rt, TITLE, CLAIM_ALL, accept=expected(tab, "enabled")):
        return False
    # Every frame after the tap must be this tab's view (success once the button is off on fresh frames at least
    # SETTLE_SECONDS apart with nothing else between) or the reward layer over the page (one dismissal tap, no
    # more). The first frame that is neither stops the task there, with nothing more sent.
    deadline, settled_at, settled_frames, dismissed, kind = time.monotonic() + CLAIM_WAIT, None, 0, False, None
    while not rt.stopped and time.monotonic() < deadline:
        kind, view, frame = look(rt)
        if kind is None:
            return False
        if kind == "view" and view.tab == tab:
            if view.button == "none":
                settled_at = time.monotonic() if settled_at is None else settled_at
                settled_frames += 1
                if settled_frames >= 2 and time.monotonic() - settled_at >= SETTLE_SECONDS:
                    rt.log(f"{name}奖励已领取。")
                    return True
            else:
                settled_at, settled_frames = None, 0  # the claim has not shown yet
        elif kind == "layer":
            settled_at, settled_frames = None, 0
            if not dismissed:
                dismissed = True
                if not dismiss_layer(rt, tab):
                    return False
                deadline = max(deadline, time.monotonic() + AFTER_DISMISS_WAIT)
                continue
        elif kind != "invalid":  # a frame voided by a focus change shows nothing either way
            raise Stop(f"领取「{name}」之后出现了认不出的画面（不是这个页签，也不是奇迹之旅上的奖励层）", [frame])
        rt.pause(.4)
    if rt.stopped:
        return False
    if kind == "layer" and dismissed:
        raise Stop(f"领取「{name}」后的「恭喜获得」奖励层点了一次空白处仍然没有关闭", [frame])
    raise Stop(f"领取「{name}」之后没有回到「一键领取」已熄灭的页签", [frame])


def run(rt) -> bool:
    from navigation.models import NavigationError
    claimed = None
    try:
        if not pages.to_hud(rt):
            return fail(rt, "没能回到大世界")
        if not rt.key("monthly"):
            return False
        if pages.wait_for(rt, lambda screen: pages.is_page(screen, TITLE), 5) is None:
            return fail(rt, "没能打开奇迹之旅页面")
        claimed = (open_tab(rt, TASK) and claim(rt, TASK)
                   and open_tab(rt, TREASURE) and claim(rt, TREASURE))
    except Stop as error:
        for index, frame in enumerate(error.frames):
            rt.diagnostic(f"android_monthly_stop_{index}", str(error), image=frame)
        fail(rt, str(error))
    except (pages.GuardRefusal, actions.ScreenUnrecognised, NavigationError) as error:
        fail(rt, str(error))
    # The way back only after a successful result: after a stop, a guard refusal or a fatal error (raised past this
    # point, e.g. the device stopped answering) nothing more is sent.
    if not claimed:
        return False
    if rt.stopped or not pages.back_through(rt, (TITLE,)):
        return fail(rt, "没能回到大世界")
    return True


def fail(rt, reason) -> bool:
    if not rt.stopped:
        rt.log(f"奇迹之旅奖励：{reason}，本项未完成。")
    return False
