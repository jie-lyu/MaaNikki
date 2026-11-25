"""Android 每周免费礼包 reading (agent/host/android/shop.py, pages.swipe_on_page): python scripts/test_android_weekly.py"""
from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402

from host.android import actions, pages, shop  # noqa: E402
from host.android.actions import Screen  # noqa: E402

BACKGROUND = (193, 226, 238)  # BGR of the empty gift list (saved frames)
GOLD = (70, 150, 200)
TAB_BOX = [82, 335, 95, 28]
BACK_BOX = [37, 22, 40, 40]
HUD = Screen("hud", {"duck": [30, 18, 54, 54]})
SHORTCUT = [655, 48]  # the 商城 shortcut on the HUD
SHORTCUT_MARK = "test:shortcut"  # answered only by a frame of the scripted HUD with the shortcut in place
SHOP = Screen("page", {"back": BACK_BOX}, title="商城")


def row(text, box):
    return SimpleNamespace(text=text, box=list(box))


def result(rows):
    rows = list(rows)
    if not rows:
        return SimpleNamespace(hit=False, box=None, best_result=None, filtered_results=[])
    return SimpleNamespace(hit=True, box=rows[0].box, best_result=rows[0], filtered_results=rows)


MISS = result([])


def gift(content_y, centre, band="免费", pill=True, counter="本周限购0/1"):
    return {"y": content_y, "x": centre, "band": band, "pill": pill, "counter": counter}


def sold_out_week():
    """The three gifts where survey c3 found them: 虹迹赠礼 mid-list, 芳馨赠礼 and 星实置业之礼 at the end (content y)."""
    return {"虹迹赠礼": gift(1200, 839), "芳馨赠礼": gift(1700, 839), "星实置业之礼": gift(2017, 377)}


def list_frame(offset, *, selected=True):
    """The gift list at ``offset``: non-periodic bars in the card columns move with the list; the column at x ~960-1110
    stays empty background (where the swipes start)."""
    frame = np.full((720, 1280, 3), BACKGROUND, np.uint8)
    rng = np.random.default_rng(7)
    for content_y in np.cumsum(rng.integers(30, 90, 80)):
        y = int(content_y) - offset + shop.LIST_TOP
        if shop.LIST_TOP <= y < 700:
            frame[y:y + 12, 290:950] = frame[y:y + 12, 1120:1180] = (60, 90, 120)
    if selected:
        x, y, w, h = TAB_BOX
        frame[y:y + h, x:x + w] = GOLD
    return frame


class Game:
    """A scripted 商城: the HUD, the opening event tab (甄选礼包 out of view), the left list after one swipe (the tab
    in view, unselected), then the gift list, moved ``step`` px per list swipe up to ``end`` (``stuck``: swipes do
    nothing). ``screens``: frames shown instead of the state, one per capture, once the given number of touches has
    been sent. ``tab_error``: the 甄选礼包 recognition cannot run."""

    def __init__(self, gifts=None, *, step=300, end=None, stuck=False, start="hud", screens=None,
                 forbidden=(), prices=(), tab_error=False):
        self.gifts = gifts if gifts is not None else sold_out_week()
        last = max(g["y"] for g in self.gifts.values()) if self.gifts else 1400
        self.step, self.end = step, end if end is not None else last - 598
        self.stuck, self.where, self.offset = stuck, start, 0
        self.screens, self.forbidden, self.prices = dict(screens or {}), list(forbidden), list(prices)
        self.touches, self.swipes, self.tab_error = [], 0, tab_error
        self.shortcuts = 0  # shortcut taps (kept out of ``touches``, which ``screens`` counts)

    def titles(self):
        return {name: [g["x"] - 45, g["y"] - self.offset, 90, 28] for name, g in self.gifts.items()
                if shop.LIST_TOP <= g["y"] - self.offset <= 700}

    def recognize(self, node, override=None):
        if node == SHORTCUT_MARK:
            return result([row("商城", SHORTCUT + [1, 1])]) if self.where == "hud" else MISS
        if node == pages.FORBIDDEN:
            return result(self.forbidden)
        if node == shop.PRICE_GUARD:
            return result(self.prices)
        if node == shop.GIFTS_TAB:
            if self.tab_error:
                return None
            return result([row("甄选礼包", TAB_BOX)]) if self.where in ("left", "gifts") else MISS
        if node == actions.BACK_ARROW:
            return result([row("", BACK_BOX)])
        titles = self.titles()
        if node == shop.GIFT_TITLE:
            return result(row(name, box) for name, box in titles.items())
        offsets = {shop.CARD_TEXT: shop.BAND, shop.SOLD_OUT: shop.PILL, shop.WEEKLY_LIMIT: shop.LIMIT}
        if node in offsets:
            for name, box in titles.items():
                region = shop._region(box, offsets[node])
                if override and override.get("roi") == region:
                    card = self.gifts[name]
                    text = {shop.CARD_TEXT: card["band"], shop.SOLD_OUT: "已售罄" if card["pill"] else "",
                            shop.WEEKLY_LIMIT: card["counter"]}[node]
                    return result([row(text, region)] if text else [])
            return MISS
        return MISS

    def screen_now(self, rt):
        if self.screens.get(len(self.touches)):
            return self.screens[len(self.touches)].pop(0)
        if self.where == "hud":
            return HUD, np.zeros((720, 1280, 3), np.uint8), self.recognize
        if self.where in ("event", "left"):
            return SHOP, list_frame(0, selected=False), self.recognize
        return SHOP, list_frame(self.offset), self.recognize

    def key(self, name):
        assert name == "shop" and self.where == "hud"
        self.where = "event"
        return True

    def action(self, name, source_frame=None, **fields):
        assert source_frame is not None
        if name == "Swipe":
            assert set(fields) == {"begin", "end", "duration"} and type(fields["duration"]) is int
            self.touches.append(("swipe", tuple(fields["begin"]), tuple(fields["end"]), fields["duration"]))
            if fields["begin"][0] < shop.LIST[0]:
                assert self.where == "event"
                self.where = "left"
            else:
                assert self.where == "gifts"
                self.swipes += 1
                if not self.stuck:
                    self.offset = min(self.end, self.offset + self.step)
            return True
        target = fields["target"]
        if target == SHORTCUT:
            assert self.where == "hud"
            self.shortcuts += 1
            return self.key("shop")
        if target == TAB_BOX:
            self.touches.append("tab")
            self.where = "gifts"
            return True
        if target == BACK_BOX:
            self.touches.append("back")
            self.where = "hud"
            return True
        raise AssertionError(f"unexpected tap {target}")


def unknown_frame(nodes=None):
    nodes = nodes or {}
    return (Screen("unknown", reason="认不出当前画面"), np.zeros((720, 1280, 3), np.uint8),
            lambda node, override=None: nodes.get(node, MISS))


QUIT = (Screen("quit_dialog", {"cancel": [494, 480, 20, 20]}), np.full((720, 1280, 3), 7, np.uint8),
        lambda node, override=None: MISS)
