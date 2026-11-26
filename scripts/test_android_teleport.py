"""Android big-map teleport and the 星海 crystal route entry (agent/host/android/teleport.py, crystal_route.py):
python scripts/test_android_teleport.py

Runs on any OS without an emulator. The map localisation, the blank-terrain check and the drag planning run on a crop of
a real max-zoom map frame (scripts/fixtures/android/map_terrain_max.jpg, from .build/android/map-survey 05b); the flows
run against scripted screens reacting to the taps and swipes the real pages helpers send; the nodes and OCR run through
the real MaaFramework (both resource layers) on frames composed from UID-free crops of the survey frames.
scripts/android/teleport_frames.py repeats the recognition and decisions on the full saved frames (local only).
"""
from __future__ import annotations

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

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from host.android import actions, crystal_route, pages, teleport  # noqa: E402
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


def world():
    return teleport.WorldMap(resource=ROOT / "resource")


WORLD = world()


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
        # the default zoom's scale on a max-zoom frame: no trusted pose
        self.assertFalse(WORLD.locate(frame, 0.676).trusted)
        # a frame with no terrain: no trusted pose
        self.assertFalse(WORLD.locate(np.full((720, 1280, 3), 90, np.uint8)).trusted)

    def test_blank_terrain_and_the_drag(self):
        frame = compose(FIXTURES, FRAMES["xinghai max"])
        pose = WORLD.locate(frame)
        residual = WORLD.residual(frame, pose)
        self.assertTrue(teleport.blank_at(residual, (350, 400)))  # open sea
        for point in ((725, 321), (850, 511 - 5), (230, 400), (1000, 300)):  # pillar, label, outside the zone
            self.assertFalse(teleport.blank_at(residual, point), point)
        plan = teleport.plan_drag(residual, pose.to_screen(HUB["x"], HUB["y"]))
        self.assertEqual(plan, ((320, 320), (880, 282)))
        self.assertTrue(all(teleport.blank_at(residual, p) for p in plan))
        self.assertIsNone(teleport.plan_drag(np.full((720, 1280), 255, np.float32), (100, 300)))
        # a drag never asks more than DRAG_MAX per axis
        begin, end = teleport.plan_drag(np.zeros((720, 1280), np.float32), (-2000, 300))
        self.assertLessEqual(end[0] - begin[0], teleport.DRAG_MAX[0])

    def test_zoom_bar(self):
        for name, level in (("xinghai default", "below"), ("xinghai max", "max"), ("panel", None)):
            with self.subTest(frame=name):
                self.assertEqual(teleport.zoom_state(compose(FIXTURES, FRAMES[name])), level)

    def test_the_card_is_read_positively_both_ways(self):
        self.assertEqual(card_of("今日尚未进行召唤摇铃", "快前往流转之枢吧").kind, "not_rung")
        rung = card_of("1/3", "12/30", "40/100")
        self.assertEqual((rung.kind, rung.crystals), ("rung", 12))
        self.assertEqual(card_of("今日已进行召唤摇铃").kind, "rung")
        self.assertEqual(card_of("12 / 30", "0/3").crystals, 12)
        for texts in ((), ("7/8", "6/31"), ("12/30",), ("12/30", "1/3", "新的提示文字"), ("718", "12/30"),
                      ("40/30", "1/3"), ("今日尚未进行召唤摇铃", "今日已进行召唤摇铃"), ("您",)):
            with self.subTest(texts=texts):
                self.assertEqual(card_of(*texts).kind, "unknown")
        self.assertEqual(teleport.card_state(lambda node, override=None: None).kind, "unknown")

    def test_panel_names_and_buttons(self):
        self.assertEqual(teleport.point_name("星梦群屿 · 主岛"), "星梦群屿·主岛")
        self.assertEqual(teleport.point_name("星梦群屿・主岛"), "星梦群屿·主岛")
        good = teleport.Panel("星梦群屿·主岛", ("传送", "详情"), (), [1000, 637, 70, 40], [766, 639, 74, 39])
        self.assertTrue(teleport.panel_ok(good, "星梦群屿·主岛"))
        self.assertFalse(teleport.panel_ok(good, "无界枢纽"))
        for change in ({"buttons": ("传送", "立即前往", "详情")}, {"buttons": ("传送",)}, {"words": ("立即前往",)},
                       {"teleport": None}, {"detail": None}, {"detail": [990, 637, 20, 40]}):
            with self.subTest(change=change):
                self.assertFalse(teleport.panel_ok(teleport.Panel(**{**good.__dict__, **change}), "星梦群屿·主岛"))

    def test_province_headers(self):
        self.assertTrue(teleport.province_header([888, 100, 57, 32]))
        self.assertFalse(teleport.province_header([841, 161, 93, 32]))  # a region row (纪念山地)


