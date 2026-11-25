"""每日灵感 rewards on the Android client: 朝夕心愿 (MaaNikki_Zhaoxi) and 星海拾光 (MaaNikki_Xinghai) read the score and claim
the gift circles of the reward column that are lit, and nothing else.

HUD -> 奇想日历 shortcut (pages.tap_shortcut) -> the card's label on 奇想日历 -> 每日灵感 on that card's tab -> the score and
the five circles read the same on settled frames -> each lit circle, from the bottom, one tap -> the 「恭喜获得」 layer
closed by one tap on blank space -> back to the HUD by the back arrows of 每日灵感 and 奇想日历 only.

No wish is done on Android: no wish bottle or card of the page is selected, and 立即前往, 星光小铺, the green check at the bottom right of
朝夕心愿, the realm challenge, ability plans and every other subtask of the PC task are never touched (they spend,
commit or need open-world navigation). The PC task's options are not read (an Android start submits none).

What the device frames show (1280x720, survey of 2026-11-26): 奇想日历 has the 朝夕心愿 card's label ~[178,366,91,28] and
the 星海拾光 card's label ~[178,645,92,30]; a tap on either label opens 每日灵感 on that tab. 每日灵感: left tabs 朝夕心愿
~[44,182,88,28] and 星海拾光 ~[45,317,88,28], the selected one on a gold cell (0.44-0.49 of the label box gold, the other
0.000); 「每日4点刷新」 ~[1115,55,103,23]; the reward column's five circles at x ~1198, y ~429/359/289/219/149 (100 to 500
points, from the bottom); the score ~[1178,513,42,28] under them. The description panel at the bottom belongs to the
selected wish: its button at the bottom right is a green check ~(1170,680) for some wishes of 朝夕心愿 and 「立即前往」
~[1076,678,104,28] for others and on 星海拾光 (never tapped; DAILY_WORDS blocks a tap near 立即前往); 星海拾光 also shows
「星光小铺」 ~[37,668,75,22] (never tapped). Which tab's content shows is told by 星光小铺 and the colour of the panel's
large star icon (STAR_BOX).

The circles: not yet reached, a navy face with a white gift; reached and not claimed (seen once, 100 on 朝夕心愿), a
larger peach face with a gold rim and a white gift, joined to the score star by a gold line; claimed (device run 2, the
same circle after its claim), a bronze face with a cream check mark over the faded gift (circle_look has the numbers).
Device run 2: the tap on the lit circle brought up the 「恭喜获得」 layer, closed by the one tap at DISMISS_POINT; the
circle then read claimed, with its reward preview (three items, ~[925,398,245,95], left of the circle) left open
beside it (a back arrow then leaves the page as usual, run 3). Never seen: two or more lit circles at once (whether a
tap on the next circle claims it while a preview is open), and 星海拾光 with anything lit.
The score agrees with the circles: the circles up to score/100 are reached, the rest are not; a view that disagrees
or shows a circle of any other look is not acted on (it stops, with the frame, once the page has had time to settle).

Every tap is decided and confirmed on two fresh focus-confirmed frames showing the whole expected state (pages
helpers with retry=False), never next to a forbidden word (pages.FORBIDDEN) or a DAILY_WORDS word; a claim tap is
never repeated. After a claim, the frames must show the same view with that circle still lit (not yet), that circle
claimed (success once settled), the 「恭喜获得」 layer with 每日灵感 on that tab beneath it (one tap at DISMISS_POINT;
layer_over_tab on every frame that shows the layer, before and after that tap, and on both tap frames) or, briefly,
nothing recognised; the first frame that is anything else (a reward layer over anything else among them) stops the
task with that frame saved and nothing more sent. On the way back, the 每日灵感 back arrow needs that tab on the frame
that decides and on both tap frames; while it takes effect, another tab or a reward layer stops; and the frame that
shows the page gone must itself be 奇想日历 or the HUD.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import time

import cv2
import numpy as np

from host.android import actions, pages

TITLE, CALENDAR = "每日灵感", "奇想日历"
ZHAOXI, XINGHAI = "zhaoxi", "xinghai"
MODES = (ZHAOXI, XINGHAI)
NAMES = {ZHAOXI: "朝夕心愿", XINGHAI: "星海拾光"}
CARD_NODES = {ZHAOXI: "MaaNikki_Android_CalendarZhaoxiCard", XINGHAI: "MaaNikki_Android_CalendarXinghaiCard"}
TAB_NODES = {ZHAOXI: "MaaNikki_Android_DailyZhaoxiTab", XINGHAI: "MaaNikki_Android_DailyXinghaiTab"}
REFRESH = "MaaNikki_Android_DailyRefresh"
SCORE = "MaaNikki_Android_DailyScore"
CIRCLE_LIT = "MaaNikki_Android_DailyCircleLit"  # the lit face by colour: 1048-1285 px on lit circles, <= 363 claimed
SHOP_LABEL = "MaaNikki_Android_XinghaiShop"
DAILY_WORDS = "MaaNikki_Android_DailyWords"  # 立即前往, 星光小铺 (seen), 购买, 升级 (not seen on these pages)
LAYER_TITLE = "MaaNikki_Android_RewardLayerTitle"  # android_monthly.json: 「恭喜获得」
LAYER_HINT = "MaaNikki_Android_RewardLayerHint"  # 「点击空白区域继续」
NODES = (*CARD_NODES.values(), *TAB_NODES.values(), REFRESH, SCORE, CIRCLE_LIT, SHOP_LABEL, DAILY_WORDS)

TAB_LABELS = {ZHAOXI: (44, 182, 88, 28), XINGHAI: (45, 317, 88, 28)}
TAB_SELECTED, TAB_OFF = .3, .05
# The large star icon at the bottom left of the description panel: gold on 朝夕心愿 (0.22-0.24 of STAR_BOX, lavender
# 0.03-0.04), white and lavender on 星海拾光 (lavender 0.58, gold 0.07), whichever wish is selected.
STAR_BOX = (190, 610, 70, 60)
STAR_GOLD, STAR_LAVENDER, STAR_OTHER = .15, .4, .12
CIRCLE_X = 1198
CIRCLE_YS = (429, 359, 289, 219, 149)  # 100, 200, 300, 400, 500 points
CIRCLE_HALF = 26  # CIRCLE_LIT's region around a circle's centre
LOCKED, LIT, CLAIMED, UNKNOWN = "locked", "lit", "claimed", "unknown"
LOOK_NAMES = {LOCKED: "未达成", LIT: "可领取", CLAIMED: "已领取", UNKNOWN: "认不出"}
SETTLE_SECONDS = 1.0
OPEN_WAIT = 6
TRANSITION_ALLOWANCE = 1.5
CLAIM_WAIT = 8
AFTER_DISMISS_WAIT = 5
# The reward layer's dismissal tap: in the layer's blank bottom band (as (600,705) in the monthly and redeem tasks)
# and, on the saved 每日灵感 frames of both tabs, >= 50 px below the description's last line, >= 240 px right of the
# quote's author (whose length varies: 「——满载而归的搭配师」 ends at x ~555, too close to (600,705)) and >= 240 px
# left of 立即前往.
DISMISS_POINT = (800, 705)
DISMISS_CLEAR = [690, 662, 220, 58]  # no text may be read here on either tap frame
# Beneath the 「恭喜获得」 layer the page is blurred (Gaussian, sigma ~4) and dimmed: per channel ~0.33 x + 8-37 above
# y 240, ~0.14-0.26 x + 35-75 in the layer's band (y 240-460), as fitted on 奇迹之旅 device run 1 (the layer frame against
# the page frame: mean error ~1 level above the band, 2-3.5 in it). No layer over 每日灵感 has been saved (device run 2
# closed it without a frame), so these thresholds come from the 每日灵感 device frames put under that fit
# (scripts/fixtures/android/daily_layer_*.jpg): the selected tab's gold cell (UNDER_CELLS, hue 8-30, saturation >= 60)
# 0.82 warm on 朝夕心愿, 0.36-0.72 on 星海拾光 (in the band; three fits of the band), the other cell 0.00-0.12; the
# 500 and 400 gift circles of the reward column glow (gift_glow: centre minus ring brightness) 30-46 on both tabs,
# whatever the circles show. On the 奇迹之旅 layer frame the cells read 0.84 / 0.36 and the glow -9 / 9 (its gold
# chest frame there), on the redeem success layer the glow 6-7.
UNDER_CELLS = {ZHAOXI: (40, 120, 110, 95), XINGHAI: (40, 255, 110, 95)}
UNDER_ON, UNDER_OFF = {ZHAOXI: .5, XINGHAI: .2}, {ZHAOXI: .25, XINGHAI: .1}  # the expected cell; the other cell
UNDER_GLOW = 15
_YY, _XX = np.mgrid[-22:23, -22:23]
_RADIUS = np.hypot(_XX, _YY)
_CENTRE, _INNER, _OUTER = _RADIUS < 7, (_RADIUS >= 10) & (_RADIUS <= 16), (_RADIUS >= 13) & (_RADIUS <= 19)
_GY, _GX = np.mgrid[-34:35, -34:35]
_GLOW_RADIUS = np.hypot(_GX, _GY)
_GLOW_CENTRE, _GLOW_RING = _GLOW_RADIUS <= 8, (_GLOW_RADIUS >= 28) & (_GLOW_RADIUS <= 34)


def _fraction(frame, box, lower, upper) -> float:
    x, y, w, h = box
    hsv = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
    return cv2.countNonZero(cv2.inRange(hsv, lower, upper)) / (w * h)


def tab_gold(frame, mode) -> float:
    return _fraction(frame, TAB_LABELS[mode], (10, 40, 100), (35, 200, 255))


def star_colours(frame):
    """``(gold, lavender)`` shares of STAR_BOX."""
    return (_fraction(frame, STAR_BOX, (10, 60, 120), (35, 255, 255)),
            _fraction(frame, STAR_BOX, (120, 20, 150), (160, 255, 255)))


def warm_share(frame, box) -> float:
    return _fraction(frame, box, (8, 60, 0), (30, 255, 255))


def gift_glow(frame, index) -> float:
    """How much brighter the centre of circle ``index`` is than a ring around it (beneath the layer the white gift
    is a blurred glow)."""
    x, y = CIRCLE_X, CIRCLE_YS[index]
    value = cv2.cvtColor(frame[y - 34:y + 35, x - 34:x + 35], cv2.COLOR_BGR2HSV)[..., 2].astype(float)
    return float(value[_GLOW_CENTRE].mean() - value[_GLOW_RING].mean())


def layer_over_tab(frame, mode) -> bool:
    """每日灵感 on ``mode``'s tab beneath the reward layer, positively: ``mode``'s tab cell gold and the other tab's
    cell not, and the 500 and 400 gift circles glowing in the reward column (UNDER_CELLS has the numbers)."""
    if not all(gift_glow(frame, index) >= UNDER_GLOW for index in (3, 4)):
        return False
    return all(warm_share(frame, UNDER_CELLS[tab]) >= UNDER_ON[mode] if tab == mode else
               warm_share(frame, UNDER_CELLS[tab]) <= UNDER_OFF[mode] for tab in MODES)


def circle_measures(frame, index):
    """``(white, navy, warm, bronze)``: the white share of the circle's centre (the gift), the navy share of its inner
    ring, the warm (peach and gold) share of its outer ring, the bronze share of its inner ring."""
    x, y = CIRCLE_X, CIRCLE_YS[index]
    hsv = cv2.cvtColor(frame[y - 22:y + 23, x - 22:x + 23], cv2.COLOR_BGR2HSV)
    h, s, v = (hsv[..., k].astype(int) for k in range(3))
    white = ((s <= 60) & (v >= 190))[_CENTRE].mean()
    navy = ((h >= 105) & (h <= 135) & (s >= 50) & (v >= 40) & (v <= 170))[_INNER].mean()
    warm = ((h <= 30) & (s >= 35) & (v >= 120))[_OUTER].mean()
    bronze = ((h >= 5) & (h <= 25) & (s >= 50) & (v >= 85) & (v <= 170))[_INNER].mean()
    return float(white), float(navy), float(warm), float(bronze)


def circle_look(frame, index) -> str:
    """LOCKED (navy face, white gift: inner ring 0.99 navy, centre 0.74-0.84 white on the device frames), LIT (peach
    face, gold rim, white gift: outer ring 0.84-0.98 warm, centre 1.00 white, no navy), CLAIMED (bronze face, the
    gift faded under a cream check mark: inner ring 0.92-0.99 bronze, centre 0.39-0.40 white, no navy; a lit circle
    reads 0.19-0.31 bronze) or UNKNOWN (anything else)."""
    white, navy, warm, bronze = circle_measures(frame, index)
    if white >= .5 and navy >= .9 and warm <= .02:
        return LOCKED
    if white >= .5 and warm >= .7 and navy <= .02:
        return LIT
    if white <= .6 and bronze >= .7 and navy <= .02:
        return CLAIMED
    return UNKNOWN


def circle_roi(index):
    return [CIRCLE_X - CIRCLE_HALF, CIRCLE_YS[index] - CIRCLE_HALF, 2 * CIRCLE_HALF, 2 * CIRCLE_HALF]


@dataclass(frozen=True)
class View:
    """One frame of 每日灵感 settled on one tab: the tab, the score and the five circles' looks (index 0: 100 points)."""
    tab: str
    score: int
    circles: tuple

    def consistent(self) -> bool:
        """The circles agree with the score: the first score/100 reached (lit or claimed), the rest not reached."""
        reached = self.score // 100
        return all(look in (LIT, CLAIMED) for look in self.circles[:reached]) \
            and all(look == LOCKED for look in self.circles[reached:])

    def describe(self) -> str:
        return "、".join(f"{(index + 1) * 100} 分{LOOK_NAMES[look]}" for index, look in enumerate(self.circles))