# A popup's close mark on an otherwise unrecognised frame (e.g. an unseen dialog over the shop).
POPUP = unknown_frame({actions.MODAL_CLOSE: result([row("", [929, 177, 36, 36])])})
# Something recognised on an otherwise unrecognised frame (an unseen page's back arrow, the duck), or a check that
# could not run there: not a screen in transition (pages.nothing_recognised), so not waited through.
BACK_ONLY = unknown_frame({actions.BACK_ARROW: result([row("", BACK_BOX)])})
DUCK_ONLY = unknown_frame({actions.HUD_DUCK: result([row("", [30, 18, 54, 54])])})
CHECK_FAILED = unknown_frame({actions.QUIT_TITLE: None})
# All three gifts wholly in the list at once: no list swipe needed.
ALL_VISIBLE = {"虹迹赠礼": gift(450, 608), "芳馨赠礼": gift(281, 839), "星实置业之礼": gift(598, 377)}


def stop_diagnostics(rt):
    return [c for c in rt.diagnostic.call_args_list if c.args[0].startswith("android_weekly_stop_")]


def plan_shortcut(action, screen, recognize):
    """actions.plan_tap for the scripted HUD: the shortcut where the frame says it is."""
    assert action == "shop" and screen.kind == "hud"
    found = recognize(SHORTCUT_MARK)
    if not actions._hit(found):
        raise actions.ScreenUnrecognised("大世界的快捷栏上找不到「商城」")
    return actions.Plan(list(found.box[:2]), "label", True, "商城", SHORTCUT[0])


def confirm_shortcut(plan, screen, recognize):
    """actions.confirm_tap for the scripted HUD: only a frame that shows the shortcut in the same place."""
    found = recognize(SHORTCUT_MARK)
    return list(found.box[:2]) if actions._hit(found) and list(found.box[:2]) == plan.target else None


def play(game):
    rt = SimpleNamespace(stopped=False, logs=[], key=Mock(side_effect=AssertionError("no named action")),
                         _frame_still_valid=Mock(return_value=True), pause=Mock(return_value=True),
                         diagnostic=Mock(), TOUCH_KEY_ATTEMPTS=3, action=game.action)
    rt.log = rt.logs.append
    with patch.object(pages, "screen_now", side_effect=game.screen_now), \
            patch.object(actions, "plan_tap", side_effect=plan_shortcut), \
            patch.object(actions, "confirm_tap", side_effect=confirm_shortcut), \
            patch.object(shop, "keep_frame") as kept, \
            patch("time.monotonic", side_effect=itertools.count(0, .25).__next__):
        outcome = shop.run(rt)
    rt.kept = kept
    return outcome, rt


