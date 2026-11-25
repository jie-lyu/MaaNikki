"""Android 奇迹之旅奖励 flow (agent/host/android/monthly.py): python scripts/test_android_monthly.py"""
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

from host.android import actions, monthly, pages  # noqa: E402
from host.android.actions import Screen  # noqa: E402

GOLD, GREY, BLUE = (70, 150, 200), (150, 152, 155), (220, 60, 20)  # BGR: gold in both HSV ranges; grey; blue


def hit(box, text=""):
    return SimpleNamespace(hit=True, box=box, best_result=SimpleNamespace(text=text, box=box))


MISS = SimpleNamespace(hit=False, box=None, best_result=None)
HUD = Screen("hud", {"duck": [30, 18, 54, 54]})
PAGE = Screen("page", {"back": [37, 22, 40, 40]}, title="奇迹之旅")
TAB_BOXES = {monthly.TREASURE: [25, 200, 88, 30], monthly.TASK: [23, 313, 90, 31]}
CLAIM_BOX = [738, 635, 100, 38]  # where the real framework finds 「键领取」 on the saved frames
BACK_BOX = [37, 22, 40, 40]


def paint(frame, box, colour, fraction=1.0):
    x, y, w, h = box
    frame[y:y + h, x:x + round(w * fraction)] = colour


def view(tab, button, *, label=None, gold_tabs=None, content=True, extra=None):
    """``(screen, frame, recognize)`` for the 奇迹之旅 page on ``tab`` with the button ``enabled`` (gold, labelled),
    ``none`` (no button), ``grey`` (labelled, greyed), ``odd`` (a quarter gold), ``unlabelled`` (gold, no label) or
    ``blue`` (labelled, a saturated blue face); ``content=False`` hides the tab's content marker, ``extra`` adds nodes."""
    frame = np.zeros((720, 1280, 3), np.uint8)
    for gold_tab in gold_tabs or (tab,):
        paint(frame, monthly.TAB_LABELS[gold_tab], GOLD)
    face = {"enabled": (GOLD, 1), "unlabelled": (GOLD, 1), "grey": (GREY, 1), "odd": (GOLD, .25), "none": (GREY, 1),
            "blue": (BLUE, 1)}
    paint(frame, monthly.BUTTON_FACE, *face[button])
    label = button in ("enabled", "grey", "odd", "blue") if label is None else label
    nodes = {node: hit(TAB_BOXES[name]) for name, node in monthly.TAB_NODES.items()}
    nodes[actions.BACK_ARROW] = hit(BACK_BOX)
    if content:
        nodes[monthly.CONTENT_NODES[tab]] = hit([198, 215, 99, 32])
    if label:
        nodes[monthly.CLAIM_ALL] = hit(CLAIM_BOX, "键领取")
    nodes.update(extra or {})
    return PAGE, frame, lambda node, override=None: nodes.get(node, MISS)


def other(screen, nodes=None):
    nodes = nodes or {}
    return screen, np.zeros((720, 1280, 3), np.uint8), lambda node, override=None: nodes.get(node, MISS)


UNKNOWN = other(Screen("unknown", reason="认不出当前画面"))
SHOP = other(Screen("page", {"back": BACK_BOX}, title="商城"), {actions.BACK_ARROW: hit(BACK_BOX)})
_layer_nodes = {monthly.LAYER_TITLE: hit([570, 165, 136, 42], "恭喜获得"),
                monthly.LAYER_HINT: hit([569, 640, 140, 19], "点击空白区域继续"),
                monthly.LAYER_OVER_MONTHLY[0]: hit([20, 10, 200, 60]),
                monthly.LAYER_OVER_MONTHLY[1]: hit([950, 620, 310, 85])}
REWARD_LAYER = other(Screen("unknown", reason="认不出当前画面"), _layer_nodes)
# The layer's texts over another page (no 奇迹之旅 landmark beneath): not the layer this task may tap.
LAYER_ELSEWHERE = other(Screen("unknown", reason="认不出当前画面"),
                        {k: v for k, v in _layer_nodes.items() if k not in monthly.LAYER_OVER_MONTHLY})


