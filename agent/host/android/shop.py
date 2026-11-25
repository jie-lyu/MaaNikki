"""每周免费礼包 (MaaNikki_WeeklyFreeGifts) on the Android client: read the three free weekly gifts in 商城 -> 甄选礼包
and report each one. This version claims nothing.

HUD -> 商城 shortcut -> (the left tab list swiped when 甄选礼包 is not showing) -> 甄选礼包 tab -> the gift list read
frame by frame, swiped down in small guarded steps, until 虹迹赠礼, 芳馨赠礼 and 星实置业之礼 have each been read on a
settled frame -> back arrow to the HUD. The PC task's account state (UID, weekly record) is not used: nothing is
claimed, so nothing is recorded.

What the saved frames show (1280x720, survey c3 of 2026-11-26): the three gifts are the last cards of 甄选礼包 (虹迹赠礼
in 虹染礼包, 芳馨赠礼 in 花露礼包, 星实置业之礼 alone in 支援礼包, the last card). A card's title is OCR'd exactly; its
price band ~41 px below reads 「免费」 (also when sold out); a sold-out card shows the pill 「已售罄」 ~57 px above the
title and 「本周限购0/1」 at its top right. The counter is what is left of the weekly limit: unbought cards read
「本周限购10/10」 and 「本周限购20/20」. Paid cards read ¥ prices, game-currency cards bare digits (「8060」, 「100」).
All three gifts were sold out in that week, so a claimable card and the claim popup (商品详情) have never been seen:
a claimable gift is only reported, never tapped.

Every touch is a guarded tap or swipe decided and confirmed on fresh frames (pages.tap_on_page, pages.swipe_on_page):
never next to a forbidden word (购买, ¥, 共鸣, 获取 ...), and the swipe points also never on or next to a price band
(¥, bare digits, 免费: the whole band of the card, not only its text), on the 回到顶部 button, or (start point) on
anything but empty list background: a swipe started in the narrow gap between two card columns once did not scroll,
swipes started on empty background always did. The survey's 400 px swipes scrolled the list 480-670 px (it keeps
moving after the lift), more than a card's height plus the visible list allows without skipping a card; these swipes
are 250 px over 1 s (device run 1, 2026-11-26: one such swipe moved the list ~315 px, estimated from the frames,
and its lift was confirmed on the device), and the list's measured movement is logged after every swipe; the frame
the first list swipe was decided on is saved with the diagnostics. Any frame that is not the 商城 page on
甄选礼包, a card that cannot be read, or a gift not seen by the list's end stops the task, saves the frame and sends
nothing more.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import time

import cv2
import numpy as np

from host.android import actions, pages

TITLE = "商城"
GIFTS = ("虹迹赠礼", "芳馨赠礼", "星实置业之礼")
GIFTS_TAB = "MaaNikki_Android_ShopGiftsTab"
GIFT_TITLE = "MaaNikki_Android_ShopGiftTitle"
CARD_TEXT = "MaaNikki_Android_ShopCardText"
SOLD_OUT = "MaaNikki_Android_ShopSoldOut"
WEEKLY_LIMIT = "MaaNikki_Android_ShopWeeklyLimit"
PRICE_GUARD = "MaaNikki_Android_ShopPriceGuard"

# The 甄选礼包 label's box is gold when the tab is selected (saved frames: 0.772 selected, 0.011 and 0.000 not).
TAB_SELECTED = .45
# The left tab list, swiped as in the survey (it brought 甄选礼包 into view from the opening event tab).
LEFT_SWIPE = ((130, 600), (130, 250), 600)
LEFT_SWIPES = 2
OPEN_WAIT = 10

# The gift list (x, y, w, h): cards scroll under the top bar at y 60.
LIST = (270, 60, 910, 640)
LIST_TOP = 60
# Regions of a card relative to its title box (x, y, w, h), as (dx from the title's centre, dy from its top, w, h):
# the price band, the 已售罄 pill and the weekly counter. Measured on c3-shop-gifts-05/06 (all three gifts).
BAND = (-100, 28, 200, 58)
PILL = (-80, -85, 160, 65)
LIMIT = (-20, -185, 150, 50)
LIMIT_TEXT = re.compile(r"本周限购\s*(\d+)\s*/\s*(\d+)")

# Swipe candidates: begin points on a grid over the whole list, lowest first and right to left within a row, each
# swiped SWIPE_DISTANCE straight up; the end must stay inside the list (y >= LIST_TOP), so begins above y 310 never
# qualify. Device run 1 stopped on a frame whose only empty background was x ~500-1180, y ~80-355 (above a section
# header), where the old eight rows from y 360 found nothing.
SWIPE_XS = tuple(range(1150, 299, -30))
SWIPE_BEGIN_YS = tuple(range(640, 79, -10))
SWIPE_DISTANCE = 250
SWIPE_MS = 1000
MAX_SWIPES = 16
# Empty list background around the begin point: a flat beige patch (saved frames: std of S and V <= 2, mean V ~235,
# mean S 36-80; cards, card images, edges and texts are far from flat).
BLANK_HALF, BLANK_STD, BLANK_VALUE, BLANK_SATURATION = 30, 8, 190, 25
# A price row excludes its card's whole band (cards are ~205 px wide, the text sits in the middle), plus the margin.
BAND_HALF_WIDTH = 105
MARGIN = pages.FORBIDDEN_MARGIN
TOP_BUTTON = (1130, 585, 75, 75)  # 回到顶部, an icon without text, once the list is scrolled
# The list counts as unchanged below this mean grey difference (saved frames: 0.24 and 0.69 for frames of the same
# list position with its animations, >= 20 after any scroll).
UNCHANGED_DIFF = 3.0
SETTLE_SECONDS = 5
SHIFT_STRIP = (520, 640)  # rows of the earlier frame looked for in the later one to measure a swipe's movement


class Stop(Exception):
    """Something unexpected: report it and send nothing more (not even the way back). ``frames`` are saved."""

    def __init__(self, reason, frames=()):
        super().__init__(reason)
        self.frames = [frame for frame in frames if frame is not None]


@dataclass(frozen=True)
class Card:
    """One free gift's card read on one frame. state: "sold_out", "claimable", "unknown" (counter unreadable and no
    pill) or "not_free" (the price band is not exactly 「免费」)."""
    name: str
    state: str
    band: str
    sold_out_pill: bool
    limit: tuple | None


def _rows(result):
    if result is None or not getattr(result, "hit", False):
        return []
    rows = list(getattr(result, "filtered_results", None) or []) or [getattr(result, "best_result", None)]
    return [(str(getattr(row, "text", "") or ""), [int(v) for v in getattr(row, "box", None) or []])
            for row in rows if row is not None and len(getattr(row, "box", None) or []) == 4]


def _overlaps(box, zone) -> bool:
    x, y, w, h = box
    zx, zy, zw, zh = zone
    return x < zx + zw and zx < x + w and y < zy + zh and zy < y + h


def _grow(box, margin):
    x, y, w, h = box
    return [x - margin, y - margin, w + 2 * margin, h + 2 * margin]


def gifts_tab(recognize):
    result = recognize(GIFTS_TAB)
    return actions._box(result) if actions._hit(result) else None


def tab_gold(frame, box) -> float:
    x, y, w, h = box
    hsv = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
    return cv2.countNonZero(cv2.inRange(hsv, (10, 85, 130), (35, 255, 255))) / max(1, w * h)


def tab_state(frame, recognize):
    """"selected", "unselected" or None (甄选礼包 not in the left list on this frame)."""
    box = gifts_tab(recognize)
    if box is None:
        return None
    return "selected" if tab_gold(frame, box) >= TAB_SELECTED else "unselected"


def price_zones(recognize):
    """Where no touch point may be: every price text (¥, bare digits, 免费, 购买) grown by the margin, in the gift
    list its card's whole band grown by the margin, and the 回到顶部 button grown by the margin. None when the check
    could not run. (Outside the list, e.g. red dots on the left tabs that OCR reads as 「8」, only the text counts.)"""
    result = recognize(PRICE_GUARD)
    if result is None:
        return None
    zones = []
    for _text, (x, y, w, h) in _rows(result):
        centre = x + w // 2
        if centre >= LIST[0]:
            zones.append(_grow([centre - BAND_HALF_WIDTH, y - 8, 2 * BAND_HALF_WIDTH, h + 16], MARGIN))
        else:
            zones.append(_grow([x, y, w, h], MARGIN))
    zones.append(_grow(list(TOP_BUTTON), MARGIN))
    return zones


def forbidden_zones(recognize):
    result = recognize(pages.FORBIDDEN)
    if result is None:
        return None
    return [_grow(box, MARGIN) for _text, box in _rows(result)]


def blank(frame, point) -> bool:
    x, y = point
    patch = frame[y - BLANK_HALF:y + BLANK_HALF, x - BLANK_HALF:x + BLANK_HALF]
    if patch.shape[:2] != (2 * BLANK_HALF, 2 * BLANK_HALF):
        return False
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.float32)
    mean, std = hsv.mean(0), hsv.std(0)
    return bool(std[1] <= BLANK_STD and std[2] <= BLANK_STD and mean[2] >= BLANK_VALUE
                and mean[1] >= BLANK_SATURATION)


def points_clear(frame, recognize, begin, end, zones=None) -> bool:
    """The begin point on empty list background, neither point in a price zone (zones read on this frame)."""
    zones = price_zones(recognize) if zones is None else zones
    if zones is None or not blank(frame, begin):
        return False
    return not any(_overlaps([px, py, 1, 1], zone) for px, py in (begin, end) for zone in zones)


def in_list(point) -> bool:
    x, y = point
    lx, ly, lw, lh = LIST
    return lx <= x < lx + lw and ly <= y < ly + lh


def choose_swipe(frame, recognize, avoid=None):
    """``(begin, end)`` of the first candidate whose begin is empty background, whose end is inside the list and
    whose points are clear of every price zone and forbidden label on this frame (``avoid``: a begin point not to
    use again, nor anything within 60 px of it), else None."""
    prices, forbidden = price_zones(recognize), forbidden_zones(recognize)
    if prices is None or forbidden is None:
        return None
    for y in SWIPE_BEGIN_YS:
        for x in SWIPE_XS:
            begin, end = (x, y), (x, y - SWIPE_DISTANCE)
            if not (in_list(begin) and in_list(end)):
                continue
            if avoid is not None and abs(x - avoid[0]) < 60 and abs(y - avoid[1]) < 60:
                continue
            if not points_clear(frame, recognize, begin, end, prices):
                continue
            if any(_overlaps([px, py, 1, 1], zone) for px, py in (begin, end) for zone in forbidden):
                continue
            return begin, end
    return None


def _grey(frame):
    x, y, w, h = LIST
    return cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)


def list_diff(a, b) -> float:
    return float(np.abs(_grey(a).astype(np.int16) - _grey(b).astype(np.int16)).mean())


def movement(before, after):
    """How far the list content moved up between two frames (pixels), or None when the strip was not found (it
    moved more than ~460 px, or the content changed)."""
    x, _y, w, _h = LIST
    top, bottom = SHIFT_STRIP
    strip = cv2.cvtColor(before[top:bottom, x:x + w], cv2.COLOR_BGR2GRAY)
    area = cv2.cvtColor(after[LIST_TOP:720, x:x + w], cv2.COLOR_BGR2GRAY)
    _low, best, _at, (_left, found) = cv2.minMaxLoc(cv2.matchTemplate(area, strip, cv2.TM_CCOEFF_NORMED))
    return top - (LIST_TOP + found) if best >= .8 else None


def _region(title_box, offset):
    x, y, w, _h = title_box
    dx, dy, rw, rh = offset
    return [x + w // 2 + dx, y + dy, rw, rh]


def _inside(region) -> bool:
    x, y, w, h = region
    return 0 <= x and x + w <= 1280 and LIST_TOP <= y and y + h <= 720


def read_card(recognize, name, title_box):
    """The card under ``title_box`` (its title read exactly as ``name``), or None when the card is not wholly in the
    list on this frame (its counter, pill or band cut off)."""
    band_roi, pill_roi, limit_roi = (_region(title_box, offset) for offset in (BAND, PILL, LIMIT))
    if not all(_inside(region) for region in (band_roi, pill_roi, limit_roi)):
        return None
    band, pill, limit = (recognize(CARD_TEXT, {"roi": band_roi}), recognize(SOLD_OUT, {"roi": pill_roi}),
                         recognize(WEEKLY_LIMIT, {"roi": limit_roi}))
    if band is None or pill is None or limit is None:
        raise Stop(f"「{name}」这张卡片的文字识别没能完成")
    band_text = "".join(text for text, _box in sorted(_rows(band), key=lambda row: row[1][0]))
    band_text = re.sub(r"\s+", "", band_text)
    sold = actions._hit(pill)
    match = next((LIMIT_TEXT.search(text) for text, _box in _rows(limit) if LIMIT_TEXT.search(text)), None)
    counter = (int(match.group(1)), int(match.group(2))) if match else None
    if band_text != "免费":
        state = "not_free"
    elif sold or (counter is not None and counter[0] == 0):
        state = "sold_out"
    elif counter is not None and counter[0] >= 1:
        state = "claimable"
    else:
        state = "unknown"
    return Card(name, state, band_text, sold, counter)


def read_gifts(recognize) -> dict:
    """The free gifts wholly in the list on one frame, by name."""
    rows = [(text, box) for text, box in _rows(recognize(GIFT_TITLE)) if text in GIFTS]
    names = [text for text, _box in rows]
    if len(set(names)) != len(names):
        raise Stop("同一张画面上同一个礼包的名字出现了两次")
    cards = {}
    for name, box in rows:
        card = read_card(recognize, name, box)
        if card is not None:
            cards[name] = card
    return cards


def on_gifts(frame, recognize) -> bool:
    return tab_state(frame, recognize) == "selected"


def settled(rt, what):
    """``(frame, recognize)`` of the 商城 page on 甄选礼包 once two fresh valid frames in a row show the list in the
    same place; Stop (frame saved) when that does not happen within SETTLE_SECONDS; ``(None, None)`` when stopped."""
    deadline, previous, last = time.monotonic() + SETTLE_SECONDS, None, None
    while not rt.stopped:
        screen, frame, recognize = pages.screen_now(rt)
        if frame is None:
            return None, None
        last = frame
        if rt._frame_still_valid(frame):
            if pages.is_page(screen, TITLE) and on_gifts(frame, recognize):
                if previous is not None and list_diff(previous, frame) < UNCHANGED_DIFF:
                    return frame, recognize
                previous = frame
            else:
                previous = None
        if time.monotonic() >= deadline:
            raise Stop(f"{what}之后画面没有停在商城的「甄选礼包」页签上", [last])
        rt.pause(.3)
    return None, None


def open_gifts(rt) -> bool:
    """From the 商城 page to the 甄选礼包 tab: a guarded tap on its label (unselected on both tap frames), after at
    most LEFT_SWIPES guarded swipes of the left tab list when it is not in view."""
    frame = None
    for swipes in range(LEFT_SWIPES + 1):
        screen, frame, recognize = pages.screen_now(rt)
        if frame is None:
            return False
        if not rt._frame_still_valid(frame):
            rt.pause(.4)
            continue
        if not pages.is_page(screen, TITLE):
            raise Stop("要打开「甄选礼包」时画面不是商城页面", [frame])
        state = tab_state(frame, recognize)
        if state == "selected":
            return True
        if state == "unselected":
            def unselected(frame, recognize):
                box, zones = gifts_tab(recognize), price_zones(recognize)
                return (box is not None and tab_gold(frame, box) < TAB_SELECTED and zones is not None
                        and not any(_overlaps(box, zone) for zone in zones))
            if not pages.tap_on_page(rt, TITLE, GIFTS_TAB, accept=unselected):
                return False
            frame, _recognize = settled(rt, "点了「甄选礼包」")
            return frame is not None
        if swipes == LEFT_SWIPES:
            break
        begin, end, ms = LEFT_SWIPE

        def tab_not_in_view(frame, recognize, begin=begin, end=end):
            zones = price_zones(recognize)
            return (gifts_tab(recognize) is None and zones is not None
                    and not any(_overlaps([px, py, 1, 1], zone) for px, py in (begin, end) for zone in zones))
        if not pages.swipe_on_page(rt, TITLE, begin, end, duration=ms, accept=tab_not_in_view):
            return False
        rt.pause(.8)  # the list glides on after the lift; the tab tap is confirmed on two frames anyway
    raise Stop(f"商城左侧的页签列表滑了 {LEFT_SWIPES} 次仍然找不到「甄选礼包」", [frame])


def scan(rt):
    """Each free gift's card as first read wholly on a settled frame, by name; None when the run stopped."""
    frame, recognize = settled(rt, "打开「甄选礼包」")
    if frame is None:
        return None
    cards, swipes, unchanged, last_begin = {}, 0, 0, None
    while True:
        for name, card in read_gifts(recognize).items():
            if name in cards:
                continue
            if card.state not in ("sold_out", "claimable"):
                what = "价格栏不是「免费」" if card.state == "not_free" else "认不出是否已售罄"
                raise Stop(f"「{name}」的卡片{what}（价格栏「{card.band}」，已售罄标记 {card.sold_out_pill}，"
                           f"本周限购 {card.limit}）", [frame])
            cards[name] = card
        if len(cards) == len(GIFTS):
            return cards
        missing = "、".join(name for name in GIFTS if name not in cards)
        if swipes >= MAX_SWIPES:
            raise Stop(f"礼包列表滑了 {MAX_SWIPES} 次仍没有看到：{missing}", [frame])
        choice = choose_swipe(frame, recognize, avoid=last_begin if unchanged else None)
        if choice is None:
            raise Stop("礼包列表上找不到可以安全开始滑动的空白处", [frame])
        begin, end = choice
        reference = frame
        if swipes == 0:  # the list as the first swipe found it, for checking the start point afterwards
            rt.diagnostic("android_weekly_before_first_swipe", f"第一次滑动礼包列表之前（{begin}→{end}）", image=frame)

        def same_list(frame, recognize, reference=reference, begin=begin, end=end):
            return (on_gifts(frame, recognize) and list_diff(reference, frame) < UNCHANGED_DIFF
                    and points_clear(frame, recognize, begin, end))
        if not pages.swipe_on_page(rt, TITLE, begin, end, duration=SWIPE_MS, accept=same_list):
            return None
        swipes, last_begin = swipes + 1, begin
        what = f"每周免费礼包：第 {swipes} 次滑动礼包列表 {begin}→{end}"
        try:
            frame, recognize = settled(rt, "滑动礼包列表")
        except Stop:
            rt.log(f"{what}，之后列表没有停稳，没量出移动。")
            raise
        if frame is None:
            return None
        if list_diff(reference, frame) < UNCHANGED_DIFF:
            rt.log(f"{what}，列表没有移动。")
            unchanged += 1
            if unchanged >= 2:
                raise Stop(f"礼包列表连续两次滑不动（可能已经到底），仍没有看到：{missing}", [frame])
        else:
            unchanged = 0
            moved = movement(reference, frame)
            rt.log(f"{what}，列表移动 {moved} 像素。" if moved is not None
                   else f"{what}，列表移动超过约 460 像素或没量出。")