class FlowTests(unittest.TestCase):
    def test_a_sold_out_week_is_read_to_the_end_and_left_by_the_back_arrow(self):
        game = Game()
        outcome, rt = play(game)
        self.assertTrue(outcome)
        swipes = [t for t in game.touches if t != "tab" and t != "back"]
        self.assertEqual(swipes[0], ("swipe", (130, 600), (130, 250), 600))  # the left tab list, as in the survey
        self.assertEqual(game.touches[1], "tab")
        self.assertEqual(game.touches[-1], "back")
        for touch in swipes[1:]:
            _kind, begin, end, ms = touch
            self.assertEqual((begin[1] - end[1], ms), (shop.SWIPE_DISTANCE, shop.SWIPE_MS))
            self.assertEqual(begin[0], end[0])
        self.assertEqual(game.offset, game.end)
        for name in shop.GIFTS:
            self.assertIn(f"每周免费礼包：「{name}」本周已领取（已售罄，本周限购 0/1）。", rt.logs)
        moved = [line for line in rt.logs if "次滑动礼包列表" in line]
        self.assertEqual(len(moved), game.swipes)  # the movement is logged after every list swipe
        self.assertIn("列表移动 300 像素", moved[0])
        self.assertEqual(rt.diagnostic.call_args_list, [])  # no failure evidence on a run that goes well
        (name, _frame), = [c.args for c in rt.kept.call_args_list]  # the frame before the first list swipe is kept
        self.assertEqual(name, "before_first_swipe")
        self.assertEqual(game.shortcuts, 1)
        self.assertEqual(rt.key.call_count, 0)  # never the named action, which would cancel a quit dialog

    def test_a_claimable_gift_is_reported_never_tapped_and_the_shop_is_left(self):
        gifts = sold_out_week()
        gifts["芳馨赠礼"] = gift(1700, 839, pill=False, counter="本周限购1/1")
        game = Game(gifts)
        outcome, rt = play(game)
        self.assertFalse(outcome)  # not claimed: the task is not done
        self.assertIn("每周免费礼包「芳馨赠礼」可以领取，但领取的画面还没在设备上见过，本次没有点；请手动领取或等下一版。",
                      rt.logs)
        self.assertIn("每周免费礼包：「芳馨赠礼」没有领取，本项未完成。", rt.logs)
        self.assertEqual(game.touches[-1], "back")
        self.assertEqual([t for t in game.touches if isinstance(t, str)], ["tab", "back"])  # no tap on any card

    def test_the_counter_alone_or_the_pill_alone_means_sold_out(self):
        gifts = sold_out_week()
        gifts["虹迹赠礼"] = gift(1200, 839, pill=False, counter="本周限购0/1")
        gifts["星实置业之礼"] = gift(2017, 377, pill=True, counter="")
        outcome, rt = play(Game(gifts))
        self.assertTrue(outcome)
        self.assertIn("每周免费礼包：「星实置业之礼」本周已领取（已售罄，限购数没读出）。", rt.logs)

    def test_a_card_that_cannot_be_read_stops_with_nothing_more_sent(self):
        for changed, words in ((gift(1200, 839, pill=False, counter=""), "认不出是否已售罄"),
                               (gift(1200, 839, band="8060", pill=False, counter="本周限购1/1"), "价格栏不是「免费」"),
                               (gift(1200, 839, band="免费 ¥6"), "价格栏不是「免费」")):
            with self.subTest(changed=changed):
                gifts = sold_out_week()
                gifts["虹迹赠礼"] = changed
                game = Game(gifts)
                outcome, rt = play(game)
                self.assertFalse(outcome)
                self.assertNotIn("back", game.touches)
                self.assertTrue(any(words in line for line in rt.logs), rt.logs)
                self.assertEqual(len(stop_diagnostics(rt)), 1)

    def test_a_list_that_stops_moving_before_every_gift_was_seen_stops(self):
        game = Game({"虹迹赠礼": gift(1200, 839), "芳馨赠礼": gift(1700, 839)}, end=700)  # 星实置业之礼 missing
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertNotIn("back", game.touches)
        self.assertTrue(any("连续两次滑不动" in line and "芳馨赠礼" in line and "星实置业之礼" in line
                            for line in rt.logs), rt.logs)
        list_swipes = [t for t in game.touches if isinstance(t, tuple) and t[1][0] >= shop.LIST[0]]
        self.assertNotEqual(list_swipes[-1][1], list_swipes[-2][1])  # the retry starts somewhere else

    def test_the_number_of_swipes_is_bounded(self):
        game = Game(step=40)
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.swipes, shop.MAX_SWIPES)
        self.assertTrue(any(f"滑了 {shop.MAX_SWIPES} 次" in line for line in rt.logs))

    def test_an_unrecognised_screen_during_the_scan_stops_with_nothing_more_sent(self):
        game = Game(screens={4: [unknown_frame()] * 40})  # after list swipe 2 (left swipe, tab, swipe 1, swipe 2)
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.swipes, 2)
        self.assertNotIn("back", game.touches)
        self.assertEqual(len(stop_diagnostics(rt)), 1)
        self.assertTrue(any("第 2 次滑动礼包列表" in line and "没有停稳" in line for line in rt.logs), rt.logs)

    def test_one_positively_other_frame_after_a_swipe_stops_with_that_frame(self):
        # Review round 1 replay: one quit-dialog frame (or a popup's close mark) right after list swipe 2; the same for
        # an unrecognised frame showing a back arrow or the duck, or one whose checks could not run.
        for label, other in (("quit", QUIT), ("popup", POPUP), ("back arrow", BACK_ONLY), ("duck", DUCK_ONLY),
                             ("check failed", CHECK_FAILED)):
            with self.subTest(kind=label):
                game = Game(screens={4: [other]})
                outcome, rt = play(game)
                self.assertFalse(outcome)
                self.assertEqual(game.swipes, 2)
                self.assertNotIn("back", game.touches)
                saved, = stop_diagnostics(rt)
                self.assertIs(saved.kwargs["image"], other[1])
                self.assertTrue(any("不该出现的画面" in line for line in rt.logs), rt.logs)

    def test_one_frame_not_recognised_yet_is_waited_through(self):
        # Nothing recognisable (no page, dialog or popup mark) for one frame: only that is forgiven, briefly.
        game = Game(screens={4: [unknown_frame()]})
        outcome, rt = play(game)
        self.assertTrue(outcome)
        self.assertEqual(stop_diagnostics(rt), [])

    def test_a_mismatch_on_a_swipe_frame_stops_with_that_frame(self):
        # The confirming frame of list swipe 2 shows another 商城 tab: no retry, that frame saved.
        game = Game()
        unselected = (SHOP, list_frame(300, selected=False), game.recognize)
        settle = [(SHOP, list_frame(300), game.recognize)] * 2
        game.screens = {3: settle + [(SHOP, list_frame(300), game.recognize), unselected]}
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.swipes, 1)
        saved, = stop_diagnostics(rt)
        self.assertIs(saved.kwargs["image"], unselected[1])

    def test_a_forbidden_word_on_a_swipe_confirming_frame_saves_that_frame(self):
        # Review round 2 finding 4: the refusal carries its frame; not the next clean one.
        game = Game()
        settle = [(SHOP, list_frame(300), game.recognize)] * 3  # settled (two frames), the deciding frame
        image = list_frame(300)
        words = lambda node, override=None: result([row("购买", [shop.LIST[0], 120, shop.LIST[2], 580])]) \
            if node == pages.FORBIDDEN else game.recognize(node, override)  # noqa: E731
        game.screens = {3: settle + [(SHOP, image, words)]}
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.swipes, 1)
        saved, = stop_diagnostics(rt)
        self.assertIs(saved.kwargs["image"], image)

    def test_leaving_needs_the_gifts_tab_still_selected(self):
        game = Game(ALL_VISIBLE)
        gifts = (SHOP, list_frame(0), game.recognize)
        unselected = (SHOP, list_frame(0, selected=False), game.recognize)
        # Settled after the tab tap and again for the scan (two frames each), then another tab before leaving.
        game.screens = {2: [gifts] * 4 + [unselected]}
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertNotIn("back", game.touches)
        saved, = stop_diagnostics(rt)
        self.assertIs(saved.kwargs["image"], unselected[1])
        self.assertTrue(any("离开之前确认商城" in line for line in rt.logs), rt.logs)

    def test_a_mismatch_on_a_back_arrow_tap_frame_stops_with_that_frame(self):
        # The confirming frame of the back-arrow tap shows another 商城 tab: no second attempt, that frame saved.
        game = Game(ALL_VISIBLE)
        gifts = (SHOP, list_frame(0), game.recognize)
        unselected = (SHOP, list_frame(0, selected=False), game.recognize)
        game.screens = {2: [gifts] * 6 + [unselected]}  # settled twice, the check before leaving, the deciding frame
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches.count("back"), 0)
        saved, = stop_diagnostics(rt)
        self.assertIs(saved.kwargs["image"], unselected[1])
        self.assertTrue(any("离开商城时" in line for line in rt.logs), rt.logs)

    def test_a_mismatch_on_a_tab_tap_frame_stops_with_that_frame(self):
        # After the left list swipe: the tab is unselected on the frame waited for and on the deciding frame, but
        # already selected on the confirming frame. No second attempt (retry=False), that frame saved.
        game = Game()
        left = (SHOP, list_frame(0, selected=False), game.recognize)
        selected = (SHOP, list_frame(0), game.recognize)
        game.screens = {1: [left, left, selected]}
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(len(game.touches), 1)  # the left list swipe only: no tab tap, nothing after it
        saved, = stop_diagnostics(rt)
        self.assertIs(saved.kwargs["image"], selected[1])

    def test_a_card_read_that_stops_saves_the_frame_it_was_read_on(self):
        game = Game(ALL_VISIBLE)
        frames = [(SHOP, list_frame(0), game.recognize) for _ in range(4)]
        game.screens = {2: list(frames)}
        with patch.object(shop, "read_gifts", side_effect=shop.Stop("同一张画面上同一个礼包的名字出现了两次")):
            outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertNotIn("back", game.touches)
        saved, = stop_diagnostics(rt)
        self.assertIs(saved.kwargs["image"], frames[3][1])  # the scan's settled frame, the one read

    def test_an_unexpected_frame_after_the_back_arrow_is_saved(self):
        game = Game()
        game.screens = {}
        outcome, rt = play(game)  # the plain run, to count its touches
        count = len(game.touches)
        game = Game(screens={count: [POPUP]})
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches[-1], "back")
        names = [c.args[0] for c in rt.diagnostic.call_args_list]
        self.assertEqual(names, ["android_weekly_stop_0"])
        self.assertIs(stop_diagnostics(rt)[0].kwargs["image"], POPUP[1])

    def test_a_tab_recognition_that_could_not_run_allows_no_sidebar_swipe(self):
        game = Game(tab_error=True)
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, [])
        self.assertEqual(len(stop_diagnostics(rt)), 1)  # waited through briefly as not recognised yet, then stopped

    def test_no_safe_place_to_start_a_swipe_sends_nothing(self):
        # A forbidden word next to every candidate column: the list is not swiped at all.
        words = [row("购买", [shop.LIST[0], 120, shop.LIST[2], 580])]  # clear of the shortcut, the tab and the left list
        game = Game(forbidden=words)
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.swipes, 0)
        self.assertTrue(any("找不到可以安全开始滑动的空白处" in line for line in rt.logs))

    def test_a_modal_at_the_start_sends_nothing(self):
        modal = Screen("modal", {"close": [929, 177, 36, 36]}, title="兑换奖励")
        game = Game(start="modal")
        image = np.zeros((720, 1280, 3), np.uint8)
        game.screen_now = lambda rt: (modal, image, game.recognize)
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, [])
        saved, = rt.diagnostic.call_args_list  # the screen it would not start from
        self.assertEqual(saved.args[0], "android_weekly_start")
        self.assertIs(saved.kwargs["image"], image)

    def test_the_tab_already_selected_is_not_tapped_again(self):
        game = Game(start="hud")
        game.key = lambda name: setattr(game, "where", "gifts") or True
        outcome, _rt = play(game)
        self.assertTrue(outcome)
        self.assertNotIn("tab", game.touches)
        self.assertFalse(any(t[1][0] < shop.LIST[0] for t in game.touches if isinstance(t, tuple)))


