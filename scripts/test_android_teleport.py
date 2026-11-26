"""Android big-map teleport and the 星海 crystal route entry (agent/host/android/teleport.py, crystal_route.py):
python scripts/test_android_teleport.py

Runs on any OS without an emulator. The map localisation, the blank-terrain check and the drag planning run on a crop of
a real max-zoom map frame (scripts/fixtures/android/map_terrain_max.jpg, from .build/android/map-survey 05b); the flows
run against scripted screens reacting to the taps and swipes the real flow sends; the nodes and OCR run through the
real MaaFramework (both resource layers) on frames composed from UID-free crops of the survey frames.
scripts/android/teleport_frames.py repeats the recognition and decisions on the full saved frames (local only).
"""
from __future__ import annotations

import contextlib
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))  # Windows CI's maa package has no bin of its own
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402

from host.android import actions, crystal_route, move, pages, teleport  # noqa: E402
from host.android.actions import Screen  # noqa: E402
from host.android.teleport import Card, Pose, Stop  # noqa: E402

FIXTURES = ROOT / "scripts/fixtures/android"
MAIN = {"name": "星梦群屿·主岛", "province": "星海", "region": "星海", "x": 1433.676, "y": 997.388}
HUB = {"name": "无界枢纽", "province": "星海", "region": "星海", "x": 834.327, "y": 1019.073}
COMPOSE = r'''
import cv2
import numpy as np

PLACES = {"map_title.png": (0, 0), "map_card_xinghai.jpg": (985, 75), "map_card_region.jpg": (985, 75),
          "map_zoom_default.jpg": (20, 640), "map_zoom_max.jpg": (20, 640), "map_terrain_max.jpg": (250, 110),
          "map_menu_expanded.jpg": (800, 0), "map_menu_collapsed.jpg": (800, 0), "map_panel.jpg": (100, 466),
          "hud_bell_purple.png": (576, 572), "hud_bell_waves.png": (576, 572)}
FRAMES = {
    "xinghai default": ("map_terrain_max.jpg", "map_title.png", "map_card_xinghai.jpg", "map_zoom_default.jpg"),
    "xinghai max": ("map_terrain_max.jpg", "map_title.png", "map_card_xinghai.jpg", "map_zoom_max.jpg"),
    "region": ("map_terrain_max.jpg", "map_title.png", "map_card_region.jpg", "map_zoom_default.jpg"),
    "menu expanded": ("map_terrain_max.jpg", "map_title.png", "map_menu_expanded.jpg", "map_zoom_default.jpg"),
    "menu collapsed": ("map_terrain_max.jpg", "map_title.png", "map_menu_collapsed.jpg", "map_zoom_default.jpg"),
    "panel": ("map_terrain_max.jpg", "map_title.png", "map_card_xinghai.jpg", "map_panel.jpg"),
    "bell purple": ("hud_bell_purple.png",),
    "bell waves": ("hud_bell_waves.png",),
}


def compose(fixtures, names, background=(78, 58, 66)):
    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[:] = background
    for name in names:
        image = cv2.imread(str(fixtures / name))
        x, y = PLACES[name]
        frame[y:y + image.shape[0], x:x + image.shape[1]] = image
    return frame
'''
exec(COMPOSE)
WORLD = teleport.WorldMap(resource=ROOT / "resource")


def hit(box, text=""):
    return SimpleNamespace(hit=True, box=box, best_result=SimpleNamespace(text=text, box=box))


def rows(*items):
    found = [SimpleNamespace(text=text, box=box) for text, box in items]
    return SimpleNamespace(hit=True, box=found[0].box, best_result=found[0], filtered_results=found)


MISS = SimpleNamespace(hit=False, box=None, best_result=None, filtered_results=[])


def card_of(*texts):
    return teleport.card_state(lambda node, override=None: rows(*[(t, [1030, 130 + 30 * i, 120, 24])
                                                                  for i, t in enumerate(texts)])
                               if node == teleport.CARD_TEXT and texts else MISS)


class LookTests(unittest.TestCase):
    """The pure checks, on the real crop and on scripted OCR rows."""

    def test_the_max_zoom_crop_is_located_on_the_world_map(self):
        frame = compose(FIXTURES, FRAMES["xinghai max"])
        pose = WORLD.locate(frame)
        self.assertTrue(pose.trusted, pose)
        self.assertLess(abs(pose.x - 1394.5) + abs(pose.y - 1010.4), 1.0)
        self.assertEqual([round(v) for v in pose.to_screen(MAIN["x"], MAIN["y"])], [724, 332])
        # the icon the survey tapped at (724,322): its centre (725,321), 1.8 px from where it should be
        self.assertLess(np.hypot(*(np.subtract(teleport.pillar_expected(pose, MAIN["x"], MAIN["y"]), (725, 321)))), 3)
        self.assertFalse(WORLD.locate(frame, 0.676).trusted)  # the default zoom's scale on a max-zoom frame
        self.assertFalse(WORLD.locate(np.full((720, 1280, 3), 90, np.uint8)).trusted)  # no terrain

    def test_blank_terrain_and_the_drag(self):
        frame = compose(FIXTURES, FRAMES["xinghai max"])
        pose = WORLD.locate(frame)
        residual = WORLD.residual(frame, pose)
        self.assertTrue(teleport.blank_at(residual, (350, 400)))  # open sea
        for point in ((725, 321), (850, 506), (230, 400), (1000, 300)):  # pillar, label, outside the zone
            self.assertFalse(teleport.blank_at(residual, point), point)
        plan = teleport.plan_drag(residual, pose.to_screen(HUB["x"], HUB["y"]))
        self.assertEqual(plan, ((320, 320), (880, 282)))
        self.assertTrue(all(teleport.blank_at(residual, p) for p in plan))
        self.assertIsNone(teleport.plan_drag(np.full((720, 1280), 255, np.float32), (100, 300)))
        begin, end = teleport.plan_drag(np.zeros((720, 1280), np.float32), (-2000, 300))
        self.assertLessEqual(end[0] - begin[0], teleport.DRAG_MAX[0])

    def test_zoom_bar(self):
        for name, level in (("xinghai default", "below"), ("xinghai max", "max"), ("panel", None)):
            with self.subTest(frame=name):
                self.assertEqual(teleport.zoom_state(compose(FIXTURES, FRAMES[name])), level)

    def test_the_card_needs_a_whole_known_message(self):
        # Review round 1, finding 1: only the whole observed message (whitespace/punctuation aside) or the whole
        # counter set is a state; contradictory, partial or unfamiliar text is unknown.
        self.assertEqual(card_of("今日尚未进行召唤摇铃", "快前往流转之枢吧").kind, "not_rung")
        self.assertEqual(card_of("今日尚未进行 召唤摇铃。", "快前往流转之枢吧！").kind, "not_rung")
        rung = card_of("1/3", "12/30", "40/100")
        self.assertEqual((rung.kind, rung.crystals), ("rung", 12))
        self.assertEqual(card_of("12 / 30", "0/3", "100/100").crystals, 12)
        for texts in ((), ("昨日尚未摇铃，今日已进行召唤摇铃",), ("尚未解锁摇铃",), ("今日尚未进行召唤摇铃",),
                      ("今日尚未进行召唤摇铃", "快前往流转之枢吧", "1/3"), ("快前往流转之枢吧", "今日尚未进行召唤摇铃"),
                      ("今日已进行召唤摇铃",), ("7/8", "6/31"), ("12/30", "1/3"), ("1/3", "12/30", "40/100", "您"),
                      ("1/3", "718", "40/100"), ("1/3", "40/30", "40/100"), ("1/3", "12/30", "12/30")):
            with self.subTest(texts=texts):
                self.assertEqual(card_of(*texts).kind, "unknown")
        self.assertEqual(teleport.card_state(lambda node, override=None: None).kind, "unknown")

    def test_digits_and_slashes_are_never_normalised_into_the_message(self):
        # Review round 2, finding 5: a counter glued to a message row; full-width digits; other punctuation.
        for texts in (("今日尚未进行召唤摇铃１／３０", "快前往流转之枢吧"), ("今日尚未进行召唤摇铃1/30", "快前往流转之枢吧"),
                      ("今日尚未进行召唤摇铃", "快前往流转之枢吧３"), ("今日尚未进行召唤摇铃~", "快前往流转之枢吧"),
                      ("１/３", "12/30", "40/100")):
            with self.subTest(texts=texts):
                self.assertEqual(card_of(*texts).kind, "unknown")
        self.assertEqual(card_of("今日尚未进行召唤摇铃，", "快前往流转之枢吧！").kind, "not_rung")

    def test_the_region_card_is_read_positively(self):
        def recognize(name, dropdown=True):
            return lambda node, override=None: {teleport.CARD_DROPDOWN: hit([1199, 90, 25, 22]) if dropdown else MISS,
                                                teleport.REGION_NAME: hit([1070, 84, 50, 27], name)}.get(node, MISS)
        with patch("app_paths.RESOURCE_ROOT", ROOT):
            self.assertEqual(teleport.region_card(recognize("星海")), "星海")
            self.assertEqual(teleport.region_card(recognize("纪念山地")), "纪念山地")
            for case in (recognize("详情"), recognize("传送"), recognize("星海", dropdown=False), recognize("")):
                self.assertIsNone(teleport.region_card(case))

    def test_panel_names_and_buttons(self):
        self.assertEqual(teleport.point_name("星梦群屿 · 主岛"), "星梦群屿·主岛")
        self.assertEqual(teleport.point_name("星梦群屿・主岛"), "星梦群屿·主岛")
        good = teleport.Panel("星梦群屿·主岛", ("传送", "详情"), (), [1000, 637, 70, 40], [766, 639, 74, 39], True,
                              [1000, 637, 70, 40])
        self.assertTrue(teleport.panel_ok(good, "星梦群屿·主岛"))
        self.assertFalse(teleport.panel_ok(good, "无界枢纽"))
        for change in ({"buttons": ("传送", "立即前往", "详情")}, {"buttons": ("OK", "传送", "详情")},
                       {"buttons": ("传送",)}, {"words": ("立即前往",)}, {"teleport": None}, {"detail": None},
                       {"detail": [990, 637, 20, 40]}, {"agree": False}, {"narrow": None},
                       {"narrow": [913, 637, 150, 40]}):
            with self.subTest(change=change):
                self.assertFalse(teleport.panel_ok(teleport.Panel(**{**good.__dict__, **change}), "星梦群屿·主岛"))

    def test_both_teleport_readings_must_contain_the_tap_point_and_agree(self):
        # Route review round 3, finding 1: the band's [900,643,14,26] and the narrow pass's [913,637,150,40] share a
        # pixel but not the tap point (906,655, on 详情's right edge): refused. The centres must agree too.
        self.assertFalse(teleport.readings_agree([900, 643, 14, 26], [913, 637, 150, 40]))
        self.assertTrue(teleport.readings_agree([1000, 637, 70, 40], [1000, 637, 70, 40]))
        self.assertTrue(teleport.readings_agree([1000, 637, 70, 40], [1006, 641, 68, 36]))  # centres 6 px apart
        self.assertFalse(teleport.readings_agree([1000, 637, 70, 40], [913, 637, 150, 40]))  # point in both, centres not
        self.assertFalse(teleport.readings_agree([1000, 637, 70, 40], [1036, 637, 70, 40]))  # point outside the narrow
        self.assertFalse(teleport.readings_agree([1000, 637, 70, 40], None))

    def test_province_headers(self):
        self.assertTrue(teleport.province_header([888, 100, 57, 32]))
        self.assertFalse(teleport.province_header([841, 161, 93, 32]))  # a region row (纪念山地)

    def test_touches_use_the_walkers_own_rules(self):
        # Phase B: no copy of the walker's screen rules in teleport; the prompt rule for the minimap and the bell.
        for name in ("first_shape", "confirm_shape", "hud_unclean", "tracker_parts"):
            self.assertFalse(hasattr(teleport, name), name)
        self.assertTrue(teleport.prompt_clear(SimpleNamespace(prompts=((737, 320, 1040, 366),)), (616, 613), 30))
        self.assertFalse(teleport.prompt_clear(SimpleNamespace(prompts=((560, 560, 600, 600),)), (616, 613), 30))
        self.assertTrue(teleport.prompt_clear(SimpleNamespace(prompts=()), (159.6, 110.8), 83.25))
        centre, radius = teleport.minimap_disc()
        from host.android import touch_point
        self.assertEqual((list(centre), radius), (touch_point(list(actions.MINIMAP)), 83.25))  # the tap's own point
        self.assertFalse(teleport.prompt_clear(SimpleNamespace(prompts=((250, 120, 420, 160),)), centre, radius))