def read_score(recognize):
    result = recognize(SCORE)
    if not actions._hit(result):
        return None
    text = actions._text(result).strip()
    return int(text) if re.fullmatch(r"0|[1-5]00", text) else None


def shown_tab(frame, recognize) -> str | None:
    """The tab a frame already recognised as 每日灵感 shows: exactly one tab selected and that tab's content showing,
    else None."""
    selected = [mode for mode in MODES
                if actions._hit(recognize(TAB_NODES[mode])) and tab_gold(frame, mode) >= TAB_SELECTED]
    if len(selected) != 1 or any(tab_gold(frame, mode) > TAB_OFF for mode in MODES if mode != selected[0]):
        return None
    mode = selected[0]
    shop, (gold, lavender) = actions._hit(recognize(SHOP_LABEL)), star_colours(frame)
    content = (not shop and gold >= STAR_GOLD and lavender <= STAR_OTHER) if mode == ZHAOXI else \
        (shop and lavender >= STAR_LAVENDER and gold <= STAR_OTHER)
    return mode if content else None


def read_view(frame, recognize) -> View | None:
    """The page's state on a frame already recognised as 每日灵感, or None when it is not settled on exactly one tab
    with that tab's content, the reward column's label and a score showing."""
    mode = shown_tab(frame, recognize)
    if mode is None or not actions._hit(recognize(REFRESH)):
        return None
    score = read_score(recognize)
    if score is None:
        return None
    return View(mode, score, tuple(circle_look(frame, index) for index in range(len(CIRCLE_YS))))