class Game:
    """A scripted 奇迹之旅 page that reacts to the taps the real pages.tap_on_page / tap_on_layer send.

    ``buttons``: the 一键领取 state per tab. ``after_claim``: per tab, the frames shown (one per capture) after a
    claim tap before the page shows again; "layer" there means the reward layer stays until a tap on blank space.
    ``tabs_switch``: whether a tab tap changes the tab. ``dismiss_closes``: whether that blank tap closes the layer.
    ``script``: frames shown before the state, one per capture (e.g. a page still settling); ``after_entry`` and
    ``after_tab``: frames shown after the 奇迹之旅 shortcut or a tab tap."""

    def __init__(self, tab=monthly.TREASURE, buttons=None, after_claim=None, *, tabs_switch=True,
                 dismiss_closes=True, start="hud", extra=None, script=(), after_entry=(), after_tab=()):
        self.tab, self.where = tab, start
        self.after_entry, self.after_tab = list(after_entry), list(after_tab)
        self.buttons = {monthly.TASK: "none", monthly.TREASURE: "none", **(buttons or {})}
        self.after_claim, self.tabs_switch, self.dismiss_closes = after_claim or {}, tabs_switch, dismiss_closes
        self.extra, self.queue, self.taps = extra or {}, list(script), []

    def frame(self):
        if self.queue:
            item = self.queue.pop(0)
            if item == "layer":  # the reward layer stays from here on, until a tap on blank space
                self.where = "layer"
            elif not isinstance(item, str):
                return item
        if self.where == "hud":
            return other(HUD)
        if self.where == "layer":
            return REWARD_LAYER
        return view(self.tab, self.buttons[self.tab], extra=self.extra)

    def screen_now(self, rt):
        return self.frame()

    def key(self, name):
        assert name == "monthly" and self.where == "hud"
        self.where = "page"
        self.queue = list(self.after_entry) + self.queue
        return True

    def click(self, name, target, source_frame):
        assert name == "Click"
        if target == list(monthly.DISMISS_POINT):
            self.taps.append("dismiss")
            if self.dismiss_closes:
                self.where = "page"
            return True
        if target == BACK_BOX:
            self.taps.append("back")
            self.where = "hud"
            return True
        if target == CLAIM_BOX:
            self.taps.append("claim")
            self.buttons[self.tab] = "none"
            self.queue = list(self.after_claim.get(self.tab, ()))
            return True
        for tab, box in TAB_BOXES.items():
            if target == box:
                self.taps.append(tab)
                if self.tabs_switch:
                    self.tab = tab
                self.queue = list(self.after_tab) + self.queue
                return True
        raise AssertionError(f"unexpected tap {target}")


class ViewTests(unittest.TestCase):
    def read(self, *args, **kwargs):
        _screen, frame, recognize = view(*args, **kwargs)
        return monthly.read_view(frame, recognize)

    def test_tabs_and_button_states(self):
        self.assertEqual((v := self.read(monthly.TASK, "enabled")).tab, monthly.TASK)
        self.assertEqual(v.button, "enabled")
        self.assertEqual(self.read(monthly.TREASURE, "none").button, "none")
        self.assertEqual(self.read(monthly.TASK, "grey").button, "none")  # greyed counts as nothing to claim
        self.assertEqual(self.read(monthly.TASK, "odd").button, "unknown")
        self.assertEqual(self.read(monthly.TASK, "unlabelled").button, "unknown")  # gold without its label
        # Review round 2: off is positively a low-saturation face; any other colour (e.g. blue) is unknown.
        self.assertEqual(self.read(monthly.TASK, "blue").button, "unknown")
        self.assertEqual(self.read(monthly.TASK, "blue", label=False).button, "unknown")

    def test_an_unsettled_page_has_no_view(self):
        # Both tabs gold (a switch in progress), or the selected tab without its own content (a layer over it).
        self.assertIsNone(self.read(monthly.TASK, "enabled", gold_tabs=(monthly.TASK, monthly.TREASURE)))
        _screen, frame, recognize = view(monthly.TASK, "enabled")
        without_content = lambda node, override=None: MISS if node in monthly.CONTENT_NODES.values() \
            else recognize(node)  # noqa: E731
        self.assertIsNone(monthly.read_view(frame, without_content))
        dimmed = (frame * .45).astype(np.uint8)  # a dim layer: the tab is no longer gold
        self.assertIsNone(monthly.read_view(dimmed, recognize))


