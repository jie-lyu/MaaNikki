"""Big-map teleport on the Android client (phase E): from the open-world HUD to a teleport point of 星海.

HUD -> the minimap tapped (the big map opens on the page titled 地图) -> the region card read; another region: its title
tapped, 星海 chosen in 地图切换 (an expanded province collapsed first when 星海 is scrolled out of the list) -> "+"
tapped until the zoom bar shows the maximum -> the map frame located on the world map (navigation maps/starsea.png at
MAX_ZOOM_SCALE world px per screen px) -> drags that start and end on blank terrain until the point lies in
TARGET_ZONE -> the point's pillar icon tapped -> the point panel: its title exactly the point's name, its buttons
exactly 详情 and 传送 -> 传送 tapped once (found by OCR) -> loading -> the open-world HUD, clean, and the point's world
coordinates returned (the walker's position hint).

Rules (docs/android-support-plan.md, 阶段 E and decision 2): every touch is decided on a fresh focus-confirmed frame that
shows the whole expected state and confirmed on the next fresh frame (Teleport.send; on the HUD, hud_pair with the
walker's clean-HUD checks), sent from that confirming frame only while it is the latest capture (the gesture
freshness rule, ``_movement``) and younger than move.MAX_DECISION_AGE_S, never within pages.FORBIDDEN_MARGIN of a
forbidden label (MaaNikki_Android_Forbidden) or of a map word (MAP_WORDS: 前往 as in 立即前往, 追踪, 导航, 解锁, ...;
full OCR boxes on both frames). 立即前往 is never tapped: a panel with another title, or any text in its button band
but exactly 详情 and 传送, stops the task at once with that frame, nothing tapped. The 传送 tap is never repeated; no
BACK key is sent; any screen that is not the expected one stops the task with that frame saved (Stop).

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
import unicodedata

import cv2
import numpy as np

from host.android import actions, pages

MAP_TITLE = "地图"
STARSEA = "星海"
STARSEA_MAP = "starsea"  # the navigation map id of 星海 (move.NAME_ONLY_MAPS)
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
CARD_DROPDOWN = "MaaNikki_Android_MapCardDropdown"
NODES = (REGION_NAME, CARD_DROPDOWN, CARD_TEXT, REGION_MENU, MENU_STARSEA, MENU_COLLAPSE, ZOOM_IN, PILLAR, PANEL_TITLE,
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
NOT_RUNG_MESSAGE = ("今日尚未进行召唤摇铃", "快前往流转之枢吧")  # survey 03-06: the card's two rows, top to bottom
SURVEY_REGIONS = ("纪念山地",)  # region cards seen on the device whose region checkpoints.json does not list
COUNTER = re.compile(r"^([0-9]{1,3})[/／]([0-9]{1,3})$")
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
UNKNOWN_WAIT = 20  # a frame with nothing recognised but text (not a known loading frame) may last this long
HUD_SETTLE_S = 5.0  # a touch on the HUD waits this long for a clean frame (a remark going, the distance read)
MAX_COLLAPSES = 3
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
    right = zoom_right(frame)
    if right is None:
        return None
    return "max" if right >= ZOOM_MAX_RIGHT else "below"


def zoom_right(frame):
    """The right end of the zoom bar's orange segment, or None when there is no such segment."""
    x, y, w, h = ZOOM_BAND
    hsv = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
    orange = (hsv[..., 0] >= 10) & (hsv[..., 0] <= 25) & (hsv[..., 1] >= 120) & (hsv[..., 2] >= 150)
    columns = np.where(orange.mean(0) > .5)[0]
    if len(columns) < 30 or columns.max() - columns.min() > 130:
        return None
    return int(columns.max()) + x


def hit(recognize, node, override=None) -> bool:
    return actions._hit(recognize(node, override) if override else recognize(node))


def known_regions() -> frozenset:
    """Region names a region card may show: every region of resource/navigation/checkpoints.json and the regions seen
    on the device (SURVEY_REGIONS)."""
    import json
    from app_paths import RESOURCE_ROOT
    data = json.loads((RESOURCE_ROOT / "resource" / "navigation" / "checkpoints.json").read_text(encoding="utf-8"))
    return frozenset({record["region"] for points in data["maps"].values() for record in points} | set(SURVEY_REGIONS))


def region_card(recognize) -> str | None:
    """The region card's name, positively: the card's dropdown chevron in its place and its title reading exactly a
    known region name (known_regions); None otherwise (e.g. a button's text read where the title should be)."""
    if not hit(recognize, CARD_DROPDOWN):
        return None
    result = recognize(REGION_NAME)
    name = squash(actions._text(result)) if actions._hit(result) else ""
    return name if name in known_regions() else None


def region_name(recognize) -> str:
    return region_card(recognize) or ""