def report(rt, cards) -> list:
    """One line per gift; the names of the claimable ones."""
    claimable = []
    for name in GIFTS:
        card = cards[name]
        counter = f"本周限购 {card.limit[0]}/{card.limit[1]}" if card.limit else "限购数没读出"
        if card.state == "sold_out":
            rt.log(f"每周免费礼包：「{name}」本周已领取（已售罄，{counter}）。")
        else:
            claimable.append(name)
            rt.log(f"每周免费礼包「{name}」可以领取，但领取的画面还没在设备上见过，本次没有点；请手动领取或等下一版。")
    return claimable


def run(rt) -> bool:
    from navigation.models import NavigationError
    cards = None
    try:
        if not pages.to_hud(rt):
            return fail(rt, "没能回到大世界")
        if not rt.key("shop"):
            return False
        if pages.wait_for(rt, lambda screen: pages.is_page(screen, TITLE), OPEN_WAIT) is None:
            return fail(rt, "没能打开商城")
        if not open_gifts(rt):
            return False
        cards = scan(rt)
    except Stop as error:
        for index, frame in enumerate(error.frames):
            rt.diagnostic(f"android_weekly_stop_{index}", str(error), image=frame)
        fail(rt, str(error))
    except (pages.GuardRefusal, actions.ScreenUnrecognised, NavigationError) as error:
        fail(rt, str(error))
    # The way back only after every gift was read: after a stop, a guard refusal or a fatal error (raised past this
    # point, e.g. the device stopped answering) nothing more is sent.
    if cards is None or rt.stopped:
        return False
    claimable = report(rt, cards)
    if not pages.back_through(rt, (TITLE,)):
        return fail(rt, "没能回到大世界")
    if claimable:
        return fail(rt, f"「{'」「'.join(claimable)}」没有领取")
    return True


def fail(rt, reason) -> bool:
    if not rt.stopped:
        rt.log(f"每周免费礼包：{reason}，本项未完成。")
    return False