class FlowTests(unittest.TestCase):
    """monthly.run with the real page taps (decided and confirmed by read_view on both frames) over a Game."""

    def run_game(self, game):
        rt = SimpleNamespace(stopped=False, logs=[], key=game.key, leave_page=Mock(return_value=True),
                             _frame_still_valid=Mock(return_value=True), pause=Mock(return_value=True),
                             diagnostic=Mock(), TOUCH_KEY_ATTEMPTS=3, action=game.click)
        rt.log = rt.logs.append
        with patch.object(pages, "screen_now", side_effect=game.screen_now), \
                patch("time.monotonic", side_effect=itertools.count(0, .5).__next__):
            result = monthly.run(rt)
        return result, rt

    def test_a_gold_claim_on_the_task_tab_is_claimed_and_checked_on_settled_frames(self):
        game = Game(buttons={monthly.TASK: "enabled"})
        result, rt = self.run_game(game)
        self.assertTrue(result)
        self.assertEqual(game.taps, [monthly.TASK, "claim", monthly.TREASURE, "back"])
        self.assertIn("旅行任务奖励已领取。", rt.logs)
        self.assertIn("旅行秘宝没有可领取的奖励。", rt.logs)
        rt.diagnostic.assert_not_called()

    def test_nothing_to_claim_taps_only_the_tabs_and_the_way_back(self):
        game = Game()
        result, rt = self.run_game(game)
        self.assertTrue(result)
        self.assertEqual(game.taps, [monthly.TASK, monthly.TREASURE, "back"])
        self.assertIn("旅行任务没有可领取的奖励。", rt.logs)

    def test_the_reward_layer_gets_one_tap_on_blank_space_then_the_page_is_checked(self):
        # Run 1: the 旅行秘宝 claim brought up 「恭喜获得」; a tap on blank space closes it.
        game = Game(tab=monthly.TASK, buttons={monthly.TREASURE: "enabled"}, after_claim={monthly.TREASURE: ["layer"]})
        result, rt = self.run_game(game)
        self.assertTrue(result)
        self.assertEqual(game.taps, [monthly.TREASURE, "claim", "dismiss", "back"])
        self.assertIn("旅行秘宝奖励已领取。", rt.logs)

    def test_a_layer_still_there_after_one_tap_stops(self):
        game = Game(buttons={monthly.TASK: "enabled"}, after_claim={monthly.TASK: ["layer"]}, dismiss_closes=False)
        result, rt = self.run_game(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, [monthly.TASK, "claim", "dismiss"])  # one, no more, no way back
        self.assertTrue(any("仍然没有关闭" in text for text in rt.logs))
        rt.diagnostic.assert_called()

    def test_the_first_unexpected_frame_after_a_claim_stops_with_nothing_more_sent(self):
        # Review round 1: claim -> another page -> reward layer must not get the dismissal tap.
        for frames in ([UNKNOWN, "layer"], [SHOP, "layer"], [view(monthly.TASK, "none"), UNKNOWN],
                       [view(monthly.TREASURE, "none")]):
            with self.subTest(frames=frames):
                game = Game(buttons={monthly.TASK: "enabled"}, after_claim={monthly.TASK: frames})
                result, rt = self.run_game(game)
                self.assertFalse(result)
                self.assertEqual(game.taps, [monthly.TASK, "claim"])
                rt.leave_page.assert_not_called()
                saved = [call.kwargs["image"] for call in rt.diagnostic.call_args_list]
                self.assertEqual(len(saved), 1)
                self.assertTrue(isinstance(saved[0], np.ndarray))
                self.assertNotIn("旅行任务奖励已领取。", rt.logs)

    def test_a_layer_not_over_this_page_is_not_tapped(self):
        game = Game(buttons={monthly.TASK: "enabled"}, after_claim={monthly.TASK: [LAYER_ELSEWHERE]})
        result, rt = self.run_game(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, [monthly.TASK, "claim"])

    def test_a_layer_gone_before_the_tap_needs_a_recognised_return_to_the_tab(self):
        # The layer is seen once, then the page is back on the very next frames: no tap, success.
        game = Game(buttons={monthly.TASK: "enabled"}, after_claim={monthly.TASK: [REWARD_LAYER]})
        result, rt = self.run_game(game)
        self.assertTrue(result)
        self.assertEqual(game.taps, [monthly.TASK, "claim", monthly.TREASURE, "back"])

    def test_a_wrong_frame_on_either_dismissal_frame_is_saved_and_stops(self):
        # Review round 2: 商城 on the dismissal's deciding frame (then the tab), or on its confirming frame (then the
        # layer again): no dismissal, no tab switch, the 商城 frame itself is saved.
        for frames in ([REWARD_LAYER, SHOP], [REWARD_LAYER, REWARD_LAYER, SHOP, "layer"]):
            with self.subTest(frames=len(frames)):
                game = Game(buttons={monthly.TASK: "enabled"}, after_claim={monthly.TASK: frames})
                result, rt = self.run_game(game)
                self.assertFalse(result)
                self.assertEqual(game.taps, [monthly.TASK, "claim"])
                saved = [call.kwargs["image"] for call in rt.diagnostic.call_args_list]
                self.assertEqual(len(saved), 1)
                self.assertIs(saved[0], SHOP[1])

    def test_an_unknown_button_after_the_claim_stops(self):
        # Review round 2: claim -> quarter-gold (or gold without its label) -> reward layer: stop at the first.
        for state in ("odd", "unlabelled", "blue"):
            with self.subTest(state=state):
                odd = view(monthly.TASK, state)
                game = Game(buttons={monthly.TASK: "enabled"}, after_claim={monthly.TASK: [odd, "layer"]})
                result, rt = self.run_game(game)
                self.assertFalse(result)
                self.assertEqual(game.taps, [monthly.TASK, "claim"])
                self.assertIs(rt.diagnostic.call_args.kwargs["image"], odd[1])

    def test_a_blue_button_before_the_claim_stops(self):
        # Review round 2 (risk 35): an unseen non-gold face is not "nothing to claim"; 旅行秘宝 is never claimed.
        game = Game(buttons={monthly.TASK: "blue", monthly.TREASURE: "enabled"})
        result, rt = self.run_game(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, [monthly.TASK])

    def test_an_unexpected_screen_while_opening_the_page_or_a_tab_stops(self):
        # Review round 2: one 商城 frame right after the shortcut, or while the tab settles.
        for game, taps in ((Game(buttons={monthly.TASK: "enabled"}, after_entry=[SHOP]), []),
                           (Game(buttons={monthly.TASK: "enabled"}, after_tab=[SHOP]), [monthly.TASK])):
            with self.subTest(taps=taps):
                result, rt = self.run_game(game)
                self.assertFalse(result)
                self.assertEqual(game.taps, taps)
                self.assertIs(rt.diagnostic.call_args.kwargs["image"], SHOP[1])

    def test_brief_transition_frames_are_waited_through_but_not_for_long(self):
        unsettled = view(monthly.TREASURE, "none", content=False)
        game = Game(after_entry=[UNKNOWN, unsettled], after_tab=[unsettled])
        result, rt = self.run_game(game)
        self.assertTrue(result)
        self.assertEqual(game.taps, [monthly.TASK, monthly.TREASURE, "back"])
        game = Game(after_entry=[UNKNOWN] * 10)
        result, rt = self.run_game(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, [])
        rt.diagnostic.assert_called()

    def test_a_button_that_stays_gold_after_the_claim_is_not_success(self):
        game = Game(buttons={monthly.TASK: "enabled"},
                    after_claim={monthly.TASK: [view(monthly.TASK, "enabled")] * 40})
        result, rt = self.run_game(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, [monthly.TASK, "claim"])

    def test_an_unclear_button_stops_before_any_claim(self):
        for state in ("odd", "unlabelled"):
            with self.subTest(state=state):
                game = Game(buttons={monthly.TASK: state})
                result, rt = self.run_game(game)
                self.assertFalse(result)
                self.assertEqual(game.taps, [monthly.TASK])
                rt.diagnostic.assert_called()

    def test_the_claim_needs_the_whole_view_on_both_tap_frames(self):
        # Review round 1: the first look is a valid enabled view, then the tab content is gone on the tap frames.
        nocontent = view(monthly.TASK, "enabled", content=False)
        game = Game(tab=monthly.TASK, buttons={monthly.TASK: "enabled"},
                    script=["", "", view(monthly.TASK, "enabled"), view(monthly.TASK, "enabled"), nocontent])
        result, rt = self.run_game(game)
        self.assertFalse(result)
        self.assertNotIn("claim", game.taps)
        self.assertIs(rt.diagnostic.call_args.kwargs["image"], nocontent[1])
        # Review round 3: gone (or 商城) only on the confirming frame: no second attempt, that frame is saved.
        for wrong in (nocontent, SHOP):
            with self.subTest(confirming=wrong[0].title):
                game = Game(tab=monthly.TASK, buttons={monthly.TASK: "enabled"},
                            script=["", "", view(monthly.TASK, "enabled"), view(monthly.TASK, "enabled"),
                                    view(monthly.TASK, "enabled"), wrong])
                result, rt = self.run_game(game)
                self.assertFalse(result)
                self.assertNotIn("claim", game.taps)
                self.assertIs(rt.diagnostic.call_args.kwargs["image"], wrong[1])

    def test_a_tab_tap_does_not_retry_either(self):
        game = Game(script=["", "", view(monthly.TREASURE, "none"), view(monthly.TREASURE, "none"), SHOP])
        result, rt = self.run_game(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, [])
        self.assertIs(rt.diagnostic.call_args.kwargs["image"], SHOP[1])

    def test_a_recognised_other_screen_is_not_waited_through(self):
        # Review round 3: the quit title alone, or an unknown popup's close mark, within the transition allowance.
        quit_title = other(Screen("unknown", reason="画面上像是有确认框"), {actions.QUIT_TITLE: hit([560, 180, 160, 40])})
        popup = other(Screen("unknown", reason="画面上有弹窗"), {actions.MODAL_CLOSE: hit([929, 177, 36, 36])})
        for wrong in (quit_title, popup):
            for game, taps in ((Game(buttons={monthly.TASK: "enabled"}, after_entry=[wrong]), []),
                               (Game(buttons={monthly.TASK: "enabled"}, after_tab=[wrong]), [monthly.TASK])):
                with self.subTest(screen=wrong[0].reason, taps=taps):
                    result, rt = self.run_game(game)
                    self.assertFalse(result)
                    self.assertEqual(game.taps, taps)
                    self.assertIs(rt.diagnostic.call_args.kwargs["image"], wrong[1])

    def test_a_start_that_is_not_the_hud_saves_that_frame(self):
        # Review round 3: restarted on the reward layer left by a stopped run: no tap, the layer frame is saved.
        game = Game(start="layer")
        result, rt = self.run_game(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, [])
        rt.diagnostic.assert_called_once()
        self.assertIs(rt.diagnostic.call_args.kwargs["image"], REWARD_LAYER[1])

    def test_no_tab_is_tapped_without_a_recognised_view_of_the_page(self):
        game = Game(script=["", ""] + [view(monthly.TREASURE, "none", content=False)] * 40)
        result, rt = self.run_game(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, [])
        rt.diagnostic.assert_called()

    def test_a_tab_that_does_not_settle_stops(self):
        game = Game(tabs_switch=False)
        result, rt = self.run_game(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, [monthly.TASK])
        self.assertTrue(any("旅行任务" in text for text in rt.logs))

    def test_a_guard_refusal_ends_all_input(self):
        game = Game(buttons={monthly.TASK: "enabled"}, extra={pages.FORBIDDEN: hit([850, 640, 100, 30], "购买")})
        result, rt = self.run_game(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, [monthly.TASK])
        # The user reads a fixed refusal; the OCR'd row is in the saved scene only (review d2-mailstart-r1, 6).
        self.assertFalse(any("购买" in text for text in rt.logs), rt.logs)
        self.assertTrue(any("危险按钮" in text for text in rt.logs), rt.logs)
        self.assertTrue(any("购买" in call.args[1] for call in rt.diagnostic.call_args_list))

    def test_a_modal_at_the_start_sends_nothing(self):
        game = Game(script=[other(Screen("modal", {"close": [929, 177, 36, 36]}, title="兑换奖励"))] * 10)
        result, rt = self.run_game(game)
        self.assertFalse(result)
        self.assertEqual(game.taps, [])

    def test_a_fatal_device_error_sends_nothing_more(self):
        from host import DeviceUnreachable
        with patch.object(monthly, "claim", side_effect=DeviceUnreachable("ADB 没有回应")):
            with self.assertRaises(DeviceUnreachable):
                self.run_game(Game())

    def test_the_way_back_leaves_only_the_monthly_page(self):
        rt = SimpleNamespace(stopped=False, _frame_still_valid=Mock(return_value=True))
        with patch.object(pages, "screen_now", return_value=SHOP), patch.object(pages, "tap_on_page") as tap:
            self.assertFalse(pages.back_through(rt, (monthly.TITLE,)))
        tap.assert_not_called()