class EntryRecoveryTests(unittest.TestCase):
    """The way into the shop (review round 2): the HUD shortcut only from the HUD, one attempt; the way back to the
    HUD before the task taps each back arrow once and waits for the page to go."""

    def hud(self, game, recognize=None, image=None):
        return HUD, np.zeros((720, 1280, 3), np.uint8) if image is None else image, recognize or game.recognize

    def test_the_shortcut_never_cancels_a_quit_dialog_or_passes_a_modal(self):
        modal = (Screen("modal", {"close": [929, 177, 36, 36]}, title="兑换奖励"), np.full((720, 1280, 3), 9, np.uint8),
                 lambda node, override=None: MISS)
        for label, frames in (("quit on the deciding frame", lambda g: [self.hud(g), QUIT]),
                              ("quit on the confirming frame", lambda g: [self.hud(g), self.hud(g), QUIT]),
                              ("modal on the confirming frame", lambda g: [self.hud(g), self.hud(g), modal])):
            with self.subTest(label):
                game = Game()
                shown = frames(game)
                game.screens = {0: list(shown)}
                outcome, rt = play(game)
                self.assertFalse(outcome)
                self.assertEqual((game.touches, game.shortcuts), ([], 0))  # neither 取消 nor the shortcut
                saved, = stop_diagnostics(rt)
                self.assertIs(saved.kwargs["image"], shown[-1][1])

    def test_a_forbidden_word_by_the_shortcut_saves_the_refusing_frame(self):
        game = Game()
        image = np.full((720, 1280, 3), 5, np.uint8)
        words = lambda node, override=None: result([row("购买", [640, 70, 40, 20])]) \
            if node == pages.FORBIDDEN else game.recognize(node, override)  # noqa: E731
        game.screens = {0: [self.hud(game), self.hud(game), self.hud(game, words, image)]}
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.shortcuts, 0)
        saved, = stop_diagnostics(rt)
        self.assertIs(saved.kwargs["image"], image)

    def test_a_restart_in_the_open_shop_taps_back_once_then_runs(self):
        game = Game(start="gifts")
        outcome, rt = play(game)
        self.assertTrue(outcome)
        self.assertEqual(game.touches[0], "back")
        self.assertEqual(game.touches.count("back"), 2)  # the recovery and the way back after the scan
        self.assertEqual(game.shortcuts, 1)

    def test_a_shop_still_showing_after_its_back_arrow_is_not_tapped_again(self):
        # Review round 2 replay: the first back arrow did not take (yet); the next check still sees the shop.
        game = Game(start="gifts")
        still = [(SHOP, list_frame(0), game.recognize) for _ in range(60)]
        game.screens = {1: list(still)}
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, ["back"])
        saved, = rt.diagnostic.call_args_list
        self.assertEqual(saved.args[0], "android_weekly_start")
        self.assertTrue(any(saved.kwargs["image"] is frame[1] for frame in still))

    def test_a_modal_on_a_back_arrow_tap_frame_stops_with_that_frame(self):
        game = Game(start="gifts")
        modal = (Screen("modal", {"close": [929, 177, 36, 36]}, title="兑换奖励"), np.full((720, 1280, 3), 9, np.uint8),
                 lambda node, override=None: MISS)
        shop_frame = (SHOP, list_frame(0), game.recognize)
        game.screens = {0: [shop_frame, shop_frame, modal]}  # to_hud's look, the deciding frame, the confirming one
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, [])
        saved, = rt.diagnostic.call_args_list
        self.assertEqual(saved.args[0], "android_weekly_start")
        self.assertIs(saved.kwargs["image"], modal[1])

    def test_without_single_to_hud_keeps_its_old_behaviour(self):
        # The default (the mailbox) is unchanged: back arrows with the retrying tap, no wait for the page to go.
        rt = SimpleNamespace(stopped=False, _frame_still_valid=Mock(return_value=True))
        frames = iter([(SHOP, "a", None), (HUD, "b", None)])
        with patch.object(pages, "screen_now", side_effect=lambda rt: next(frames)), \
                patch.object(pages, "tap_on_page", return_value=True) as tap, \
                patch.object(pages, "left_page") as left:
            self.assertTrue(pages.to_hud(rt))
        tap.assert_called_once_with(rt, "商城", actions.BACK_ARROW)
        left.assert_not_called()


