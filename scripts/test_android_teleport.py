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
        good = teleport.Panel("星梦群屿·主岛", ("传送", "详情"), (), [1000, 637, 70, 40], [766, 639, 74, 39])
        self.assertTrue(teleport.panel_ok(good, "星梦群屿·主岛"))
        self.assertFalse(teleport.panel_ok(good, "无界枢纽"))
        for change in ({"buttons": ("传送", "立即前往", "详情")}, {"buttons": ("OK", "传送", "详情")},
                       {"buttons": ("传送",)}, {"words": ("立即前往",)}, {"teleport": None}, {"detail": None},
                       {"detail": [990, 637, 20, 40]}):
            with self.subTest(change=change):
                self.assertFalse(teleport.panel_ok(teleport.Panel(**{**good.__dict__, **change}), "星梦群屿·主岛"))

    def test_province_headers(self):
        self.assertTrue(teleport.province_header([888, 100, 57, 32]))
        self.assertFalse(teleport.province_header([841, 161, 93, 32]))  # a region row (纪念山地)

    def test_a_clean_hud_frame_to_touch(self):
        reader = SimpleNamespace(present=lambda frame: frame != "no minimap")

        def obs(kind="hud", regions=("登上鸣鸣列车",), frame="f", blocks=(("labels", 600, 1270, 75, False),)):
            return move.Observation(frame, kind, 0.0, "提示" if kind != "hud" else "", regions=regions, blocks=blocks)
        first = obs()
        self.assertIsNone(teleport.hud_unclean(first, reader))
        self.assertIsNone(teleport.hud_unclean(obs(regions=("登上鸣鸣列车", "#")), reader))  # with its distance
        self.assertIsNone(teleport.hud_unclean(obs(regions=("登上鸣鸣列车", "#")), reader, first))
        for case, against in ((obs("other"), None), (obs("narration"), None), (obs(frame="no minimap"), None),
                              (obs(regions=()), None), (obs(regions=("登上鸣鸣列车", "点击继续")), None),
                              (obs(regions=("前往目标",)), None), (obs(regions=("登上鸣鸣列车", "#", "#")), None),
                              (obs(regions=("别的任务",)), first),
                              (obs(blocks=(("labels", 600, 1270, 75, False), ("hearts", 300, 400, 680, False))), first)):
            with self.subTest(case=case.regions, kind=case.kind, against=against is not None):
                self.assertIsNotNone(teleport.hud_unclean(case, reader, against))


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


def stub_classify(screen, recognize):
    if screen.reason == "prompt":
        return move.Verdict("other", "大世界以外的文字（可能是提示、横幅、任务或交互按钮）")
    return move.Verdict("hud", "", ("登上鸣鸣列车",))