def daily_word_near(recognize, box) -> str | None:
    """A DAILY_WORDS label (full OCR box on the whole frame) within pages.FORBIDDEN_MARGIN of ``box``."""
    result = recognize(DAILY_WORDS)
    if result is None:
        return "检查不了要点的地方附近有没有危险按钮"
    if not getattr(result, "hit", False):
        return None
    for row in pages._rows(result):
        if pages._within_margin([int(v) for v in getattr(row, "box", None) or []], [int(v) for v in box]):
            return str(getattr(row, "text", "") or "危险按钮")
    return None


def guarded_accept(node, check=None, override=None):
    """``accept(frame, recognize)`` for pages.tap_on_page: no DAILY_WORDS label near the target ``node`` (refused as
    a guard, with that frame) and, when given, ``check(frame, recognize)``."""
    def accept(frame, recognize):
        result = recognize(node, override) if override else recognize(node)
        if not actions._hit(result):
            return False
        word = daily_word_near(recognize, actions._box(result))
        if word:
            raise pages.GuardRefusal(f"要点的地方旁边有「{word}」，没有点，任务停止", frame)
        return check is None or bool(check(frame, recognize))
    return accept


def is_reward_layer(screen, recognize) -> bool:
    """The 「恭喜获得」 layer's texts, positively: no page, HUD, modal or quit dialog recognised, the quit and modal
    checks ran and found nothing, the layer's title and its bottom hint read. Over any page: whether 每日灵感 is
    beneath it is layer_over_tab's question, asked before the layer is tapped."""
    if screen is None or screen.kind != "unknown":
        return False
    for node in (actions.QUIT_TITLE, actions.QUIT_CANCEL, actions.MODAL_CLOSE):
        result = recognize(node)
        if result is None or actions._hit(result):
            return False
    return actions._hit(recognize(LAYER_TITLE)) and actions._hit(recognize(LAYER_HINT))


