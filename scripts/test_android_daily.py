"""Android 朝夕心愿 / 星海拾光 rewards (agent/host/android/daily.py): python scripts/test_android_daily.py"""
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

import numpy as np  # noqa: E402

from host.android import actions, daily, pages  # noqa: E402
from host.android.actions import Screen  # noqa: E402

FIXTURES = ROOT / "scripts/fixtures/android"
# Composing a 1280x720 frame from UID-free crops of the device frames, each put back where it was cut. Shared with the
# framework check below (run in a fresh interpreter), hence a source string.
COMPOSE = r'''
import cv2
import numpy as np

PLACES = {"daily_title.png": (0, 0), "daily_tabs_zhaoxi.jpg": (15, 100), "daily_tabs_xinghai.jpg": (15, 100),
          "daily_refresh.png": (1095, 45), "daily_circles_lit.png": (1162, 115),
          "daily_circles_claimed.png": (1162, 115), "daily_circles_locked.png": (1162, 115),
          "daily_left_zhaoxi.jpg": (15, 585), "daily_left_xinghai.jpg": (15, 585),
          "daily_right_zhaoxi_check.jpg": (700, 585), "daily_right_zhaoxi_go.jpg": (700, 585),
          "daily_right_xinghai.jpg": (700, 585), "calendar_title.png": (0, 0), "calendar_cards.jpg": (140, 340),
          "daily_layer_tabs_zhaoxi.jpg": (15, 100), "daily_layer_tabs_xinghai.jpg": (15, 100),
          "daily_layer_column_zhaoxi.jpg": (1160, 100), "daily_layer_column_xinghai.jpg": (1160, 100),
          "monthly_layer_tabs.jpg": (15, 100), "monthly_layer_column.jpg": (1160, 100),
          "reward_layer_title.png": (540, 140), "reward_layer_hint.png": (520, 620),
          "redeem_success_layer.jpg": (230, 140)}
PAGES = {
    # device survey (100 lit), device run 1 (the 立即前往 wish selected), device run 2 (100 claimed), 星海拾光 at 0
    "zhaoxi lit": ("daily_tabs_zhaoxi.jpg", "daily_circles_lit.png", "daily_left_zhaoxi.jpg",
                   "daily_right_zhaoxi_check.jpg"),
    "zhaoxi lit go": ("daily_tabs_zhaoxi.jpg", "daily_circles_lit.png", "daily_left_zhaoxi.jpg",
                      "daily_right_zhaoxi_go.jpg"),
    "zhaoxi claimed": ("daily_tabs_zhaoxi.jpg", "daily_circles_claimed.png", "daily_left_zhaoxi.jpg",
                       "daily_right_zhaoxi_go.jpg"),
    "xinghai": ("daily_tabs_xinghai.jpg", "daily_circles_locked.png", "daily_left_xinghai.jpg",
                "daily_right_xinghai.jpg"),
    # the 朝夕心愿 tab over 星海拾光's content (a tab switch half done): no view
    "mixed": ("daily_tabs_zhaoxi.jpg", "daily_circles_locked.png", "daily_left_xinghai.jpg",
              "daily_right_xinghai.jpg"),
}


def compose(fixtures, names, background=(60, 40, 45)):
    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[:] = background
    for name in names:
        image = cv2.imread(str(fixtures / name))
        x, y = PLACES[name]
        frame[y:y + image.shape[0], x:x + image.shape[1]] = image
    return frame


def page(fixtures, name):
    return compose(fixtures, ("daily_title.png", "daily_refresh.png", *PAGES[name]))


# The 「恭喜获得」 layer. No layer over 每日灵感 was saved on the device: daily_layer_* are the device frames of
# 朝夕心愿 (device run 2, after the claim) and 星海拾光 put under the blur and dim fitted on 奇迹之旅 device run 1
# (daily.UNDER_CELLS); monthly_layer_* are crops of that run's real layer frame over 奇迹之旅, and
# redeem_success_layer.jpg the real layer after a redeemed code (redeem device run 2): layers over other pages.
LAYERS = {"zhaoxi": ("daily_layer_tabs_zhaoxi.jpg", "daily_layer_column_zhaoxi.jpg"),
          "xinghai": ("daily_layer_tabs_xinghai.jpg", "daily_layer_column_xinghai.jpg"),
          "monthly": ("monthly_layer_tabs.jpg", "monthly_layer_column.jpg")}


def layer(fixtures, name):
    if name == "redeem":
        return compose(fixtures, ("redeem_success_layer.jpg",), background=(40, 35, 45))
    return compose(fixtures, (*LAYERS[name], "reward_layer_title.png", "reward_layer_hint.png"), background=(30, 28, 36))
'''
exec(COMPOSE)


def hit(box, text=""):
    return SimpleNamespace(hit=True, box=box, best_result=SimpleNamespace(text=text, box=box))


def rows(*items):
    found = [SimpleNamespace(text=text, box=box) for text, box in items]
    return SimpleNamespace(hit=True, box=found[0].box, best_result=found[0], filtered_results=found)