# ---- the scripted map ----

BACK = [37, 22, 40, 40]
TITLE_BOX = [1070, 84, 50, 27]
DROPDOWN_BOX = [1199, 90, 25, 22]
COLLAPSE_BOX = [1205, 105, 27, 21]
STARSEA_BOX = [888, 100, 57, 32]
ZOOM_BOX = [356, 659, 18, 18]
CLOSE_BOX = [1067, 466, 51, 51]
TELEPORT_BOX = [1000, 637, 70, 40]
DETAIL_BOX = [766, 639, 74, 39]
BUTTON_BOXES = {"详情": DETAIL_BOX, "传送": TELEPORT_BOX, "立即前往": [1000, 637, 140, 40], "OK": [500, 640, 40, 30],
                "追踪": [1000, 637, 70, 40]}
NOT_RUNG = ("今日尚未进行召唤摇铃", "快前往流转之枢吧")
HUD = Screen("hud", {"duck": [30, 18, 54, 54]})
PROMPT_HUD = Screen("hud", {"duck": [30, 18, 54, 54]}, reason="prompt")  # the stub classifier reads a prompt on it
TRACKER_PROMPT_HUD = Screen("hud", {"duck": [30, 18, 54, 54]}, reason="tracker prompt")
LOADING = Screen("unknown", reason="认不出当前画面")


class Frame(SimpleNamespace):
    pass


class FakeWorld:
    def __init__(self, blank=True):
        self.blank = blank

    def locate(self, frame, scale=teleport.MAX_ZOOM_SCALE, roi=None):
        return Pose(frame.view[0], frame.view[1], .98, .7, scale)

    def residual(self, frame, pose):
        return np.zeros((720, 1280), np.float32) if self.blank else np.full((720, 1280), 255, np.float32)


def stub_classify(screen, recognize, frame=None):
    if screen.reason == "prompt":
        return move.Verdict("other", "大世界以外的文字（可能是提示、横幅、任务或交互按钮）")
    if screen.reason == "tracker prompt":  # a tracker reading 点击继续 (the walker refuses it)
        return move.Verdict("hud", "", ("点击继续",), (), False)
    return move.Verdict("hud", "", ("登上鸣鸣列车",), (), True)


class Game:
    """The map screens of one teleport, reacting to the taps and swipes the real flow sends."""

    def __init__(self, *, where="hud", region="星海", zoom="below", view=(1230.5, 987.4), collapses_needed=1,
                 panel_title=None, buttons=("详情", "传送"), card=NOT_RUNG, title="地图",
                 after_teleport=("loading", "loading", "hud"), menu_has_chevron=True, pillar_shift=(0, 0),
                 region_text=None, back_word=False, narrow_box=None):
        self.where, self.region, self.zoom, self.view = where, region, zoom, view
        self.collapses_needed, self.collapses = collapses_needed, 0
        self.panel_title, self.buttons, self.card, self.title = panel_title, buttons, card, title
        self.after_teleport, self.menu_has_chevron = list(after_teleport), menu_has_chevron
        self.pillar_shift, self.region_text, self.back_word = pillar_shift, region_text, back_word
        self.narrow_box = narrow_box or TELEPORT_BOX
        self.taps, self.queue, self.calls = [], [], []
        self.shown_title, self.expire, self.after_back = None, 0, []

    @property
    def collapsed(self):
        return self.collapses >= self.collapses_needed

    def pillar_box(self, checkpoint):
        pose = Pose(self.view[0], self.view[1], .98, .7)
        x, y = teleport.pillar_expected(pose, checkpoint["x"], checkpoint["y"])
        x, y = x + self.pillar_shift[0], y + self.pillar_shift[1]
        return [round(x) - 14, round(y) - 20, 29, 40]

    def recognize_for(self, where):
        def recognize(node, override=None):
            if node == actions.BACK_ARROW:
                return hit(BACK) if where in ("map", "menu", "panel") else MISS
            if node == teleport.MAP_WORDS and where == "map" and self.back_word:
                return rows(("立即前往", [60, 70, 90, 30]))  # inside the back arrow's margin
            if where in ("map", "panel"):
                if node == teleport.CARD_DROPDOWN:
                    return hit(DROPDOWN_BOX)
                if node == teleport.REGION_NAME:
                    return hit(TITLE_BOX, self.region_text or self.region)
                if node == teleport.CARD_TEXT and self.region == "星海":
                    return rows(*[(t, [1024, 136 + 28 * i, 177, 22]) for i, t in enumerate(self.card)])
            if where == "map":
                if node == teleport.ZOOM_IN:
                    return hit(ZOOM_BOX)
                if node == teleport.PILLAR:
                    for checkpoint in (MAIN, HUB):
                        box = self.pillar_box(checkpoint)
                        x, y, w, h = override["roi"]
                        if x <= box[0] + 14 <= x + w and y <= box[1] + 20 <= y + h:
                            return hit(box)
                    return MISS
            if where == "menu":
                if node == teleport.REGION_MENU:
                    return hit([824, 18, 92, 29], "地图切换")
                if node == teleport.MENU_STARSEA and self.collapsed:
                    return hit(STARSEA_BOX, "星海")
                if node == teleport.MENU_COLLAPSE and not self.collapsed and self.menu_has_chevron:
                    return hit([COLLAPSE_BOX[0], COLLAPSE_BOX[1] + 90 * self.collapses, 27, 21])
            if where == "panel":
                if node == teleport.PANEL_CLOSE:
                    return hit(CLOSE_BOX)
                if node == teleport.PANEL_TITLE:
                    return rows((self.shown_title, [177, 481, 177, 28]))
                if node == teleport.PANEL_BUTTONS:
                    return rows(*[(b, BUTTON_BOXES[b]) for b in self.buttons])
                if node == teleport.TELEPORT and "传送" in self.buttons:
                    return hit(self.narrow_box, "传送")
                if node == teleport.MAP_WORDS and "立即前往" in self.buttons:
                    return rows(("立即前往", BUTTON_BOXES["立即前往"]))
            if where == "loading text" and node == move.SCREEN_TEXT:
                return rows(("星海的风景图", [400, 650, 300, 30]))
            if where == "loading prompt" and node == move.SCREEN_TEXT:
                return rows(("点击继续", [560, 650, 160, 30]))
            return MISS
        return recognize

    def screen_now(self, rt):
        where = self.queue.pop(0) if self.queue else self.where
        if where == "panel lingering":
            where, self.shown_title = "panel", MAIN["name"]
        if where == "wrong panel":
            where, self.shown_title = "panel", "星梦群屿·海滩"
        frame = Frame(view=self.view, zoom=None if where == "panel" else self.zoom, where=where)
        if where in ("hud", "prompt", "tracker prompt"):
            return {"hud": HUD, "prompt": PROMPT_HUD, "tracker prompt": TRACKER_PROMPT_HUD}[where], frame, \
                lambda node, override=None: MISS
        if where in ("loading", "loading text", "loading prompt"):
            return LOADING, frame, self.recognize_for(where)
        if where == "other":
            return Screen("unknown", reason="画面上有弹窗"), frame, \
                lambda node, override=None: hit([900, 100, 40, 40]) if node == actions.MODAL_CLOSE else MISS
        return Screen("page", {"back": BACK}, title=self.title), frame, self.recognize_for(where)

    def action(self, name, target=None, begin=None, end=None, duration=None, source_frame=None, _movement=False,
               _not_after=None):
        self.calls.append({"name": name, "movement": _movement, "source": source_frame, "not_after": _not_after})
        if self.expire:
            self.expire -= 1
            from host.android.gesture import DecisionExpired
            raise DecisionExpired("aged out inside dispatch")
        if name == "Swipe":
            self.taps.append(("swipe", tuple(begin), tuple(end)))
            s = teleport.MAX_ZOOM_SCALE
            self.view = (self.view[0] - (end[0] - begin[0]) * s, self.view[1] - (end[1] - begin[1]) * s)
            return True
        target = list(target)
        if self.where == "hud" and target == [159, 104]:
            self.taps.append("minimap")
            self.where = "map"
        elif target == TITLE_BOX:
            self.taps.append("card title")
            self.where = "menu"
        elif self.where == "menu" and target[0] == COLLAPSE_BOX[0]:
            self.taps.append("collapse")
            self.collapses += 1
        elif target == STARSEA_BOX:
            self.taps.append("starsea")
            self.where, self.region = "map", "星海"
        elif target == ZOOM_BOX:
            self.taps.append("zoom")
            self.zoom = "max"
        elif target == CLOSE_BOX:
            self.taps.append("close")
            self.where = "map"
        elif target == TELEPORT_BOX:
            self.taps.append("teleport")
            self.queue = list(self.after_teleport)
            self.where = self.queue[-1] if self.queue else "map"
        elif target == BACK:
            self.taps.append("back")
            self.where = "hud"
            self.queue = list(self.after_back)
        elif self.where == "map" and target in (self.pillar_box(MAIN), self.pillar_box(HUB)):
            main = target == self.pillar_box(MAIN)
            self.taps.append("pillar " + ("main" if main else "hub"))
            self.where = "panel"
            self.shown_title = self.panel_title or (MAIN if main else HUB)["name"]
        else:
            raise AssertionError(f"unexpected tap {target} on {self.where}")
        return True