class LayerTests(unittest.TestCase):
    def test_the_layer_is_recognised_only_positively(self):
        screen, _frame, recognize = REWARD_LAYER
        self.assertTrue(monthly.is_reward_layer(screen, recognize))
        only_title = lambda node, override=None: recognize(node) if node == monthly.LAYER_TITLE else MISS  # noqa
        self.assertFalse(monthly.is_reward_layer(screen, only_title))
        self.assertFalse(monthly.is_reward_layer(*LAYER_ELSEWHERE[::2]))  # no 奇迹之旅 page beneath
        for kind in ("page", "hud", "modal", "quit_dialog"):
            self.assertFalse(monthly.is_reward_layer(Screen(kind), recognize))
        not_run = lambda node, override=None: None if node == actions.MODAL_CLOSE else recognize(node)  # noqa
        self.assertFalse(monthly.is_reward_layer(screen, not_run))  # a guard check that did not run
        with_close = lambda node, override=None: hit([929, 177, 36, 36]) if node == actions.MODAL_CLOSE \
            else recognize(node)  # noqa: E731
        self.assertFalse(monthly.is_reward_layer(screen, with_close))


class LayerTapTests(unittest.TestCase):
    """pages.tap_on_layer: decided and confirmed on fresh frames, forbidden-word check, the confirming frame."""

    def rt(self, frames):
        rt = SimpleNamespace(TOUCH_KEY_ATTEMPTS=3, clicks=[], _frame_still_valid=Mock(return_value=True),
                             pause=Mock(return_value=True))
        frames = list(frames)
        rt.action = lambda name, target, source_frame: rt.clicks.append((name, target, source_frame)) or True
        return rt, (lambda rt_: frames.pop(0) if len(frames) > 1 else frames[0])

    def test_the_point_is_tapped_on_the_confirming_frame(self):
        confirming = (REWARD_LAYER[0], np.ones((720, 1280, 3), np.uint8), REWARD_LAYER[2])
        rt, source = self.rt([REWARD_LAYER, confirming])
        with patch.object(pages, "screen_now", side_effect=source):
            self.assertTrue(pages.tap_on_layer(rt, "奖励层", monthly.is_reward_layer, monthly.DISMISS_POINT))
        self.assertEqual(len(rt.clicks), 1)
        name, target, frame = rt.clicks[0]
        self.assertEqual((name, target), ("Click", list(monthly.DISMISS_POINT)))
        self.assertIs(frame, confirming[1])

    def test_nothing_is_tapped_off_the_layer_or_next_to_a_forbidden_word(self):
        rt, source = self.rt([UNKNOWN])
        with patch.object(pages, "screen_now", side_effect=source), self.assertRaises(pages.PageMismatch):
            pages.tap_on_layer(rt, "奖励层", monthly.is_reward_layer, monthly.DISMISS_POINT)
        nodes = dict(_layer_nodes, **{pages.FORBIDDEN: hit([560, 690, 30, 20], "购买")})
        near = (REWARD_LAYER[0], REWARD_LAYER[1], lambda node, override=None: nodes.get(node, MISS))
        rt, source = self.rt([near])
        with patch.object(pages, "screen_now", side_effect=source), self.assertRaises(pages.GuardRefusal):
            pages.tap_on_layer(rt, "奖励层", monthly.is_reward_layer, monthly.DISMISS_POINT)
        self.assertEqual(rt.clicks, [])

    def test_a_tap_frame_that_is_not_the_layer_goes_back_to_the_caller_without_a_second_attempt(self):
        # Review round 2: 商城 on the confirming frame, then the layer again: no retry, the 商城 frame is handed back.
        rt, source = self.rt([REWARD_LAYER, SHOP, REWARD_LAYER, REWARD_LAYER])
        with patch.object(pages, "screen_now", side_effect=source), self.assertRaises(pages.LayerMismatch) as caught:
            pages.tap_on_layer(rt, "奖励层", monthly.is_reward_layer, monthly.DISMISS_POINT)
        self.assertEqual(rt.clicks, [])
        self.assertIs(caught.exception.frame, SHOP[1])
        self.assertEqual(caught.exception.screen.title, "商城")