MISS = SimpleNamespace(hit=False, box=None, best_result=None)
BACK = [37, 22, 40, 40]
TAB_BOXES = {daily.ZHAOXI: [44, 182, 88, 28], daily.XINGHAI: [45, 317, 88, 28]}
CARD_BOXES = {daily.ZHAOXI: [178, 366, 91, 28], daily.XINGHAI: [178, 645, 92, 30]}
LIT_BOX = [1174, 406, 47, 49]  # where the real ColorMatch finds the lit 100 circle
GO = ("立即前往", [1076, 678, 104, 28])
FRAMES = {name: page(FIXTURES, name) for name in PAGES}
HUD = Screen("hud", {"duck": [30, 18, 54, 54]})
UNKNOWN = Screen("unknown", reason="认不出当前画面")


def page_recognizer(name, score, *, words=(), lit=None):
    """``recognize`` for the composed page ``name``: the OCR nodes as the real framework reads them there."""
    tab = daily.XINGHAI if name in ("xinghai",) else daily.ZHAOXI
    lit = (name.startswith("zhaoxi lit")) if lit is None else lit
    nodes = {actions.BACK_ARROW: hit(BACK), daily.REFRESH: hit([1115, 55, 103, 23], "每日4点刷新"),
             daily.SCORE: hit([1177, 510, 43, 32], str(score)),
             **{node: hit(TAB_BOXES[mode]) for mode, node in daily.TAB_NODES.items()}}
    if name in ("xinghai", "mixed"):
        nodes[daily.SHOP_LABEL] = hit([37, 668, 75, 22], "星光小铺")
    if words:
        nodes[daily.DAILY_WORDS] = rows(*words)
    del tab

    def recognize(node, override=None):
        if node == daily.CIRCLE_LIT:
            return hit(LIT_BOX) if lit and override == {"roi": daily.circle_roi(0)} else MISS
        return nodes.get(node, MISS)
    return recognize


def daily_page(name, score, **kwargs):
    return Screen("page", {"back": BACK}, title=daily.TITLE), FRAMES[name], page_recognizer(name, score, **kwargs)


CALENDAR = (Screen("page", {"back": BACK}, title=daily.CALENDAR), np.zeros((720, 1280, 3), np.uint8),
            lambda node, override=None: {actions.BACK_ARROW: hit(BACK),
                                         **{n: hit(CARD_BOXES[m]) for m, n in daily.CARD_NODES.items()}}.get(node, MISS))
LAYER_TEXTS = {daily.LAYER_TITLE: hit([570, 165, 136, 42], "恭喜获得"),
               daily.LAYER_HINT: hit([569, 640, 140, 19], "点击空白区域继续")}
LAYER_FRAMES = {name: layer(FIXTURES, name) for name in (*LAYERS, "redeem")}
# The layer's texts read on each of them (the framework check below reads them for real).
LAYER_SCREENS = {name: (UNKNOWN, frame, lambda node, override=None: LAYER_TEXTS.get(node, MISS))
                 for name, frame in LAYER_FRAMES.items()}
LAYER = LAYER_SCREENS["zhaoxi"]
SHOP = (Screen("page", {"back": BACK}, title="商城"), np.zeros((720, 1280, 3), np.uint8),
        lambda node, override=None: hit(BACK) if node == actions.BACK_ARROW else MISS)
BLANK = (UNKNOWN, np.zeros((720, 1280, 3), np.uint8), lambda node, override=None: MISS)


