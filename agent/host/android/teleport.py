"""Big-map teleport on the Android client (phase E): from the open-world HUD to a teleport point of 星海.

HUD -> the minimap tapped (the big map opens on the page titled 地图) -> the region card read; another region: its title
tapped, 星海 chosen in 地图切换 (an expanded province collapsed first when 星海 is scrolled out of the list) -> "+"
tapped until the zoom bar shows the maximum -> the map frame located on the world map (navigation maps/starsea.png at
MAX_ZOOM_SCALE world px per screen px) -> drags that start and end on blank terrain until the point lies in
TARGET_ZONE -> the point's pillar icon tapped -> the point panel: its title exactly the point's name, its buttons
exactly 详情 and 传送 -> 传送 tapped once (found by OCR) -> loading -> the open-world HUD, clean, and the point's world
coordinates returned (the walker's position hint).

Rules (docs/android-support-plan.md, 阶段 E and decision 2): every touch is decided on a fresh focus-confirmed frame that
shows the whole expected state and confirmed on a second fresh frame (pages.tap_on_page / swipe_on_page with
retry=False), never within pages.FORBIDDEN_MARGIN of a forbidden label (MaaNikki_Android_Forbidden) or of a map word
(MAP_WORDS: 前往 as in 立即前往, 追踪, 导航, 解锁, ...; full OCR boxes on both frames). 立即前往 is never tapped: a panel
with any other button than 详情 and 传送, or another title, is closed by its ✕ and the task stops. The 传送 tap is never
repeated; no BACK key is sent; any screen that is not the expected one stops the task with that frame saved (Stop).

What the device frames show (1280x720, .build/android/map-survey, 2026-11-26, only opened, looked at and closed):
- the map page: back arrow ~(57,43), title 「地图」; the region card ~x 985-1240, y 78-215 with the region's name
  (星海 [1070,84,50,27]; elsewhere the region, e.g. 纪念山地) and, on 星海 before the bell, 「今日尚未进行召唤摇铃」 /
  「快前往流转之枢吧」; zoom bar y ~669 with "−" ~(47,668) and "+" ~(365,668): 星海 has three levels, the orange segment
  x 163-248 by default and x 251-332 at the maximum (one tap); the bottom-left round buttons, 标记点筛选 at the bottom
  right and the stamina 「350/350 +」 at the top right are never tapped;
- 地图切换 (a tap on the card's title): a right-hand list of province headers and region rows that opens scrolled to the
  current region; 星海 is a single-region province listed first, so from 心愿原野 it is above the visible rows; a tap on
  the expanded province's 「^」 collapsed it and showed 星海 [888,100,57,32]; a tap on 星海 switched the map and closed
  the list by itself;
- drags are 1:1 (no inertia at 500 ms); the max zoom is ~0.4655 world px per screen px (measured 1.452x the default
  ~0.676); every teleport pillar is the same constant-size icon (template map_pillar.png scores 0.91-1.00 on every
  pillar seen, <= 0.54 elsewhere);
- the point panel of 星梦群屿·主岛: title [177,481,177,28], buttons 「详情」 [766,639,74,39] and 「传送」 [1000,637,70,40]
  (button frames ~20 px apart), a round ✕ ~(1092,491) that closed it; no 立即前往, 追踪 or candidate list.
Not seen yet: the 无界枢纽 panel, the teleport loading screen, the arrival, a list with more provinces above 星海.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time

import cv2
import numpy as np

from host.android import actions, pages

MAP_TITLE = "地图"
STARSEA = "星海"
REGION_NAME = "MaaNikki_Android_MapRegionName"
CARD_TEXT = "MaaNikki_Android_MapCardText"
REGION_MENU = "MaaNikki_Android_MapRegionMenu"
MENU_STARSEA = "MaaNikki_Android_MapMenuStarsea"
MENU_COLLAPSE = "MaaNikki_Android_MapMenuCollapse"
ZOOM_IN = "MaaNikki_Android_MapZoomIn"
PILLAR = "MaaNikki_Android_MapPillar"
PANEL_TITLE = "MaaNikki_Android_MapPanelTitle"
PANEL_BUTTONS = "MaaNikki_Android_MapPanelButtons"
TELEPORT = "MaaNikki_Android_MapTeleport"
PANEL_CLOSE = "MaaNikki_Android_MapPanelClose"
MAP_WORDS = "MaaNikki_Android_MapWords"
HUD_BELL = "MaaNikki_Android_HudBell"
NODES = (REGION_NAME, CARD_TEXT, REGION_MENU, MENU_STARSEA, MENU_COLLAPSE, ZOOM_IN, PILLAR, PANEL_TITLE,
         PANEL_BUTTONS, TELEPORT, PANEL_CLOSE, MAP_WORDS, HUD_BELL)

# ---- the world map ----
MAX_ZOOM_SCALE = 0.4655  # world px per screen px at the maximum zoom (1.452 x the default ~0.676)
LOCATE_ROI = (250, 110, 700, 350)  # terrain only: clear of the back arrow, the card, the bottom buttons and a panel
LOCATE_MIN = 0.85  # the survey's max-zoom frames: 0.97-0.99
LOCATE_MARGIN = 0.12  # over the best match elsewhere (the survey's: 0.69-0.75)
LOCATE_SUPPRESS = 40  # world px around the best match left out when looking for the runner-up
# ---- blank terrain: the frame against the world map rendered at the located pose ----
FIT_ZONE = (260, 130, 940, 470)  # where the brightness fit is taken (x0, y0, x1, y1): no fixed control, no panel
RESIDUAL_BLUR = 9
BLANK_RADIUS = 24  # px around a drag end that must be blank
BLANK_MAX = 12.0  # survey 05b: bare sea 2-8, land 6; a pillar ~60, a label ~54, the card ~65, a round button ~100
# ---- drags and the target zone ----
DRAG_ZONE = (260, 140, 940, 470)  # both ends of every drag (x0, y0, x1, y1)
TARGET_ZONE = (340, 180, 860, 430)  # where the point must lie before its pillar is tapped
TARGET_POINT = (600, 300)
DRAG_MAX = (560, 300)
DRAG_MIN = 60
DRAG_STEP = 20  # the grid of candidate drag starts
DRAG_MS = 500
DRAG_TOLERANCE = 40  # px between the expected and the observed map shift after a drag
MAX_DRAGS = 8
POSE_TOLERANCE = 8  # px between the deciding frame's and the confirming frame's located views
# ---- the pillar ----
PILLAR_ABOVE_U = 4.7  # world px from a checkpoint up to its icon's centre (survey: 4.6 at max zoom, 4.7 by default)
PILLAR_TOLERANCE = 18  # px (the 无界枢纽 checkpoint sits ~5 px off its icon at the default zoom)
PILLAR_WINDOW = (40, 55, 80, 90)  # search window: left/up from the expected icon centre, width, height
# ---- the zoom bar ----
ZOOM_BAND = (60, 664, 290, 11)  # x, y, w, h
ZOOM_MAX_RIGHT = 320  # the orange segment ends here or further right at the maximum (survey: x 332; default 248)
# ---- the panel ----
PANEL_ZONE = (100, 466, 1100, 240)
PANEL_BUTTON_NAMES = ("传送", "详情")
DOTS = "·•・‧∙⋅."
# ---- the card ----
NOT_RUNG = re.compile(r"尚未.*摇铃")
RUNG = re.compile(r"已.*摇铃")
COUNTER = re.compile(r"^(\d{1,3})[/／](\d{1,3})$")
TOTALS = (3, 30, 100)  # 星之魄, 星光结晶, 星光微尘 (the PC card's counters)
CRYSTAL_TOTAL = 30
# ---- waits (seconds) ----
OPEN_WAIT = 8
MENU_WAIT = 5
SWITCH_WAIT = 6
ZOOM_WAIT = 3
PANEL_WAIT = 4
CARD_WAIT = 4
ARRIVE_WAIT = 60
MAP_LINGER = 10  # the map may stay up this long after 传送 before something else must show
CLEAN_HUD_WAIT = 15
STEP = .4


class Stop(Exception):
    """Something unexpected: report it and send nothing more. ``frames`` are saved; the message is fixed text (it
    reaches the user), never what OCR read."""

    def __init__(self, reason, frames=()):
        super().__init__(reason)
        self.frames = [frame for frame in frames if frame is not None]


def squash(text) -> str:
    return re.sub(r"\s", "", str(text or ""))


def point_name(text) -> str:
    """A point name as compared: whitespace aside, every middle dot as 「·」."""
    return "".join("·" if ch in DOTS else ch for ch in squash(text))


def _cjk(text) -> int:
    return sum("一" <= ch <= "鿿" for ch in text)


def _rows(result):
    if result is None or not getattr(result, "hit", False):
        return []
    return [(str(getattr(row, "text", "") or ""), [int(v) for v in getattr(row, "box", None) or []])
            for row in pages._rows(result) if row is not None]


def _centre(box):
    return box[0] + box[2] / 2, box[1] + box[3] / 2


# ---- the world map ----

@dataclass(frozen=True)
class Pose:
    """Where a map frame shows the world: the world point under the screen centre (640, 360), the match score, the
    best score elsewhere and the scale (world px per screen px)."""
    x: float
    y: float
    score: float
    second: float
    scale: float = MAX_ZOOM_SCALE

    @property
    def trusted(self) -> bool:
        return self.score >= LOCATE_MIN and self.score - self.second >= LOCATE_MARGIN

    def to_screen(self, x, y):
        return 640 + (x - self.x) / self.scale, 360 + (y - self.y) / self.scale

    def log(self) -> dict:
        return {"x": round(self.x, 1), "y": round(self.y, 1), "score": round(self.score, 3),
                "second": round(self.second, 3)}


def _resource():
    from app_paths import RESOURCE_ROOT
    return RESOURCE_ROOT / "resource"


class WorldMap:
    """The world map of one navigation map (luma, as the map page draws it: on the survey frames a fitted gain of
    1.01 and offset of -1 to 0). Loading it reads the image once; keep one per run."""

    def __init__(self, map_id: str = "starsea", resource=None, *, image=None):
        if image is None:
            from navigation.models import NavigationError, maps
            spec = maps(resource or _resource()).get(map_id)
            if spec is None:
                raise NavigationError("没有这张地图的资源，不能在安卓模拟器上传送。")
            image = cv2.imread(str(spec.image), cv2.IMREAD_UNCHANGED)
            if image is None:
                raise NavigationError("地图图片无法读取，不能在安卓模拟器上传送。")
        if image.ndim == 3:
            image = cv2.cvtColor(image[..., :3], cv2.COLOR_BGR2GRAY)
        self.image = np.ascontiguousarray(image, dtype=np.uint8)

    def locate(self, frame, scale: float = MAX_ZOOM_SCALE, roi=LOCATE_ROI) -> Pose:
        """The pose of a map frame: LOCATE_ROI scaled to world px and matched on the whole world map
        (TM_CCOEFF_NORMED), with the best score more than LOCATE_SUPPRESS world px away as ``second``."""
        x0, y0, w, h = roi
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        patch = cv2.resize(gray[y0:y0 + h, x0:x0 + w], None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        rates = cv2.matchTemplate(self.image, patch, cv2.TM_CCOEFF_NORMED)
        rates = np.nan_to_num(rates, nan=-1, posinf=-1, neginf=-1)
        _, score, _, (lx, ly) = cv2.minMaxLoc(rates)
        rest = rates.copy()
        cv2.circle(rest, (lx, ly), LOCATE_SUPPRESS, -1.0, -1)
        second = float(rest.max())
        return Pose(lx + (640 - x0) * scale, ly + (360 - y0) * scale, float(score), second, scale)

    def rendered(self, pose: Pose):
        """The world map drawn into screen space at ``pose`` (float32, 0 where the world map has no data)."""
        s = pose.scale
        transform = np.float32([[s, 0, pose.x - 640 * s], [0, s, pose.y - 360 * s]])
        return cv2.warpAffine(self.image.astype(np.float32), transform, (1280, 720),
                              flags=cv2.WARP_INVERSE_MAP | cv2.INTER_LINEAR, borderValue=0)

    def residual(self, frame, pose: Pose):
        """Per pixel, how far the frame is from bare terrain: |luma - (gain x world + offset)|, the fit taken over
        FIT_ZONE (twice, the second time without the pixels the first fit missed by more than 20), blurred; 255
        where the world map has no data. Icons, labels, panels and fixed controls stand out (BLANK_MAX)."""
        gray = (cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame).astype(np.float32)
        world = self.rendered(pose)
        x0, y0, x1, y1 = FIT_ZONE
        g, w = gray[y0:y1, x0:x1].ravel(), world[y0:y1, x0:x1].ravel()
        keep = w > 0
        gain, offset = 1.0, 0.0
        for _ in range(2):
            if keep.sum() < 100:
                break
            a = np.stack([w[keep], np.ones(int(keep.sum()), np.float32)], 1)
            gain, offset = np.linalg.lstsq(a, g[keep], rcond=None)[0]
            keep = (w > 0) & (np.abs(g - (gain * w + offset)) <= 20)
        residual = cv2.blur(np.abs(gray - (gain * world + offset)), (RESIDUAL_BLUR, RESIDUAL_BLUR))
        residual[cv2.erode((world > 0).astype(np.uint8), np.ones((7, 7), np.uint8)) == 0] = 255
        return residual


def blank_at(residual, point) -> bool:
    """Bare terrain within BLANK_RADIUS of ``point`` (and the point in DRAG_ZONE): no icon, label or control."""
    x, y = int(round(point[0])), int(round(point[1]))
    x0, y0, x1, y1 = DRAG_ZONE
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    r = BLANK_RADIUS
    return float(residual[y - r:y + r + 1, x - r:x + r + 1].max()) <= BLANK_MAX


def in_zone(point, zone) -> bool:
    return zone[0] <= point[0] <= zone[2] and zone[1] <= point[1] <= zone[3]


def plan_drag(residual, current, desired=TARGET_POINT):
    """``(begin, end)`` of a drag that moves the map point at ``current`` (screen) toward ``desired``: at most
    DRAG_MAX per axis, both ends in DRAG_ZONE and on blank terrain (blank_at), the begin nearest to the zone's middle;
    shortened step by step when no such pair exists; None when none of at least DRAG_MIN does."""
    dx, dy = desired[0] - current[0], desired[1] - current[1]
    factor = min(1.0, DRAG_MAX[0] / max(abs(dx), 1e-6), DRAG_MAX[1] / max(abs(dy), 1e-6))
    dx, dy = dx * factor, dy * factor
    x0, y0, x1, y1 = DRAG_ZONE
    middle = ((x0 + x1) / 2, (y0 + y1) / 2)
    for shrink in (1.0, .75, .5, .35):
        vx, vy = round(dx * shrink), round(dy * shrink)
        if math.hypot(vx, vy) < DRAG_MIN:
            break
        best = None
        for bx in range(x0, x1 + 1, DRAG_STEP):
            for by in range(y0, y1 + 1, DRAG_STEP):
                end = (bx + vx, by + vy)
                if not in_zone(end, DRAG_ZONE) or not blank_at(residual, (bx, by)) or not blank_at(residual, end):
                    continue
                cost = math.hypot(bx + vx / 2 - middle[0], by + vy / 2 - middle[1])
                if best is None or cost < best[0]:
                    best = (cost, (bx, by), end)
        if best is not None:
            return best[1], best[2]
    return None


def pillar_expected(pose: Pose, x, y):
    """Where the pillar icon of the checkpoint at world (x, y) should be on a frame at ``pose``."""
    sx, sy = pose.to_screen(x, y)
    return sx, sy - PILLAR_ABOVE_U / pose.scale


def pillar_window(expected):
    left, up, w, h = PILLAR_WINDOW
    x, y = int(round(expected[0])) - left, int(round(expected[1])) - up
    x, y = max(0, min(1280 - w, x)), max(0, min(720 - h, y))
    return [x, y, w, h]


# ---- reading the screens ----

def zoom_state(frame):
    """"max" when the zoom bar's orange segment ends at its right end, "below" when it ends further left, None when
    no orange segment is seen (the bar covered, e.g. by a panel)."""
    x, y, w, h = ZOOM_BAND
    hsv = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
    orange = (hsv[..., 0] >= 10) & (hsv[..., 0] <= 25) & (hsv[..., 1] >= 120) & (hsv[..., 2] >= 150)
    columns = np.where(orange.mean(0) > .5)[0]
    if len(columns) < 30 or columns.max() - columns.min() > 130:
        return None
    return "max" if columns.max() + x >= ZOOM_MAX_RIGHT else "below"


def hit(recognize, node, override=None) -> bool:
    return actions._hit(recognize(node, override) if override else recognize(node))


def region_name(recognize) -> str:
    result = recognize(REGION_NAME)
    return squash(actions._text(result)) if actions._hit(result) else ""


def map_state(rt, screen, frame, recognize) -> str:
    """One frame: "invalid" (voided by a focus change), "hud", "menu" (地图切换 open), "panel" (a point panel: its ✕),
    "map" (the map page with its zoom bar and nothing over it), "transition" (nothing at all recognised) or "other"
    (anything else, the map page with something unknown over it included)."""
    if not rt._frame_still_valid(frame):
        return "invalid"
    if screen is not None and screen.kind == "hud":
        return "hud"
    if pages.is_exactly(screen, MAP_TITLE):
        if hit(recognize, REGION_MENU):
            return "menu"
        if hit(recognize, PANEL_CLOSE):
            return "panel"
        return "map" if zoom_state(frame) is not None else "other"
    if pages.nothing_recognised(screen, recognize):
        return "transition"
    return "other"


@dataclass(frozen=True)
class Card:
    """The 星海 card: "not_rung" (今日尚未进行召唤摇铃 read), "rung" (no such line, and the counters of the PC card,
    including 星光结晶's /30, or a line saying the bell was rung), or "unknown"; ``crystals``: 星光结晶 n of n/30."""
    kind: str
    crystals: int | None = None
    counters: tuple = ()


def card_state(recognize) -> Card:
    """The card's state, positively both ways: the not-rung line read, or a rung state read (never only "the
    not-rung line was not read"). Single characters are icon noise (OCR read 「您」 on a counter icon)."""
    result = recognize(CARD_TEXT)
    if result is None:
        return Card("unknown")
    not_rung = rung = bad = False
    counters, others = {}, 0
    for text, _box in _rows(result):
        row = squash(text)
        counter = COUNTER.fullmatch(row)
        if counter:
            have, total = int(counter.group(1)), int(counter.group(2))
            if total in TOTALS and have <= total and total not in counters:
                counters[total] = have
            else:
                bad = True
        elif NOT_RUNG.search(row):
            not_rung = True
        elif RUNG.search(row):
            rung = True
        elif _cjk(row) >= 2 or len(row) >= 3:
            others += 1  # e.g. 快前往流转之枢吧 under the not-rung line
    crystals = counters.get(CRYSTAL_TOTAL)
    kept = tuple(sorted(counters.items()))
    if not_rung and not rung:
        return Card("not_rung", crystals, kept)
    if not not_rung and not bad and not others and (rung or (crystals is not None and len(counters) >= 2)):
        return Card("rung", crystals, kept)
    return Card("unknown", crystals, kept)


def word_near(recognize, box) -> str | None:
    """A MAP_WORDS label (full OCR box, whole frame) within pages.FORBIDDEN_MARGIN of ``box``; a check that could
    not run refuses too."""
    result = recognize(MAP_WORDS)
    if result is None:
        return "检查不了要点的地方附近有没有危险按钮"
    for text, found in _rows(result):
        if pages._within_margin(found, [int(v) for v in box]):
            return text or "危险按钮"
    return None


def guard(recognize, box, frame):
    """Raise GuardRefusal when a map word lies next to ``box`` on ``frame`` (the shared forbidden labels are checked
    by the pages helpers)."""
    word = word_near(recognize, box)
    if word:
        raise pages.GuardRefusal(pages.GUARD_TAP, frame, word)


@dataclass(frozen=True)
class Panel:
    title: str
    buttons: tuple  # the button texts read, sorted
    words: tuple  # map words read inside the panel
    teleport: list | None  # the 传送 box
    detail: list | None  # the 详情 box


def read_panel(recognize) -> Panel:
    title = point_name("".join(text for text, _box in _rows(recognize(PANEL_TITLE))))
    rows = [(squash(text), box) for text, box in _rows(recognize(PANEL_BUTTONS)) if _cjk(squash(text)) >= 1]
    x, y, w, h = PANEL_ZONE
    words = tuple(sorted(squash(text) for text, box in _rows(recognize(MAP_WORDS))
                         if len(box) == 4 and x <= _centre(box)[0] <= x + w and y <= _centre(box)[1] <= y + h))
    teleport = recognize(TELEPORT)
    detail = next((box for text, box in rows if text == "详情"), None)
    return Panel(title, tuple(sorted(text for text, _box in rows)), words,
                 actions._box(teleport) if actions._hit(teleport) else None, detail)


def panel_ok(panel: Panel, name: str) -> bool:
    """The panel of ``name`` with exactly the buttons 详情 and 传送, no map word on it, 详情 clear of 传送's margin."""
    return (panel.title == point_name(name) and panel.buttons == tuple(sorted(PANEL_BUTTON_NAMES))
            and not panel.words and panel.teleport is not None and panel.detail is not None
            and not pages._within_margin(panel.detail, panel.teleport))


# ---- the flow ----

class Teleport:
    """The map flow on the agent's Runtime ``rt``. ``world``: a WorldMap (built on first use)."""

    def __init__(self, rt, world=None):
        self.rt = rt
        self._world = world
        self.card_frame = None  # the frame the last card reading was taken on

    @property
    def world(self):
        if self._world is None:
            self._world = WorldMap()
        return self._world

    # -- looking --

    def look(self):
        """``(state, screen, frame, recognize)`` on a fresh frame; state None when the run stopped."""
        screen, frame, recognize = pages.screen_now(self.rt)
        if frame is None:
            return None, None, None, None
        return map_state(self.rt, screen, frame, recognize), screen, frame, recognize

    def wait(self, what, done, allowed, seconds):
        """Fresh frames until ``done(state, frame, recognize)`` holds: returns ``(state, screen, frame,
        recognize)`` of that frame. Frames whose state is in ``allowed`` (or voided by a focus change) are waited
        through up to ``seconds``; any other frame stops at once, and so does the time running out (with that
        frame). None when the run stopped."""
        start = time.monotonic()
        while not self.rt.stopped:
            state, screen, frame, recognize = self.look()
            if state is None:
                return None
            if state != "invalid":
                if done(state, frame, recognize):
                    return state, screen, frame, recognize
                if state not in allowed:
                    raise Stop(f"{what}时出现了不该出现的画面", [frame])
                if time.monotonic() - start >= seconds:
                    raise Stop(f"{what}超时", [frame])
            self.rt.pause(STEP)
        return None

    def map_frame(self, what):
        """A fresh frame of the map page with nothing over it, else Stop with it."""
        found = self.wait(what, lambda state, f, r: state == "map", (), 0)
        return found

    def located(self, frame, what):
        pose = self.world.locate(frame)
        if not pose.trusted:
            raise Stop(f"{what}时认不出大地图显示的是哪里", [frame])
        return pose

    # -- open, region, zoom --

    def open_map(self) -> bool:
        """From the HUD: one strict tap on the minimap (the HUD on both tap frames), then the map page."""
        from host.android import touch_point
        point = touch_point(actions.MINIMAP)
        if not pages.tap_on_layer(self.rt, "大世界", lambda screen, recognize: screen is not None
                                  and screen.kind == "hud", point):
            return False
        return self.wait("打开大地图", lambda state, f, r: state in ("map", "menu"), ("hud", "transition"),
                         OPEN_WAIT) is not None

    def region(self, name: str = STARSEA) -> bool:
        """The map showing ``name``: read on the card; otherwise 地图切换 opened by the card's title, the row tapped
        (an expanded province collapsed first while the row is not in the list), and the card read again."""
        found = self.wait("读取地图区域", lambda state, f, r: state in ("map", "menu"), (), 0)
        if found is None:
            return False
        state, _screen, frame, recognize = found
        if state == "map" and region_name(recognize) == name:
            return True
        if state == "map":
            def card_title(frame_, recognize_):  # the map with nothing over it, showing another region
                result = recognize_(REGION_NAME)
                if actions._hit(result):
                    guard(recognize_, actions._box(result), frame_)
                return map_state(self.rt, _PAGE, frame_, recognize_) == "map" \
                    and region_name(recognize_) not in ("", name)
            if not pages.tap_on_page(self.rt, MAP_TITLE, REGION_NAME, accept=card_title, retry=False, exact=True):
                return False
            if self.wait("打开地图切换", lambda state_, f, r: state_ == "menu", ("map", "transition"),
                         MENU_WAIT) is None:
                return False
        for _ in range(3):
            found = self.wait("读取地图切换列表", lambda state_, f, r: state_ == "menu", (), 0)
            if found is None:
                return False
            _state, _screen, frame, recognize = found
            row = recognize(MENU_STARSEA)
            if actions._hit(row) and name == STARSEA and province_header(actions._box(row)):
                if not pages.tap_on_page(self.rt, MAP_TITLE, MENU_STARSEA, accept=self._menu_accept(MENU_STARSEA),
                                         retry=False, exact=True):
                    return False
                done = self.wait(f"切换到{name}", lambda state_, f, r: state_ == "map" and region_name(r) == name,
                                 ("menu", "transition", "map"), SWITCH_WAIT)
                return done is not None
            if not hit(recognize, MENU_COLLAPSE):
                raise Stop(f"地图切换列表里找不到「{name}」，也没有可以折叠的省份，没有滑动列表", [frame])
            before = actions._box(recognize(MENU_COLLAPSE))
            if not pages.tap_on_page(self.rt, MAP_TITLE, MENU_COLLAPSE, accept=self._menu_accept(MENU_COLLAPSE),
                                     retry=False, exact=True):
                return False

            def changed(state_, frame_, recognize_):
                if state_ != "menu":
                    return False
                if actions._hit(recognize_(MENU_STARSEA)):
                    return True
                again = recognize_(MENU_COLLAPSE)
                return not actions._hit(again) or not actions._near(actions._box(again), before)
            if self.wait("折叠地图切换列表里的省份", changed, ("menu", "transition"), MENU_WAIT) is None:
                return False
        raise Stop(f"折叠省份之后地图切换列表里仍然找不到「{name}」", [])

    def _menu_accept(self, node):
        """accept for a tap in 地图切换: the list open (its title read), the target there, no map word next to it."""
        def accept(frame, recognize):
            result = recognize(node)
            if not actions._hit(result) or not hit(recognize, REGION_MENU):
                return False
            guard(recognize, actions._box(result), frame)
            if node == MENU_STARSEA:
                return province_header(actions._box(result))
            return not hit(recognize, MENU_STARSEA)  # collapse only while the row is not in the list
        return accept

    def zoom(self) -> bool:
        """The maximum zoom: "+" tapped (at most twice) while the zoom bar shows less, each tap seen to take effect."""
        for taps in range(3):
            found = self.map_frame("放大地图")
            if found is None:
                return False
            frame = found[2]
            if zoom_state(frame) == "max":
                return True
            if taps == 2:
                break

            def below(frame_, recognize_):
                result = recognize_(ZOOM_IN)
                if actions._hit(result):
                    guard(recognize_, actions._box(result), frame_)
                return map_state(self.rt, _PAGE, frame_, recognize_) == "map" and zoom_state(frame_) == "below"
            if not pages.tap_on_page(self.rt, MAP_TITLE, ZOOM_IN, accept=below, retry=False, exact=True):
                return False
            right = zoom_right(frame)
            if self.wait("放大地图", lambda state_, f, r: state_ == "map" and zoom_right(f) != right,
                         ("map", "transition"), ZOOM_WAIT) is None:
                return False
        raise Stop("点了两次「+」地图仍然没有放到最大", [frame])

    # -- drag and select --

    def center(self, name, x, y):
        """Drag the map until the point at world (x, y) lies in TARGET_ZONE; returns the pose of the last frame."""
        for _ in range(MAX_DRAGS + 1):
            found = self.map_frame(f"把「{name}」拖进画面")
            if found is None:
                return None
            _state, _screen, frame, _recognize = found
            pose = self.located(frame, "拖动地图")
            here = pose.to_screen(x, y)
            if in_zone(here, TARGET_ZONE):
                return pose
            if _ == MAX_DRAGS:
                break
            plan = plan_drag(self.world.residual(frame, pose), here)
            if plan is None:
                raise Stop("地图上找不到两端都是空白地形的拖动位置，没有拖动", [frame])
            begin, end = plan
            if not pages.swipe_on_page(self.rt, MAP_TITLE, begin, end, duration=DRAG_MS,
                                       accept=self._drag_accept(pose, begin, end)):
                return None
            moved = self.wait("拖动地图", lambda state_, f, r: state_ == "map", ("map", "transition"), ZOOM_WAIT)
            if moved is None:
                return None
            after = self.located(moved[2], "拖动地图")
            shift = ((pose.x - after.x) / pose.scale, (pose.y - after.y) / pose.scale)
            if math.hypot(shift[0] - (end[0] - begin[0]), shift[1] - (end[1] - begin[1])) > DRAG_TOLERANCE:
                raise Stop("拖动地图后地图移动的距离和预期不符", [moved[2]])
        raise Stop(f"拖动 {MAX_DRAGS} 次后「{name}」仍然不在画面中间", [frame])

    def _drag_accept(self, pose, begin, end):
        def accept(frame, recognize):
            if map_state(self.rt, _PAGE, frame, recognize) != "map" or not self._same_view(frame, pose):
                return False
            residual = self.world.residual(frame, self.world.locate(frame))
            for point in (begin, end):
                if not blank_at(residual, point):
                    return False
                guard(recognize, [int(point[0]), int(point[1]), 1, 1], frame)
            return True
        return accept

    def _same_view(self, frame, pose) -> bool:
        again = self.world.locate(frame)
        return again.trusted and math.hypot(again.x - pose.x, again.y - pose.y) / pose.scale <= POSE_TOLERANCE

    def select(self, name, x, y, pose) -> bool:
        """Tap the pillar of the point at world (x, y): on both tap frames the map with nothing over it at ``pose``,
        the pillar found within PILLAR_TOLERANCE of where the point's icon should be, no map word next to it."""
        expected = pillar_expected(pose, x, y)
        window = {"roi": pillar_window(expected)}

        def accept(frame, recognize):
            if map_state(self.rt, _PAGE, frame, recognize) != "map" or not self._same_view(frame, pose):
                return False
            result = recognize(PILLAR, window)
            if not actions._hit(result):
                return False
            box = actions._box(result)
            guard(recognize, box, frame)
            return math.dist(_centre(box), expected) <= PILLAR_TOLERANCE
        found = self.map_frame(f"点「{name}」")
        if found is None:
            return False
        if not accept(found[2], found[3]):
            raise Stop(f"在「{name}」应该在的位置没有认出传送点的图标", [found[2]])
        if not pages.tap_on_page(self.rt, MAP_TITLE, PILLAR, override=window, accept=accept, retry=False, exact=True):
            return False
        return self.wait(f"打开「{name}」的详情", lambda state, f, r: state == "panel", ("map", "transition"),
                         PANEL_WAIT) is not None

    def confirm(self, name) -> bool:
        """On the point panel: exactly the panel of ``name`` (title, buttons 详情 and 传送, nothing else) on both tap
        frames -> one tap on 传送 (by its OCR box); any other panel -> closed by its ✕, then Stop."""
        found = self.wait(f"读取「{name}」的详情", lambda state, f, r: state == "panel", (), 0)
        if found is None:
            return False
        _state, _screen, frame, recognize = found
        panel = read_panel(recognize)
        if not panel_ok(panel, name):
            reason = (f"点开的不是「{name}」的详情" if panel.title != point_name(name) else
                      f"「{name}」的详情上有预期以外的按钮或文字")
            self.close_panel()
            raise Stop(f"{reason}，已经关闭详情，没有传送", [frame])

        def accept(frame_, recognize_):
            if map_state(self.rt, _PAGE, frame_, recognize_) != "panel":
                return False
            current = read_panel(recognize_)
            if current.teleport is not None:
                guard(recognize_, current.teleport, frame_)
            return panel_ok(current, name)
        if not pages.tap_on_page(self.rt, MAP_TITLE, TELEPORT, accept=accept, retry=False, exact=True):
            return False
        self.rt.log(f"已点「{name}」的传送。", detail=True)
        return True

    def close_panel(self) -> bool:
        def accept(frame, recognize):
            result = recognize(PANEL_CLOSE)
            if actions._hit(result):
                guard(recognize, actions._box(result), frame)
            return map_state(self.rt, _PAGE, frame, recognize) == "panel"
        return pages.tap_on_page(self.rt, MAP_TITLE, PANEL_CLOSE, accept=accept, retry=False, exact=True)

    # -- arrival --

    def arrive(self, name):
        """After 传送: the loading frames (nothing recognised) waited through up to ARRIVE_WAIT, the map up to
        MAP_LINGER; the HUD on two consecutive frames, then a clean HUD (move.classify: nothing over it) within
        CLEAN_HUD_WAIT without any input. Returns that Observation; Stop on anything else."""
        from host.android.move import RuntimeIo
        start, huds = time.monotonic(), 0
        while not self.rt.stopped:
            state, _screen, frame, _recognize = self.look()
            if state is None:
                return None
            elapsed = time.monotonic() - start
            if state == "hud":
                huds += 1
                if huds >= 2:
                    break
            elif state != "invalid":
                huds = 0
                if not (state == "transition" or (state in ("map", "panel") and elapsed <= MAP_LINGER)):
                    raise Stop(f"传送到「{name}」时出现了不该出现的画面", [frame])
            if elapsed >= ARRIVE_WAIT:
                raise Stop(f"传送到「{name}」后 {ARRIVE_WAIT} 秒内没有回到大世界", [frame])
            self.rt.pause(STEP)
        if self.rt.stopped:
            return None
        return clean_hud(self.rt, RuntimeIo(self.rt), f"传送到「{name}」后", CLEAN_HUD_WAIT)

    # -- the whole teleport --

    def transport(self, checkpoint):
        """Teleport to ``checkpoint`` (a checkpoints.json record of 星海: name, x, y) from the HUD or from the map page
        of 星海; returns ``(x, y)``, the walker's position hint, once a clean HUD shows. None when the run stopped."""
        name, x, y = checkpoint["name"], float(checkpoint["x"]), float(checkpoint["y"])
        state = self.look()[0]
        if state is None:
            return None
        if state == "hud":
            if not self.open_map():
                return None
        if not (self.region(STARSEA) and self.zoom()):
            return None
        pose = self.center(name, x, y)
        if pose is None or not self.select(name, x, y, pose) or not self.confirm(name):
            return None
        if self.arrive(name) is None:
            return None
        self.rt.log(f"已传送到「{name}」。")
        return x, y

    def card(self) -> Card | None:
        """The 星海 card read the same on two consecutive frames of the map (rung or not rung); a reading that flips
        between the two, or nothing positive within CARD_WAIT, stops with the frame."""
        start, last = time.monotonic(), None
        while not self.rt.stopped:
            found = self.map_frame("读取星海地图卡片")
            if found is None:
                return None
            _state, _screen, frame, recognize = found
            if region_name(recognize) != STARSEA:
                raise Stop("读取地图卡片时地图不是星海", [frame])
            card = card_state(recognize)
            self.card_frame = frame
            if card.kind != "unknown" and last is not None and last.kind != "unknown" and card.kind != last.kind:
                raise Stop("地图卡片上的摇铃状态前后读得不一样", [frame])
            if card.kind != "unknown" and card == last:
                return card
            last = card
            if time.monotonic() - start >= CARD_WAIT:
                raise Stop("读不出地图卡片上今天是否已经召唤摇铃（既不是未摇铃的提示，也不是认得的已摇铃状态）", [frame])
            self.rt.pause(STEP)
        return None

    def leave(self) -> bool:
        """The map page left by its back arrow (one tap, the map with nothing over it on both tap frames), and fresh
        frames showing the HUD."""
        def accept(frame, recognize):
            return map_state(self.rt, _PAGE, frame, recognize) == "map"
        if not pages.tap_on_page(self.rt, MAP_TITLE, actions.BACK_ARROW, accept=accept, retry=False, exact=True):
            return False
        state, frame, after = pages.left_page(self.rt, MAP_TITLE)
        if state is None:
            return False
        if state != "left" or after.kind != "hud":
            raise Stop("点了地图的返回箭头之后没有回到大世界", [frame])
        return True


_PAGE = actions.Screen("page", title=MAP_TITLE)  # tap_on_page has already checked the page and its title


def province_header(box) -> bool:
    """A province header's name (星海 [888,100,57,32]; 心愿原野 [886,97], 伊赞之土 [888,620]) rather than a region
    row's (纪念山地 [841,161], 花愿镇 [842,252]): the headers' names start ~45 px further right, after their emblem."""
    return 865 <= box[0] <= 915


def zoom_right(frame):
    x, y, w, h = ZOOM_BAND
    hsv = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
    orange = (hsv[..., 0] >= 10) & (hsv[..., 0] <= 25) & (hsv[..., 1] >= 120) & (hsv[..., 2] >= 150)
    columns = np.where(orange.mean(0) > .5)[0]
    return int(columns.max()) + x if len(columns) else None


def clean_hud(rt, io, what, seconds):
    """Wait, without input, for a frame that is the open-world HUD and nothing else (move.RuntimeIo.observe: "hud");
    returns that Observation. Anything else is waited through up to ``seconds``, then Stop with the last frame. None
    when the run stopped."""
    start, last = time.monotonic(), None
    while not rt.stopped:
        obs = io.observe()
        if obs is None:
            return None
        if obs.kind == "hud":
            return obs
        last = obs
        if time.monotonic() - start >= seconds:
            raise Stop(f"{what}大世界的画面一直不干净（{obs.reason or '有旁白或没拿到截图'}）",
                       [last.frame] if last is not None else [])
        rt.pause(STEP)
    return None