def map_state(rt, screen, frame, recognize) -> str:
    """One frame: "invalid" (voided by a focus change), "hud", "menu" (地图切换 open), "panel" (a point panel: its ✕),
    "map" (the page titled exactly 地图 with its zoom bar and nothing over it), "transition" (nothing at all
    recognised) or "other" (anything else, the map page with something unknown over it included)."""
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
    """The 星海 card: "not_rung" (exactly the observed daily message NOT_RUNG_MESSAGE), "rung" (exactly the PC card's
    three counters n/3, n/30, n/100, nothing else) or "unknown"; ``crystals``: 星光结晶's n of n/30."""
    kind: str
    crystals: int | None = None
    counters: tuple = ()


MESSAGE_PUNCTUATION = "，。！、,.!"  # the only characters message_text drops besides whitespace (the observed variants)
COUNTER_MARKS = "/／"


def message_text(text) -> str | None:
    """A card row as compared with NOT_RUNG_MESSAGE: whitespace and MESSAGE_PUNCTUATION dropped, nothing else; None
    for a row holding a digit (any script) or a slash, which no message row has (a counter must never be normalised
    into the message)."""
    text = str(text or "")
    if any(ch.isdigit() or unicodedata.numeric(ch, None) is not None or ch in COUNTER_MARKS for ch in text):
        return None
    return "".join(ch for ch in text if not ch.isspace() and ch not in MESSAGE_PUNCTUATION)


def card_state(recognize) -> Card:
    """The card's state, positively both ways and from the card's whole message area: every row read there must
    belong to one known state. "not_rung": exactly the two rows of NOT_RUNG_MESSAGE, top to bottom, no digit or slash
    in either; "rung": exactly three rows, the counters of 星之魄 (/3), 星光结晶 (/30) and 星光微尘 (/100) in ASCII digits
    once each (the PC card; not seen on the phone yet); any other text, a contradictory row, a misread counter or a
    row missing: "unknown"."""
    result = recognize(CARD_TEXT)
    if result is None:
        return Card("unknown")
    texts = [squash(text) for text, _box in sorted(_rows(result), key=lambda row: (row[1][1:2], row[1][:1]))
             if squash(text)]
    if [message_text(text) for text in texts] == list(NOT_RUNG_MESSAGE):
        return Card("not_rung")
    counters = [COUNTER.fullmatch(text) for text in texts]
    if len(texts) == len(TOTALS) and all(counters):
        values = {int(match.group(2)): int(match.group(1)) for match in counters}
        if sorted(values) == sorted(TOTALS) and all(have <= total for total, have in values.items()):
            return Card("rung", values[CRYSTAL_TOTAL], tuple(sorted(values.items())))
    return Card("unknown")


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
    """Raise GuardRefusal when a shared forbidden label (MaaNikki_Android_Forbidden) or a map word lies within
    pages.FORBIDDEN_MARGIN of ``box`` on ``frame`` (full OCR boxes, whole frame)."""
    box = [int(v) for v in box]
    word = pages._forbidden_near(recognize, box) or word_near(recognize, box)
    if word:
        raise pages.GuardRefusal(pages.GUARD_TAP, frame, word)


@dataclass(frozen=True)
class Panel:
    title: str
    buttons: tuple  # every text read in the button band, sorted
    words: tuple  # map words read inside the panel
    teleport: list | None  # the 传送 box of the button band's full pass
    detail: list | None  # the 详情 box of the same pass
    agree: bool = False  # the narrower 传送 pass (TELEPORT) read the same label over an overlapping box


def _overlap(a, b) -> bool:
    """Two boxes [x, y, w, h] share at least one pixel (half-open)."""
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def read_panel(recognize) -> Panel:
    title = point_name("".join(text for text, _box in _rows(recognize(PANEL_TITLE))))
    rows = [(squash(text), box) for text, box in _rows(recognize(PANEL_BUTTONS)) if squash(text)]
    x, y, w, h = PANEL_ZONE
    words = tuple(sorted(squash(text) for text, box in _rows(recognize(MAP_WORDS))
                         if len(box) == 4 and x <= _centre(box)[0] <= x + w and y <= _centre(box)[1] <= y + h))
    teleports = [box for text, box in rows if text == "传送"]
    teleport = teleports[0] if len(teleports) == 1 and len(teleports[0]) == 4 else None
    detail = next((box for text, box in rows if text == "详情"), None)
    narrow = recognize(TELEPORT)
    agree = bool(teleport is not None and actions._hit(narrow) and squash(actions._text(narrow)) == "传送"
                 and _overlap(actions._box(narrow), teleport))
    return Panel(title, tuple(sorted(text for text, _box in rows)), words, teleport, detail, agree)