class Game:
    """The 奇想日历 -> 每日灵感 screens, reacting to the taps the real pages helpers send. ``pages``: per tab, the
    page shown (a ``daily_page`` tuple); ``after_claim``: the frames shown after the claim tap, one per capture, before
    the claimed page ("layer": the layer over this tab stays until the blank tap); ``claimed``: the page after the
    claim; ``dismiss_closes``: whether the blank tap closes the layer; ``after_card``: frames before the page opens;
    ``after_back``: frames shown after the first back arrow, one per capture, before the page it leads to;
    ``after_dismiss``: frames shown after the blank tap, one per capture, before the page (or the layer) again."""

    def __init__(self, tab=daily.ZHAOXI, *, page=None, claimed=None, after_claim=(), dismiss_closes=True,
                 after_card=(), start=None, opens_on=None, after_back=None, after_dismiss=()):
        self.where, self.tab, self.taps = "hud", tab, []
        self.page, self.claimed = page or daily_page("zhaoxi lit", 100), claimed or daily_page("zhaoxi claimed", 100)
        self.other = daily_page("xinghai", 0) if tab == daily.ZHAOXI else daily_page("zhaoxi claimed", 100)
        self.opens_on = opens_on or tab
        self.after_claim, self.after_card, self.dismiss_closes = list(after_claim), list(after_card), dismiss_closes
        self.after_back, self.after_dismiss = after_back, list(after_dismiss)
        self.queue = [start] if start else []

    def screen_now(self, rt):
        if self.queue:
            item = self.queue.pop(0)
            if item == "layer":
                self.where = "layer"
            else:
                return item
        return {"hud": (HUD, np.zeros((720, 1280, 3), np.uint8), lambda node, override=None: MISS),
                "calendar": CALENDAR, "layer": LAYER_SCREENS[self.tab], "page": self.page,
                "other": self.other}[self.where]

    def shortcut(self, rt, action):
        assert action == "calendar" and self.where == "hud"
        self.taps.append("calendar")
        self.where = "calendar"
        return True

    def click(self, name, target, source_frame):
        assert name == "Click"
        if target == list(daily.DISMISS_POINT):
            self.taps.append("dismiss")
            if self.dismiss_closes:
                self.where = "page"
            self.queue = list(self.after_dismiss)
            return True
        if target == BACK:
            self.taps.append("back")
            self.where = {"page": "calendar", "other": "calendar", "calendar": "hud"}[self.where]
            if self.after_back is not None:
                self.queue, self.after_back = list(self.after_back), None
            return True
        if target == LIT_BOX:
            self.taps.append("claim")
            self.page = self.claimed
            self.queue = list(self.after_claim)
            return True
        for mode, box in CARD_BOXES.items():
            if target == box and self.where == "calendar":
                self.taps.append(f"card {mode}")
                self.where = "page" if self.opens_on == self.tab else "other"
                self.queue = list(self.after_card)
                return True
        for mode, box in TAB_BOXES.items():
            if target == box:
                self.taps.append(f"tab {mode}")
                self.where = "page" if mode == self.tab else "other"
                return True
        raise AssertionError(f"unexpected tap {target}")


def play(game, mode=None):
    rt = SimpleNamespace(stopped=False, logs=[], _frame_still_valid=Mock(return_value=True),
                         pause=Mock(return_value=True), diagnostic=Mock(), TOUCH_KEY_ATTEMPTS=3, action=game.click)
    rt.log = rt.logs.append
    with patch.object(pages, "screen_now", side_effect=game.screen_now), \
            patch.object(pages, "tap_shortcut", side_effect=game.shortcut), \
            patch("time.monotonic", side_effect=itertools.count(0, .5).__next__):
        result = daily.run(rt, mode or game.tab)
    return result, rt


def saved(rt):
    return [call.kwargs["image"] for call in rt.diagnostic.call_args_list]