# ---- the scripted map ----

BACK = [37, 22, 40, 40]
TITLE_BOX = [1070, 84, 50, 27]
COLLAPSE_BOX = [1205, 105, 27, 21]
STARSEA_BOX = [888, 100, 57, 32]
ZOOM_BOX = [356, 659, 18, 18]
CLOSE_BOX = [1067, 466, 51, 51]
TELEPORT_BOX = [1000, 637, 70, 40]
DETAIL_BOX = [766, 639, 74, 39]
HUD = Screen("hud", {"duck": [30, 18, 54, 54]})
MAP = Screen("page", {"back": BACK}, title="地图")
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


class Game:
    """The map screens of one teleport, reacting to the taps and swipes the real pages helpers send."""

    def __init__(self, *, where="hud", region="星海", zoom="below", view=(1230.5, 987.4), collapsed=False,
                 panel_title=None, buttons=("详情", "传送"), card=("今日尚未进行召唤摇铃",),
                 after_teleport=("loading", "loading", "hud"), menu_has_chevron=True, pillar_shift=(0, 0)):
        self.where, self.region, self.zoom, self.view = where, region, zoom, view
        self.collapsed, self.panel_title, self.buttons, self.card = collapsed, panel_title, buttons, card
        self.after_teleport, self.menu_has_chevron = list(after_teleport), menu_has_chevron
        self.pillar_shift = pillar_shift
        self.taps, self.queue = [], []
        self.target = MAIN

    def pillar_box(self, checkpoint):
        pose = Pose(self.view[0], self.view[1], .98, .7)
        x, y = teleport.pillar_expected(pose, checkpoint["x"], checkpoint["y"])
        x, y = x + self.pillar_shift[0], y + self.pillar_shift[1]
        return [round(x) - 14, round(y) - 20, 29, 40]

    def recognize_for(self, where):
        def recognize(node, override=None):
            if node == actions.BACK_ARROW:
                return hit(BACK)
            if where in ("map", "panel"):
                if node == teleport.REGION_NAME:
                    return hit(TITLE_BOX, self.region)
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
                    return hit(COLLAPSE_BOX)
            if where == "panel":
                if node == teleport.PANEL_CLOSE:
                    return hit(CLOSE_BOX)
                if node == teleport.PANEL_TITLE:
                    return rows((self.shown_title, [177, 481, 177, 28]))
                if node == teleport.PANEL_BUTTONS:
                    boxes = {"详情": DETAIL_BOX, "传送": TELEPORT_BOX, "立即前往": [1000, 637, 140, 40]}
                    return rows(*[(b, boxes.get(b, [500, 640, 60, 30])) for b in self.buttons])
                if node == teleport.TELEPORT and "传送" in self.buttons:
                    return hit(TELEPORT_BOX, "传送")
                if node == teleport.MAP_WORDS and "立即前往" in self.buttons:
                    return rows(("立即前往", [1000, 637, 140, 40]))
            return MISS
        return recognize

    def screen_now(self, rt):
        where = self.queue.pop(0) if self.queue else self.where
        frame = Frame(view=self.view, zoom=None if where == "panel" else self.zoom, where=where)
        if where == "hud":
            return HUD, frame, lambda node, override=None: MISS
        if where == "loading":
            return LOADING, frame, lambda node, override=None: MISS
        if where == "other":
            return Screen("unknown", reason="画面上有弹窗"), frame, \
                lambda node, override=None: hit([900, 100, 40, 40]) if node == actions.MODAL_CLOSE else MISS
        return MAP, frame, self.recognize_for(where)

    def action(self, name, target=None, begin=None, end=None, duration=None, source_frame=None):
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
        elif target == COLLAPSE_BOX:
            self.taps.append("collapse")
            self.collapsed = True
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


class CleanIo:
    """move.RuntimeIo stand-in: every observation a clean HUD."""

    def __init__(self, rt=None, kinds=None):
        self.kinds = list(kinds or [])

    def observe(self):
        from host.android.move import Observation
        kind = self.kinds.pop(0) if self.kinds else "hud"
        return Observation(Frame(view=None, zoom=None, where="hud"), kind, 0.0, "旁白" if kind != "hud" else "")


def scripted_rt(game):
    rt = SimpleNamespace(stopped=False, logs=[], _frame_still_valid=Mock(return_value=True),
                         pause=Mock(return_value=True), diagnostic=Mock(), TOUCH_KEY_ATTEMPTS=3, action=game.action)
    rt.log = lambda text, detail=False: rt.logs.append(text)
    return rt