def blank_at_dismiss(recognize) -> bool:
    """No text read around DISMISS_POINT and no DAILY_WORDS label near it (both checks ran). A single character is
    the OCR's reading of empty background, not a label (as in redeem.blank_at_dismiss)."""
    result = recognize(actions.PAGE_TITLE, {"roi": DISMISS_CLEAR, "expected": ".+"})
    if result is None or daily_word_near(recognize, [*DISMISS_POINT, 1, 1]):
        return False
    rows = pages._rows(result) if getattr(result, "hit", False) else []
    return not any(len(str(getattr(row, "text", "") or "").strip()) >= 2 for row in rows if row is not None)


def classify(rt, screen, frame, recognize):
    """``(kind, view)`` for one frame: "view" (每日灵感 settled on a tab), "unsettled" (每日灵感 otherwise), "calendar"
    (奇想日历), "layer" (the reward layer), "invalid" (voided by a focus change) or "other"."""
    if not rt._frame_still_valid(frame):
        return "invalid", None
    if screen is not None and screen.kind == "page" and screen.title == TITLE:
        view = read_view(frame, recognize)
        return ("view", view) if view is not None else ("unsettled", None)
    if screen is not None and screen.kind == "page" and screen.title == CALENDAR:
        return "calendar", None
    return ("layer" if is_reward_layer(screen, recognize) else "other"), None