class HudIo:
    """move.RuntimeIo stand-in: each observation a HUD frame of ``kinds`` (then clean HUD frames), stamped with the
    (patched) clock; ``regions``: the tracker each shows."""

    def __init__(self, kinds=(), regions=None, prompts=None, name_only=None, fragments=None):
        self.kinds, self.regions, self.prompts = list(kinds), list(regions or []), list(prompts or [])
        self.fragments = list(fragments or [])
        self.name_only = list(name_only or [])  # per frame; default: a name-only tracker shows its evidence
        self.frames = []

    def observe(self):
        import time
        kind = self.kinds.pop(0) if self.kinds else "hud"
        region = self.regions.pop(0) if self.regions else ("登上鸣鸣列车", "#")
        prompts = self.prompts.pop(0) if self.prompts else ()
        fragments = self.fragments.pop(0) if self.fragments else ()
        evidence = self.name_only.pop(0) if self.name_only else region == ("登上鸣鸣列车",)
        frame = Frame(view=None, zoom=None, where="hud", index=len(self.frames))
        self.frames.append(frame)
        return move.Observation(frame, kind, time.monotonic(), "提示" if kind != "hud" else "", regions=region,
                                blocks=(("labels", 600, 1270, 75, False),),
                                name_only=kind in move.MOVE_KINDS and evidence, prompts=prompts,
                                fragments=fragments)


READER = SimpleNamespace(present=lambda frame: True)


def scripted_rt(game):
    rt = SimpleNamespace(stopped=False, logs=[], _frame_still_valid=Mock(return_value=True),
                         pause=Mock(return_value=True), diagnostic=Mock(), TOUCH_KEY_ATTEMPTS=3, action=game.action,
                         _recognizer_on=lambda frame: (lambda node, override=None: MISS))
    rt.log = lambda text, detail=False: rt.logs.append(text)
    return rt


def patched(game, step=.25):
    stack = contextlib.ExitStack()
    stack.enter_context(patch.object(pages, "screen_now", side_effect=game.screen_now))
    stack.enter_context(patch.object(teleport, "zoom_state", side_effect=lambda frame: frame.zoom))
    stack.enter_context(patch.object(teleport, "zoom_right",
                                     side_effect=lambda frame: {"max": 333, "below": 248}.get(frame.zoom)))
    stack.enter_context(patch("host.android.move.classify", side_effect=stub_classify))
    stack.enter_context(patch("app_paths.RESOURCE_ROOT", ROOT))
    stack.enter_context(patch("time.monotonic", side_effect=itertools.count(0, step).__next__))
    return stack


def teleporter(rt, world_=None, io=None, reader=READER):
    return teleport.Teleport(rt, world_ or FakeWorld(), io=io or HudIo(), reader=reader)


def transport(game, checkpoint=MAIN, world_=None, io=None, reader=READER, step=.25):
    rt = scripted_rt(game)
    with patched(game, step):
        tp = teleporter(rt, world_, io, reader)
        try:
            return tp.transport(checkpoint), rt, None
        except (Stop, pages.FrameMismatch, pages.GuardRefusal) as error:
            return None, rt, error