def patched(game):
    import contextlib
    stack = contextlib.ExitStack()
    stack.enter_context(patch.object(pages, "screen_now", side_effect=game.screen_now))
    stack.enter_context(patch.object(teleport, "zoom_state", side_effect=lambda frame: frame.zoom))
    stack.enter_context(patch.object(teleport, "zoom_right",
                                     side_effect=lambda frame: {"max": 333, "below": 248}.get(frame.zoom)))
    stack.enter_context(patch("host.android.move.RuntimeIo", CleanIo))
    stack.enter_context(patch("time.monotonic", side_effect=itertools.count(0, .25).__next__))
    return stack


def transport(game, checkpoint=MAIN, world_=None):
    rt = scripted_rt(game)
    with patched(game):
        tp = teleport.Teleport(rt, world_ or FakeWorld())
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

    def test_from_another_region_through_the_menu_collapsing_the_province_first(self):
        game = Game(region="纪念山地")
        result, _rt, error = transport(game)
        self.assertIsNone(error)
        self.assertEqual(game.taps[:5], ["minimap", "card title", "collapse", "starsea", "zoom"])
        self.assertEqual(game.taps[-2:], ["pillar main", "teleport"])

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

    def test_another_title_closes_the_panel_and_stops_without_teleporting(self):
        game = Game(panel_title="星梦群屿·海滩")
        result, rt, error = transport(game)
        self.assertIsInstance(error, Stop)
        self.assertEqual(game.taps[-2:], ["pillar main", "close"])
        self.assertNotIn("teleport", game.taps)
        self.assertIn("已经关闭详情，没有传送", str(error))

    def test_another_button_closes_the_panel_and_never_taps_it(self):
        for buttons in (("详情", "传送", "立即前往"), ("详情", "追踪"), ("立即前往",)):
            with self.subTest(buttons=buttons):
                game = Game(buttons=buttons)
                _result, _rt, error = transport(game)
                self.assertIsInstance(error, Stop)
                self.assertEqual(game.taps[-1], "close")
                self.assertNotIn("teleport", game.taps)

    def test_the_teleport_tap_is_never_repeated(self):
        game = Game(after_teleport=["map"] * 200)
        _result, _rt, error = transport(game)
        self.assertIsInstance(error, Stop)
        self.assertEqual(game.taps.count("teleport"), 1)
        self.assertEqual(game.taps[-1], "teleport")

    def test_an_unexpected_screen_after_teleport_stops_with_its_frame(self):
        game = Game(after_teleport=["loading", "other", "hud"])
        _result, _rt, error = transport(game)
        self.assertIsInstance(error, Stop)
        self.assertEqual(error.frames[0].where, "other")
        self.assertEqual(game.taps[-1], "teleport")

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

    def test_an_unclean_hud_after_arrival_stops(self):
        game = Game(zoom="max", view=(1394.5, 1010.4))
        rt = scripted_rt(game)
        with patched(game), patch("host.android.move.RuntimeIo", lambda rt_: CleanIo(kinds=["other"] * 400)):
            with self.assertRaises(Stop):
                teleport.Teleport(rt, FakeWorld()).transport(MAIN)
        self.assertEqual(game.taps[-1], "teleport")

    def test_the_card_needs_two_agreeing_frames_and_stops_when_unreadable(self):
        for card, kind in ((("今日尚未进行召唤摇铃",), "not_rung"), (("1/3", "5/30", "9/100"), "rung")):
            with self.subTest(card=card):
                game = Game(where="map", card=card)
                rt = scripted_rt(game)
                with patched(game):
                    self.assertEqual(teleport.Teleport(rt, FakeWorld()).card().kind, kind)
        game = Game(where="map", card=("12/30",))
        rt = scripted_rt(game)
        with patched(game), self.assertRaises(Stop):
            teleport.Teleport(rt, FakeWorld()).card()
        self.assertEqual(game.taps, [])

    def test_leaving_the_map(self):
        game = Game(where="map")
        rt = scripted_rt(game)
        with patched(game):
            self.assertTrue(teleport.Teleport(rt, FakeWorld()).leave())
        self.assertEqual(game.taps, ["back"])


# ---- the route entry ----

class FakeMap:
    def __init__(self, cards, transports=None):
        self.cards, self.calls, self.card_frame = list(cards), [], "card frame"
        self.transports = transports

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

    def leave(self):
        self.calls.append("leave")
        return True