class EntryTests(unittest.TestCase):
    def test_the_shared_task_branches_to_the_android_flow_on_a_touch_target_only(self):
        from daily.gameplay import Gameplay
        rt = SimpleNamespace(ui=SimpleNamespace(), stopped=False, logs=[],
                             context=SimpleNamespace(get_node_data=Mock(return_value={})))
        rt.log = lambda text, **kwargs: rt.logs.append(text)
        with patch("host.game_backend", return_value="android"), \
                patch.object(monthly, "run", return_value=True) as run:
            self.assertTrue(Gameplay(rt).monthly())
        run.assert_called_once_with(rt)
        with patch("host.game_backend", return_value="win32"), patch.object(monthly, "run") as run:
            self.assertTrue(Gameplay(rt).monthly())  # nothing selected on the PC: skipped as before
        run.assert_not_called()
        self.assertIn("未选择奇迹之旅或乐园构想奖励，跳过。", rt.logs)


class ResourceTests(unittest.TestCase):
    def test_nodes_tier_and_forbidden_words(self):
        tiers = json.loads((ROOT / "agent/host/android/tasks.json").read_text(encoding="utf-8"))
        self.assertIn("MaaNikki_MonthlyPass", tiers["experimental"])
        self.assertIn("MaaNikki_MonthlyPass", tiers["notes"])
        self.assertEqual(tiers["entries"], [])  # verified only after device runs
        if not (ROOT / "resource_android").is_dir():  # the release source archive does not list it (plan risk 13)
            self.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
            return
        nodes = json.loads((ROOT / "resource_android/pipeline/android_monthly.json").read_text(encoding="utf-8"))
        self.assertEqual(set(nodes), {monthly.CLAIM_ALL, *monthly.TAB_NODES.values(), *monthly.CONTENT_NODES.values(),
                                      monthly.LAYER_TITLE, monthly.LAYER_HINT, *monthly.LAYER_OVER_MONTHLY})
        for name in monthly.LAYER_OVER_MONTHLY:
            self.assertTrue((ROOT / "resource_android/image" / nodes[name]["template"]).is_file(), name)
            self.assertGreaterEqual(nodes[name]["threshold"], .9)
        claim = re.compile(nodes[monthly.CLAIM_ALL]["expected"])
        self.assertTrue(claim.search("键领取") and claim.search("一键领取"))  # OCR drops 「一」
        self.assertFalse(claim.search("领取") or claim.search("领取全部"))
        mail = json.loads((ROOT / "resource_android/pipeline/android_mailbox.json").read_text(encoding="utf-8"))
        forbidden = re.compile(mail[pages.FORBIDDEN]["expected"])
        for text in ("解锁悠远颂歌", "购买等级", "全部恢复默认", "恢复默认图像", "¥6", "￥396¥298", "星石获取",
                     "品获取萦雪", "兑换钻石", "开通", "删除已读", "共鸣"):
            self.assertTrue(forbidden.search(text), text)
        for text in ("键领取", "一键领取", "旅行任务", "旅行秘宝", "本周任务", "领取全部", "奇迹之旅", "乐园构想",
                     "尚未解锁"):  # 尚未解锁 on the calendar: bare 解锁 would block calendar taps later
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
from host.android import actions, monthly, pages, touch_point