class TeleportFlowTests(unittest.TestCase):
    def test_from_the_hud_on_starsea(self):
        game = Game()
        result, rt, error = transport(game)
        self.assertIsNone(error)
        self.assertEqual(result, (MAIN["x"], MAIN["y"]))
        self.assertEqual(game.taps[:2], ["minimap", "zoom"])
        self.assertEqual(game.taps[2][0], "swipe")
        self.assertEqual(game.taps[3:], ["pillar main", "teleport"])
        self.assertIn("已传送到「星梦群屿·主岛」。", rt.logs)

    def test_every_map_input_is_sent_with_the_freshness_rule_from_the_confirming_frame(self):
        # Review round 1, finding 8: the gesture freshness rule (latest capture, after every input) on each input.
        game = Game()
        transport(game)
        self.assertEqual(len(game.calls), 5)
        self.assertTrue(all(call["movement"] for call in game.calls))

    def test_an_aged_confirmation_is_decided_again_and_never_sent(self):
        game = Game(where="map", zoom="max", view=(1394.5, 1010.4))
        _result, _rt, error = transport(game, step=2.0)  # every confirmation older than MAX_DECISION_AGE_S
        self.assertIsInstance(error, Stop)
        self.assertEqual(game.calls, [])

    def test_from_another_region_through_the_menu_collapsing_the_province_first(self):
        game = Game(region="纪念山地")
        result, _rt, error = transport(game)
        self.assertIsNone(error)
        self.assertEqual(game.taps[:5], ["minimap", "card title", "collapse", "starsea", "zoom"])
        self.assertEqual(game.taps[-2:], ["pillar main", "teleport"])

    def test_the_list_is_read_again_after_the_last_collapse(self):
        # Review round 1, finding 11: the third collapse reveals 星海, which is then tapped.
        game = Game(region="纪念山地", collapses_needed=teleport.MAX_COLLAPSES)
        _result, _rt, error = transport(game)
        self.assertIsNone(error)
        self.assertEqual(game.taps.count("collapse"), teleport.MAX_COLLAPSES)
        self.assertIn("starsea", game.taps)
        game = Game(region="纪念山地", collapses_needed=teleport.MAX_COLLAPSES + 1)
        _result, _rt, error = transport(game)
        self.assertIsInstance(error, Stop)
        self.assertTrue(error.frames)
        self.assertNotIn("starsea", game.taps)

    def test_a_region_title_that_is_no_region_is_not_tapped(self):
        # Review round 1, finding 6: 详情 read where the card's title should be.
        game = Game(region="纪念山地", region_text="详情")
        _result, _rt, error = transport(game)
        self.assertIsInstance(error, Stop)
        self.assertEqual(game.taps, ["minimap"])

    def test_the_hub_is_dragged_in_from_off_screen(self):
        game = Game(zoom="max", view=(1394.5, 1010.4))  # survey 05b: the hub ~1160 px left of the screen
        result, _rt, error = transport(game, HUB)
        self.assertIsNone(error)
        self.assertEqual(result, (HUB["x"], HUB["y"]))
        swipes = [t for t in game.taps if isinstance(t, tuple)]
        self.assertGreaterEqual(len(swipes), 2)
        for _kind, begin, end in swipes:
            for x, y in (begin, end):
                self.assertTrue(teleport.in_zone((x, y), teleport.DRAG_ZONE))

    def test_a_drag_is_never_decided_on_another_page(self):
        # Review round 1, finding 7: the exact title (地图册 is not 地图), no fabricated screen.
        game = Game(where="map", zoom="max", title="地图册")
        _result, _rt, error = transport(game)
        self.assertIsInstance(error, Stop)
        self.assertEqual(game.taps, [])

    def test_a_rejected_panel_stops_at_once_with_nothing_tapped(self):
        # Review round 1, finding 4: no close tap; every text in the button band accounted for.
        for case in ({"panel_title": "星梦群屿·海滩"}, {"buttons": ("详情", "传送", "立即前往")},
                     {"buttons": ("详情", "追踪")}, {"buttons": ("立即前往",)}, {"buttons": ("详情", "传送", "OK")}):
            with self.subTest(case=case):
                game = Game(zoom="max", view=(1394.5, 1010.4), **case)
                _result, rt, error = transport(game)
                self.assertIsInstance(error, Stop)
                self.assertEqual(game.taps[-1], "pillar main")
                self.assertTrue(error.frames)
                self.assertIn("没有点任何按钮", str(error))

    def test_the_teleport_tap_is_never_repeated(self):
        game = Game(after_teleport=["map"] * 200)
        _result, _rt, error = transport(game)
        self.assertIsInstance(error, Stop)
        self.assertEqual(game.taps.count("teleport"), 1)
        self.assertEqual(game.taps[-1], "teleport")

    def test_arrival_accepts_loading_and_the_destinations_own_panel(self):
        game = Game(after_teleport=["panel lingering", "loading", "loading", "hud"])
        result, _rt, error = transport(game)
        self.assertIsNone(error)
        self.assertEqual(result, (MAIN["x"], MAIN["y"]))

    def test_arrival_stops_at_the_first_recognised_unexpected_screen(self):
        # Review round 1, finding 5: a wrong panel, a HUD with a prompt, a popup.
        for sequence in (["loading", "wrong panel", "hud"], ["loading", "prompt", "hud"], ["loading", "other", "hud"]):
            with self.subTest(sequence=sequence):
                game = Game(after_teleport=sequence)
                _result, _rt, error = transport(game)
                self.assertIsInstance(error, Stop)
                self.assertEqual(error.frames[0].where, {"wrong panel": "panel"}.get(sequence[1], sequence[1]))
                self.assertEqual(game.taps[-1], "teleport")

    def test_an_unknown_frame_with_text_is_given_a_bounded_wait(self):
        game = Game(after_teleport=["loading text"] * 400)
        _result, _rt, error = transport(game)
        self.assertIsInstance(error, Stop)
        self.assertIn(f"{teleport.UNKNOWN_WAIT} 秒", str(error))

    def test_no_blank_terrain_means_no_drag(self):
        game = Game(zoom="max")
        _result, _rt, error = transport(game, world_=FakeWorld(blank=False))
        self.assertIsInstance(error, Stop)
        self.assertEqual(game.taps, ["minimap"])

    def test_a_list_without_starsea_or_a_province_to_collapse_is_not_swiped(self):
        game = Game(region="纪念山地", menu_has_chevron=False)
        _result, _rt, error = transport(game)
        self.assertIsInstance(error, Stop)
        self.assertEqual(game.taps, ["minimap", "card title"])

    def test_a_pillar_away_from_the_point_is_not_tapped(self):
        game = Game(zoom="max", view=(1394.5, 1010.4), pillar_shift=(30, 0))
        _result, _rt, error = transport(game)
        self.assertIsInstance(error, Stop)
        self.assertNotIn("pillar main", game.taps)

    def test_the_minimap_is_tapped_only_from_clean_hud_frames(self):
        # Review round 1, finding 3: the walker's clean-HUD checks on both tap frames, the minimap in place.
        for io, reader in ((HudIo(kinds=["other"]), READER), (HudIo(kinds=["hud", "other"]), READER),
                           (HudIo(regions=[("登上鸣鸣列车",), ("别的任务",)]), READER),
                           (HudIo(regions=[("登上鸣鸣列车", "点击继续")]), READER),
                           (HudIo(), SimpleNamespace(present=lambda frame: False))):
            with self.subTest(kinds=io.kinds, regions=io.regions):
                game = Game()
                _result, _rt, error = transport(game, io=io, reader=reader)
                self.assertIsInstance(error, Stop)
                self.assertEqual(game.taps, [])
        game = Game()
        result, _rt, error = transport(game, io=HudIo(kinds=["narration", "narration"]))
        self.assertIsNone(error)  # an ambient remark is waited through
        self.assertEqual(game.taps[0], "minimap")

    def test_the_confirmations_expiry_reaches_the_dispatch(self):
        # Review round 2, finding 3: every map input carries its confirming frame's expiry (_not_after); one that
        # expires inside the dispatch is decided again on new frames.
        game = Game()
        transport(game)
        self.assertTrue(all(call["not_after"] is not None for call in game.calls))
        game = Game(where="map", zoom="max", view=(1394.5, 1010.4))
        game.expire = 1
        _result, _rt, error = transport(game)
        self.assertIsNone(error)
        self.assertEqual(game.taps, ["pillar main", "teleport"])
        self.assertEqual(len(game.calls), 3)  # the expired pillar tap, then the pillar and 传送

    def test_the_two_passes_must_agree_on_where_teleport_is(self):
        # Review round 2, finding 4; round 3, finding 1: the narrower pass reading 传送 elsewhere (on 详情's edge),
        # or over a wider box that holds the tap point but is centred elsewhere, stops before any tap.
        for narrow in ([900, 643, 14, 26], [913, 637, 150, 40]):
            with self.subTest(narrow=narrow):
                game = Game(zoom="max", view=(1394.5, 1010.4), narrow_box=narrow)
                _result, _rt, error = transport(game)
                self.assertIsInstance(error, Stop)
                self.assertNotIn("teleport", game.taps)

    def open_on(self, io, map_id="starsea"):
        game = Game()
        rt = scripted_rt(game)
        with patched(game):
            try:
                return teleporter(rt, io=io).open_map(map_id), game, None
            except Stop as error:
                return None, game, error

    def test_the_minimap_tap_follows_the_walkers_startup_and_confirmation_rules(self):
        # Review round 2, findings 7 and 13; phase B: the walker's own rules (move.first_frame_rule and
        # later_frame_rule), the name-only tracker only on 星海.
        alone = ("登上鸣鸣列车",)
        for io, map_id in ((HudIo(kinds=["misread", "hud", "hud"]), "starsea"),  # a misread first: refused
                           (HudIo(regions=[alone, ("登上鸣鸣列车", "#")]), "starsea"),  # a distance appearing
                           (HudIo(regions=[alone, alone], name_only=[True, False]), "starsea"),  # evidence gone
                           (HudIo(kinds=["hud", "misread"]), "starsea"),
                           # route review round 2, finding 7: only 交谈 in the tracker on both frames (and an
                           # instruction, finding 6), on 星海 too
                           (HudIo(regions=[("交谈",)] * 3), "starsea"),
                           (HudIo(regions=[("与大喵交谈",)] * 3), "starsea")):
            with self.subTest(kinds=io.kinds, regions=io.regions[:2], evidence=io.name_only, map_id=map_id):
                _look, game, error = self.open_on(io, map_id)
                self.assertIsInstance(error, Stop)
                self.assertEqual(game.taps, [])
        _look, game, error = self.open_on(HudIo(regions=[alone] * 4))
        self.assertIsNone(error)  # a name-only tracker on 星海, its evidence on both frames
        self.assertEqual(game.taps, ["minimap"])
        _look, game, error = self.open_on(HudIo(kinds=["hud", "narration", "hud", "hud"]), None)
        self.assertIsNone(error)
        self.assertEqual(game.calls[0]["source"].index, 3)

    def test_a_restart_on_starsea_opens_the_map_on_its_name_only_tracker(self):
        # Route review round 3, finding 9: start, wait, stop on 星海 (the name-only tracker), start again: the
        # map-opening tap (map not known) may use the name-only tracker; the map must then show 星海 positively.
        alone = ("登上鸣鸣列车",)
        look, game, error = self.open_on(HudIo(regions=[alone] * 4), None)
        self.assertIsNone(error)
        self.assertEqual((game.taps, look.state), (["minimap"], "map"))
        # the evidence must still hold on the confirming frame
        _look, game, error = self.open_on(HudIo(regions=[alone, alone], name_only=[True, False]), None)
        self.assertIsInstance(error, Stop)
        self.assertEqual(game.taps, [])
        # the map opens on another region: the name-only exception did not apply; stop with that frame
        game = Game(region="心愿原野")
        rt = scripted_rt(game)
        with patched(game), self.assertRaises(Stop) as caught:
            teleporter(rt, io=HudIo(regions=[alone] * 4)).open_map(None)
        self.assertEqual(game.taps, ["minimap"])
        self.assertEqual(caught.exception.frames[0].where, "map")
        # the same HUD with its distance: no 星海 card needed
        game = Game(region="心愿原野")
        rt = scripted_rt(game)
        with patched(game):
            self.assertIsNotNone(teleporter(rt, io=HudIo()).open_map(None))

    def test_the_whole_restart_reaches_the_card(self):
        # The second start of start-wait-stop-start on 星海: Run.open_card(None) from the name-only HUD reads the card.
        game = Game()
        rt = scripted_rt(game)
        run = crystal_route.Run(rt, reader=READER, world=FakeWorld(), io=HudIo(regions=[("登上鸣鸣列车",)] * 4))
        with patched(game):
            card = run.open_card(None)
        self.assertEqual(card.kind, "not_rung")
        self.assertEqual(game.taps, ["minimap"])
        self.assertIsNotNone(run.map.card_day)

    def test_a_prompt_near_the_minimap_blocks_its_tap_one_elsewhere_does_not(self):
        # Phase B: hud_prompt is fine for the tap unless the prompt lies within 40 px + the minimap's radius.
        near, column = ((250, 120, 420, 160),), ((737, 320, 1040, 366),)  # extents x0, y0, x1, y1
        _look, game, error = self.open_on(HudIo(kinds=["hud_prompt", "hud_prompt"], prompts=[column, column]), None)
        self.assertIsNone(error)
        self.assertEqual(game.taps, ["minimap"])
        for prompts in ([near, near], [column, near]):
            with self.subTest(prompts=prompts):
                _look, game, error = self.open_on(HudIo(kinds=["hud_prompt", "hud_prompt"], prompts=prompts), None)
                self.assertIsInstance(error, Stop)
                self.assertEqual(game.taps, [])

    def test_the_first_frame_of_each_phase_is_judged_and_kept(self):
        # Review round 2, finding 9: a popup at the start, a wrong panel for one frame after the pillar.
        game = Game()
        game.queue = ["other"]
        _result, _rt, error = transport(game)
        self.assertIsInstance(error, Stop)
        self.assertEqual((game.taps, error.frames[0].where), ([], "other"))
        game = Game(zoom="max", view=(1394.5, 1010.4))
        original = game.action

        def wrong_panel_first(*args, **kwargs):
            done = original(*args, **kwargs)
            if game.taps[-1] == "pillar main":
                game.queue = ["wrong panel"]
            return done
        game.action = wrong_panel_first
        _result, rt, error = transport(game)
        self.assertIsInstance(error, Stop)
        self.assertEqual(game.taps, ["minimap", "pillar main"])
        self.assertEqual(error.frames[0].where, "panel")

    def test_the_frame_in_hand_and_the_opening_transitions_are_judged(self):
        # Route review round 3, finding 4: a HUD with 点击继续 in the tracker as transport()'s first frame, and an
        # explicit 点击继续 on the first transition after the minimap tap: both stop with that frame saved, before
        # the pillar or 传送.
        game = Game()
        game.queue = ["tracker prompt"]
        _result, _rt, error = transport(game)
        self.assertIsInstance(error, Stop)
        self.assertEqual((game.taps, error.frames[0].where), ([], "tracker prompt"))
        game = Game()
        original = game.action

        def prompt_after_the_minimap(*args, **kwargs):
            done = original(*args, **kwargs)
            if game.taps[-1] == "minimap":
                game.queue = ["loading prompt", "map"]
            return done
        game.action = prompt_after_the_minimap
        _result, _rt, error = transport(game)
        self.assertIsInstance(error, Stop)
        self.assertEqual((game.taps, error.frames[0].where), (["minimap"], "loading prompt"))
        game = Game()  # a plain loading frame on the way is still waited through
        original_plain = game.action

        def loading_after_the_minimap(*args, **kwargs):
            done = original_plain(*args, **kwargs)
            if game.taps[-1] == "minimap":
                game.queue = ["loading", "map"]
            return done
        game.action = loading_after_the_minimap
        _result, _rt, error = transport(game)
        self.assertIsNone(error)

    def test_arrival_refuses_what_the_walker_refuses(self):
        for sequence in (["loading", "tracker prompt", "hud"], ["loading", "loading prompt", "hud"]):
            with self.subTest(sequence=sequence):
                game = Game(after_teleport=sequence)
                _result, _rt, error = transport(game)
                self.assertIsInstance(error, Stop)
                self.assertEqual(game.taps[-1], "teleport")

    def test_the_exit_needs_a_hud_the_walker_would_start_on(self):
        for after in (["prompt", "hud"], ["tracker prompt", "hud"], ["other", "hud"]):
            with self.subTest(after=after):
                game = Game(where="map")
                game.after_back = after
                rt = scripted_rt(game)
                with patched(game), self.assertRaises(Stop) as caught:
                    teleporter(rt).leave()
                self.assertEqual(caught.exception.frames[0].where, after[0])

    def test_the_card_needs_two_agreeing_frames_and_stops_when_unreadable(self):
        for card, kind in ((NOT_RUNG, "not_rung"), (("1/3", "5/30", "9/100"), "rung")):
            with self.subTest(card=card):
                game = Game(where="map", card=card)
                rt = scripted_rt(game)
                with patched(game):
                    self.assertEqual(teleporter(rt).card().kind, kind)
        game = Game(where="map", card=("12/30",))
        rt = scripted_rt(game)
        with patched(game), self.assertRaises(Stop):
            teleporter(rt).card()
        self.assertEqual(game.taps, [])

    def test_the_card_day_comes_from_its_own_captures(self):
        # Route review round 3, finding 6: the day is bound to the card's captures, not to when it is used; a pair
        # straddling the 04:00 reset stops.
        import datetime as dt
        from account_state import game_day
        utc = dt.timezone.utc
        before_reset = dt.datetime(2026, 11, 26, 19, 49, tzinfo=utc)  # 03:49 UTC+8 on the 27th: still the 26th
        after_reset = dt.datetime(2026, 11, 26, 20, 11, tzinfo=utc)  # 04:11: the 27th
        game = Game(where="map")
        rt = scripted_rt(game)
        tp = teleporter(rt)
        tp.now = iter([before_reset] * 4).__next__
        with patched(game):
            self.assertEqual(tp.card().kind, "not_rung")
        self.assertEqual(tp.card_day, game_day(before_reset))
        tp = teleporter(rt)
        tp.now = iter([before_reset, before_reset, after_reset, after_reset]).__next__  # paused after the 1st capture
        with patched(game), self.assertRaises(Stop) as caught:
            tp.card()
        self.assertIn("4 点重置", str(caught.exception))
        self.assertIsNone(tp.card_day)

    def test_leaving_the_map_ends_on_a_confirmed_hud(self):
        game = Game(where="map")
        rt = scripted_rt(game)
        with patched(game):
            self.assertIsNotNone(teleporter(rt).leave())
        self.assertEqual(game.taps, ["back"])

    def test_the_exit_is_guarded_by_map_words(self):
        # Review round 1, finding 6: a map word inside the back arrow's margin refuses the exit tap.
        game = Game(where="map", back_word=True)
        rt = scripted_rt(game)
        with patched(game), self.assertRaises(pages.GuardRefusal):
            teleporter(rt).leave()
        self.assertEqual(game.taps, [])