def route_run(cards, *, outcome="arrived", ring=True):
    rt = SimpleNamespace(stopped=False, logs=[], _frame_still_valid=Mock(return_value=True),
                         pause=Mock(return_value=True), diagnostic=Mock(), TOUCH_KEY_ATTEMPTS=3)
    rt.log = lambda text, detail=False: rt.logs.append(text)
    run = crystal_route.Run(rt, reader=Mock(), world=FakeWorld(), io=CleanIo())
    run.map = FakeMap(cards)
    rings = []
    walk = SimpleNamespace(outcome=outcome, ok=outcome == "arrived", message="已到达目标位置。", attempted=3, confirmed=3)
    with patch.object(pages, "screen_now", return_value=(HUD, Frame(where="hud"), lambda n, o=None: MISS)), \
            patch.object(crystal_route.Run, "ring", side_effect=lambda hub: rings.append(hub["name"]) or ring), \
            patch.object(crystal_route.Run, "walk", return_value=walk), \
            patch("host.game_backend", return_value="android"), \
            patch("host.android_user_stop", return_value=False), \
            patch.object(crystal_route, "Run", side_effect=lambda rt_: run):
        result = crystal_route.run(rt, {"kind": "android_crystal_route"})
    return result, rt, run.map.calls, rings


class RouteTests(unittest.TestCase):
    def test_not_rung_hub_bell_then_the_route(self):
        result, rt, calls, rings = route_run([Card("not_rung"), Card("rung", 4, ((3, 0), (30, 4))),
                                              Card("rung", 9, ((3, 1), (30, 9)))])
        self.assertTrue(result, rt.logs)
        self.assertEqual(rings, ["无界枢纽"])
        self.assertEqual(calls, ["open", "region", "card", "transport 无界枢纽", "open", "region", "card",
                                 "transport 星梦群屿·主岛", "open", "region", "card", "leave"])
        self.assertIn(crystal_route.NOTICE, rt.logs)
        self.assertIn("星光结晶：路线前 4/30，路线后 9/30（本趟 +5）。", rt.logs)

    def test_already_rung_skips_the_hub_and_the_bell(self):
        result, _rt, calls, rings = route_run([Card("rung", 4, ((3, 0), (30, 4))), Card("rung", 4, ((3, 0), (30, 4)))])
        self.assertTrue(result)
        self.assertEqual(rings, [])
        self.assertNotIn("transport 无界枢纽", calls)

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
        result, rt, calls, _rings = route_run([Card("rung", 4, ((3, 0), (30, 4)))], outcome="screen")
        self.assertFalse(result)
        self.assertEqual(calls[-1], "transport 星梦群屿·主岛")
        self.assertIn("没有读到路线前后的星光结晶数量，收集情况没有看到。", rt.logs)

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


class BellTests(unittest.TestCase):
    """Run.ring: one press, on a clean HUD at the hub with the bell where it belongs, confirmed on a second frame."""

    def ring(self, kinds=(), *, at_hub=True, bell_box=(593, 590, 47, 47), expire=0, sent=True):
        from host.android import gesture
        rt = SimpleNamespace(stopped=False, logs=[], pause=Mock(return_value=True), TOUCH_KEY_ATTEMPTS=3)
        rt.log = lambda text, detail=False: rt.logs.append(text)
        rt._recognizer_on = lambda frame: (lambda node, override=None: hit(list(bell_box))
                                           if node == teleport.HUD_BELL and bell_box else MISS)
        reader = Mock()
        reader.present.return_value = True
        reader.fix.return_value = SimpleNamespace(x=HUB["x"] + (0 if at_hub else 30), y=HUB["y"], reliable=True)
        run = crystal_route.Run(rt, reader=reader, world=FakeWorld(), io=CleanIo(kinds=kinds))
        calls = []

        def press(rt_, frame, point, seconds, not_after=None):
            calls.append((point, seconds))
            if len(calls) <= expire:
                raise gesture.DecisionExpired("aged out")
            return sent
        with patch.object(gesture, "bell_step", side_effect=press), patch("time.monotonic", return_value=0.0):
            try:
                return run.ring(HUB), calls, None
            except (Stop, pages.GuardRefusal) as error:
                return None, calls, error

    def test_one_press_on_the_confirmed_bell(self):
        result, calls, error = self.ring()
        self.assertIsNone(error)
        self.assertTrue(result)
        self.assertEqual(calls, [([616, 613], 4.0)])

    def test_narration_is_waited_through(self):
        result, calls, _error = self.ring(kinds=["narration", "narration"])
        self.assertTrue(result)
        self.assertEqual(len(calls), 1)

    def test_an_aged_decision_is_decided_again_not_repeated(self):
        result, calls, _error = self.ring(expire=1)
        self.assertTrue(result)
        self.assertEqual(len(calls), 2)  # the first was refused before contact-down (DecisionExpired)

    def test_refusals(self):
        for case in ({"kinds": ["other"]}, {"at_hub": False}, {"bell_box": None}, {"bell_box": (640, 590, 47, 47)}):
            with self.subTest(case=case):
                _result, calls, error = self.ring(**case)
                self.assertIsInstance(error, Stop)
                self.assertEqual(calls, [])


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