class LookTests(unittest.TestCase):
    """The colour checks on the device crops (no framework needed)."""

    def test_circle_looks_on_the_device_crops(self):
        lit, claimed, xinghai = FRAMES["zhaoxi lit"], FRAMES["zhaoxi claimed"], FRAMES["xinghai"]
        self.assertEqual([daily.circle_look(lit, i) for i in range(5)], [daily.LIT] + [daily.LOCKED] * 4)
        self.assertEqual([daily.circle_look(claimed, i) for i in range(5)], [daily.CLAIMED] + [daily.LOCKED] * 4)
        self.assertEqual([daily.circle_look(xinghai, i) for i in range(5)], [daily.LOCKED] * 5)

    def test_anything_else_is_unknown(self):
        frame = FRAMES["zhaoxi lit"].copy()
        x, y = daily.CIRCLE_X, daily.CIRCLE_YS[0]
        frame[y - 22:y + 23, x - 22:x + 23] = (128, 128, 128)  # grey
        self.assertEqual(daily.circle_look(frame, 0), daily.UNKNOWN)
        frame[y - 22:y + 23, x - 22:x + 23] = (0, 0, 0)  # nothing there
        self.assertEqual(daily.circle_look(frame, 0), daily.UNKNOWN)
        dimmed = (FRAMES["zhaoxi lit"] * .45).astype(np.uint8)  # under a layer
        self.assertEqual(daily.circle_look(dimmed, 0), daily.UNKNOWN)

    def test_tabs_and_content(self):
        for name, mode in (("zhaoxi lit", daily.ZHAOXI), ("zhaoxi lit go", daily.ZHAOXI), ("xinghai", daily.XINGHAI)):
            with self.subTest(name=name):
                view = daily.read_view(FRAMES[name], page_recognizer(name, 100 if mode == daily.ZHAOXI else 0))
                self.assertEqual(view.tab, mode)
                self.assertTrue(view.consistent())
        self.assertIsNone(daily.read_view(FRAMES["mixed"], page_recognizer("mixed", 0)))
        # 星光小铺 not read on 星海拾光 (or read on 朝夕心愿), the score unread, the tabs dimmed: no view.
        recognize = page_recognizer("xinghai", 0)
        self.assertIsNone(daily.read_view(FRAMES["xinghai"], lambda node, override=None: MISS
                                          if node == daily.SHOP_LABEL else recognize(node, override)))
        recognize = page_recognizer("zhaoxi lit", 100)
        self.assertIsNone(daily.read_view(FRAMES["zhaoxi lit"], lambda node, override=None: hit([37, 668, 75, 22])
                                          if node == daily.SHOP_LABEL else recognize(node, override)))
        self.assertIsNone(daily.read_view(FRAMES["zhaoxi lit"], page_recognizer("zhaoxi lit", "1OO")))
        self.assertIsNone(daily.read_view((FRAMES["zhaoxi lit"] * .45).astype(np.uint8),
                                          page_recognizer("zhaoxi lit", 100)))

    def test_the_page_beneath_the_layer(self):
        # Review round 1: only 每日灵感 on the expected tab beneath the layer lets the dismissal tap through.
        expected = {"zhaoxi": {daily.ZHAOXI}, "xinghai": {daily.XINGHAI}, "monthly": set(), "redeem": set()}
        for name, frame in LAYER_FRAMES.items():
            with self.subTest(layer=name):
                self.assertEqual({mode for mode in daily.MODES if daily.layer_over_tab(frame, mode)}, expected[name])
        self.assertFalse(daily.layer_over_tab(np.zeros((720, 1280, 3), np.uint8), daily.ZHAOXI))
        # The 500 and 400 circles glow whatever they show: here lit and claimed (the 100 circle's crops moved up),
        # under the fitted blur and dim (above the band: ~0.33 x + 20).
        import cv2
        x, y = daily.CIRCLE_X, daily.CIRCLE_YS[0]
        for name in ("zhaoxi lit", "zhaoxi claimed"):
            with self.subTest(circles=name):
                frame = FRAMES[name].copy()
                for index in (3, 4):
                    top = daily.CIRCLE_YS[index]
                    frame[top - 30:top + 30, x - 30:x + 30] = FRAMES[name][y - 30:y + 30, x - 30:x + 30]
                under = (cv2.GaussianBlur(frame, (0, 0), 4) * .33 + 20).astype(np.uint8)
                self.assertTrue(all(daily.gift_glow(under, index) >= daily.UNDER_GLOW for index in (3, 4)))

    def test_the_score_must_agree_with_the_circles(self):
        view = daily.View
        L, C, N, U = daily.LIT, daily.CLAIMED, daily.LOCKED, daily.UNKNOWN
        self.assertTrue(view("zhaoxi", 0, (N,) * 5).consistent())
        self.assertTrue(view("zhaoxi", 300, (C, L, C, N, N)).consistent())
        self.assertTrue(view("zhaoxi", 500, (C,) * 5).consistent())
        self.assertFalse(view("zhaoxi", 100, (N,) * 5).consistent())  # the survey's frame mid-animation
        self.assertFalse(view("zhaoxi", 0, (L, N, N, N, N)).consistent())
        self.assertFalse(view("zhaoxi", 200, (C, U, N, N, N)).consistent())
        self.assertFalse(view("zhaoxi", 100, (C, L, N, N, N)).consistent())