class Game:
    """The map screens of one teleport, reacting to the taps and swipes the real flow sends."""

    def __init__(self, *, where="hud", region="星海", zoom="below", view=(1230.5, 987.4), collapses_needed=1,
                 panel_title=None, buttons=("详情", "传送"), card=NOT_RUNG, title="地图",
                 after_teleport=("loading", "loading", "hud"), menu_has_chevron=True, pillar_shift=(0, 0),
                 region_text=None, back_word=False):
        self.where, self.region, self.zoom, self.view = where, region, zoom, view
        self.collapses_needed, self.collapses = collapses_needed, 0
        self.panel_title, self.buttons, self.card, self.title = panel_title, buttons, card, title
        self.after_teleport, self.menu_has_chevron = list(after_teleport), menu_has_chevron
        self.pillar_shift, self.region_text, self.back_word = pillar_shift, region_text, back_word
        self.taps, self.queue, self.calls = [], [], []
        self.shown_title = None

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
                    return hit(TELEPORT_BOX, "传送")
                if node == teleport.MAP_WORDS and "立即前往" in self.buttons:
                    return rows(("立即前往", BUTTON_BOXES["立即前往"]))
            if where == "loading text" and node == move.SCREEN_TEXT:
                return rows(("温馨提示文字", [400, 650, 300, 30]))
            return MISS
        return recognize

    def screen_now(self, rt):
        where = self.queue.pop(0) if self.queue else self.where
        if where == "panel lingering":
            where, self.shown_title = "panel", MAIN["name"]
        if where == "wrong panel":
            where, self.shown_title = "panel", "星梦群屿·海滩"
        frame = Frame(view=self.view, zoom=None if where == "panel" else self.zoom, where=where)
        if where in ("hud", "prompt"):
            return (PROMPT_HUD if where == "prompt" else HUD), frame, lambda node, override=None: MISS
        if where in ("loading", "loading text"):
            return LOADING, frame, self.recognize_for(where)
        if where == "other":
            return Screen("unknown", reason="画面上有弹窗"), frame, \
                lambda node, override=None: hit([900, 100, 40, 40]) if node == actions.MODAL_CLOSE else MISS
        return Screen("page", {"back": BACK}, title=self.title), frame, self.recognize_for(where)

    def action(self, name, target=None, begin=None, end=None, duration=None, source_frame=None, _movement=False):
        self.calls.append({"name": name, "movement": _movement, "source": source_frame})
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

    def __init__(self, kinds=(), regions=None):
        self.kinds, self.regions = list(kinds), list(regions or [])
        self.frames = []

    def observe(self):
        import time
        kind = self.kinds.pop(0) if self.kinds else "hud"
        region = self.regions.pop(0) if self.regions else ("登上鸣鸣列车",)
        frame = Frame(view=None, zoom=None, where="hud", index=len(self.frames))
        self.frames.append(frame)
        return move.Observation(frame, kind, time.monotonic(), "提示" if kind != "hud" else "", regions=region,
                                blocks=(("labels", 600, 1270, 75, False),))


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

    def open_map(self):
        self.calls.append("open")
        return True

    def region(self, name="星海"):
        self.calls.append("region")
        return True

    def card(self):
        self.calls.append("card")
        card = self.cards.pop(0)
        if isinstance(card, Exception):
            raise card
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


def route_run(cards, *, outcome="arrived", ring=True, record=None, leave=True, user_stop=False):
    rt = SimpleNamespace(stopped=False, logs=[], _frame_still_valid=Mock(return_value=True),
                         pause=Mock(return_value=True), diagnostic=Mock(), TOUCH_KEY_ATTEMPTS=3)
    rt.log = lambda text, detail=False: rt.logs.append(text)
    record = record or crystal_route.BellRecord(tempfile.mkdtemp())
    run = crystal_route.Run(rt, reader=READER, world=FakeWorld(), io=HudIo(), record=record)
    run.map = FakeMap(cards, leave=leave)
    rings = []

    def fake_ring(self, hub):
        rings.append(hub["name"])
        from account_state import game_day
        record.mark(game_day(), "pressed")
        return ring
    walk = SimpleNamespace(outcome=outcome, ok=outcome == "arrived", message="已到达目标位置。", attempted=3, confirmed=3)
    with patch.object(crystal_route.Run, "ring", fake_ring), \
            patch.object(crystal_route.Run, "walk", return_value=walk), \
            patch("host.game_backend", return_value="android"), \
            patch("host.android_user_stop", return_value=user_stop), \
            patch.object(crystal_route, "Run", side_effect=lambda rt_: run):
        result = crystal_route.run(rt, {"kind": "android_crystal_route"})
    return result, rt, run.map.calls, rings


RUNG4 = Card("rung", 4, ((3, 0), (30, 4), (100, 0)))
RUNG9 = Card("rung", 9, ((3, 1), (30, 9), (100, 3)))


class RouteTests(unittest.TestCase):
    def test_not_rung_hub_bell_then_the_route(self):
        result, rt, calls, rings = route_run([Card("not_rung"), RUNG4, RUNG9])
        self.assertTrue(result, rt.logs)
        self.assertEqual(rings, ["无界枢纽"])
        self.assertEqual(calls, ["open", "region", "card", "transport 无界枢纽", "await", "open", "region", "card",
                                 "transport 星梦群屿·主岛", "open", "region", "card", "leave"])
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