fixtures = repo / "scripts/fixtures/android"
crop = lambda name: cv2.imread(str(fixtures / name))
# UID-free crops of device frames, put back where they were (1280x720).
title, subtabs, rows = crop("monthly_title.png"), crop("monthly_task_subtabs.png"), crop("monthly_treasure_rows.png")
tabs = {"task": crop("monthly_tabs_task.png"), "treasure": crop("monthly_tabs_treasure.png")}
bottom = {"gold": crop("monthly_bottom_task.png"), "none": crop("monthly_bottom_treasure.png")}
layer_title, layer_hint = crop("reward_layer_title.png"), crop("reward_layer_hint.png")  # 「恭喜获得」, device run 1
templates = repo / "resource_android/image/android"
under_title = cv2.imread(str(templates / "monthly_layer_title.png"))    # the page blurred under the layer, run 1
under_unlock = cv2.imread(str(templates / "monthly_layer_unlock.png"))
settings = crop("page_title.png")                                       # back arrow and 设置 title
duck, row = cv2.imread(str(templates / "menu_duck.png")), crop("hud_label_row.png")  # the HUD, from device frames
paid = bottom["gold"][17:59, 338:540]                                    # 「解锁悠远颂歌」 off the 奇迹之旅 page
frames = {}
for name, tab, button in (("task claimable", "task", "gold"), ("treasure nothing", "treasure", "none"),
                          ("task button gone", "task", "none"), ("treasure with gold", "treasure", "gold"),
                          ("dimmed task", "task", "gold"), ("reward layer", "treasure", "gold")):
    frame = np.full((720, 1280, 3), 90, np.uint8)
    marks = [(title, 0, 0), (tabs[tab], 0, 120), (bottom[button], 640, 615)]
    marks.append((subtabs, 170, 195) if tab == "task" else (rows, 160, 290))
    for image, x, y in marks:
        frame[y:y + image.shape[0], x:x + image.shape[1]] = image
    if name in ("dimmed task", "reward layer"):
        frame = (frame * 0.45).astype(np.uint8)
    if name == "reward layer":  # the layer over the page, where run 1 saw it
        frame[140:220, 540:740], frame[620:680, 520:760] = layer_title, layer_hint
        frame[10:70, 20:220], frame[620:705, 950:1260] = under_title, under_unlock
    frames[name] = frame