def panel_ok(panel: Panel, name: str) -> bool:
    """The panel of ``name`` with exactly the buttons 详情 and 传送 (every text read in the button band accounted for),
    the narrower 传送 pass agreeing with the band's, no map word on it, 详情 clear of 传送's margin."""
    return (panel.title == point_name(name) and panel.buttons == tuple(sorted(PANEL_BUTTON_NAMES))
            and not panel.words and panel.teleport is not None and panel.detail is not None and panel.agree
            and not pages._within_margin(panel.detail, panel.teleport))


def province_header(box) -> bool:
    """A province header's name (星海 [888,100,57,32]; 心愿原野 [886,97], 伊赞之土 [888,620]) rather than a region
    row's (纪念山地 [841,161], 花愿镇 [842,252]): the headers' names start ~45 px further right, after their emblem."""
    return 865 <= box[0] <= 915


def minimap_disc():
    """The minimap as a control for prompt_clear: centred on the point its tap uses (actions.MINIMAP's touch point),
    with the minimap's radius on the frame (host.android.minimap.RADIUS)."""
    from host.android import touch_point
    from host.android.minimap import RADIUS
    return tuple(touch_point(list(actions.MINIMAP))), RADIUS


def prompt_clear(obs, centre, radius) -> bool:
    """No interaction prompt of a "hud_prompt" frame within move.CONTROL_CLEAR_PX of a target drawn as a disc of
    ``radius`` around ``centre`` (move.clear_of: the walker's rule for its own controls)."""
    from host.android.move import clear_of
    return all(clear_of(extent, centre, radius) for extent in obs.prompts)


def observation(look, verdict):
    """A move.Observation of a Look classified by move.classify (for move.first_frame_rule on frames the map flow
    captured)."""
    from host.android.move import Observation
    return Observation(look.frame, verdict.kind, look.at, verdict.reason, regions=verdict.regions,
                       blocks=verdict.blocks, name_only=verdict.name_only, prompts=verdict.prompts)


def stopping(rt) -> bool:
    """The run stopped or the Android stop published (checked before anything reaches the user)."""
    from host import android_user_stop
    return bool(rt.stopped or android_user_stop())


def say(rt, text, *, detail=False) -> bool:
    """Log ``text`` unless the run is stopping (a published stop included); False when it was not logged."""
    if stopping(rt):
        return False
    rt.log(text, detail=detail)
    return True


@dataclass
class Look:
    """One fresh frame: ``at`` the clock before its capture was requested (its age counts from there)."""
    at: float
    screen: object
    frame: object
    recognize: object
    state: str


def _same_fields(first, second) -> bool:
    for name in ("target", "begin", "end"):
        if (name in first) != (name in second):
            return False
        if name in first and not actions._near(first[name], second[name]):
            return False
    return first.get("duration") == second.get("duration")


# ---- the flow ----