class BellTests(unittest.TestCase):
    """Run.ring: one press, on clean HUD frames at the hub with the bell where it belongs, recorded first."""

    def ring(self, kinds=(), regions=None, *, at_hub=True, bell_box=(593, 590, 47, 47), outcomes=(True,),
             record=None):
        from host import UnsupportedInput
        from host.android import gesture
        rt = SimpleNamespace(stopped=False, logs=[], pause=Mock(return_value=True), TOUCH_KEY_ATTEMPTS=3,
                             _frame_still_valid=Mock(return_value=True))
        rt.log = lambda text, detail=False: rt.logs.append(text)
        rt._recognizer_on = lambda frame: (lambda node, override=None: hit(list(bell_box))
                                           if node == teleport.HUD_BELL and bell_box else MISS)
        reader = Mock()
        reader.present.return_value = True
        reader.fix.return_value = SimpleNamespace(x=HUB["x"] + (0 if at_hub else 30), y=HUB["y"], reliable=True)
        record = record or crystal_route.BellRecord(tempfile.mkdtemp())
        io = HudIo(kinds=kinds, regions=regions)
        run = crystal_route.Run(rt, reader=reader, world=FakeWorld(), io=io, record=record)
        calls, states, outcomes = [], [], list(outcomes)
        from account_state import game_day

        def press(rt_, frame, point, seconds, not_after=None):
            calls.append((point, seconds, frame.index))
            states.append((record.attempt(game_day()) or {}).get("state"))  # what was recorded at contact-down
            outcome = outcomes.pop(0)
            if outcome == "expired":
                raise gesture.DecisionExpired("aged out")
            if outcome == "unsupported":
                raise UnsupportedInput("superseded")
            if outcome == "raise":
                raise RuntimeError("unconfirmed")
            return outcome
        with patch.object(gesture, "bell_step", side_effect=press), \
                patch("time.monotonic", side_effect=itertools.count(0, .1).__next__):
            try:
                return run.ring(HUB), calls, None, states, (record.attempt(game_day()) or {}).get("state")
            except (Stop, pages.GuardRefusal, UnsupportedInput, RuntimeError) as error:
                return None, calls, error, states, (record.attempt(game_day()) or {}).get("state")

    def test_one_press_on_the_confirmed_bell_recorded_first(self):
        result, calls, error, states, final = self.ring()
        self.assertIsNone(error)
        self.assertTrue(result)
        self.assertEqual(calls, [([616, 613], 4.0, 1)])  # the confirming (second, latest) frame
        self.assertEqual((states, final), (["pressing"], "pressed"))

    def test_narration_is_waited_through(self):
        result, calls, _error, _states, _final = self.ring(kinds=["narration", "narration"])
        self.assertTrue(result)
        self.assertEqual(len(calls), 1)

    def test_a_refusal_before_contact_down_is_rolled_back_and_decided_again(self):
        result, calls, _error, states, final = self.ring(outcomes=("expired", True))
        self.assertTrue(result)
        self.assertEqual((len(calls), states, final), (2, ["pressing", "pressing"], "pressed"))
        _result, calls, error, _states, final = self.ring(outcomes=("unsupported",))
        self.assertIsNotNone(error)
        self.assertEqual((len(calls), final), (1, None))

    def test_an_interrupted_or_unconfirmed_press_stays_recorded(self):
        _result, _calls, _error, _states, final = self.ring(outcomes=(False,))
        self.assertEqual(final, "interrupted")
        _result, _calls, error, _states, final = self.ring(outcomes=("raise",))
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(final, "pressing")

    def test_a_recorded_attempt_blocks_the_press(self):
        from account_state import game_day
        record = crystal_route.BellRecord(tempfile.mkdtemp())
        record.mark(game_day(), "interrupted")
        _result, calls, error, _states, _final = self.ring(record=record)
        self.assertIsInstance(error, Stop)
        self.assertEqual(calls, [])

    def test_a_broken_record_blocks_the_press(self):
        record = crystal_route.BellRecord(tempfile.mkdtemp())
        record.path.parent.mkdir(parents=True)
        record.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(crystal_route.RecordError):
            self.ring(record=record)

    def test_refusals(self):
        # Review round 1, finding 3: the walker's clean-HUD checks on both frames (a prompt, a quest directive in
        # the tracker, a changed tracker on the confirming frame), at the hub, the bell in place.
        for case in ({"kinds": ["other"]}, {"kinds": ["hud", "other"]}, {"regions": [("前往目标",)]},
                     {"regions": [("登上鸣鸣列车",), ("别的任务",)]}, {"at_hub": False}, {"bell_box": None},
                     {"bell_box": (640, 590, 47, 47)}):
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
                         ([1034, 656], none2, [1092, 491]))
        self.assertEqual((out["bell purple"]["bell"], out["bell waves"]["bell"]), ([616, 613], [616, 613]))


if __name__ == "__main__":
    unittest.main()