# Review round 1: the layer's texts over the dimmed 设置 page (with and without the 解锁 corner) must be refused.
for name, unlock in (("layer over settings", False), ("layer over settings with unlock corner", True)):
    frame = np.full((720, 1280, 3), 90, np.uint8)
    frame[10:10 + settings.shape[0], 0:settings.shape[1]] = settings
    frame = (frame * 0.45).astype(np.uint8)
    frame[140:220, 540:740], frame[620:680, 520:760] = layer_title, layer_hint
    if unlock:
        frame[620:705, 950:1260] = under_unlock
    frames[name] = frame
# Review round 2: the HUD, and the HUD with the paid-pass label over the 奇迹之旅 shortcut icon (its label visible).
# Review round 3: the 共鸣 glyphs (x 713-742 in the label row) moved 23 px right, their visible edge then 37 px from
# the 奇迹之旅 target: OCR merges them into the next label and the word cannot be read on its own.
moved = row.copy()
moved[:, 108:131], moved[:, 131:168] = row[:, 80:103], row[:, 108:145]
for name, over, label_row in (("hud", False, row), ("hud with paid label over the shortcut", True, row),
                              ("hud with 共鸣 moved right", False, moved)):
    frame = np.full((720, 1280, 3), 90, np.uint8)
    frame[18:72, 30:84], frame[50:100, 600:1280] = duck, label_row
    if over:
        frame[10:52, 707:909] = paid
    frames[name] = frame
current, results = {}, {}