class ToHudSingleTests(unittest.TestCase):
    """pages.to_hud(single=True) on a scripted stack of screens (review round 3)."""

    @staticmethod
    def page(title):
        return Screen("page", {"back": BACK_BOX}, title=title)

    def run_stack(self, stack, inserts=None, attempts=4):
        """``stack``: screens from the top (shown) down; a back-arrow tap pops the top one. ``inserts``: frames shown
        instead, keyed by the number of taps sent (one per capture)."""
        stack, inserts, sent, rejected = list(stack), dict(inserts or {}), [], []
        images = {}

        def recognize(node, override=None):
            return result([row("", BACK_BOX)]) if node == actions.BACK_ARROW else MISS

        def screen_now(rt):
            if inserts.get(len(sent)):
                return inserts[len(sent)].pop(0)
            top = stack[0]
            image = images.setdefault(id(top), np.full((720, 1280, 3), len(images) + 1, np.uint8))
            return top, image, recognize

        def action(name, target=None, source_frame=None, **fields):
            assert name == "Click" and target == BACK_BOX and source_frame is not None
            sent.append(stack[0].title)
            stack.pop(0)
            return True
        rt = SimpleNamespace(stopped=False, _frame_still_valid=Mock(return_value=True), pause=Mock(return_value=True),
                             TOUCH_KEY_ATTEMPTS=3, action=action)
        with patch.object(pages, "screen_now", side_effect=screen_now), \
                patch("time.monotonic", side_effect=itertools.count(0, .25).__next__):
            outcome = pages.to_hud(rt, attempts, rejected=lambda frame, reason: rejected.append((frame, reason)),
                                   single=True)
        return outcome, sent, rejected

    def test_reaching_the_hud_after_the_last_allowed_back_arrow_is_success(self):
        stack = [self.page(t) for t in ("一", "二", "三", "四")] + [HUD]
        outcome, sent, rejected = self.run_stack(stack, attempts=4)
        self.assertTrue(outcome)
        self.assertEqual(sent, ["一", "二", "三", "四"])
        self.assertEqual(rejected, [])

    def test_a_dialog_or_popup_after_a_back_arrow_stops_with_that_frame(self):
        modal = (Screen("modal", {"close": [929, 177, 36, 36]}, title="兑换奖励"), np.full((720, 1280, 3), 9, np.uint8),
                 lambda node, override=None: MISS)
        for label, other in (("quit", QUIT), ("modal", modal), ("popup", POPUP)):
            with self.subTest(label):
                # Review round 3 replay: one Back, the dialog for one poll, then the same page again.
                shop_page = self.page("商城")
                again = (shop_page, np.full((720, 1280, 3), 4, np.uint8),
                         lambda node, override=None: result([row("", BACK_BOX)])
                         if node == actions.BACK_ARROW else MISS)
                outcome, sent, rejected = self.run_stack([shop_page, shop_page, HUD], {1: [other] + [again] * 10})
                self.assertFalse(outcome)
                self.assertEqual(sent, ["商城"])  # never a second Back
                (frame, _reason), = rejected
                self.assertIs(frame, other[1])

    def test_titles_must_match_exactly(self):
        # Review round 3 replay: the look sees 美鸭梨, the tap frames show 美鸭梨挖掘 (the same prefix): no tap.
        dig = (self.page("美鸭梨挖掘"), np.full((720, 1280, 3), 6, np.uint8),
               lambda node, override=None: result([row("", BACK_BOX)]) if node == actions.BACK_ARROW else MISS)
        outcome, sent, rejected = self.run_stack([self.page("美鸭梨"), HUD], {0: [(self.page("美鸭梨"), np.zeros(
            (720, 1280, 3), np.uint8), dig[2]), dig, dig, dig]})
        self.assertFalse(outcome)
        self.assertEqual(sent, [])
        (frame, _reason), = rejected
        self.assertIs(frame, dig[1])

    def test_the_wait_is_for_the_page_that_was_tapped(self):
        # 美鸭梨挖掘 tapped (its exact title on both tap frames), still showing afterwards: not "another page".
        stack = [self.page("美鸭梨挖掘"), HUD]
        dig = stack[0]
        outcome, sent, rejected = self.run_stack(stack, {1: [(dig, np.full((720, 1280, 3), 6, np.uint8),
                                                              lambda node, override=None: MISS)] * 40})
        self.assertFalse(outcome)
        self.assertEqual(sent, ["美鸭梨挖掘"])
        self.assertEqual(len(rejected), 1)
        self.assertIn("一直没有离开", rejected[0][1])


class CardTests(unittest.TestCase):
    def read(self, card, title_y=450):
        game = Game({"虹迹赠礼": {**card, "y": title_y}})
        return shop.read_card(game.recognize, "虹迹赠礼", game.titles()["虹迹赠礼"])

    def test_states(self):
        self.assertEqual(self.read(gift(0, 839)).state, "sold_out")
        self.assertEqual(self.read(gift(0, 839, pill=False, counter="本周限购1/1")).state, "claimable")
        self.assertEqual(self.read(gift(0, 839, pill=True, counter="本周限购1/1")).state, "sold_out")
        self.assertEqual(self.read(gift(0, 839, pill=False, counter="")).state, "unknown")
        self.assertEqual(self.read(gift(0, 839, band="7260", pill=False, counter="本周限购10/10")).state, "not_free")
        self.assertEqual(self.read(gift(0, 839)).limit, (0, 1))
        for impossible in ("本周限购1/0", "本周限购2/1", "本周限购0/0"):  # misreads: neither sold out nor claimable
            self.assertEqual(self.read(gift(0, 839, pill=False, counter=impossible)).state, "unknown", impossible)
            self.assertEqual(self.read(gift(0, 839, pill=True, counter=impossible)).state, "unknown", impossible)

    def test_a_card_not_wholly_in_the_list_is_not_read(self):
        self.assertIsNone(self.read(gift(0, 839), title_y=240))  # the counter is under the top bar
        self.assertIsNone(self.read(gift(0, 839), title_y=640))  # the band is below the frame
        self.assertIsNotNone(self.read(gift(0, 839), title_y=598))  # 星实置业之礼 at the list's end

    def test_the_same_gift_twice_on_one_frame_stops(self):
        twice = lambda node, override=None: result([row("芳馨赠礼", [794, 281, 90, 28]),  # noqa: E731
                                                    row("芳馨赠礼", [313, 598, 90, 28])]) \
            if node == shop.GIFT_TITLE else MISS
        with self.assertRaises(shop.Stop):
            shop.read_gifts(twice)


class TabTests(unittest.TestCase):
    def test_a_recognition_that_could_not_run_is_not_an_absent_tab(self):
        frame = list_frame(0)
        self.assertIsNone(shop.tab_state(frame, lambda node, override=None: None))
        self.assertEqual(shop.tab_state(frame, lambda node, override=None: MISS), "absent")
        found = lambda node, override=None: result([row("甄选礼包", TAB_BOX)])  # noqa: E731
        self.assertEqual(shop.tab_state(frame, found), "selected")
        self.assertEqual(shop.tab_state(list_frame(0, selected=False), found), "unselected")
        # Only a completed recognition without the label lets the sidebar be swiped.
        self.assertEqual(shop.shop_view(SHOP, frame, lambda node, override=None: None), "pending")
        self.assertEqual(shop.shop_view(SHOP, frame, lambda node, override=None: MISS), "absent")
        self.assertEqual(shop.shop_view(*QUIT), "other")
        self.assertEqual(shop.shop_view(*POPUP), "other")
        self.assertEqual(shop.shop_view(*unknown_frame()), "pending")
        for other in (BACK_ONLY, DUCK_ONLY, CHECK_FAILED):
            self.assertEqual(shop.shop_view(*other), "other")
        self.assertEqual(shop.shop_view(Screen("page", {"back": BACK_BOX}, title="奇迹之旅"), frame,
                                        lambda node, override=None: MISS), "other")