class Teleport:
    """The map flow on the agent's Runtime ``rt``. ``world``: a WorldMap, ``io``: a move.RuntimeIo, ``reader``: a
    minimap.MinimapReader (each built on first use)."""

    def __init__(self, rt, world=None, *, io=None, reader=None):
        self.rt = rt
        self._world, self._io, self._reader = world, io, reader
        self.card_frame = None  # the frame the last card reading was taken on

    @property
    def world(self):
        if self._world is None:
            self._world = WorldMap()
        return self._world

    @property
    def io(self):
        if self._io is None:
            from host.android.move import RuntimeIo
            self._io = RuntimeIo(self.rt)
        return self._io

    @property
    def reader(self):
        if self._reader is None:
            from host.android.minimap import MinimapReader
            self._reader = MinimapReader("starsea")
        return self._reader

    # -- looking --

    def capture(self) -> Look | None:
        at = time.monotonic()
        screen, frame, recognize = pages.screen_now(self.rt)
        if frame is None:
            return None
        return Look(at, screen, frame, recognize, map_state(self.rt, screen, frame, recognize))

    def look(self):
        """``(state, screen, frame, recognize)`` on a fresh frame; state None when the run stopped."""
        found = self.capture()
        if found is None:
            return None, None, None, None
        return found.state, found.screen, found.frame, found.recognize

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
        return self.wait(what, lambda state, f, r: state == "map", (), 0)

    def located(self, frame, what):
        pose = self.world.locate(frame)
        if not pose.trusted:
            raise Stop(f"{what}时认不出大地图显示的是哪里", [frame])
        return pose

    # -- touches --

    def send(self, what, decide, action="Click", delay=.5) -> bool:
        """One tap (or swipe) on the map: ``decide(look)`` returns its fields (target, or begin/end/duration) when a
        frame shows the whole expected state and None when it does not (a guard raises GuardRefusal itself). Decided
        on a fresh frame, confirmed on the next (same fields within actions.CONFIRM_TOLERANCE), sent with that second
        frame with the gesture freshness rule (``_movement``: refused when captured before the last input or
        superseded by a later capture) and its expiry (``_not_after``: its capture request + move.MAX_DECISION_AGE_S,
        checked by Runtime._dispatch_touch right before submission). A frame that is not the state stops with it (no
        second attempt); only a focus change or an expired confirmation decides again. False when the run stopped."""
        from host.android.gesture import DecisionExpired
        from host.android.move import MAX_DECISION_AGE_S
        from task_control import ForegroundChanged
        last = None
        for _attempt in range(self.rt.TOUCH_KEY_ATTEMPTS):
            first = self.capture()
            if first is None:
                return False
            if first.state == "invalid":
                continue
            fields = decide(first)
            if fields is None:
                raise Stop(f"要{what}时画面不是预期的样子，没有点", [first.frame])
            second = self.capture()
            if second is None:
                return False
            if second.state == "invalid" or not self.rt._frame_still_valid(first.frame):
                continue
            again = decide(second)
            if again is None or not _same_fields(fields, again):
                raise Stop(f"确认{what}时画面变了，没有点", [second.frame])
            last = second
            not_after = second.at + MAX_DECISION_AGE_S
            if time.monotonic() > not_after:
                continue  # already too old to stand for the screen: decided again on new frames
            if not (self.rt._frame_still_valid(first.frame) and self.rt._frame_still_valid(second.frame)):
                continue
            try:
                sent = self.rt.action(action, **again, source_frame=second.frame, _movement=True,
                                      _not_after=not_after)
            except (ForegroundChanged, DecisionExpired):
                continue
            return bool(sent) and bool(self.rt.pause(delay))
        raise Stop(f"连续 {self.rt.TOUCH_KEY_ATTEMPTS} 次没能在新的截图上及时确认{what}",
                   [last.frame] if last is not None else [])

    def clean_hud(self, what, map_id, disc):
        """``(obs, shape)``: a frame to decide a touch on the open world by the walker's first-frame rule
        (move.first_frame_rule on ``map_id``: a name-only tracker only on 星海), with no interaction prompt near the
        target (``disc``: its centre and radius; prompt_clear). Frames the rule waits through are waited through
        without input up to move.STARTUP_WAIT_S; a frame it refuses, or a prompt near the target, stops with that
        frame. None when the run stopped."""
        from host.android.move import STARTUP_WAIT_S, first_frame_rule
        start = time.monotonic()
        while not self.rt.stopped:
            obs = self.io.observe()
            if obs is None:
                return None
            verdict, reason, shape = first_frame_rule(obs, self.reader, map_id)
            if verdict == "ok" and not prompt_clear(obs, *disc):
                raise Stop(f"要{what}时要点的地方旁边有交互提示，没有点", [obs.frame])
            if verdict == "ok":
                return obs, shape
            if verdict == "stop":
                raise Stop(f"要{what}时画面不是干净的大世界（{reason}）", [obs.frame])
            if time.monotonic() - start >= STARTUP_WAIT_S:
                raise Stop(f"要{what}时一直没有等到干净的大世界画面，没有点", [obs.frame])
            if not self.rt.pause(STEP):
                return None
        return None

    def hud_pair(self, what, locate, map_id, disc):
        """A touch on the open world decided on a frame that passes the walker's first-frame rule (clean_hud) and
        confirmed on the next by its later-frame rule against the first (move.later_frame_rule): a frame a step could
        be decided on (MOVE_KINDS) with no prompt near the target confirms; ambient narration (or a blank capture)
        starts the pair again once it clears, within move.NARRATION_MAX_S; anything else stops with that frame (a
        misread included: a touch is never decided on its recapture). ``locate(obs)`` returns the target box (raising
        Stop or GuardRefusal itself). Returns ``(first, second, target)``, the target as found on the second. None
        when the run stopped."""
        from host.android.move import MOVE_KINDS, NARRATION_MAX_S, later_frame_rule
        start = time.monotonic()
        while not self.rt.stopped:
            found = self.clean_hud(what, map_id, disc)
            if found is None:
                return None
            first, shape = found
            target = locate(first)
            second = self.io.observe()
            if second is None:
                return None
            verdict, reason = later_frame_rule(second, self.reader, shape)
            if verdict == "stop":
                raise Stop(f"确认{what}时画面变了（{reason}）", [second.frame])
            if second.kind in ("narration", "blank"):
                if time.monotonic() - start >= NARRATION_MAX_S:
                    raise Stop(f"确认{what}时旁白一直没有消失，没有点", [second.frame])
                if not self.rt.pause(STEP):
                    return None
                continue
            if second.kind not in MOVE_KINDS:
                raise Stop(f"确认{what}时画面不是干净的大世界（{second.reason or '认不出的画面'}），没有点", [second.frame])
            if not prompt_clear(second, *disc):
                raise Stop(f"确认{what}时要点的地方旁边有交互提示，没有点", [second.frame])
            again = locate(second)
            if not actions._near(again, target):
                raise Stop(f"确认{what}时目标的位置变了，没有点", [second.frame])
            return first, second, again
        return None

    # -- open, region, zoom --

    def open_map(self, map_id=None):
        """From the open world (both tap frames by the walker's rules on ``map_id``, None when the map is not known
        yet: then the tracker must show its distance; the minimap chevron at its default pivot so the minimap is
        where its tap point is; no interaction prompt near the minimap): one tap on the minimap, then the map.
        Returns the first Look of the map page (the map or 地图切换), carried into the next step; None when the run
        stopped."""
        from host.android import touch_point
        from host.android.gesture import DecisionExpired
        from host.android.move import MAX_DECISION_AGE_S
        from task_control import ForegroundChanged

        def minimap(obs):
            box = list(actions.MINIMAP)
            guard(self.rt._recognizer_on(obs.frame), box, obs.frame)
            return box
        for _attempt in range(self.rt.TOUCH_KEY_ATTEMPTS):
            pair = self.hud_pair("点小地图打开大地图", minimap, map_id, minimap_disc())
            if pair is None:
                return None
            first, second, box = pair
            not_after = second.at + MAX_DECISION_AGE_S
            if time.monotonic() > not_after:
                continue
            if not (self.rt._frame_still_valid(first.frame) and self.rt._frame_still_valid(second.frame)):
                continue
            try:
                sent = self.rt.action("Click", target=touch_point(box), source_frame=second.frame, _movement=True,
                                      _not_after=not_after)
            except (ForegroundChanged, DecisionExpired):
                continue
            if not (sent and self.rt.pause(.5)):
                return None
            found = self.wait("打开大地图", lambda state, f, r: state in ("map", "menu"), ("hud", "transition"),
                              OPEN_WAIT)
            return None if found is None else Look(time.monotonic(), found[1], found[2], found[3], found[0])
        raise Stop(f"连续 {self.rt.TOUCH_KEY_ATTEMPTS} 次没能在新的截图上及时确认要点的小地图", [])


    def _menu_target(self, node):
        """decide for a tap in 地图切换: the list open (its title read) on the page titled exactly 地图, the target
        there (星海 as a province header; the collapse chevron only while 星海 is not in the list), guarded."""
        def decide(look):
            if look.state != "menu" or not pages.is_exactly(look.screen, MAP_TITLE):
                return None
            result = look.recognize(node)
            if not actions._hit(result):
                return None
            box = actions._box(result)
            if node == MENU_STARSEA and not province_header(box):
                return None
            if node == MENU_COLLAPSE and hit(look.recognize, MENU_STARSEA):
                return None
            guard(look.recognize, box, look.frame)
            return {"target": box}
        return decide

    def region(self, name: str = STARSEA, entry=None) -> bool:
        """The map showing ``name``: read on the card (region_card); otherwise 地图切换 opened by the card's title (a
        positively read region card of another region), the row tapped (expanded provinces collapsed first while
        the row is not in the list, the list read once more after the last collapse), and the card read again.
        ``entry``: the frame the caller already has (open_map's first map frame), judged first: anything but the map
        or 地图切换 stops with it."""
        if entry is None:
            found = self.wait("读取地图区域", lambda state, f, r: state in ("map", "menu"), (), 0)
            if found is None:
                return False
            entry = Look(time.monotonic(), found[1], found[2], found[3], found[0])
        if entry.state not in ("map", "menu"):
            raise Stop("读取地图区域时出现了不该出现的画面", [entry.frame])
        state, frame, recognize = entry.state, entry.frame, entry.recognize
        if state == "map" and region_card(recognize) == name:
            return True
        if state == "map":
            def card_title(look):
                if look.state != "map" or not pages.is_exactly(look.screen, MAP_TITLE):
                    return None
                current = region_card(look.recognize)
                if current is None or current == name:
                    return None
                box = actions._box(look.recognize(REGION_NAME))
                guard(look.recognize, box, look.frame)
                return {"target": box}
            if not self.send("点地图卡片的区域名", card_title):
                return False
            if self.wait("打开地图切换", lambda state_, f, r: state_ == "menu", ("map", "transition"),
                         MENU_WAIT) is None:
                return False
        for collapses in range(MAX_COLLAPSES + 1):
            found = self.wait("读取地图切换列表", lambda state_, f, r: state_ == "menu", (), 0)
            if found is None:
                return False
            _state, _screen, frame, recognize = found
            row = recognize(MENU_STARSEA)
            if actions._hit(row) and name == STARSEA and province_header(actions._box(row)):
                if not self.send(f"点地图切换里的{name}", self._menu_target(MENU_STARSEA)):
                    return False
                done = self.wait(f"切换到{name}", lambda s, f, r: s == "map" and region_card(r) == name,
                                 ("menu", "transition", "map"), SWITCH_WAIT)
                return done is not None
            if collapses == MAX_COLLAPSES:
                break
            if not hit(recognize, MENU_COLLAPSE):
                raise Stop(f"地图切换列表里找不到「{name}」，也没有可以折叠的省份，没有滑动列表", [frame])
            before = actions._box(recognize(MENU_COLLAPSE))
            if not self.send("折叠地图切换列表里的省份", self._menu_target(MENU_COLLAPSE)):
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
        raise Stop(f"折叠省份之后地图切换列表里仍然找不到「{name}」", [frame])

    def zoom(self) -> bool:
        """The maximum zoom: "+" tapped (at most twice) while the zoom bar shows less, each tap seen to take effect."""
        def below(look):
            if look.state != "map" or not pages.is_exactly(look.screen, MAP_TITLE) or zoom_state(look.frame) != "below":
                return None
            result = look.recognize(ZOOM_IN)
            if not actions._hit(result):
                return None
            guard(look.recognize, actions._box(result), look.frame)
            return {"target": actions._box(result)}
        frame = None
        for taps in range(3):
            found = self.map_frame("放大地图")
            if found is None:
                return False
            frame = found[2]
            if zoom_state(frame) == "max":
                return True
            if taps == 2:
                break
            right = zoom_right(frame)
            if not self.send("点地图的放大按钮", below):
                return False
            if self.wait("放大地图", lambda state_, f, r: state_ == "map" and zoom_right(f) != right,
                         ("map", "transition"), ZOOM_WAIT) is None:
                return False
        raise Stop("点了两次「+」地图仍然没有放到最大", [frame])

    # -- drag and select --

    def _same_view(self, frame, pose) -> bool:
        again = self.world.locate(frame)
        return again.trusted and math.hypot(again.x - pose.x, again.y - pose.y) / pose.scale <= POSE_TOLERANCE

    def center(self, name, x, y):
        """Drag the map until the point at world (x, y) lies in TARGET_ZONE; returns the pose of the last frame."""
        frame = None
        for drags in range(MAX_DRAGS + 1):
            found = self.map_frame(f"把「{name}」拖进画面")
            if found is None:
                return None
            frame = found[2]
            pose = self.located(frame, "拖动地图")
            here = pose.to_screen(x, y)
            if in_zone(here, TARGET_ZONE):
                return pose
            if drags == MAX_DRAGS:
                break
            plan = plan_drag(self.world.residual(frame, pose), here)
            if plan is None:
                raise Stop("地图上找不到两端都是空白地形的拖动位置，没有拖动", [frame])
            begin, end = [list(point) for point in plan]

            def drag(look, pose=pose, begin=begin, end=end):
                if look.state != "map" or not pages.is_exactly(look.screen, MAP_TITLE) \
                        or not self._same_view(look.frame, pose):
                    return None
                residual = self.world.residual(look.frame, self.world.locate(look.frame))
                if not all(blank_at(residual, point) for point in (begin, end)):
                    return None
                for point in (begin, end):
                    guard(look.recognize, [*point, 1, 1], look.frame)
                return {"begin": begin, "end": end, "duration": DRAG_MS}
            if not self.send("拖动地图", drag, action="Swipe"):
                return None
            moved = self.wait("拖动地图", lambda state_, f, r: state_ == "map", ("map", "transition"), ZOOM_WAIT)
            if moved is None:
                return None
            after = self.located(moved[2], "拖动地图")
            shift = ((pose.x - after.x) / pose.scale, (pose.y - after.y) / pose.scale)
            if math.hypot(shift[0] - (end[0] - begin[0]), shift[1] - (end[1] - begin[1])) > DRAG_TOLERANCE:
                raise Stop("拖动地图后地图移动的距离和预期不符", [moved[2]])
        raise Stop(f"拖动 {MAX_DRAGS} 次后「{name}」仍然不在画面中间", [frame])

    def select(self, name, x, y, pose):
        """Tap the pillar of the point at world (x, y): on both tap frames the map with nothing over it at ``pose``,
        the pillar found within PILLAR_TOLERANCE of where the point's icon should be, guarded. Returns the first frame
        that shows a panel (validated by confirm, never replaced by a later capture); None when the run stopped."""
        expected = pillar_expected(pose, x, y)
        window = {"roi": pillar_window(expected)}

        def pillar(look):
            if look.state != "map" or not pages.is_exactly(look.screen, MAP_TITLE) \
                    or not self._same_view(look.frame, pose):
                return None
            result = look.recognize(PILLAR, window)
            if not actions._hit(result) or math.dist(_centre(actions._box(result)), expected) > PILLAR_TOLERANCE:
                return None
            guard(look.recognize, actions._box(result), look.frame)
            return {"target": actions._box(result)}
        if not self.send(f"点「{name}」的传送点图标", pillar):
            return None
        found = self.wait(f"打开「{name}」的详情", lambda state, f, r: state == "panel", ("map", "transition"),
                          PANEL_WAIT)
        return None if found is None else Look(time.monotonic(), found[1], found[2], found[3], found[0])

    def confirm(self, name, panel_look) -> bool:
        """On the point panel: ``panel_look`` (the first frame that showed a panel) must already be exactly the panel
        of ``name`` (title, the buttons 详情 and 传送 and nothing else read in the button band, the two 传送 passes
        agreeing, no map word), else it is saved and the task stops, nothing tapped (not even its ✕). Then one tap on
        传送 (the band's OCR box), decided and confirmed on fresh frames held to the same check."""
        if not pages.is_exactly(panel_look.screen, MAP_TITLE):
            raise Stop(f"打开「{name}」的详情时出现了不该出现的画面", [panel_look.frame])
        reject(panel_look.recognize, panel_look.frame, name)

        def teleport(look):
            if look.state != "panel" or not pages.is_exactly(look.screen, MAP_TITLE):
                return None
            panel = reject(look.recognize, look.frame, name)
            guard(look.recognize, panel.teleport, look.frame)
            return {"target": panel.teleport}
        if not self.send(f"点「{name}」的传送", teleport):
            return False
        say(self.rt, f"已点「{name}」的传送。", detail=True)
        return True

    # -- arrival --

    def hud_verdict(self, look, what):
        """A HUD Look on 星海 judged by the walker: ``"ok"`` when it passes the walker's first-frame rule (classify with
        the frame, then move.first_frame_rule on 星海), ``"wait"`` for what the walker waits through; anything it refuses stops here with
        that frame (a prompt, a tracker with 点击继续, a misread before a clean frame, ...)."""
        from host.android.move import classify, first_frame_rule
        verdict, reason, _shape = first_frame_rule(
            observation(look, classify(look.screen, look.recognize, look.frame)), self.reader, STARSEA_MAP)
        if verdict == "stop":
            raise Stop(f"{what}大世界上{reason}", [look.frame])
        return verdict

    def await_hud(self, what, seconds, destination=None):
        """Wait, without input, for a HUD frame the walker would start on (hud_verdict) after 传送 (``destination``:
        the point's name) or after the bell. On the way: loading frames (nothing recognised, no text: loading_frame)
        up to ``seconds``; frames with nothing recognised but plain text up to UNKNOWN_WAIT; after 传送, up to
        MAP_LINGER, the map and the destination's own panel; HUD frames the walker waits through (an ambient remark,
        the tracker's distance not yet settled). Anything else stops with that frame on first sight: a dialog, a
        popup, another page or panel, a HUD the walker refuses, and text that reads as a prompt (a NARRATION_DENY
        word or an overlay glyph) on a frame with nothing recognised. Returns that HUD Look; None when stopped."""
        start, unknown_since = time.monotonic(), None
        while not self.rt.stopped:
            look = self.capture()
            if look is None:
                return None
            elapsed = time.monotonic() - start
            if look.state == "hud":
                if self.hud_verdict(look, what) == "ok":
                    return look
            elif look.state in ("map", "panel") and destination is not None and elapsed <= MAP_LINGER:
                if look.state == "panel" and not panel_ok(read_panel(look.recognize), destination):
                    raise Stop(f"{what}出现了别的地点的详情", [look.frame])
            elif look.state == "transition":
                kind = transition_text(look.recognize)
                if kind == "prompt":
                    raise Stop(f"{what}出现了像是要点击或指引的文字", [look.frame])
                if kind == "text":
                    unknown_since = time.monotonic() if unknown_since is None else unknown_since
                    if time.monotonic() - unknown_since >= UNKNOWN_WAIT:
                        raise Stop(f"{what}出现了认不出的画面，等了 {UNKNOWN_WAIT} 秒没有变化", [look.frame])
            elif look.state != "invalid":
                raise Stop(f"{what}出现了不该出现的画面", [look.frame])
            if elapsed >= seconds:
                raise Stop(f"{what} {seconds} 秒内没有回到干净的大世界", [look.frame])
            self.rt.pause(STEP)
        return None

    # -- the whole teleport --

    def transport(self, checkpoint, entry=None):
        """Teleport to ``checkpoint`` (a checkpoints.json record of 星海: name, x, y) from the HUD or from the map page
        of 星海; returns ``(x, y)``, the walker's position hint, once a HUD the walker would start on shows. ``entry``:
        the frame the caller already has of the map (carried, not replaced); without it the first frame captured here
        must be the HUD or the map (anything else stops with it at once). None when the run stopped."""
        name, x, y = checkpoint["name"], float(checkpoint["x"]), float(checkpoint["y"])
        if entry is None:
            entry = self.capture()
            if entry is None:
                return None
            while entry.state == "invalid" and not self.rt.stopped:
                entry = self.capture()
                if entry is None:
                    return None
            if entry.state == "hud":
                entry = self.open_map()
                if entry is None:
                    return None
        if entry.state not in ("map", "menu"):
            raise Stop(f"要传送到「{name}」时画面不是大地图", [entry.frame])
        if not (self.region(STARSEA, entry) and self.zoom()):
            return None
        pose = self.center(name, x, y)
        if pose is None:
            return None
        panel = self.select(name, x, y, pose)
        if panel is None or not self.confirm(name, panel):
            return None
        if self.await_hud(f"传送到「{name}」时", ARRIVE_WAIT, destination=name) is None:
            return None
        say(self.rt, f"已传送到「{name}」。")
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
            self.card_frame = frame
            if region_card(recognize) != STARSEA:
                raise Stop("读取地图卡片时地图不是星海", [frame])
            card = card_state(recognize)
            if card.kind != "unknown" and last is not None and last.kind != "unknown" and card.kind != last.kind:
                raise Stop("地图卡片上的摇铃状态前后读得不一样", [frame])
            if card.kind != "unknown" and card == last:
                return card
            last = card
            if time.monotonic() - start >= CARD_WAIT:
                raise Stop("读不出地图卡片上今天是否已经召唤摇铃（不是认得的未摇铃提示，也不是认得的已摇铃状态）", [frame])
            self.rt.pause(STEP)
        return None

    def leave(self):
        """The map page left by its back arrow (one tap: the map with nothing over it on both tap frames, the arrow
        guarded by full OCR boxes). Frames of the map page and loading frames are waited through up to
        pages.LEAVE_PAGE_WAIT; the very first frame that is anything else must be a HUD the walker would start on
        (hud_verdict; a remark is waited through, up to CLEAN_HUD_WAIT), else it is saved and the task stops.
        Returns the HUD Look; None when the run stopped."""
        def back(look):
            if look.state != "map" or not pages.is_exactly(look.screen, MAP_TITLE):
                return None
            result = look.recognize(actions.BACK_ARROW)
            if not actions._hit(result):
                return None
            guard(look.recognize, actions._box(result), look.frame)
            return {"target": actions._box(result)}
        if not self.send("点地图的返回箭头", back):
            return None
        start, left = time.monotonic(), False
        while not self.rt.stopped:
            look = self.capture()
            if look is None:
                return None
            elapsed = time.monotonic() - start
            if look.state == "hud":
                left = True
                if self.hud_verdict(look, "离开地图后") == "ok":
                    return look
            elif look.state != "invalid":
                waiting = not left and (look.state == "map" or (
                    look.state == "transition" and transition_text(look.recognize) == "none"))
                if not waiting:
                    raise Stop("点了地图的返回箭头之后没有回到干净的大世界", [look.frame])
            if elapsed >= (CLEAN_HUD_WAIT if left else pages.LEAVE_PAGE_WAIT):
                raise Stop("点了地图的返回箭头之后没有回到干净的大世界", [look.frame])
            self.rt.pause(STEP)
        return None


def transition_text(recognize) -> str:
    """What a frame with nothing recognised reads (full-frame OCR): "prompt" (a NARRATION_DENY word or an overlay
    glyph: an instruction or a tap marker), "text" (other text of two or more CJK characters) or "none" (a loading
    frame, loading_frame)."""
    from host.android.move import NARRATION_DENY, SCREEN_TEXT, _glyph
    result = recognize(SCREEN_TEXT)
    if result is None:
        return "text"
    texts = [text for text, _box in _rows(result)]
    if any(_glyph(text) or any(word in text for word in NARRATION_DENY) for text in texts):
        return "prompt"
    return "text" if any(_cjk(text) >= 2 for text in texts) else "none"


def reject(recognize, frame, name) -> Panel:
    """The panel on ``frame`` when it is exactly the panel of ``name``; any other panel stops with that frame."""
    panel = read_panel(recognize)
    if panel_ok(panel, name):
        return panel
    raise Stop(f"点开的不是「{name}」的详情，没有点任何按钮" if panel.title != point_name(name) else
               f"「{name}」的详情上有预期以外的按钮或文字，没有点任何按钮", [frame])