# ---- the route entry ----

class FakeMap:
    def __init__(self, cards, *, leave=True, transports_ok=True):
        self.cards, self.calls, self.card_frame = list(cards), [], "card frame"
        self._leave, self.transports_ok = leave, transports_ok
        self.card_day = None

    def open_map(self, map_id=None):
        self.calls.append(f"open {map_id}" if map_id else "open")
        return "map look"

    def region(self, name="星海", entry=None):
        self.calls.append("region")
        assert entry == "map look"  # the first map frame is carried in
        return True

    def card(self):
        from account_state import game_day
        self.calls.append("card")
        card = self.cards.pop(0)
        if isinstance(card, Exception):
            raise card
        self.card_day = game_day(crystal_route.now_utc())  # the captures' own time (Teleport.card)
        return card

    def transport(self, record):
        self.calls.append(f"transport {record['name']}")
        return float(record["x"]), float(record["y"])

    def await_hud(self, what, seconds, destination=None):
        self.calls.append("await")
        return "hud look"

    def leave(self):
        self.calls.append("leave")
        if isinstance(self._leave, Exception):
            raise self._leave
        return "hud look" if self._leave else None


SAFE_NOON = __import__("datetime").datetime(2026, 11, 26, 4, 0, tzinfo=__import__("datetime").timezone.utc)  # 12:00 UTC+8


def route_run(cards, *, outcome="arrived", ring=True, record=None, leave=True, user_stop=False,
              stop_after_walk=False, clock=None):
    rt = SimpleNamespace(stopped=False, logs=[], _frame_still_valid=Mock(return_value=True),
                         pause=Mock(return_value=True), diagnostic=Mock(), TOUCH_KEY_ATTEMPTS=3)
    rt.log = lambda text, detail=False: rt.logs.append(text)
    record = record or crystal_route.BellRecord(tempfile.mkdtemp())
    run = crystal_route.Run(rt, reader=READER, world=FakeWorld(), io=HudIo(), record=record)
    run.map = FakeMap(cards, leave=leave)
    rings, walked = [], []
    times = iter(clock) if clock is not None else None

    def fake_ring(self, hub, day):
        rings.append((hub["name"], day))
        record.mark(day, "pressed")
        return ring

    def fake_walk(self, route, hint):
        walked.append(hint)
        return SimpleNamespace(outcome=outcome, ok=outcome == "arrived", message="已到达目标位置。", attempted=3,
                               confirmed=3)

    def published():
        return user_stop or (stop_after_walk and bool(walked))
    with patch.object(crystal_route.Run, "ring", fake_ring), patch.object(crystal_route.Run, "walk", fake_walk), \
            patch("host.game_backend", return_value="android"), \
            patch("host.android_user_stop", side_effect=lambda *a, **k: published()), \
            patch.object(crystal_route, "now_utc", side_effect=lambda: next(times) if times else SAFE_NOON), \
            patch.object(crystal_route, "Run", side_effect=lambda rt_: run), reports(rt):
        result = crystal_route.run(rt, {"kind": "android_crystal_route"})
    return result, rt, run.map.calls, [name for name, _day in rings]


def reports(rt, during_write=None):
    """teleport.announce's file write and display stubbed: shown texts go to rt.logs (as say's do);
    ``during_write()`` runs inside the write (a stop published then)."""
    stack = contextlib.ExitStack()
    stack.enter_context(patch("message_log.write_message",
                              side_effect=lambda text, display, report=None: during_write and during_write()))
    stack.enter_context(patch.object(teleport, "show", side_effect=lambda rt_, text: rt_.logs.append(text)))
    return stack


RUNG4 = Card("rung", 4, ((3, 0), (30, 4), (100, 0)))
RUNG9 = Card("rung", 9, ((3, 1), (30, 9), (100, 3)))


class RouteTests(unittest.TestCase):
    def test_not_rung_hub_bell_then_the_route(self):
        result, rt, calls, rings = route_run([Card("not_rung"), RUNG4, RUNG9])
        self.assertTrue(result, rt.logs)
        self.assertEqual(rings, ["无界枢纽"])
        self.assertEqual(calls, ["open", "region", "card", "transport 无界枢纽", "await", "open starsea", "region",
                                 "card", "transport 星梦群屿·主岛", "open starsea", "region", "card", "leave"])
        self.assertIn(crystal_route.NOTICE, rt.logs)
        self.assertIn("星光结晶：路线前 4/30，路线后 9/30（本趟 +5）。", rt.logs)

    def test_already_rung_skips_the_hub_and_the_bell(self):
        result, _rt, calls, rings = route_run([RUNG4, RUNG4])
        self.assertTrue(result)
        self.assertEqual(rings, [])
        self.assertNotIn("transport 无界枢纽", calls)

    def test_an_attempt_recorded_today_is_never_pressed_again(self):
        # Review round 1, finding 2: a second run (restart) the same game day with the card still not rung.
        record = crystal_route.BellRecord(tempfile.mkdtemp())
        _result, _rt, _calls, rings = route_run([Card("not_rung"), Card("not_rung")], record=record)
        self.assertEqual(rings, ["无界枢纽"])
        result, rt, calls, rings = route_run([Card("not_rung")], record=record)
        self.assertFalse(result)
        self.assertEqual(rings, [])
        self.assertEqual(calls, ["open", "region", "card"])
        self.assertIn(crystal_route.MANUAL, rt.logs[-1])
        self.assertEqual(rt.diagnostic.call_args.kwargs["image"], "card frame")

    def test_an_unreadable_card_stops_before_any_teleport_or_bell(self):
        result, rt, calls, rings = route_run([Stop("读不出地图卡片", ["frame"])])
        self.assertFalse(result)
        self.assertEqual(rings, [])
        self.assertEqual(calls, ["open", "region", "card"])
        rt.diagnostic.assert_called_once()

    def test_still_not_rung_after_the_bell_stops_without_a_second_press(self):
        result, rt, calls, rings = route_run([Card("not_rung"), Card("not_rung")])
        self.assertFalse(result)
        self.assertEqual(rings, ["无界枢纽"])
        self.assertNotIn("transport 星梦群屿·主岛", calls)
        self.assertEqual(rt.diagnostic.call_args.kwargs["image"], "card frame")

    def test_after_a_walk_that_stopped_on_another_screen_nothing_more_is_sent(self):
        result, rt, calls, _rings = route_run([RUNG4], outcome="screen")
        self.assertFalse(result)
        self.assertEqual(calls[-1], "transport 星梦群屿·主岛")
        self.assertIn("没有读到路线前后的星光结晶数量，收集情况没有看到。", rt.logs)

    def test_success_needs_the_count_the_final_hud_and_no_stop(self):
        # Review round 1, finding 9.
        result, rt, _calls, _rings = route_run([RUNG4, Stop("读不出", ["final"])])
        self.assertFalse(result)
        self.assertEqual(rt.diagnostic.call_args.kwargs["image"], "final")
        result, rt, _calls, _rings = route_run([RUNG4, Card("unknown")])
        self.assertFalse(result)
        self.assertEqual(rt.diagnostic.call_args.kwargs["image"], "card frame")
        result, _rt, _calls, _rings = route_run([RUNG4, RUNG9], leave=False)
        self.assertFalse(result)
        result, rt, _calls, _rings = route_run([RUNG4, RUNG9], leave=Stop("没有回到大世界", ["exit"]))
        self.assertFalse(result)
        self.assertEqual(rt.diagnostic.call_args.kwargs["image"], "exit")
        result, _rt, _calls, _rings = route_run([RUNG4, RUNG9], user_stop=True)
        self.assertFalse(result)
        result, _rt, _calls, _rings = route_run([RUNG4, RUNG9], outcome="blocked")
        self.assertFalse(result)  # counts read, but the walk did not arrive

    def test_a_stop_published_during_the_walk_reports_nothing(self):
        # Review round 2, finding 11: published while the walk ran (rt.stopped still False): no result reported.
        result, rt, _calls, _rings = route_run([RUNG4, RUNG9], stop_after_walk=True)
        self.assertFalse(result)
        self.assertFalse(any("路线前" in line or "收集情况" in line for line in rt.logs), rt.logs)

    def test_the_walk_result_is_not_logged_after_a_stop_published_during_its_write(self):
        rt = SimpleNamespace(stopped=False, logs=[])
        rt.log = lambda text, detail=False: rt.logs.append(text)
        published = []

        class Io:
            def __init__(self, rt_, folder):
                pass

            def record(self, kind, record, frame):
                if kind == "result":
                    published.append(True)  # the stop lands while the result is being written
        walker = Mock()
        walker.return_value.walk.return_value = SimpleNamespace(outcome="arrived", message="已到达目标位置。",
                                                                position=(1, 2), attempted=3, confirmed=3)
        run = crystal_route.Run(rt, reader=READER, world=FakeWorld(), io=HudIo())
        route = SimpleNamespace(id="xinghai_crystal_3", map_id="starsea", start_radius=9, timeout=900, points=())
        with patch("host.android.walk_probe.DiagnosticIo", Io), patch("host.android.move.Walker", walker), \
                patch("host.android.move.route_goals", return_value=[]), reports(rt), \
                patch("host.android_user_stop", side_effect=lambda *a, **k: bool(published)):
            run.walk(route, (1, 2))
        self.assertEqual(rt.logs, [])
        self.assertEqual(walker.call_args.kwargs["map_id"], "starsea")  # the name-only rule knows the map (finding 6)

    def test_a_stop_published_during_the_message_write_displays_nothing(self):
        # Route review round 3, finding 10: the stop lands inside the message file write of 「本趟 +5」 (and of the
        # walk's arrival): nothing is displayed. Without a stop it is written (no display text, marked as a report)
        # and then displayed.
        published = []
        rt = SimpleNamespace(stopped=False, logs=[])
        rt.log = lambda text, detail=False: rt.logs.append(text)
        run = crystal_route.Run(rt, reader=READER, world=FakeWorld(), io=HudIo())
        with reports(rt, during_write=lambda: published.append(True)), \
                patch("host.android_user_stop", side_effect=lambda *a, **k: bool(published)):
            run.report(4, 9)
        self.assertEqual(rt.logs, [])
        with reports(rt), patch("host.android_user_stop", return_value=False), \
                patch("message_log.write_message") as write:
            self.assertTrue(teleport.announce(rt, "星光结晶：路线前 4/30，路线后 9/30（本趟 +5）。"))
        write.assert_called_once_with("星光结晶：路线前 4/30，路线后 9/30（本趟 +5）。", None,
                                      report="星光结晶：路线前 4/30，路线后 9/30（本趟 +5）。")
        self.assertEqual(rt.logs, ["星光结晶：路线前 4/30，路线后 9/30（本趟 +5）。"])

    def test_the_card_and_the_press_belong_to_one_day(self):
        import datetime as dt
        near_reset = dt.datetime(2026, 11, 26, 20, 5, tzinfo=dt.timezone.utc)  # 04:05 UTC+8
        result, rt, calls, rings = route_run([Card("not_rung")], clock=[near_reset] * 5)
        self.assertFalse(result)
        self.assertEqual((rings, calls), ([], ["open", "region", "card"]))
        self.assertIn("太近", rt.logs[-1])
        next_day = dt.datetime(2026, 11, 26, 21, 0, tzinfo=dt.timezone.utc)  # the card read before 04:00, then 05:00
        result, rt, calls, rings = route_run([Card("not_rung"), RUNG4], clock=[SAFE_NOON, SAFE_NOON, next_day])
        self.assertFalse(result)
        self.assertEqual(rings, ["无界枢纽"])
        self.assertIn("已经过了每日 4 点重置", rt.logs[-1])

    def test_off_android_or_with_options_nothing_runs(self):
        rt = SimpleNamespace(stopped=False, log=Mock())
        with patch("host.game_backend", return_value="win32"), patch.object(crystal_route, "Run") as run:
            self.assertFalse(crystal_route.run(rt, {"kind": "android_crystal_route"}))
        run.assert_not_called()
        with patch("host.game_backend", return_value="android"), patch.object(crystal_route, "Run") as run:
            self.assertFalse(crystal_route.run(rt, {"kind": "android_crystal_route", "route": 5}))
        run.assert_not_called()

    def test_the_notice_says_route_3_always(self):
        self.assertIn("固定走 3 号路线", crystal_route.NOTICE)
        self.assertIn("收集得很少或没有收集", crystal_route.NOTICE)
        for words in ("阻止再按一次", "手动打开星海地图", "手动摇一次铃", "不会有任何损失"):
            self.assertIn(words, crystal_route.BELL_NOTE)
        _result, rt, _calls, _rings = route_run([RUNG4, RUNG9])
        self.assertEqual(rt.logs[:2], [crystal_route.NOTICE, crystal_route.BELL_NOTE])