class SwipePointTests(unittest.TestCase):
    def test_a_price_band_excludes_the_whole_band_and_the_top_button_is_avoided(self):
        frame = list_frame(0)
        # 「免费」 text centred at x 840: a point 60 px right of the text is on the band, so it is excluded.
        prices = shop.price_zones(lambda node, override=None: result([row("免费", [812, 491, 57, 35])]))
        self.assertFalse(shop.points_clear(frame, None, (1060, 640), (900, 500), prices))
        self.assertTrue(shop.points_clear(frame, None, (1060, 640), (1060, 390), prices))
        self.assertFalse(shop.points_clear(frame, None, (1100, 640), (1100, 390), prices))  # 回到顶部
        # Outside the list (red dots on the left tabs read as digits) only the text itself counts.
        left = shop.price_zones(lambda node, override=None: result([row("8", [16, 222, 12, 16])]))
        self.assertFalse(any(shop._overlaps([130, 250, 1, 1], zone) for zone in left))

    def test_the_begin_point_must_be_empty_background(self):
        frame = list_frame(0)
        frame[520:700, 950:1120] = (250, 250, 250)  # a card's white panel over the lower empty column
        recognize = lambda node, override=None: MISS  # noqa: E731
        self.assertFalse(shop.blank(frame, (1060, 640)))
        begin, end = shop.choose_swipe(frame, recognize)
        self.assertTrue(shop.blank(frame, begin) and shop.blank(frame, end))
        self.assertFalse(950 - 30 < begin[0] < 1120 + 30 and begin[1] > 520 - shop.BLANK_HALF, begin)  # not on it
        frame[60:720, 240:1240] = BACKGROUND  # an empty list: the lowest begin right of centre, clear of 回到顶部
        self.assertEqual(shop.choose_swipe(frame, recognize), ((1060, 640), (1060, 390)))
        self.assertEqual(shop.choose_swipe(frame, recognize, avoid=(1060, 640)), ((1000, 640), (1000, 390)))
        # Empty background only near the top (device run 1): the stroke must still end inside the list.
        frame[340:720, 240:1240] = (250, 250, 250)  # the begin's 60 px patch fits only at y 310 and above
        begin, end = shop.choose_swipe(frame, recognize)
        self.assertEqual((begin[1], end[1]), (shop.LIST_TOP + shop.SWIPE_DISTANCE, shop.LIST_TOP))
        # Not tall enough for a full stroke: a shorter one, at the same speed.
        frame[330:720, 240:1240] = (250, 250, 250)
        begin, end = shop.choose_swipe(frame, recognize)
        self.assertEqual((begin, end), ((1150, 300), (1150, 100)))
        self.assertEqual(shop.stroke_ms(begin, end), 800)
        frame[160:720, 240:1240] = (250, 250, 250)  # no room even for the shortest stroke
        self.assertIsNone(shop.choose_swipe(frame, recognize))

    def test_an_end_on_empty_background_is_preferred(self):
        # Review round 2: when a stroke with both ends on empty background fits, one is used.
        frame = list_frame(0)
        frame[60:720, 240:1240] = BACKGROUND
        frame[330:450, 300:1240] = (250, 250, 250)  # a row of cards above the empty lower part of the list
        recognize = lambda node, override=None: MISS  # noqa: E731
        self.assertFalse(shop.points_clear(frame, recognize, (1060, 640), (1060, 390), []))
        begin, end = shop.choose_swipe(frame, recognize)
        self.assertEqual(begin[1] - end[1], shop.SWIPE_DISTANCE)
        for point in (begin, end):
            self.assertTrue(shop.blank(frame, point), point)
            self.assertFalse(330 - shop.BLANK_HALF < point[1] < 450 + shop.BLANK_HALF, point)

    def test_the_end_falls_back_to_a_card_only_when_no_blank_stroke_fits(self):
        # Device run 2 (839474c8): cards everywhere above y ~560, empty background only below. The press stays on
        # empty background; the lift may then lie on a card's illustration (still clear of price zones and words).
        frame = list_frame(0)
        frame[60:720, 240:1240] = BACKGROUND
        frame[60:560, 270:1180] = (250, 250, 250)
        recognize = lambda node, override=None: MISS  # noqa: E731
        begin, end = shop.choose_swipe(frame, recognize)
        self.assertEqual((begin, end), ((1060, 640), (1060, 390)))
        self.assertTrue(shop.blank(frame, begin))
        self.assertFalse(shop.blank(frame, end))
        self.assertTrue(shop.points_clear(frame, recognize, begin, end, [], end_blank=False))
        self.assertFalse(shop.points_clear(frame, recognize, begin, end, []))  # a blank-end choice would not hold
        # A price band where the lift would be: that end is never used; the press is never on a card.
        band = lambda node, override=None: result([row("¥98", [1000, 380, 60, 24])]) \
            if node == shop.PRICE_GUARD else MISS  # noqa: E731
        choice = shop.choose_swipe(frame, band)
        self.assertNotEqual(choice[1], (1060, 390))
        self.assertTrue(shop.blank(frame, choice[0]))
        frame[60:720, 270:1180] = (250, 250, 250)  # no empty background for the press at all
        self.assertIsNone(shop.choose_swipe(frame, recognize))

    def test_a_failed_check_offers_no_swipe(self):
        self.assertIsNone(shop.choose_swipe(list_frame(0), lambda node, override=None: None))


class SwipeOnPageTests(unittest.TestCase):
    def rt(self, frames):
        sent = []
        rt = SimpleNamespace(TOUCH_KEY_ATTEMPTS=3, _frame_still_valid=Mock(return_value=True),
                             pause=Mock(return_value=True), sent=sent,
                             action=lambda name, **fields: sent.append((name, fields)) or True)
        return rt, patch.object(pages, "screen_now", side_effect=frames)

    def frame(self, words=(), screen=SHOP, tag=0):
        image = np.full((720, 1280, 3), tag, np.uint8)
        return screen, image, lambda node, override=None: result(words) if node == pages.FORBIDDEN else MISS

    def test_the_swipe_is_sent_on_the_confirming_frame_through_the_touch_path(self):
        deciding, confirming = self.frame(tag=1), self.frame(tag=2)
        rt, frames = self.rt([deciding, confirming])
        with frames:
            self.assertTrue(pages.swipe_on_page(rt, "商城", (1070, 640), (1070, 390), duration=1000,
                                                accept=lambda frame, recognize: True))
        (name, fields), = rt.sent
        self.assertEqual(name, "Swipe")
        self.assertEqual((fields["begin"], fields["end"], fields["duration"]), ([1070, 640], [1070, 390], 1000))
        self.assertIs(fields["source_frame"], confirming[1])

    def test_a_forbidden_word_near_either_point_refuses_on_either_frame(self):
        near_end = [row("¥6", [1050, 370, 40, 27])]
        for frames in ([self.frame(near_end)], [self.frame(), self.frame(near_end)]):
            rt, patched = self.rt(frames)
            with patched, self.assertRaises(pages.GuardRefusal):
                pages.swipe_on_page(rt, "商城", (1070, 640), (1070, 390), duration=1000,
                                    accept=lambda frame, recognize: True)
            self.assertEqual(rt.sent, [])

    def test_the_page_and_accept_must_hold_on_both_frames(self):
        rt, patched = self.rt([self.frame(screen=HUD)])
        with patched, self.assertRaises(pages.PageMismatch):
            pages.swipe_on_page(rt, "商城", (1070, 640), (1070, 390), duration=1000, accept=lambda f, r: True)
        rt, patched = self.rt([self.frame(tag=1)])
        with patched, self.assertRaises(pages.PageMismatch):
            pages.swipe_on_page(rt, "商城", (1070, 640), (1070, 390), duration=1000, accept=lambda f, r: False)
        # accept fails on the confirming frame: not decided again; that frame goes back with the refusal.
        frames = [self.frame(tag=1), self.frame(tag=2), self.frame(tag=1), self.frame(tag=1)]
        rt, patched = self.rt(frames)
        with patched, self.assertRaises(pages.SwipeMismatch) as raised:
            pages.swipe_on_page(rt, "商城", (1070, 640), (1070, 390), duration=1000,
                                accept=lambda frame, recognize: frame[0, 0, 0] == 1)
        self.assertEqual(raised.exception.frame[0, 0, 0], 2)
        self.assertIsInstance(raised.exception, pages.FrameMismatch)  # the run's FrameMismatch path saves it too
        self.assertEqual(rt.sent, [])
        # Another page on the confirming frame: the same.
        rt, patched = self.rt([self.frame(tag=1), self.frame(screen=HUD, tag=3)])
        with patched, self.assertRaises(pages.SwipeMismatch) as raised:
            pages.swipe_on_page(rt, "商城", (1070, 640), (1070, 390), duration=1000, accept=lambda f, r: True)
        self.assertEqual(raised.exception.frame[0, 0, 0], 3)
        self.assertEqual(rt.sent, [])

    def test_a_focus_change_at_dispatch_is_decided_again(self):
        from task_control import ForegroundChanged
        frames = [self.frame(tag=1), self.frame(tag=2), self.frame(tag=3), self.frame(tag=4)]
        rt, patched = self.rt(frames)
        calls = []

        def action(name, **fields):
            calls.append(fields["source_frame"][0, 0, 0])
            if len(calls) == 1:
                raise ForegroundChanged("lost")
            return True
        rt.action = action
        with patched:
            self.assertTrue(pages.swipe_on_page(rt, "商城", (1070, 640), (1070, 390), duration=1000,
                                                accept=lambda f, r: True))
        self.assertEqual(calls, [2, 4])