def look(rt):
    """``(kind, view, frame, screen, recognize)`` from one fresh frame; kind None when the run stopped."""
    screen, frame, recognize = pages.screen_now(rt)
    if frame is None:
        return None, None, None, None, None
    kind, view = classify(rt, screen, frame, recognize)
    return kind, view, frame, screen, recognize


class Stop(Exception):
    """Something unexpected: report it and send nothing more (not even the way back). ``frames`` are saved."""

    def __init__(self, reason, frames=()):
        super().__init__(reason)
        self.frames = [frame for frame in frames if frame is not None]


def wait_state(rt, what, done, source, seconds=OPEN_WAIT):
    """Poll fresh frames until ``done(kind, view)`` holds; returns that view (or True when it has none). Frames for
    which ``source(screen, kind, view)`` holds are waited through up to ``seconds``; a frame on which nothing is
    recognised yet or 每日灵感 not settled only within TRANSITION_ALLOWANCE; any other frame, or the time running out,
    saves that frame and stops. None when the run stopped."""
    start = time.monotonic()
    while not rt.stopped:
        kind, view, frame, screen, recognize = look(rt)
        if kind is None:
            return None
        if kind != "invalid":
            if done(kind, view):
                return view if view is not None else True
            elapsed = time.monotonic() - start
            transition = kind == "unsettled" or (kind == "other" and pages.nothing_recognised(screen, recognize))
            if not (source(screen, kind, view) or (transition and elapsed <= TRANSITION_ALLOWANCE)):
                raise Stop(f"{what}时出现了不该出现的画面", [frame])
            if elapsed >= seconds:
                raise Stop(f"{what}超时", [frame])
        rt.pause(.4)
    return None


def open_page(rt, mode) -> bool:
    """HUD -> 奇想日历 -> the card of ``mode`` -> 每日灵感 settled on a tab."""
    if not pages.tap_shortcut(rt, "calendar"):
        return False
    if wait_state(rt, "打开奇想日历", lambda kind, v: kind == "calendar",
                  lambda screen, kind, v: screen is not None and screen.kind == "hud") is None:
        return False
    if not pages.tap_on_page(rt, CALENDAR, CARD_NODES[mode], accept=guarded_accept(CARD_NODES[mode]), retry=False,
                             exact=True):
        return False
    return wait_state(rt, f"打开{NAMES[mode]}", lambda kind, v: kind == "view",
                      lambda screen, kind, v: kind == "calendar") is not None and not rt.stopped