class FlowTests(unittest.TestCase):
    """daily.run with the real pages helpers over a Game."""

    def test_a_lit_circle_is_claimed_the_layer_closed_and_the_claim_seen_on_settled_frames(self):
        game = Game(after_claim=[BLANK, "layer"])
        result, rt = play(game)
        self.assertTrue(result, rt.logs)
        self.assertEqual(game.taps, ["calendar", f"card {daily.ZHAOXI}", "claim", "dismiss", "back", "back"])
        self.assertIn("朝夕心愿 100 分的奖励已领取。", rt.logs)
        self.assertIn("朝夕心愿：领取了 1 个奖励。", rt.logs)
        rt.diagnostic.assert_not_called()

    def test_nothing_lit_taps_only_the_way_there_and_back(self):
        for game in (Game(page=daily_page("zhaoxi claimed", 100)), Game(daily.XINGHAI, page=daily_page("xinghai", 0))):
            with self.subTest(tab=game.tab):
                result, rt = play(game)
                self.assertTrue(result, rt.logs)
                self.assertEqual(game.taps, ["calendar", f"card {game.tab}", "back", "back"])
                self.assertTrue(any("没有可领取的奖励" in text for text in rt.logs))

    def test_the_page_opening_on_the_other_tab_switches_once(self):
        game = Game(daily.XINGHAI, page=daily_page("xinghai", 0), opens_on=daily.ZHAOXI)
        result, rt = play(game)
        self.assertTrue(result, rt.logs)
        self.assertEqual(game.taps, ["calendar", f"card {daily.XINGHAI}", f"tab {daily.XINGHAI}", "back", "back"])

    def test_a_circle_that_is_not_claimed_after_the_tap_stops_without_a_second_tap(self):
        for claimed, words in ((daily_page("zhaoxi lit", 100), "没有看到它变成已领取"),
                               (daily_page("xinghai", 0), "不该出现"), (SHOP, "不该出现")):
            with self.subTest(words=words, title=claimed[0].title):
                game = Game(claimed=claimed)
                result, rt = play(game)
                self.assertFalse(result)
                self.assertEqual(game.taps, ["calendar", f"card {daily.ZHAOXI}", "claim"])
                self.assertTrue(any(words in text for text in rt.logs), rt.logs)
                self.assertEqual(len(saved(rt)), 1)

    def test_a_circle_of_an_unknown_look_after_the_claim_waits_then_stops(self):
        frame = FRAMES["zhaoxi lit"].copy()
        x, y = daily.CIRCLE_X, daily.CIRCLE_YS[0]
        frame[y - 22:y + 23, x - 22:x + 23] = (128, 128, 128)
        odd = (Screen("page", {"back": BACK}, title=daily.TITLE), frame, page_recognizer("zhaoxi lit", 100, lit=False))
        game = Game(claimed=odd)
        result, rt = play(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, ["calendar", f"card {daily.ZHAOXI}", "claim"])
        self.assertTrue(any("样子认不出" in text for text in rt.logs), rt.logs)
        self.assertIs(saved(rt)[0], frame)

    def test_a_layer_still_there_after_one_tap_stops(self):
        game = Game(after_claim=["layer"], dismiss_closes=False)
        result, rt = play(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, ["calendar", f"card {daily.ZHAOXI}", "claim", "dismiss"])
        self.assertTrue(any("仍然没有关闭" in text for text in rt.logs), rt.logs)

    def test_a_reward_layer_over_another_page_is_not_tapped(self):
        # Review round 1: the redeem success layer, the 奇迹之旅 layer and the layer over the other tab (title, hint
        # and blank point all as here): the first frame showing one stops, with that frame, before any dismissal tap.
        for name in ("redeem", "monthly", "xinghai"):
            with self.subTest(layer=name):
                game = Game(after_claim=[LAYER_SCREENS[name]])
                result, rt = play(game)
                self.assertFalse(result)
                self.assertEqual(game.taps, ["calendar", f"card {daily.ZHAOXI}", "claim"])
                self.assertIs(saved(rt)[0], LAYER_FRAMES[name])
                self.assertEqual(len(saved(rt)), 1)
                self.assertTrue(any("认不出每日灵感的「朝夕心愿」页签" in text for text in rt.logs), rt.logs)

    def test_a_reward_layer_over_another_page_on_a_tap_frame_is_not_tapped(self):
        # The layer over 朝夕心愿 first, then another page's layer on the deciding or on the confirming frame.
        for frames in ([LAYER, LAYER_SCREENS["redeem"]], [LAYER, LAYER, LAYER_SCREENS["redeem"]],
                       [LAYER, LAYER, LAYER_SCREENS["monthly"]]):
            with self.subTest(frames=len(frames), last=frames[-1][1] is LAYER_FRAMES["redeem"]):
                game = Game(after_claim=frames)
                result, rt = play(game)
                self.assertFalse(result)
                self.assertEqual(game.taps, ["calendar", f"card {daily.ZHAOXI}", "claim"])
                self.assertIs(saved(rt)[0], frames[-1][1])
                self.assertTrue(any("认不出每日灵感" in text for text in rt.logs), rt.logs)

    def test_a_reward_layer_over_another_page_after_the_dismissal_stops(self):
        # Review round 2: the real 奇迹之旅 layer frame right after the dismissal tap, then the claimed page: that
        # frame stops the task (saved), no back arrow is tapped.
        game = Game(after_claim=["layer"], after_dismiss=[LAYER_SCREENS["monthly"]])
        result, rt = play(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, ["calendar", f"card {daily.ZHAOXI}", "claim", "dismiss"])
        self.assertIs(saved(rt)[0], LAYER_FRAMES["monthly"])
        self.assertEqual(len(saved(rt)), 1)
        self.assertTrue(any("认不出每日灵感的「朝夕心愿」页签" in text for text in rt.logs), rt.logs)
        # The layer over 朝夕心愿 still showing after the tap is waited for (the page follows): success.
        game = Game(after_claim=["layer"], after_dismiss=[LAYER, LAYER])
        result, rt = play(game)
        self.assertTrue(result, rt.logs)
        self.assertEqual(game.taps, ["calendar", f"card {daily.ZHAOXI}", "claim", "dismiss", "back", "back"])

    def test_a_frame_shown_while_the_back_arrow_takes_effect_is_checked(self):
        # Review round 2: after the 朝夕心愿 back arrow, one capture of the 星海拾光 page (still titled 每日灵感) or of
        # the 奇迹之旅 layer (its texts read, nothing else recognised), then 奇想日历: that capture stops (saved), no
        # calendar back arrow.
        for frames, stop in (([daily_page("xinghai", 0), CALENDAR], FRAMES["xinghai"]),
                             ([LAYER_SCREENS["monthly"], CALENDAR], LAYER_FRAMES["monthly"])):
            with self.subTest(first=frames[0][0].kind):
                game = Game(page=daily_page("zhaoxi claimed", 100), after_back=frames)
                result, rt = play(game)
                self.assertFalse(result)
                self.assertEqual(game.taps, ["calendar", f"card {daily.ZHAOXI}", "back"])
                self.assertIs(saved(rt)[0], stop)
                self.assertEqual(len(saved(rt)), 1)
        # 每日灵感 on 朝夕心愿 or not settled on a tab (a blank frame too) is waited through: success.
        unsettled = (Screen("page", {"back": BACK}, title=daily.TITLE), FRAMES["mixed"], page_recognizer("mixed", 0))
        game = Game(page=daily_page("zhaoxi claimed", 100),
                    after_back=[daily_page("zhaoxi claimed", 100), unsettled, BLANK, CALENDAR])
        result, rt = play(game)
        self.assertTrue(result, rt.logs)
        self.assertEqual(game.taps, ["calendar", f"card {daily.ZHAOXI}", "back", "back"])

    def test_the_frame_the_back_arrow_leads_to_is_checked(self):
        # Review round 1: 商城 on the frame that shows 每日灵感 gone stops with that frame (no second back arrow), and
        # so does 每日灵感 again once 奇想日历 showed.
        for frames, stop in (([SHOP], SHOP[1]), ([CALENDAR, daily_page("zhaoxi claimed", 100)],
                                                 FRAMES["zhaoxi claimed"])):
            with self.subTest(first=frames[0][0].title):
                game = Game(page=daily_page("zhaoxi claimed", 100), after_back=frames)
                result, rt = play(game)
                self.assertFalse(result)
                self.assertEqual(game.taps, ["calendar", f"card {daily.ZHAOXI}", "back"])
                self.assertIs(saved(rt)[0], stop)
                self.assertEqual(len(saved(rt)), 1)
                self.assertTrue(any("不该出现" in text for text in rt.logs), rt.logs)

    def test_the_other_tab_does_not_authorise_the_daily_back_arrow(self):
        # Review round 1: the way back starts on 朝夕心愿, but a tap frame of the back arrow shows 星海拾光.
        zhaoxi, xinghai = daily_page("zhaoxi claimed", 100), daily_page("xinghai", 0)

        def runtime():
            return SimpleNamespace(stopped=False, _frame_still_valid=Mock(return_value=True),
                                   pause=Mock(return_value=True), TOUCH_KEY_ATTEMPTS=3, action=Mock(return_value=True))
        for frames in ([zhaoxi, xinghai, xinghai], [zhaoxi, zhaoxi, xinghai]):
            with self.subTest(deciding=frames[1] is xinghai):
                rt = runtime()
                with patch.object(pages, "screen_now", side_effect=frames + [zhaoxi] * 5), \
                        self.assertRaises(pages.FrameMismatch) as caught:
                    daily.back_home(rt, daily.ZHAOXI)
                self.assertIs(caught.exception.frame, xinghai[1])
                rt.action.assert_not_called()
        # Review round 2: the frame that decides the way back already shows 星海拾光: stop with it, no tap.
        rt = runtime()
        with patch.object(pages, "screen_now", side_effect=[xinghai] + [zhaoxi] * 5), \
                self.assertRaises(daily.Stop) as caught:
            daily.back_home(rt, daily.ZHAOXI)
        self.assertIs(caught.exception.frames[0], xinghai[1])
        rt.action.assert_not_called()
        # The expected tab on both tap frames: both arrows are tapped.
        rt, hud = runtime(), (HUD, np.zeros((720, 1280, 3), np.uint8), lambda node, override=None: MISS)
        with patch.object(pages, "screen_now", side_effect=[zhaoxi] * 3 + [CALENDAR] * 4 + [hud]):
            self.assertTrue(daily.back_home(rt, daily.ZHAOXI))
        self.assertEqual(rt.action.call_count, 2)

    def test_a_layer_over_text_at_the_dismissal_point_is_not_tapped(self):
        text = (UNKNOWN, LAYER[1],
                lambda node, override=None: rows(("——满载而归的搭配师", [700, 680, 200, 25]))
                if node == actions.PAGE_TITLE else LAYER[2](node, override))
        game = Game(after_claim=[text, text, text])
        result, rt = play(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, ["calendar", f"card {daily.ZHAOXI}", "claim"])
        self.assertIs(saved(rt)[0], text[1])

    def test_a_view_that_never_agrees_with_its_score_is_not_acted_on(self):
        # The survey's frame: score 100 with every circle navy (mid-animation); it never settles here.
        game = Game(page=daily_page("xinghai", 100), tab=daily.XINGHAI)
        result, rt = play(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, ["calendar", f"card {daily.XINGHAI}"])
        self.assertTrue(any("对不上或认不出" in text for text in rt.logs), rt.logs)
        self.assertEqual(len(saved(rt)), 1)

    def test_a_brief_animation_is_waited_through(self):
        # The page opens with the score not counted up yet: those frames are waited through, then the claim.
        early = daily_page("zhaoxi lit", 0)
        game = Game(after_card=[BLANK, early, early])
        result, rt = play(game)
        self.assertTrue(result, rt.logs)
        self.assertIn("claim", game.taps)

    def test_an_unexpected_screen_while_opening_stops_with_that_frame(self):
        for after in ([SHOP], [BLANK] * 12):
            with self.subTest(frames=len(after)):
                game = Game(after_card=after)
                result, rt = play(game)
                self.assertFalse(result)
                self.assertEqual(game.taps, ["calendar", f"card {daily.ZHAOXI}"])
                self.assertEqual(len(saved(rt)), 1)

    def test_a_start_screen_that_is_not_safe_to_leave_stops_with_that_frame(self):
        popup = (UNKNOWN, np.ones((720, 1280, 3), np.uint8),
                 lambda node, override=None: hit([900, 100, 40, 40]) if node == actions.MODAL_CLOSE else MISS)
        game = Game(start=popup)
        result, rt = play(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, [])
        self.assertIs(saved(rt)[0], popup[1])

    def test_no_tap_next_to_a_daily_word(self):
        # 立即前往 within the margin of the circle's point (a layout that moved): a guard refusal, no tap.
        near = ("立即前往", [1150, 440, 100, 28])
        game = Game(page=daily_page("zhaoxi lit", 100, words=(near,)))
        result, rt = play(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, ["calendar", f"card {daily.ZHAOXI}"])
        self.assertTrue(any("立即前往" in text for text in rt.logs), rt.logs)
        # Where it is on the device frames, it is far from every target.
        game = Game(page=daily_page("zhaoxi lit go", 100, words=(GO,)))
        result, rt = play(game)
        self.assertTrue(result, rt.logs)

    def test_a_user_stop_ends_the_task_without_a_failure_line(self):
        rt = SimpleNamespace(stopped=False, log=Mock())
        self.assertFalse(daily.fail(rt, daily.ZHAOXI, "没能回到大世界"))
        rt.log.assert_called_once()
        rt.log.reset_mock()
        with patch("host.android_user_stop", return_value=True):
            self.assertFalse(daily.fail(rt, daily.ZHAOXI, "没能回到大世界"))
        rt.log.assert_not_called()


