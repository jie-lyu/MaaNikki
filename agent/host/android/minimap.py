"""Android: position and camera heading from the phone HUD's minimap (phase E movement).

The phone minimap is a soft-edged disc about 1.25x the PC's. The frame is scaled about the phone pivot so the disc
lands on the PC pivot and radius (navigation.models.MINIMAP_CENTER / MINIMAP_RADIUS), and the shared
navigation.vision.Locator then runs unchanged: map correlation for the position, the minimap's camera cone
(navigation.camera) for the camera yaw. Only the matched ring is narrowed (MASK), to keep the soft rim and the HUD icons
that sit on it out of the correlation; the cone keeps the full PC geometry.

Numbers (offline sweep over saved device frames, .build/android/minimap-offline: refine.json, summary.json; device
measurements, .build/android/phase-e-measure): pivot (159.6, 110.8) and phone radius 83.25 (scale 0.801) maximise the
match score; mask 0.85; scores 0.68-0.82 with a position hint, static spread 0.03 map units. Without a hint the coarse
search found the right place on only 9 of 57 frames, so this module never searches without one (MinimapReader.fix needs
a hint) and callers refuse to start without a caller-supplied first position.

Cost: building the Locator loads the whole map image (about 0.45 s for miraland); one match is about 10 ms. Build one
MinimapReader per walk and keep it: that, not the search radius, is the speed win (the Locator's window is never
smaller than about +-45 map units around the hint, bounded by the sampled patch, whatever ``search_radius`` says).

Not here: recognising a moved minimap. The player can rearrange the HUD; this reader only checks that the cyan player
chevron sits at the default pivot (chevron_at_pivot) and the walk refuses when it does not.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

import cv2
import numpy as np

from navigation.camera import camera_heading
from navigation.models import MINIMAP_CENTER, MINIMAP_RADIUS, NavigationError, maps
from navigation.vision import Locator

CENTER = (159.6, 110.8)  # the minimap pivot on the 1280x720 phone frame (pixel-edge coordinates, as the PC constants)
RADIUS = 83.25  # the phone radius that corresponds to the PC's MINIMAP_RADIUS
MASK = 0.85  # the matched ring's radius, as a fraction of the PC radius
SCORE_FLOOR = 0.55  # every map, also those whose catalog entry has no floor (miraland: a wrong 0.38 peak is "reliable")
CHEVRON_SEARCH = 22  # pixels around CENTER searched for the cyan chevron
CHEVRON_TOLERANCE = 4.0  # its centroid must lie this close to CENTER (rotating it moves the centroid ~1-2 px)
CHEVRON_MIN_AREA = 30


@dataclass(frozen=True)
class Fix:
    """One minimap reading: map position, match score, camera yaw (degrees, 0 = map up, clockwise; None when the
    cone could not be read confidently) and whether the position passed the trust checks (SCORE_FLOOR included)."""
    x: float
    y: float
    score: float
    heading: float | None
    heading_confidence: float
    reliable: bool
    method: str = ""
    candidate: tuple[float, float] | None = None

    def log(self) -> dict:
        return {"x": round(self.x, 2), "y": round(self.y, 2), "score": round(self.score, 3),
                "heading": None if self.heading is None else round(self.heading, 1),
                "heading_confidence": round(self.heading_confidence, 2), "reliable": self.reliable,
                "method": self.method}


def to_pc(frame, center=CENTER, radius=RADIUS):
    """The phone frame scaled about ``center`` so its minimap lands on the PC pivot with the PC radius (1280x720)."""
    s = MINIMAP_RADIUS / radius
    te = (MINIMAP_CENTER[0] - s * center[0], MINIMAP_CENTER[1] - s * center[1])
    ti = (te[0] + .5 * s - .5, te[1] + .5 * s - .5)  # pixel-edge to pixel-centre coordinates
    sigma = .5 * math.sqrt(max(0.0, 1 / s ** 2 - 1))
    source = cv2.GaussianBlur(frame, (0, 0), sigma) if sigma > 0 else frame
    matrix = np.float32([[s, 0, ti[0]], [0, s, ti[1]]])
    return cv2.warpAffine(source, matrix, (1280, 720), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def chevron_at_pivot(frame, center=CENTER) -> bool:
    """True when the cyan player chevron's centroid lies within CHEVRON_TOLERANCE px of the minimap pivot."""
    if frame is None or getattr(frame, "ndim", 0) != 3:
        return False
    gx, gy = round(center[0]), round(center[1])
    x0, y0 = gx - CHEVRON_SEARCH, gy - CHEVRON_SEARCH
    crop = frame[max(0, y0):y0 + 2 * CHEVRON_SEARCH + 1, max(0, x0):x0 + 2 * CHEVRON_SEARCH + 1]
    if crop.size == 0:
        return False
    mask = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), (75, 60, 100), (105, 255, 255))
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
    for index in range(1, count):
        if stats[index, cv2.CC_STAT_AREA] < CHEVRON_MIN_AREA:
            continue
        cx, cy = centroids[index]
        if math.hypot(cx + max(0, x0) + .5 - center[0], cy + max(0, y0) + .5 - center[1]) <= CHEVRON_TOLERANCE:
            return True
    return False