DAY = __import__("datetime").date(2026, 11, 26)  # the game day of SAFE_NOON


class BellTests(unittest.TestCase):
    """Run.ring: one press, on HUD frames the walker accepts at the hub with the bell where it belongs, recorded first
    and kept unless the device input count shows nothing went down (review round 2, findings 1 and 2)."""

    def ring(self, kinds=(), regions=None, *, at_hub=True, bell_box=(593, 590, 47, 47), outcomes=(True,),
             record=None, clock=None, day=DAY, prompts=None, name_only=None, valid=None, fragments=None):
        from host import UnsupportedInput
        from host.android import gesture
        rt = SimpleNamespace(stopped=False, logs=[], pause=Mock(return_value=True), TOUCH_KEY_ATTEMPTS=3,
                             _frame_still_valid=Mock(side_effect=valid or (lambda frame: True)))
        rt.log = lambda text, detail=False: rt.logs.append(text)
        rt._recognizer_on = lambda frame: (lambda node, override=None: hit(list(bell_box))
                                           if node == teleport.HUD_BELL and bell_box else MISS)
        reader = Mock()
        reader.present.return_value = True
        reader.fix.return_value = SimpleNamespace(x=HUB["x"] + (0 if at_hub else 30), y=HUB["y"], reliable=True)
        record = record or crystal_route.BellRecord(tempfile.mkdtemp())
        io = HudIo(kinds=kinds, regions=regions, prompts=prompts, name_only=name_only, fragments=fragments)
        run = crystal_route.Run(rt, reader=reader, world=FakeWorld(), io=io, record=record)
        calls, states, outcomes = [], [], list(outcomes)
        device = SimpleNamespace(inputs=0)
        times = iter(clock) if clock is not None else None

        self.deadlines = []

        def press(rt_, frame, point, seconds, not_after=None):
            self.deadlines.append((not_after, frame.index))
            calls.append((point, seconds, frame.index))
            states.append((record.attempt(day) or {}).get("state"))  # what was recorded when it was handed over
            outcome = outcomes.pop(0)
            if outcome in ("expired", "unsupported", "stopped before down"):  # all refused before contact-down
                if outcome == "expired":
                    raise gesture.DecisionExpired("aged out")
                if outcome == "unsupported":
                    raise UnsupportedInput("superseded")
                return False
            device.inputs += 1  # past every refusal: the first contact goes down
            if outcome == "cleanup failed":  # the lift was confirmed, then releasing the reservation failed
                raise UnsupportedInput("touch marker could not be removed")
            if outcome == "raise":
                raise RuntimeError("unconfirmed")
            return outcome  # True (confirmed) or False (cut short by a stop after contact-down)
        with patch.object(gesture, "bell_step", side_effect=press), patch("host.device_input", return_value=device), \
                patch.object(crystal_route, "now_utc", side_effect=lambda: next(times) if times else SAFE_NOON), \
                patch("time.monotonic", side_effect=itertools.count(0, .1).__next__):
            try:
                result, error = run.ring(HUB, day), None
            except (Stop, pages.GuardRefusal, UnsupportedInput, RuntimeError) as caught:
                result, error = None, caught
        return result, calls, error, states, (record.attempt(day) or {}).get("state")

    def test_one_press_on_the_confirmed_bell_recorded_first(self):
        result, calls, error, states, final = self.ring()
        self.assertIsNone(error)
        self.assertTrue(result)
        self.assertEqual(calls, [([616, 613], 4.0, 1)])  # the confirming (second, latest) frame
        self.assertEqual((states, final), (["pressing"], "pressed"))

    def test_narration_is_waited_through_and_restarts_the_pair(self):
        result, calls, _error, _states, _final = self.ring(kinds=["narration", "narration"])
        self.assertTrue(result)
        self.assertEqual(len(calls), 1)
        # Review round 2, finding 13: narration on the confirming frame: decided again once it clears.
        result, calls, _error, _states, _final = self.ring(kinds=["hud", "narration", "hud", "hud"])
        self.assertTrue(result)
        self.assertEqual(calls[0][2], 3)  # the confirming frame of the second pair (frames 2 and 3)

    def test_only_a_definitely_unsent_press_is_cleared(self):
        result, calls, _error, states, final = self.ring(outcomes=("expired", True))
        self.assertTrue(result)
        self.assertEqual((len(calls), states, final), (2, ["pressing", "pressing"], "pressed"))
        _result, calls, error, _states, final = self.ring(outcomes=("unsupported",))
        self.assertIsNotNone(error)
        self.assertEqual((len(calls), final), (1, None))
        result, _calls, error, _states, final = self.ring(outcomes=("stopped before down",))
        self.assertEqual((result, error, final), (False, None, None))  # no "interrupted" record for nothing sent

    def test_a_press_that_went_down_stays_recorded_whatever_it_raised(self):
        _result, _calls, _error, _states, final = self.ring(outcomes=(False,))
        self.assertEqual(final, "interrupted")
        _result, _calls, error, _states, final = self.ring(outcomes=("raise",))
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(final, "pressing")
        _result, _calls, error, _states, final = self.ring(outcomes=("cleanup failed",))
        self.assertIsNotNone(error)
        self.assertEqual(final, "pressing")  # the device took the press: a restart must not press again

    def test_a_recorded_attempt_blocks_the_press(self):
        record = crystal_route.BellRecord(tempfile.mkdtemp())
        record.mark(DAY, "interrupted")
        _result, calls, error, _states, _final = self.ring(record=record)
        self.assertIsInstance(error, Stop)
        self.assertEqual(calls, [])

    def test_no_press_near_the_reset_or_on_another_day(self):
        import datetime as dt
        utc = dt.timezone.utc
        for when, words in ((dt.datetime(2026, 11, 26, 19, 55, tzinfo=utc), "太近"),   # 03:55 UTC+8 next day
                            (dt.datetime(2026, 11, 26, 20, 5, tzinfo=utc), "太近"),    # 04:05
                            (dt.datetime(2026, 11, 26, 21, 0, tzinfo=utc), "已经过了")):  # 05:00, the next day
            with self.subTest(when=when):
                _result, calls, error, _states, final = self.ring(clock=[when] * 5)
                self.assertIsInstance(error, Stop)
                self.assertIn(words, str(error))
                self.assertEqual((calls, final), ([], None))
        # the day turning between the check before the pair and the one after it
        _result, calls, error, _states, final = self.ring(
            clock=[SAFE_NOON, dt.datetime(2026, 11, 26, 20, 0, tzinfo=utc)])
        self.assertIsInstance(error, Stop)
        self.assertEqual((calls, final), ([], None))

    def test_the_press_must_go_down_before_the_exclusion_window(self):
        # Route review round 3, finding 6: checked at 03:49:59.900 UTC+8, the press's not_after is capped at the
        # window's start (0.1 s later), so 300 ms in the record and dispatch path refuse it before contact-down
        # (gesture.perform); decided again, the window has begun: stop, nothing pressed, no record.
        import datetime as dt
        utc = dt.timezone.utc
        almost = dt.datetime(2026, 11, 26, 19, 49, 59, 900000, tzinfo=utc)
        inside = dt.datetime(2026, 11, 26, 19, 50, 0, 200000, tzinfo=utc)
        _result, calls, error, _states, final = self.ring(clock=[almost, almost, inside], outcomes=("expired",),
                                                          day=dt.date(2026, 11, 26))
        self.assertIsInstance(error, Stop)
        self.assertIn("太近", str(error))
        self.assertEqual((len(calls), final), (1, None))
        not_after, _index = self.deadlines[0]
        self.assertLess(not_after, 1.2)  # far below the frame's own expiry (its capture + MAX_DECISION_AGE_S)

    def test_the_window_is_inclusive_at_full_precision(self):
        import datetime as dt
        from account_state import GAME_TIMEZONE as local
        for when, inside in ((dt.datetime(2026, 11, 27, 3, 50, 0, 700000, tzinfo=local), True),
                             (dt.datetime(2026, 11, 27, 3, 50, tzinfo=local), True),
                             (dt.datetime(2026, 11, 27, 4, 10, tzinfo=local), True),
                             (dt.datetime(2026, 11, 27, 3, 49, 59, 999999, tzinfo=local), False),
                             (dt.datetime(2026, 11, 27, 4, 10, 0, 1, tzinfo=local), False)):
            with self.subTest(when=when):
                self.assertEqual(crystal_route.reset_window(when)[0], inside)
        _inside, start = crystal_route.reset_window(dt.datetime(2026, 11, 27, 3, 49, 59, 900000, tzinfo=local))
        self.assertEqual(start, dt.datetime(2026, 11, 27, 3, 50, tzinfo=local))
        _inside, start = crystal_route.reset_window(dt.datetime(2026, 11, 27, 12, 0, tzinfo=local))
        self.assertEqual(start, dt.datetime(2026, 11, 28, 3, 50, tzinfo=local))

    def test_either_frame_with_a_void_focus_mark_restarts_the_pair(self):
        # Route review round 3, finding 7: the deciding frame's focus mark expired, the confirming one current:
        # the pair starts again; the press is decided on the next pair.
        result, calls, error, _states, final = self.ring(valid=lambda frame: frame.index != 0)
        self.assertIsNone(error)
        self.assertTrue(result)
        self.assertEqual([index for _point, _seconds, index in calls], [3])
        self.assertEqual(final, "pressed")

    def test_text_by_the_bell_blocks_the_press(self):
        # Route review round 3, finding 2: any text the noise filters dropped near the bell (a gesture contact).
        near = ((600, 560, 640, 580),)
        for fragments in ([near, ()], [(), near]):
            with self.subTest(fragments=fragments):
                _result, calls, error, _states, final = self.ring(fragments=fragments)
                self.assertIsInstance(error, Stop)
                self.assertEqual((calls, final), ([], None))
        result, calls, _error, _states, _final = self.ring(fragments=[((100, 100, 120, 120),)] * 2)
        self.assertTrue(result)

    def test_future_dated_records_are_refused_and_the_reserved_day_is_never_pruned(self):
        # Route review round 3, finding 8.
        import datetime as dt
        record = crystal_route.BellRecord(tempfile.mkdtemp())
        today = dt.date(2026, 11, 26)
        record.path.parent.mkdir(parents=True)
        later = {str(today + dt.timedelta(days=n)): {"state": "pressed", "at": "x"} for n in range(1, 15)}
        record.path.write_text(json.dumps({"schema_version": 1, "attempts": later}), encoding="utf-8")
        for call in (lambda: record.attempt(today), lambda: record.mark(today, "pressing")):
            with self.assertRaises(crystal_route.RecordError) as caught:
                call()
            self.assertIn("比今天还晚", str(caught.exception))
        self.assertEqual(json.loads(record.path.read_text(encoding="utf-8"))["attempts"], later)  # untouched
        earlier = {str(today - dt.timedelta(days=n)): {"state": "pressed", "at": "x"} for n in range(1, 31)}
        record.path.write_text(json.dumps({"schema_version": 1, "attempts": earlier}), encoding="utf-8")
        record.mark(today, "pressing")
        kept = json.loads(record.path.read_text(encoding="utf-8"))["attempts"]
        self.assertEqual(len(kept), crystal_route.RECORD_KEEP)
        self.assertEqual(record.attempt(today)["state"], "pressing")
        record.mark(today, "pressed")
        self.assertEqual(record.attempt(today)["state"], "pressed")

    def test_a_prompt_near_the_bell_blocks_the_press_one_elsewhere_does_not(self):
        # Phase B: hud_prompt (movement allowed, never tapped) is fine for the bell unless the prompt lies within
        # 40 px + the bell's radius on either frame; the route never taps a prompt.
        column, near = ((737, 320, 1040, 366),), ((560, 540, 600, 580),)
        result, calls, error, _states, _final = self.ring(kinds=["hud_prompt", "hud_prompt"],
                                                           prompts=[column, column])
        self.assertEqual((result, error, len(calls)), (True, None, 1))
        for prompts in ([near, near], [column, near]):
            with self.subTest(prompts=prompts):
                _result, calls, error, _states, final = self.ring(kinds=["hud_prompt", "hud_prompt"], prompts=prompts)
                self.assertIsInstance(error, Stop)
                self.assertEqual((calls, final), ([], None))

    def test_the_record_key_is_made_in_one_place(self):
        # Per-account records later (the UID read from the HUD): every read and write goes through record_key.
        record = crystal_route.BellRecord(tempfile.mkdtemp())
        with patch.object(crystal_route, "record_key", side_effect=lambda day: f"account-1/{day}"):
            record.mark(DAY, "pressed")
            self.assertEqual(record.attempt(DAY)["state"], "pressed")
            self.assertEqual(list(record.load()["attempts"]), [f"account-1/{DAY}"])
            record.clear(DAY)
            self.assertIsNone(record.attempt(DAY))

    def test_a_broken_record_blocks_the_press(self):
        record = crystal_route.BellRecord(tempfile.mkdtemp())
        record.path.parent.mkdir(parents=True)
        record.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(crystal_route.RecordError):
            self.ring(record=record)

    def test_refusals(self):
        # Review round 2, finding 7: the walker's rules on both frames (a misread before a clean frame, a prompt, a
        # quest directive in the tracker, a changed tracker or a distance appearing on the confirming frame), at the
        # hub, the bell in place.
        for case in ({"kinds": ["misread"]}, {"kinds": ["other"]}, {"kinds": ["hud", "other"]},
                     {"kinds": ["hud", "misread"]}, {"regions": [("前往目标",)]},
                     {"regions": [("登上鸣鸣列车",), ("别的任务",)]}, {"regions": [("登上鸣鸣列车",), ("登上鸣鸣列车", "#")]},
                     {"at_hub": False}, {"bell_box": None}, {"bell_box": (640, 590, 47, 47)},
                     # route review round 2, findings 6 and 7: an instruction or the talk prompt in the tracker, and
                     # the name-only evidence gone on the confirming frame
                     {"regions": [("交谈",)]}, {"regions": [("与大喵交谈",)]},
                     {"regions": [("登上鸣鸣列车",), ("登上鸣鸣列车",)], "name_only": [True, False]}):
            with self.subTest(case=case):
                _result, calls, error, _states, final = self.ring(**case)
                self.assertIsInstance(error, Stop)
                self.assertEqual((calls, final), ([], None))