class EntryTests(unittest.TestCase):
    def test_the_shared_entry_branches_to_the_android_flow_on_a_touch_target_only(self):
        from action import daily_tasks
        runtime = SimpleNamespace(stopped=False)
        for mode in ("zhaoxi", "xinghai"):
            argv = SimpleNamespace(custom_action_param=json.dumps({"mode": mode}),
                                   task_detail=SimpleNamespace(task_id=7))
            pc = Mock()
            pc.return_value.run_daily.return_value = True
            pc.return_value.stopped = False
            with self.subTest(mode=mode), patch.object(daily_tasks, "Executors", return_value=runtime), \
                    patch.object(daily_tasks, "DailyRun", pc):
                with patch("host.game_backend", return_value="android"), \
                        patch.object(daily, "run", return_value=True) as run:
                    self.assertTrue(daily_tasks.DailyTasksAction.run.__wrapped__(None, None, argv))
                run.assert_called_once_with(runtime, mode)
                pc.assert_not_called()
                with patch("host.game_backend", return_value="win32"), patch.object(daily, "run") as run:
                    self.assertTrue(daily_tasks.DailyTasksAction.run.__wrapped__(None, None, argv))
                run.assert_not_called()
                pc.assert_called_once_with(None, mode, 7)

    def test_the_nodes_and_the_task_list(self):
        if not (ROOT / "resource_android").is_dir():
            self.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
            return
        nodes = json.loads((ROOT / "resource_android/pipeline/android_daily.json").read_text(encoding="utf-8"))
        self.assertEqual(set(nodes), set(daily.NODES))
        tiers = json.loads((ROOT / "agent/host/android/tasks.json").read_text(encoding="utf-8"))
        for entry in ("MaaNikki_Zhaoxi", "MaaNikki_Xinghai"):
            self.assertIn(entry, tiers["experimental"])
            self.assertIn(entry, tiers["notes"])
        import re
        for text in ("立即前往", "星光小铺", "购买", "升级"):
            self.assertTrue(re.search(nodes[daily.DAILY_WORDS]["expected"], text), text)
        for text in ("朝夕心愿", "星海拾光", "每日灵感", "每日4点刷新", "100", "奇想日历"):
            self.assertFalse(re.search(nodes[daily.DAILY_WORDS]["expected"], text), text)