class PhoneLocator(Locator):
    """The shared Locator on to_pc frames: the matched ring narrowed to MASK, the pivot fixed (the frame was already
    placed, so the PC's border and bezel detection is skipped), the camera cone with the full PC geometry."""

    def __init__(self, spec):
        super().__init__(spec)
        self.full_spec = spec
        self._base_spec = self.spec = replace(spec, radius=MINIMAP_RADIUS * MASK)

    def sync_minimap_center(self, frame, reference=None):
        return self.spec

    def camera_heading(self, reference, x, y, *, use_terrain=False):
        heading, confidence, self.heading_diagnostics = camera_heading(
            reference, self.map, self.full_spec, x, y,
            terrain=self.camera_background if use_terrain else None)
        return heading, confidence


def _resource():
    from app_paths import RESOURCE_ROOT
    return RESOURCE_ROOT / "resource"


class MinimapReader:
    """Position and camera yaw on phone frames for one map; keep one per walk (building it loads the map)."""

    def __init__(self, map_id: str, resource=None, *, locator=None):
        if locator is None:
            spec = maps(resource or _resource()).get(map_id)
            if spec is None:
                raise NavigationError("没有这张地图的资源，不能在安卓模拟器上移动。")
            locator = PhoneLocator(spec)
        self.map_id = map_id
        self.locator = locator

    @staticmethod
    def present(frame) -> bool:
        """The minimap is where this reader expects it (the player chevron at the default pivot)."""
        return chevron_at_pivot(frame)

    def fix(self, frame, hint, search_radius) -> Fix:
        """Match ``frame``'s minimap in a window around ``hint`` (required: no unhinted search). Nothing is kept
        between calls (no speed gate, no camera terrain memory): the walk decides what to trust."""
        if hint is None:
            raise NavigationError("没有位置提示，不做小地图的全图搜索。")
        locator = self.locator
        locator.camera_background.reset()  # each reading stands alone, as in the offline and device measurements
        pose = locator.locate(to_pc(frame), hint=(float(hint[0]), float(hint[1])), search_radius=search_radius,
                              commit=False)
        diagnostics = locator.heading_diagnostics or {}
        score = float(pose.score) if pose.score is not None and math.isfinite(pose.score) else 0.0
        candidate = diagnostics.get("candidate_position")
        return Fix(float(pose.x), float(pose.y), score, pose.heading, float(pose.heading_confidence or 0.0),
                   bool(pose.position_reliable) and score >= SCORE_FLOOR, str(diagnostics.get("position_match", "")),
                   tuple(float(v) for v in candidate) if candidate else None)