class EntryTests(unittest.TestCase):
    def test_the_weekly_shop_entry_branches_to_the_android_flow_on_a_touch_target_only(self):
        from action import gameplay
        runtime = SimpleNamespace(stopped=False, log=Mock(), main=Mock())
        argv = SimpleNamespace(custom_action_param=json.dumps({"kind": "weekly_shop"}),
                               task_detail=SimpleNamespace(entry="MaaNikki_WeeklyFreeGifts"))
        weekly = Mock()
        weekly.return_value.run.return_value = True
        with patch.object(gameplay, "Executors", return_value=runtime), patch.object(gameplay, "Gameplay"), \
                patch("daily.shop.WeeklyShop", weekly):
            with patch("host.game_backend", return_value="android"), \
                    patch.object(shop, "run", return_value=True) as run:
                self.assertTrue(gameplay.GameplayAction.run.__wrapped__(None, None, argv))
            run.assert_called_once_with(runtime)
            weekly.assert_not_called()  # the PC class (which reads the UID) is not even built
            with patch("host.game_backend", return_value="win32"), patch.object(shop, "run") as run:
                self.assertTrue(gameplay.GameplayAction.run.__wrapped__(None, None, argv))
            run.assert_not_called()
            weekly.assert_called_once_with(runtime)


class ResourceTests(unittest.TestCase):
    def test_nodes_tier_and_guard_words(self):
        tiers = json.loads((ROOT / "agent/host/android/tasks.json").read_text(encoding="utf-8"))
        self.assertIn("MaaNikki_WeeklyFreeGifts", tiers["experimental"])
        note = tiers["notes"]["MaaNikki_WeeklyFreeGifts"]
        # Shown at the task for an Adb instance (MacTaskMarks: the warning line and the hover text), against the shared
        # description, which promises claiming: the note must say plainly that Android only checks and reports.
        self.assertTrue(note.startswith("安卓上这一版只检查并报告"), note)
        for words in ("从不领取", "请手动领取", "2026-11-26 在设备上完整跑通", "领取的弹窗还没见过"):
            self.assertIn(words, note)
        self.assertEqual(tiers["entries"], [])
        if not (ROOT / "resource_android").is_dir():  # the release source archive does not list it (plan risk 13)
            self.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
            return
        nodes = json.loads((ROOT / "resource_android/pipeline/android_shop.json").read_text(encoding="utf-8"))
        self.assertEqual(set(nodes), {shop.GIFTS_TAB, shop.GIFT_TITLE, shop.CARD_TEXT, shop.SOLD_OUT,
                                      shop.WEEKLY_LIMIT, shop.PRICE_GUARD})
        title = re.compile(nodes[shop.GIFT_TITLE]["expected"])
        for name in shop.GIFTS:
            self.assertTrue(title.search(name))
        for text in ("虹迹赠礼·一", "斑斓虹染礼包·二", "芳馨", "星实置业之礼包"):
            self.assertFalse(title.search(text), text)
        price = re.compile(nodes[shop.PRICE_GUARD]["expected"])
        for text in ("¥6", "￥396¥298", "¥61¥6", "8060", "18560", "100", "免费", "购买", "4365"):
            self.assertTrue(price.search(text), text)
        for text in ("本周限购0/1", "剩余限购1/1", "虹迹赠礼", "x80", "X10000", "-25%", "4日", "甄选礼包", "UID 117297341"):
            self.assertFalse(price.search(text), text)
        limit = re.compile(nodes[shop.WEEKLY_LIMIT]["expected"])
        self.assertTrue(limit.search("本周限购0/1") and limit.search("本周限购 10/10"))
        self.assertFalse(limit.search("剩余限购1/1"))
        forbidden = re.compile(json.loads((ROOT / "resource_android/pipeline/android_mailbox.json")
                                          .read_text(encoding="utf-8"))[pages.FORBIDDEN]["expected"])
        for text in ("¥6", "￥124￥45", "购买", "星石获取", "共鸣商店"):
            self.assertTrue(forbidden.search(text), text)
        for text in ("甄选礼包", "免费", "已售罄", "本周限购0/1", "购物车", "商城"):
            self.assertFalse(forbidden.search(text), text)


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
from host.android import actions, pages, shop, touch_point

fixtures = repo / "scripts/fixtures/android"
crop = lambda name: cv2.imread(str(fixtures / name))
# UID-free crops of the survey frames (c3-shop-*), put back where they were on a flat list background.
title, selected, unselected, tabs = (crop(n) for n in ("shop_title.png", "shop_tab_selected.png",
                                                       "shop_tab_unselected.png", "shop_tabs_forbidden.png"))
stop_list = crop("shop_run1_stop_list.jpg")  # device run 1's stop frame, x 250-1240, y 60-420 (no UID)
fangxin, xingshi, hongji, currency, paid = (crop(f"shop_card_{n}.png") for n in
                                            ("fangxin", "xingshi", "hongji", "currency", "paid"))
# An unseen claimable card, made up: 虹迹赠礼 without its 已售罄 pill and with a counter of 「本周限购10/10」.
claimable = hongji.copy()
claimable[0:34, 70:212] = currency[0:34, 70:212]
claimable[100:142, 55:160] = currency[100:142, 55:160]