def open_tab(rt, mode) -> bool:
    """Select ``mode``'s tab: unless it is already selected, one guarded tap on its label (both tap frames the
    settled page on the other tab), then wait for it to settle."""
    view = wait_state(rt, f"切换到「{NAMES[mode]}」之前确认页面", lambda kind, v: kind == "view",
                      lambda screen, kind, v: False)
    if view is None or rt.stopped:
        return False
    if view.tab == mode:
        return True
    source = view.tab

    def on_source(frame, recognize):
        current = read_view(frame, recognize)
        return current is not None and current.tab == source
    if not pages.tap_on_page(rt, TITLE, TAB_NODES[mode], accept=guarded_accept(TAB_NODES[mode], on_source),
                             retry=False, exact=True):
        return False
    return wait_state(rt, f"切换到「{NAMES[mode]}」", lambda kind, v: kind == "view" and v.tab == mode,
                      lambda screen, kind, v: kind == "view" and v.tab == source) is not None and not rt.stopped


def settle(rt, mode) -> View | None:
    """The view of ``mode``'s tab once it reads the same, and agrees with the score, on at least two fresh frames
    SETTLE_SECONDS apart (the page's opening animation can light circles or count the score up). Frames of the page
    in between (another reading, not settled) are waited through up to OPEN_WAIT; anything else stops at once, and so
    does a view that never settles (with the last frame and what it showed)."""
    start, stable, since, count, frame = time.monotonic(), None, None, 0, None
    while not rt.stopped:
        kind, view, frame, screen, recognize = look(rt)
        if kind is None:
            return None
        if kind == "view" and view.tab == mode:
            if view == stable:
                count += 1
                if view.consistent() and count >= 2 and time.monotonic() - since >= SETTLE_SECONDS:
                    return view
            else:
                stable, since, count = view, time.monotonic(), 1
        elif kind not in ("invalid", "unsettled"):
            raise Stop(f"读取{NAMES[mode]}的奖励时出现了不该出现的画面", [frame])
        if time.monotonic() - start >= OPEN_WAIT:
            if stable is None:
                raise Stop(f"{NAMES[mode]}页面一直没有停稳", [frame])
            raise Stop(f"{NAMES[mode]}的积分和奖励圆圈对不上或认不出（积分 {stable.score}；{stable.describe()}）", [frame])
        rt.pause(.4)
    return None


def dismiss_layer(rt, mode, expected) -> bool:
    """One guarded tap on blank space of the reward layer (pages.tap_on_layer: the layer recognised with nothing
    read around the point and 每日灵感 on ``mode``'s tab beneath it, on both tap frames). A tap frame that is not
    that layer decides: the page back on ``mode`` with a view in ``expected`` continues (nothing was sent); anything
    else, a reward layer over anything else among them, saves it and stops."""
    try:
        return pages.tap_on_layer(rt, f"「{NAMES[mode]}」上的「恭喜获得」奖励层",
                                  lambda screen, recognize: is_reward_layer(screen, recognize)
                                  and blank_at_dismiss(recognize), DISMISS_POINT,
                                  verify=lambda frame: layer_over_tab(frame, mode))
    except pages.LayerMismatch as mismatch:
        if rt.stopped:
            return False
        kind, view = classify(rt, mismatch.screen, mismatch.frame, mismatch.recognize)
        if kind == "view" and view in expected:
            return True
        if kind == "layer" and not layer_over_tab(mismatch.frame, mode):
            raise Stop(f"「恭喜获得」奖励层下面认不出每日灵感的「{NAMES[mode]}」页签，没有点", [mismatch.frame]) from None
        raise Stop("「恭喜获得」奖励层在点之前变了，但画面没有回到每日灵感", [mismatch.frame]) from None