class Frame(CustomController):
    def connect(self): return True
    def request_uuid(self): return "android-monthly-test"
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
        if screen.kind == "hud":
            plans = {name: actions.plan_tap(name, screen, recognize).target for name in ("menu", "monthly", "shop")}
            results[current["name"]] = {"kind": "hud", "plans": plans, "refused": {
                name: pages.forbidden_word_near(recognize, target) for name, target in plans.items()}}
            return CustomAction.RunResult(success=True)
        out = {"kind": screen.kind, "title": screen.title, "layer": monthly.is_reward_layer(screen, recognize),
               "dismiss_refused": pages._forbidden_near(recognize, [*monthly.DISMISS_POINT, 1, 1])}
        hint = recognize(monthly.LAYER_HINT)
        out["hint"] = list(hint.box) if actions._hit(hint) else None
        if screen.kind == "page":
            view = monthly.read_view(image, recognize)
            out["view"] = view and [view.tab, view.button]
            targets = {name: recognize(node) for name, node in
                       (("task_tab", monthly.TAB_NODES["task"]), ("treasure_tab", monthly.TAB_NODES["treasure"]),
                        ("claim", monthly.CLAIM_ALL))}
            out["taps"] = {name: touch_point(list(r.box)) for name, r in targets.items() if actions._hit(r)}
            out["refused"] = {name: pages._forbidden_near(recognize, list(r.box))
                              for name, r in targets.items() if actions._hit(r)}
            forbidden = recognize(pages.FORBIDDEN)
            out["forbidden"] = [r.text for r in (forbidden.filtered_results or [])] if actions._hit(forbidden) else []
            # A point just left of 解锁悠远颂歌 (its box starts at x 985) must be refused.
            out["beside_unlock"] = pages._forbidden_near(recognize, [950, 645, 20, 20])
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
    """The real nodes and OCR, both layers loaded, on frames composed from UID-free crops of the saved 奇迹之旅
    frames (in a fresh interpreter, as in test_android_actions.py)."""

    def test_the_monthly_page_targets_states_and_guard(self):
        if not (ROOT / "resource_android").is_dir():
            self.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
            return
        env = {**os.environ, "MAAFW_BINARY_PATH": str(ROOT / "maafw")}
        # -X utf8: the child prints Chinese text, and -I ignores PYTHONUTF8 (Windows consoles default to cp1252).
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", "-c", FRAMEWORK_CHECK, str(ROOT)],
                                capture_output=True, text=True, encoding="utf-8", timeout=300, env=env,
                                cwd=tempfile.gettempdir())
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        hud, covered = outcome.pop("hud"), outcome.pop("hud with paid label over the shortcut")
        shifted = outcome.pop("hud with 共鸣 moved right")
        self.assertLessEqual(abs(shifted["plans"]["monthly"][0] - 800), 6)  # the entry is still found ...
        self.assertEqual(shifted["refused"]["monthly"], "共鸣")              # ... and refused
        self.assertEqual(hud["refused"], {"menu": None, "monthly": None, "shop": None})  # 共鸣 is not in the way
        self.assertEqual(covered["refused"]["monthly"], "解锁悠远颂歌")
        self.assertIsNone(covered["refused"]["menu"])
        self.assertEqual({name: value.get("view") for name, value in outcome.items()},
                         {"task claimable": ["task", "enabled"], "treasure nothing": ["treasure", "none"],
                          "task button gone": ["task", "none"], "treasure with gold": ["treasure", "enabled"],
                          "dimmed task": None, "reward layer": None, "layer over settings": None,
                          "layer over settings with unlock corner": None})
        self.assertEqual({name: value["layer"] for name, value in outcome.items()},
                         {name: name == "reward layer" for name in outcome})
        self.assertEqual({value["dismiss_refused"] for value in outcome.values()}, {None})
        x, y = monthly.DISMISS_POINT
        hx, hy, hw, hh = outcome["reward layer"]["hint"]
        self.assertGreaterEqual(max(hx - x, x - (hx + hw), hy - y, y - (hy + hh)), 40)  # not on the hint
        cx, cy = outcome["task claimable"]["taps"]["claim"]
        self.assertGreaterEqual(abs(cx - x), 150)  # far from 一键领取 underneath
        self.assertEqual(outcome["dimmed task"]["kind"], "unknown")  # the dim layer hides the page itself
        claimable = outcome["task claimable"]
        self.assertEqual((claimable["kind"], claimable["title"]), ("page", "奇迹之旅"))
        for name, (x, y) in {"claim": (788, 654), "task_tab": (67, 330), "treasure_tab": (68, 212)}.items():
            tap = claimable["taps"][name]
            self.assertLessEqual(max(abs(tap[0] - x), abs(tap[1] - y)), 6, (name, tap))
        self.assertEqual(claimable["refused"], {"claim": None, "task_tab": None, "treasure_tab": None})
        self.assertIn("解锁悠远颂歌", claimable["forbidden"])
        self.assertEqual(claimable["beside_unlock"], "解锁悠远颂歌")
        self.assertNotIn("claim", outcome["treasure nothing"]["taps"])


if __name__ == "__main__":
    unittest.main()