class EntryTests(unittest.TestCase):
    ENTRY = "MaaNikki_AndroidCrystalRoute"

    def test_the_agent_allows_it_only_when_the_runner_names_it(self):
        import host.android as android
        self.assertIn(self.ENTRY, android.DEV_ENTRIES)
        verified, experimental, excluded = android.task_tiers()
        self.assertNotIn(self.ENTRY, set(verified) | set(experimental) | set(excluded))
        env = {k: v for k, v in os.environ.items() if k != android.DEV_ENTRIES_ENV}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(android.task_allowed(self.ENTRY))
        with patch.dict(os.environ, {**env, android.DEV_ENTRIES_ENV: self.ENTRY}, clear=True):
            self.assertTrue(android.task_allowed(self.ENTRY))
            self.assertFalse(android.task_allowed("MaaNikki_AndroidWalkProbe"))

    def test_no_interface_task_list_or_package_offers_it(self):
        for path in ("interface.json", "build/android/interface-overlay.json", "agent/host/android/tasks.json",
                     "scripts/fixtures/windows-package-files.txt"):
            self.assertNotIn(self.ENTRY, (ROOT / path).read_text(encoding="utf-8"), path)
        package = (ROOT / "scripts/fixtures/windows-package-files.txt").read_text(encoding="utf-8")
        for name in ("android_crystal_route", "android_teleport", "host/android/"):
            self.assertNotIn(name, package)

    def test_the_runner_runs_it_without_options(self):
        sys.path.insert(0, str(ROOT / "scripts" / "android"))
        import runner
        self.assertEqual(runner.dev_override(self.ENTRY, {}),
                         [{self.ENTRY: {"custom_action_param": {"kind": "android_crystal_route"}}}])
        with self.assertRaises(runner.RunnerError):
            runner.dev_override(self.ENTRY, {"route": "5"})
        self.assertIn(self.ENTRY, runner.RUNNER_DEV_ENTRIES)
        with self.assertRaises(runner.RunnerError):
            runner.task_override(runner.android_interface(), self.ENTRY, {})

    def test_the_runner_shows_only_display_text(self):
        # Review round 2, finding 12: rows without display text (a failure scene quoting OCR) are not printed.
        import datetime as dt
        sys.path.insert(0, str(ROOT / "scripts" / "android"))
        import runner
        since = dt.datetime.now().astimezone()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "debug" / "messages"
            folder.mkdir(parents=True)
            stamp = (since + dt.timedelta(seconds=1)).isoformat()
            rows_ = [{"timestamp": stamp, "message": "失败现场：读到「SENTINEL」", "display": None},
                     {"timestamp": stamp, "message": "detail", "display": None},
                     {"timestamp": stamp, "message": "星海结晶路线：本次未完成。", "display": "星海结晶路线：本次未完成。"},
                     # route review round 3, finding 13: a result written before its display (the walk probe's
                     # terminal report, teleport.announce) carries its report text and is printed
                     {"timestamp": stamp, "message": "走路探针：已到达目标位置。", "display": None,
                      "report": "走路探针：已到达目标位置。"}]
            (folder / "x.log").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows_), encoding="utf-8")
            self.assertEqual(runner.agent_messages(Path(tmp), since),
                             ["星海结晶路线：本次未完成。", "走路探针：已到达目标位置。"])

    def test_the_message_log_keeps_a_report_field_only_when_given(self):
        import message_log
        with tempfile.TemporaryDirectory() as tmp, patch.object(message_log, "_DIRECTORY", Path(tmp)):
            self.assertTrue(message_log.write_message("a", None))
            self.assertTrue(message_log.write_message("b", None, report="b"))
            self.assertTrue(message_log.write_message("c", "c"))
            entries = [json.loads(line) for path in Path(tmp).glob("*.log")
                       for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([("report" in e, e.get("report"), e["display"]) for e in entries],
                         [(False, None, None), (True, "b", None), (False, None, "c")])

    def test_gameplay_runs_it_only_for_its_entry_and_kind_and_only_on_android(self):
        from action import gameplay
        runtime = SimpleNamespace(stopped=False, log=Mock(), main=Mock())
        param = json.dumps({"kind": "android_crystal_route"})

        def run(entry, raw, backend="android"):
            argv = SimpleNamespace(custom_action_param=raw, task_detail=SimpleNamespace(entry=entry))
            with patch.object(gameplay, "Executors", return_value=runtime), \
                    patch("host.game_backend", return_value=backend), \
                    patch.object(crystal_route, "run", return_value=True) as route:
                return gameplay.GameplayAction.run.__wrapped__(None, None, argv), route
        result, route = run(self.ENTRY, param)
        self.assertTrue(result)
        route.assert_called_once()
        for entry, raw, backend in ((self.ENTRY, json.dumps({"kind": "main"}), "android"),
                                    ("MaaNikki_Mailbox", param, "android"), (self.ENTRY, param, "win32"),
                                    (self.ENTRY, param, "macos"),
                                    ("MaaNikki_AndroidWalkProbe", param, "android")):
            with self.subTest(entry=entry, raw=raw, backend=backend):
                result, route = run(entry, raw, backend)
                self.assertFalse(result)
                route.assert_not_called()
        runtime.main.assert_not_called()

    def test_the_nodes_and_templates(self):
        if not (ROOT / "resource_android").is_dir():
            self.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
            return
        nodes = json.loads((ROOT / "resource_android/pipeline/android_teleport.json").read_text(encoding="utf-8"))
        self.assertEqual(set(nodes), set(teleport.NODES))
        for node in nodes.values():
            for template in ([node["template"]] if isinstance(node.get("template"), str) else node.get("template", [])):
                self.assertTrue((ROOT / "resource_android/image" / template).is_file(), template)
        entry = json.loads((ROOT / "resource_android/pipeline/android_crystal_route.json").read_text(encoding="utf-8"))
        self.assertEqual(entry[self.ENTRY]["custom_action_param"], {"kind": "android_crystal_route"})
        import re
        for text in ("立即前往", "前往", "追踪", "导航", "未解锁", "购买"):
            self.assertTrue(re.search(nodes[teleport.MAP_WORDS]["expected"], text), text)
        for text in ("传送", "详情", "星海", "地图切换", "星梦群屿·主岛", "无界枢纽", "今日尚未进行召唤摇铃"):
            self.assertFalse(re.search(nodes[teleport.MAP_WORDS]["expected"], text), text)

    def test_the_route_and_its_points(self):
        from navigation.models import load_route
        route = load_route(ROOT / "resource", crystal_route.ROUTE)
        self.assertEqual((route.map_id, route.start_radius), ("starsea", 9.0))
        self.assertEqual(route.document["teleport"]["name"], MAIN["name"])
        from host.android.move import route_goals
        goals = route_goals(route)
        self.assertEqual([i for i, g in enumerate(goals) if g.jump], [13, 22, 25, 33, 37])
        self.assertEqual([(i, g.action, g.params) for i, g in enumerate(goals) if g.action],
                         [(38, "wait", {"seconds": 0.9})])
        with patch("app_paths.RESOURCE_ROOT", ROOT):
            self.assertEqual(crystal_route.checkpoint("无界枢纽")["x"], HUB["x"])


FRAMEWORK_CHECK = r"""
import json, sys
from pathlib import Path
from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.resource import Resource
from maa.tasker import Tasker

repo = Path(sys.argv[1])
sys.path.insert(0, str(repo / "agent"))
from host.android import actions, pages, teleport, touch_point
fixtures = repo / "scripts/fixtures/android"
exec(sys.argv[2])
frames = {name: compose(fixtures, names) for name, names in FRAMES.items()}
world = teleport.WorldMap(resource=repo / "resource")
current, results = {}, {}

class Rt:
    @staticmethod
    def _frame_still_valid(frame): return True

class Frame(CustomController):
    def connect(self): return True
    def request_uuid(self): return "android-teleport-test"
    def screencap(self): return current["frame"]
    def refuse(self, *args): return False
    start_app = stop_app = click = swipe = touch_down = touch_move = touch_up = refuse
    click_key = input_text = key_down = key_up = refuse

def tap(result):
    return touch_point(list(result.box)) if actions._hit(result) else None

class Judge(CustomAction):
    def run(self, context, argv):
        image = context.tasker.controller.post_screencap().wait().get()
        def recognize(node, override=None):
            return context.run_recognition(node, image, {node: override} if override else None)
        name = current["name"]
        if name.startswith("bell"):
            results[name] = {"bell": tap(recognize(teleport.HUD_BELL))}
            return CustomAction.RunResult(success=True)
        screen = actions.read_screen(recognize)
        out = {"kind": screen.kind, "title": screen.title, "state": teleport.map_state(Rt, screen, image, recognize)}
        if out["state"] in ("map", "panel"):
            card = teleport.card_state(recognize)
            out["region"] = teleport.region_name(recognize)
            out["card"] = [card.kind, card.crystals]
            title = recognize(teleport.REGION_NAME)
            out["title_tap"] = tap(title)
            out["title_guards"] = [pages._forbidden_near(recognize, list(title.box)),
                                   teleport.word_near(recognize, list(title.box))]
            out["words"] = [text for text, _box in teleport._rows(recognize(teleport.MAP_WORDS))]
        if out["state"] == "map":
            out["zoom_in"] = tap(recognize(teleport.ZOOM_IN))
            pose = world.locate(image)
            if pose.trusted:
                expected = teleport.pillar_expected(pose, 1433.676, 997.388)
                pillar = recognize(teleport.PILLAR, {"roi": teleport.pillar_window(expected)})
                out["pillar"] = tap(pillar)
                out["pillar_guards"] = pillar.box and [pages._forbidden_near(recognize, list(pillar.box)),
                                                      teleport.word_near(recognize, list(pillar.box))]
        if out["state"] == "menu":
            row = recognize(teleport.MENU_STARSEA)
            out["starsea"] = tap(row)
            out["header"] = actions._hit(row) and teleport.province_header(actions._box(row))
            out["collapse"] = tap(recognize(teleport.MENU_COLLAPSE))
        if out["state"] == "panel":
            panel = teleport.read_panel(recognize)
            out["panel"] = [panel.title, list(panel.buttons), list(panel.words), teleport.panel_ok(panel, "星梦群屿·主岛"),
                            teleport.panel_ok(panel, "无界枢纽")]
            out["teleport"] = touch_point(panel.teleport) if panel.teleport else None
            out["teleport_guards"] = [pages._forbidden_near(recognize, panel.teleport),
                                      teleport.word_near(recognize, panel.teleport)]
            out["close"] = tap(recognize(teleport.PANEL_CLOSE))
        results[name] = out
        return CustomAction.RunResult(success=True)

resource = Resource()
for bundle in (repo / "resource", repo / "resource_android"):
    assert resource.post_bundle(bundle).wait().succeeded, bundle
action = Judge()
assert resource.register_custom_action("judge", action)
controller = Frame()
assert controller.post_connection().wait().succeeded
tasker = Tasker()
assert tasker.bind(resource, controller)
entry = {"Judge": {"recognition": "DirectHit", "action": "Custom", "custom_action": "judge", "next": []}}
for name, frame in frames.items():
    current.update(name=name, frame=frame)
    tasker.post_task("Judge", entry).wait()
print(json.dumps(results, ensure_ascii=False))
"""


@unittest.skipUnless((ROOT / "maafw").exists(), "needs the MaaFramework runtime in ./maafw")
class FrameworkTests(unittest.TestCase):
    """The real nodes and OCR, both layers loaded, on frames composed from UID-free crops of the survey frames (in a
    fresh interpreter, as in test_android_daily.py)."""

    def test_the_map_screens_targets_and_guards(self):
        if not (ROOT / "resource_android").is_dir():
            self.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
            return
        env = {**os.environ, "MAAFW_BINARY_PATH": str(ROOT / "maafw")}
        # -X utf8: the child prints Chinese text, and -I ignores PYTHONUTF8 (Windows consoles default to cp1252).
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", "-c", FRAMEWORK_CHECK, str(ROOT), COMPOSE],
                                capture_output=True, text=True, encoding="utf-8", timeout=300, env=env,
                                cwd=tempfile.gettempdir())
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        out = json.loads(result.stdout.strip().splitlines()[-1])
        none2 = [None, None]
        for name in ("xinghai default", "xinghai max"):
            with self.subTest(frame=name):
                value = out[name]
                self.assertEqual((value["kind"], value["title"], value["state"], value["region"], value["card"]),
                                 ("page", "地图", "map", "星海", ["not_rung", None]))
                self.assertEqual((value["title_tap"], value["title_guards"]), ([1094, 98], none2))
                self.assertEqual(value["zoom_in"], [365, 668])
                self.assertEqual((value["pillar"], value["pillar_guards"]), ([725, 320], none2))
                self.assertEqual(value["words"], ["快前往流转之枢吧"])  # 57 px below the title: outside the margin
        region = out["region"]
        self.assertEqual((region["state"], region["region"], region["card"][0]), ("map", "纪念山地", "unknown"))
        self.assertEqual(region["title_guards"], none2)
        expanded, collapsed = out["menu expanded"], out["menu collapsed"]
        self.assertEqual((expanded["state"], expanded["starsea"], expanded["collapse"]), ("menu", None, [1218, 115]))
        self.assertEqual((collapsed["state"], collapsed["starsea"], collapsed["header"], collapsed["collapse"]),
                         ("menu", [916, 115], True, None))
        panel = out["panel"]
        self.assertEqual(panel["state"], "panel")
        self.assertEqual(panel["panel"], ["星梦群屿·主岛", ["传送", "详情"], [], True, False])
        self.assertEqual((panel["teleport"], panel["teleport_guards"], panel["close"]),
                         ([1034, 658], none2, [1092, 491]))
        self.assertEqual((out["bell purple"]["bell"], out["bell waves"]["bell"]), ([616, 613], [616, 613]))


if __name__ == "__main__":
    unittest.main()