def claim(rt, mode, index, before) -> View | None:
    """Claim the lit circle ``index`` of ``before`` (the settled view): one tap, decided and confirmed on frames that
    show exactly ``before``. Returns the settled view with that circle claimed; Stop on anything else."""
    points = (index + 1) * 100
    what = f"{NAMES[mode]} {points} 分的奖励"

    def same(frame, recognize):
        return read_view(frame, recognize) == before
    roi = {"roi": circle_roi(index)}
    if not pages.tap_on_page(rt, TITLE, CIRCLE_LIT, override=roi, accept=guarded_accept(CIRCLE_LIT, same, roi),
                             retry=False, exact=True):
        return None
    pending = before
    done = View(mode, before.score, before.circles[:index] + (CLAIMED,) + before.circles[index + 1:])
    marked = time.monotonic()
    deadline, settled_at, settled_frames, dismissed, frame, last, kind = marked + CLAIM_WAIT, None, 0, False, None, None, None
    while not rt.stopped and time.monotonic() < deadline:
        kind, view, frame, screen, recognize = look(rt)
        if kind is None:
            return None
        if kind == "view" and view == done:
            settled_at = time.monotonic() if settled_at is None else settled_at
            settled_frames += 1
            if settled_frames >= 2 and time.monotonic() - settled_at >= SETTLE_SECONDS:
                rt.log(f"{what}已领取。")
                return view
        elif kind == "view" and (view == pending or (
                view.tab == mode and view.score == before.score and view.circles[index] == UNKNOWN
                and view.circles[:index] == before.circles[:index]
                and view.circles[index + 1:] == before.circles[index + 1:])):
            # Not landed yet, or that circle changing its look (waited for until the deadline, never acted on).
            settled_at, settled_frames, last = None, 0, view
        elif kind == "layer":
            settled_at, settled_frames = None, 0
            if not layer_over_tab(frame, mode):  # every layer frame, before and after the one tap
                raise Stop(f"领取{what}之后出现的「恭喜获得」奖励层下面认不出每日灵感的「{NAMES[mode]}」页签"
                           + ("" if dismissed else "，没有点"), [frame])
            if dismissed:
                pass  # still the layer after the one tap: waited for until the deadline
            else:
                dismissed = True
                if not dismiss_layer(rt, mode, (pending, done)):
                    return None
                marked = time.monotonic()
                deadline = max(deadline, marked + AFTER_DISMISS_WAIT)
                continue
        elif kind == "invalid" or ((kind == "unsettled" or (kind == "other" and pages.nothing_recognised(
                screen, recognize))) and time.monotonic() - marked <= TRANSITION_ALLOWANCE):
            pass
        else:
            shown = (f"积分 {view.score}；{view.describe()}" if kind == "view" else
                     "不是每日灵感，也不是奖励层" if kind != "unsettled" else "每日灵感没有停稳")
            raise Stop(f"领取{what}之后出现了不该出现的画面（{shown}）", [frame])
        rt.pause(.4)
    if rt.stopped:
        return None
    if kind == "layer":
        raise Stop(f"领取{what}后的「恭喜获得」奖励层" + ("点了一次空白处仍然没有关闭" if dismissed else "没有关闭"),
                   [frame])
    if last is not None and last.circles[index] == UNKNOWN:
        raise Stop(f"领取{what}之后，这个奖励圆圈的样子认不出（领取后的样子还没在设备上见过）", [frame])
    raise Stop(f"领取{what}之后没有看到它变成已领取", [frame])


def left_page(rt, mode, title, seconds: float = pages.LEAVE_PAGE_WAIT, step: float = .3):
    """pages.left_page for this task's back arrows, with two more positive mismatches: a frame of 每日灵感 showing
    another tab than ``mode``'s, and the 「恭喜获得」 layer's texts (over any page), are ``("other", frame, screen)``
    at once instead of frames waited through. ``("left", frame, screen)`` on the first valid frame that shows the HUD
    or a page with another title; frames of the page still showing (每日灵感 on ``mode``'s tab or not settled on a
    tab) and frames on which nothing at all is recognised are waited through; ``("timeout", frame, screen)`` with
    the last valid frame; ``(None, None, None)`` when the run stopped."""
    deadline, last = time.monotonic() + seconds, (None, None)
    while not rt.stopped:
        screen, frame, recognize = pages.screen_now(rt)
        if frame is None:
            return None, None, None
        if rt._frame_still_valid(frame):
            last = frame, screen
            if screen.kind == "hud" or (screen.kind == "page" and screen.title != title):
                return "left", frame, screen
            if screen.kind == "page":
                if title == TITLE and shown_tab(frame, recognize) not in (None, mode):
                    return "other", frame, screen
            elif is_reward_layer(screen, recognize) or not pages.nothing_recognised(screen, recognize):
                return "other", frame, screen
        if time.monotonic() >= deadline:
            return ("timeout", *last) if last[0] is not None else ("timeout", frame, screen)
        rt.pause(step)
    return None, None, None