FRAMEWORK_CHECK = r"""
import json, sys
from pathlib import Path
import cv2
import numpy as np
from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.resource import Resource
from maa.tasker import Tasker

repo = Path(sys.argv[1])
sys.path.insert(0, str(repo / "agent"))
from host.android import actions, daily, pages, touch_point
fixtures = repo / "scripts/fixtures/android"
exec(sys.argv[2])
frames = {name: page(fixtures, name) for name in PAGES}
frames["calendar"] = compose(fixtures, ("calendar_title.png", "calendar_cards.jpg"))
for name in (*LAYERS, "redeem"):
    frames[f"layer {name}"] = layer(fixtures, name)
current, results = {}, {}

class Frame(CustomController):
    def connect(self): return True
    def request_uuid(self): return "android-daily-test"
    def screencap(self): return current["frame"]
    def refuse(self, *args): return False
    start_app = stop_app = click = swipe = touch_down = touch_move = touch_up = refuse
    click_key = input_text = key_down = key_up = refuse

class Judge(CustomAction):
    def run(self, context, argv):
        image = context.tasker.controller.post_screencap().wait().get()
        def recognize(node, override=None):
            return context.run_recognition(node, image, {node: override} if override else None)
        screen = actions.read_screen(recognize)
        out = {"kind": screen.kind, "title": screen.title, "layer": daily.is_reward_layer(screen, recognize),
               "blank": daily.blank_at_dismiss(recognize),
               "dismiss_refused": pages._forbidden_near(recognize, [*daily.DISMISS_POINT, 1, 1]),
               "over": [mode for mode in daily.MODES if daily.layer_over_tab(image, mode)]}
        if screen.kind == "page" and screen.title == daily.TITLE:
            view = daily.read_view(image, recognize)
            out["view"] = view and [view.tab, view.score, list(view.circles)]
            targets = {mode: recognize(node) for mode, node in daily.TAB_NODES.items()}
            targets.update({f"circle {i}": recognize(daily.CIRCLE_LIT, {"roi": daily.circle_roi(i)}) for i in range(5)})
            out["taps"] = {k: touch_point(list(r.box)) for k, r in targets.items() if actions._hit(r)}
            out["refused"] = {k: pages._forbidden_near(recognize, list(r.box)) or daily.daily_word_near(
                recognize, list(r.box)) for k, r in targets.items() if actions._hit(r)}
            words = recognize(daily.DAILY_WORDS)
            out["words"] = [r.text for r in (words.filtered_results or [])] if actions._hit(words) else []
            out["beside_go"] = daily.daily_word_near(recognize, [1100, 650, 20, 20])
        if screen.kind == "page" and screen.title == daily.CALENDAR:
            out["cards"] = {mode: touch_point(list(recognize(node).box)) for mode, node in daily.CARD_NODES.items()
                            if actions._hit(recognize(node))}
        results[current["name"]] = out
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
    """The real nodes and OCR, both layers loaded, on frames composed from UID-free crops of the device frames (in a
    fresh interpreter, as in test_android_monthly.py)."""

    def test_the_daily_pages_targets_states_and_guards(self):
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
        L, C, N = daily.LIT, daily.CLAIMED, daily.LOCKED
        self.assertEqual({name: out[name].get("view") for name in PAGES},
                         {"zhaoxi lit": ["zhaoxi", 100, [L, N, N, N, N]], "zhaoxi lit go": ["zhaoxi", 100, [L, N, N, N, N]],
                          "zhaoxi claimed": ["zhaoxi", 100, [C, N, N, N, N]], "xinghai": ["xinghai", 0, [N] * 5],
                          "mixed": None})
        for name in PAGES:
            with self.subTest(name=name):
                value = out[name]
                self.assertEqual((value["kind"], value["title"], value["layer"]), ("page", daily.TITLE, False))
                self.assertTrue(value["blank"])
                self.assertIsNone(value["dismiss_refused"])
                taps = value["taps"]
                for mode, (x, y) in {"zhaoxi": (88, 196), "xinghai": (89, 331)}.items():
                    self.assertLessEqual(max(abs(taps[mode][0] - x), abs(taps[mode][1] - y)), 6, (mode, taps[mode]))
                self.assertEqual(set(value["refused"].values()), {None})
                lit = name.startswith("zhaoxi lit")
                self.assertEqual({k for k in taps if k.startswith("circle")}, {"circle 0"} if lit else set())
                if lit:
                    self.assertLessEqual(max(abs(taps["circle 0"][0] - 1196), abs(taps["circle 0"][1] - 429)), 4)
        self.assertEqual(out["xinghai"]["words"], ["星光小铺", "立即前往"])
        self.assertEqual(out["zhaoxi lit go"]["words"], ["立即前往"])
        self.assertEqual(out["xinghai"]["beside_go"], "立即前往")
        self.assertEqual(out["zhaoxi lit"]["words"], [])
        calendar = out["calendar"]
        self.assertEqual((calendar["kind"], calendar["title"]), ("page", daily.CALENDAR))
        for mode, (x, y) in {"zhaoxi": (223, 380), "xinghai": (223, 660)}.items():
            self.assertLessEqual(max(abs(calendar["cards"][mode][0] - x), abs(calendar["cards"][mode][1] - y)), 6)
        for name, over in (("zhaoxi", ["zhaoxi"]), ("xinghai", ["xinghai"]), ("monthly", []), ("redeem", [])):
            with self.subTest(layer=name):
                layer = out[f"layer {name}"]
                self.assertEqual((layer["kind"], layer["layer"], layer["over"]), ("unknown", True, over))
                self.assertNotIn("view", layer)
                if name in ("zhaoxi", "xinghai"):
                    self.assertEqual((layer["blank"], layer["dismiss_refused"]), (True, None))


if __name__ == "__main__":
    unittest.main()