def compose(tab, cards):
    frame = np.full((720, 1280, 3), (193, 226, 238), np.uint8)
    frame[0:60, :] = (120, 170, 200)
    for image, x, y in [(title, 0, 0), (tab, 0, 315), (tabs, 60, 465), *cards]:
        frame[y:y + image.shape[0], x:x + image.shape[1]] = image
    return frame

frames = {
    "list end": compose(selected, [(fangxin, 733, 118), (xingshi, 270, 430)]),
    "hongji row": compose(selected, [(hongji, 733, 285), (currency, 270, 285), (paid, 962, 180)]),
    "claimable": compose(selected, [(claimable, 733, 285), (currency, 270, 285)]),
    "tab unselected": compose(unselected, []),
    "run 1 stop": compose(selected, [(stop_list, 250, 60)]),
    # A full row of cards above empty list (as c3-shop-gifts-00/-02, where a 250 px stroke from the empty part
    # ended inside a paid card): the stroke must end on the empty part too.
    "card row": compose(selected, [(currency, x, 180) for x in (270, 500, 733, 962)]),
    # Device run 2's stop (839474c8): two full rows of cards, a third row with one card, empty background only at
    # x ~500-1230 below y ~575. No stroke with both ends blank fits; the lift falls back to a card.
    "two full rows": compose(selected, [(currency, x, y) for x in (270, 500, 733, 962) for y in (65, 320)]
                             + [(currency[:145], 270, 575)]),
}
frames["run 1 stop"][420:720, 250:1240] = 250  # below the crop: white card panels, not background
current, results = {}, {}

class Frame(CustomController):
    def connect(self): return True
    def request_uuid(self): return "android-weekly-test"
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
        out = {"kind": screen.kind, "title": screen.title, "tab": shop.tab_state(image, recognize)}
        box = shop.gifts_tab(recognize)
        out["tab_tap"] = touch_point(box) if box else None
        out["tab_refused"] = pages._forbidden_near(recognize, box) if box else None
        if screen.kind == "page":
            out["back_refused"] = pages._forbidden_near(recognize, screen.targets["back"])
            zones = shop.price_zones(recognize)
            out["back_in_price_zone"] = any(shop._overlaps(screen.targets["back"], z) for z in zones)
        out["cards"] = {n: [c.state, c.band, c.sold_out_pill, list(c.limit) if c.limit else None]
                        for n, c in shop.read_gifts(recognize).items()}
        choice = shop.choose_swipe(image, recognize)
        out["swipe"] = choice and [list(choice[0]), list(choice[1]),
                                   [pages._forbidden_near(recognize, [*p, 1, 1]) for p in choice]]
        out["prices"] = [t for t, _b in shop._rows(recognize(shop.PRICE_GUARD))]
        out["forbidden"] = [t for t, _b in shop._rows(recognize(pages.FORBIDDEN))]
        # Right beside the ¥ band of the paid card (text at x 1009-1132, y 236-260): refused by the guard.
        out["beside_paid_band"] = pages._forbidden_near(recognize, [1070, 290, 1, 1])
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
    """The real nodes and OCR, both layers loaded, on frames composed from UID-free crops of the saved 商城 frames
    (in a fresh interpreter, as in test_android_monthly.py)."""

    def test_the_gift_cards_tab_swipe_points_and_guards(self):
        if not (ROOT / "resource_android").is_dir():
            self.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
            return
        env = {**os.environ, "MAAFW_BINARY_PATH": str(ROOT / "maafw")}
        # -X utf8: the child prints Chinese text, and -I ignores PYTHONUTF8 (Windows consoles default to cp1252).
        completed = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", "-c", FRAMEWORK_CHECK, str(ROOT)],
                                   capture_output=True, text=True, encoding="utf-8", timeout=300, env=env,
                                   cwd=tempfile.gettempdir())
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        outcome = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual({name: (value["kind"], value["title"]) for name, value in outcome.items()},
                         {name: ("page", "商城") for name in outcome})
        self.assertEqual({name: value["cards"] for name, value in outcome.items()}, {
            "list end": {"芳馨赠礼": ["sold_out", "免费", True, [0, 1]],
                         "星实置业之礼": ["sold_out", "免费", True, [0, 1]]},
            "hongji row": {"虹迹赠礼": ["sold_out", "免费", True, [0, 1]]},
            "claimable": {"虹迹赠礼": ["claimable", "免费", False, [10, 10]]},
            "tab unselected": {}, "run 1 stop": {}, "card row": {}, "two full rows": {}})
        self.assertEqual({name: value["tab"] for name, value in outcome.items()},
                         {"list end": "selected", "hongji row": "selected", "claimable": "selected",
                          "tab unselected": "unselected", "run 1 stop": "selected", "card row": "selected",
                          "two full rows": "selected"})
        for value in outcome.values():
            x, y = value["tab_tap"]
            self.assertLessEqual(max(abs(x - 129), abs(y - 348)), 6)  # the survey tapped (130,350)
            self.assertIsNone(value["tab_refused"])
            self.assertIsNone(value["back_refused"])
            self.assertFalse(value["back_in_price_zone"])
            self.assertIn("星石获取", value["forbidden"])
            self.assertIn("共鸣商店", value["forbidden"])
        for name in ("list end", "hongji row", "claimable", "run 1 stop"):
            begin, end, refused = outcome[name]["swipe"]
            self.assertEqual(refused, [None, None])
            self.assertEqual(begin[1] - end[1], shop.SWIPE_DISTANCE)
        row = outcome["hongji row"]
        self.assertTrue({"8060", "免费", "￥396¥298"} <= set(row["prices"]) or {"8060", "免费", "¥396¥298"}
                        <= set(row["prices"]), row["prices"])
        self.assertTrue(any("¥" in text or "￥" in text for text in row["forbidden"]), row["forbidden"])
        self.assertIsNotNone(row["beside_paid_band"])
        self.assertEqual(outcome["list end"]["swipe"][0], [1060, 640])
        # Run 1 stopped here: the only empty background is above the 颂愿感悟之礼 header (x ~500-1180, y ~80-355).
        begin, end, _refused = outcome["run 1 stop"]["swipe"]
        self.assertTrue(500 <= begin[0] <= 1180 and 310 <= begin[1] <= 360, begin)
        self.assertGreaterEqual(end[1], shop.LIST_TOP)
        self.assertIn("¥", "".join(outcome["run 1 stop"]["forbidden"]) + "".join(outcome["run 1 stop"]["prices"]))
        # Below the row of cards (y 180-430): both ends on the empty part, so a shorter stroke.
        begin, end, refused = outcome["card row"]["swipe"]
        self.assertEqual(refused, [None, None])
        self.assertGreaterEqual(end[1], 430 + shop.BLANK_HALF)
        self.assertLess(begin[1] - end[1], shop.SWIPE_DISTANCE)
        begin, end, refused = outcome["two full rows"]["swipe"]
        self.assertEqual(refused, [None, None])
        self.assertTrue(500 <= begin[0] <= 1230 and begin[1] >= 575 + shop.BLANK_HALF, begin)
        self.assertLess(end[1], 575)  # on a card: the fallback


if __name__ == "__main__":
    unittest.main()