def back_home(rt, mode, attempts: int = 3) -> bool:
    """The way back after the task: the back arrow, one attempt each, only on a page titled exactly 每日灵感 showing
    ``mode``'s tab (shown_tab, on the frame that decides which page this is and on both tap frames) or 奇想日历
    (tap_on_page with retry=False: a tap frame that is not raises FrameMismatch with that frame), and no other back
    arrow until fresh frames show that page gone (left_page: another tab or a reward layer meanwhile stops). The very
    frame that shows it gone must be the HUD or, after 每日灵感, 奇想日历; any other page there (商城, ...), 每日灵感
    again once it was left, or anything else stops with that frame."""
    titles = (TITLE, CALENDAR)

    def on_tab(frame_, recognize_):
        return shown_tab(frame_, recognize_) == mode
    for _ in range(attempts):
        screen, frame, _recognize = pages.screen_now(rt)
        if frame is None:
            return False
        if not rt._frame_still_valid(frame):
            continue
        if screen.kind == "hud":
            return True
        if screen.kind != "page" or screen.title not in titles:
            raise Stop("回大世界时出现了不该出现的画面", [frame])
        title = screen.title
        if title == TITLE and shown_tab(frame, _recognize) != mode:
            raise Stop(f"回大世界时每日灵感不在「{NAMES[mode]}」页签上", [frame])
        if not pages.tap_on_page(rt, title, actions.BACK_ARROW, accept=on_tab if title == TITLE else None,
                                 retry=False, exact=True):
            return False
        state, frame, after = left_page(rt, mode, title)
        if state is None:
            return False
        if state != "left":
            raise Stop(f"点了「{title}」的返回箭头之后" + ("页面一直没有离开" if state == "timeout" else "出现了不该出现的画面"),
                       [frame])
        if after.kind == "hud":
            return True
        if not (title == TITLE and after.kind == "page" and after.title == CALENDAR):
            raise Stop(f"点了「{title}」的返回箭头之后出现了不该出现的页面（{after.title}）", [frame])
        titles = (CALENDAR,)
    raise Stop("回大世界时返回次数用完了", [])


def collect(rt, mode) -> bool:
    name = NAMES[mode]
    start = []
    if not pages.to_hud(rt, rejected=lambda frame, reason: start.append((frame, reason)), single=True):
        if start:
            raise Stop(f"开始时不在大世界，也没法安全地回去：{start[0][1]}", [start[0][0]])
        return False
    if not (open_page(rt, mode) and open_tab(rt, mode)):
        return False
    view = settle(rt, mode)
    if view is None:
        return False
    lit = [index for index, look_ in enumerate(view.circles) if look_ == LIT]
    rt.log(f"{name}当前积分 {view.score}（{view.describe()}）。")
    for index in lit:
        view = claim(rt, mode, index, view)
        if view is None:
            return False
    claimed = sum(look_ == CLAIMED for look_ in view.circles)
    rt.log(f"{name}：领取了 {len(lit)} 个奖励。" if lit else
           f"{name}：没有可领取的奖励（已领取 {claimed} 个，积分 {view.score}）。")
    return back_home(rt, mode)


def run(rt, mode) -> bool:
    from navigation.models import NavigationError
    if mode not in MODES:
        return False
    rt.log(f"{NAMES[mode]}（安卓）：只读取积分并领取已达成的奖励，不做心愿任务。")
    try:
        return bool(collect(rt, mode)) and not rt.stopped
    except Stop as error:
        for index, frame in enumerate(error.frames):
            rt.diagnostic(f"android_daily_stop_{index}", str(error), image=frame)
        fail(rt, mode, str(error))
    except pages.FrameMismatch as error:  # a tap frame that was not the expected state: no retry
        rt.diagnostic("android_daily_stop_0", str(error), image=error.frame)
        fail(rt, mode, str(error))
    except (pages.GuardRefusal, actions.ScreenUnrecognised, NavigationError) as error:
        frame = getattr(error, "frame", None)
        if frame is not None:
            rt.diagnostic("android_daily_stop_0", str(error), image=frame)
        fail(rt, mode, str(error))
    return False


def fail(rt, mode, reason) -> bool:
    from host import android_user_stop
    if not rt.stopped and not android_user_stop():  # a user stop is no failure
        rt.log(f"{NAMES[mode]}：{reason}，本项未完成。")
    return False
