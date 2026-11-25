"""Android 美鸭梨挖掘 flow (agent/host/android/dig.py): python scripts/test_android_dig.py"""
from __future__ import annotations

import ast
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
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))

import numpy as np  # noqa: E402

from host.android import actions, dig, pages  # noqa: E402
from host.android.actions import Screen  # noqa: E402

# Where the saved frames show things (1280x720).
BACK = [37, 22, 40, 40]
DUCK = [30, 18, 54, 54]
DUCK_POINT = [56, 44]
ENTRY = [326, 347, 108, 25]      # 「美鸭梨挖掘」 on the menu (menu-p1)
HEADER = [1153, 88, 80, 19]      # 「挖掘队列」 (dig)
COUNT = [1162, 107, 62, 23]      # 「(0/4)」
CLOSE = [900, 150, 36, 36]
DIG_PAGE_WORDS = [("！选择物资", [780, 406, 109, 32]), ("挖掘", [797, 639, 73, 39]), ("消耗", [809, 533, 52, 26]),
                  ("小时", [970, 529, 26, 14])]

HUD = Screen("hud", {"duck": DUCK})
UNKNOWN = Screen("unknown", reason="认不出当前画面")
TITLES = {"menu": "美鸭梨", "menu2": "美鸭梨", "dig": "美鸭梨挖掘", "mail": "邮件8/300", "stuck": "美鸭梨挖掘"}
BACK_TO = {"dig": "menu", "stuck": "stuck", "menu": "hud", "menu2": "hud", "mail": "hud"}


def row(text, box):
    return SimpleNamespace(text=text, box=list(box))


def result(rows):
    rows = list(rows)
    if not rows:
        return SimpleNamespace(hit=False, box=None, best_result=None, filtered_results=[])
    return SimpleNamespace(hit=True, box=rows[0].box, best_result=rows[0], filtered_results=rows)


MISS = result([])


class Game:
    """A scripted game: HUD -> 美鸭梨 (page 1, or ``menu2`` without the entry) -> 美鸭梨挖掘. ``count``: what the queue
    counter reads (None: nothing), ``harvest``: harvest-like texts read on the dig page, ``busy_slots``: slots
    (1-based) that show something bright, ``words``: extra DIG_WORDS rows per place, ``screens``: frames shown instead,
    keyed by the number of touches sent, ``stop_on``: the touch after which the run is stopped, ``titles``: page
    titles read instead of TITLES's, ``request_after``: the number of touches after which stop.request is published
    while the tasker is not stopping yet (rt.stopped stays False; host.android_user_stop answers True)."""

    def __init__(self, *, start="hud", count="(0/4)", harvest=(), busy_slots=(), words=None, screens=None,
                 menu="menu", stop_on=None, titles=None, request_after=None):
        self.request_after, self.requested = request_after, False
        self.where, self.count, self.harvest, self.busy_slots = start, count, tuple(harvest), tuple(busy_slots)
        self.words, self.screens, self.menu, self.stop_on = dict(words or {}), dict(screens or {}), menu, stop_on
        self.titles = {**TITLES, **(titles or {})}
        self.touches, self.rt = [], None

    def snapshot(self, where, **changes):
        """``(screen, frame, recognize)`` at ``where``, with ``count``/``harvest``/``busy_slots`` changed if given;
        ``layer=True``: the reward layer's texts read too, over a screen that still reads."""
        state = {"count": self.count, "harvest": self.harvest, "busy_slots": self.busy_slots, "layer": False,
                 **changes}
        frame = np.zeros((720, 1280, 3), np.uint8)
        if where in ("dig", "stuck"):
            for slot in state["busy_slots"]:
                x, y = dig.SLOTS[slot - 1]
                frame[y - 30:y + 30, x - 30:x + 30] = 220
        if where == "hud":
            screen = HUD
        elif where in TITLES:
            screen = Screen("page", {"back": BACK}, title=self.titles[where])
        else:
            screen = UNKNOWN
        return screen, frame, lambda node, override=None: self.recognize(where, state, node)

    def recognize(self, where, state, node):
        on_dig = where in ("dig", "stuck")
        if node == pages.FORBIDDEN:
            return MISS
        if node in (actions.QUIT_TITLE, actions.QUIT_CANCEL):
            return result([row("退出游戏", [560, 200, 160, 40])]) if where == "quit" else MISS
        if node == actions.MODAL_CLOSE:
            return result([row("", CLOSE)]) if where == "popup" else MISS
        layer = where == "layer" or state["layer"]
        if node == dig.LAYER_TITLE:
            return result([row("恭喜获得", [560, 170, 160, 40])]) if layer else MISS
        if node == dig.LAYER_HINT:
            return result([row("点击空白区域继续", [540, 640, 200, 30])]) if layer else MISS
        if node == actions.HUD_DUCK:
            return result([row("", DUCK)]) if where == "hud" else MISS
        if node == actions.BACK_ARROW:
            return result([row("", BACK)]) if where in TITLES else MISS
        if node == dig.MENU_DIG:
            return result([row("美鸭梨挖掘", ENTRY)]) if where == "menu" else MISS
        if node == dig.QUEUE_HEADER:
            return result([row("挖掘队列", HEADER)]) if on_dig else MISS
        if node == dig.QUEUE_COUNT:
            return result([row(state["count"], COUNT)]) if on_dig and state["count"] is not None else MISS
        if node == dig.DIG_WORDS:
            return result([row(*item) for item in (DIG_PAGE_WORDS if on_dig else []) + self.words.get(where, [])])
        if node == dig.HARVEST_LIKE:
            return result([row(text, [1150, 200, 90, 30]) for text in state["harvest"]]) if on_dig else MISS
        return MISS

    def screen_now(self, rt):
        if rt.stopped:
            return None, None, None
        if self.screens.get(len(self.touches)):
            return self.screens[len(self.touches)].pop(0)
        return self.snapshot(self.where)

    def action(self, name, target=None, source_frame=None, **_fields):
        assert name == "Click" and source_frame is not None
        target = list(target)
        if target == DUCK_POINT and self.where == "hud":
            self.where, what = self.menu, "duck"
        elif target == ENTRY and self.where == "menu":
            self.where, what = "dig", "entry"
        elif target == BACK and self.where in BACK_TO:
            self.where, what = BACK_TO[self.where], "back"
        else:
            raise AssertionError(f"unexpected tap {target} on {self.where}")
        self.touches.append(what)
        if self.request_after == len(self.touches):
            self.requested = True
        if self.stop_on == what:
            self.rt.stopped = True
        return True


def runtime(game):
    rt = SimpleNamespace(stopped=False, logs=[], key=Mock(side_effect=AssertionError("shared named action")),
                         _frame_still_valid=Mock(return_value=True), pause=Mock(return_value=True),
                         diagnostic=Mock(return_value={"image_status": "saved"}), TOUCH_KEY_ATTEMPTS=3,
                         action=game.action)
    rt.log = lambda text, **kwargs: rt.logs.append(text)
    game.rt = rt
    return rt


def play(game, call=None):
    rt = runtime(game)
    with patch.object(pages, "screen_now", side_effect=game.screen_now), \
            patch("host.android_user_stop", lambda **_kwargs: game.requested), \
            patch("time.monotonic", side_effect=itertools.count(0, .25).__next__):
        outcome = (call or dig.run)(rt)
    return outcome, rt


def stops(rt):
    return [c for c in rt.diagnostic.call_args_list if c.args[0].startswith("android_dig_stop_")]


WAY = ["duck", "entry", "back", "back"]


class FlowTests(unittest.TestCase):
    def test_an_empty_queue_succeeds_with_its_message_after_the_way_back(self):
        game = Game()
        outcome, rt = play(game)
        self.assertTrue(outcome, rt.logs)
        self.assertEqual(game.touches, WAY)
        self.assertEqual(game.where, "hud")
        self.assertEqual(rt.logs, ["美鸭梨挖掘：没有可收获的挖掘。"])
        rt.diagnostic.assert_not_called()
        rt.key.assert_not_called()

    def test_a_non_empty_queue_sends_no_harvest_tap_and_reports_after_the_way_back(self):
        for name, changes in (("count", {"count": "(1/4)"}), ("harvest word", {"harvest": ("一键收获",)}),
                              ("bright slot", {"busy_slots": (2,)})):
            with self.subTest(name):
                game = Game(**changes)
                outcome, rt = play(game)
                self.assertFalse(outcome)
                self.assertEqual(game.touches, WAY)  # in, the page's back arrow, the menu's back arrow: nothing else
                self.assertEqual(game.where, "hud")
                queue = [c for c in rt.diagnostic.call_args_list if c.args[0] == "android_dig_queue"]
                self.assertEqual(len(queue), 1)
                self.assertIsNotNone(queue[0].kwargs["image"])
                self.assertEqual(stops(rt), [])
                self.assertEqual(len(rt.logs), 1)
                self.assertIn("还没在设备上见过收获", rt.logs[0])
                self.assertIn("手动收获", rt.logs[0])
                self.assertNotIn("没有可收获的挖掘", rt.logs[0])

    def test_an_empty_first_read_needs_a_second_empty_read(self):
        game = Game()
        game.screens = {2: [game.snapshot("dig"), game.snapshot("dig", count="(1/4)")]}
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, WAY)
        self.assertIn("队列 1/4", rt.logs[0])
        rt.pause.assert_any_call(dig.SETTLE_SECONDS)

    def test_the_start_goes_back_from_any_page_first(self):
        for start, back in (("dig", ["back", "back"]), ("mail", ["back"]), ("menu", ["back"])):
            with self.subTest(start):
                game = Game(start=start)
                outcome, rt = play(game)
                self.assertTrue(outcome, rt.logs)
                self.assertEqual(game.touches, back + WAY)

    def test_recognised_unexpected_screens_stop_with_the_frame_and_send_nothing_more(self):
        cases = {
            "quit dialog at the start": (Game(start="quit"), []),
            "popup after the entry": (None, ["duck", "entry"]),
            "menu opened on page 2": (Game(menu="menu2"), ["duck"]),
            "another page after the entry": (None, ["duck", "entry"]),
        }
        popup = Game()
        popup.screens = {2: [popup.snapshot("popup")] * 3}
        cases["popup after the entry"] = (popup, ["duck", "entry"])
        other = Game()
        other.screens = {2: [other.snapshot("mail")] * 3}
        cases["another page after the entry"] = (other, ["duck", "entry"])
        for name, (game, touches) in cases.items():
            with self.subTest(name):
                outcome, rt = play(game)
                self.assertFalse(outcome)
                self.assertEqual(game.touches, touches)
                self.assertEqual(len(stops(rt)), 1)
                self.assertIsNotNone(stops(rt)[0].kwargs["image"])
                self.assertTrue(rt.logs and rt.logs[-1].endswith("本项未完成。"), rt.logs)

    def test_a_dig_page_that_never_settles_stops_without_the_way_back(self):
        game = Game(count=None)
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, ["duck", "entry"])
        self.assertEqual(len(stops(rt)), 1)

    def test_frames_with_nothing_recognised_yet_are_forgiven_briefly(self):
        game = Game()
        blank = (UNKNOWN, np.zeros((720, 1280, 3), np.uint8), lambda node, override=None: MISS)
        game.screens = {2: [blank, game.snapshot("dig", count=None)]}
        outcome, rt = play(game)
        self.assertTrue(outcome, rt.logs)
        self.assertEqual(game.touches, WAY)

    def test_a_page_that_does_not_go_after_its_back_arrow_stops(self):
        game = Game()
        original = game.action

        def action(name, target=None, source_frame=None, **fields):
            if game.where == "dig" and list(target) == BACK:
                game.where = "stuck"
                game.touches.append("back")
                return True
            return original(name, target=target, source_frame=source_frame, **fields)
        game.action = action
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, ["duck", "entry", "back"])
        self.assertEqual(len(stops(rt)), 1)
        self.assertNotIn("没有可收获的挖掘", " ".join(rt.logs))

    def test_a_dig_word_near_a_target_refuses_it_on_either_tap_frame(self):
        near_back = ("再次挖掘", [80, 50, 90, 30])
        near_entry = ("选择物资", [330, 380, 100, 30])
        for name, words, touches in (("back arrow of the dig page", {"dig": [near_back]}, ["duck", "entry"]),
                                     ("the menu entry", {"menu": [near_entry]}, ["duck"]),
                                     ("the menu's back arrow", {"menu": [("消耗", [40, 70, 50, 26])]},
                                      ["duck", "entry", "back"])):
            with self.subTest(name):
                game = Game(words=words)
                outcome, rt = play(game)
                self.assertFalse(outcome)
                self.assertEqual(game.touches, touches)
                self.assertTrue(any("没有点，任务停止" in line for line in rt.logs), rt.logs)
                self.assertEqual(len(stops(rt)), 1)  # the refusing frame is saved
        # Only on the confirming frame: decided on a clean frame, refused on the second.
        game = Game()
        clean, dirty = game.snapshot("dig"), Game(words={"dig": [near_back]}).snapshot("dig")
        # Two reads of the queue, the way back's look, the back arrow's deciding frame, then its confirming frame.
        game.screens = {2: [clean, clean, clean, clean, dirty]}
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, ["duck", "entry"])
        self.assertEqual(game.screens[2], [])  # refused on the fifth frame, the confirming one
        self.assertIs(stops(rt)[0].kwargs["image"], dirty[1])

    def test_a_dig_word_near_the_whole_duck_refuses_it(self):
        # Within the margin of the duck's box, not of the tap point: the duck is checked as the shared tap reads it.
        game = Game(words={"hud": [("加速", [40, 100, 60, 26])]})
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, [])
        self.assertEqual(len(stops(rt)), 1)
        self.assertEqual(rt.logs, [f"美鸭梨挖掘：{pages.GUARD_TAP}，本项未完成。"])

    def test_a_quit_dialog_on_a_duck_tap_frame_stops_with_that_frame(self):
        for name, before in (("the duck's own look", 1), ("deciding", 2), ("confirming", 3)):
            with self.subTest(name):
                game = Game()
                quit_ = game.snapshot("quit")
                # The start's look, the duck's own look, then its deciding and confirming frames.
                game.screens = {0: [game.snapshot("hud")] * before + [quit_]}
                outcome, rt = play(game)
                self.assertFalse(outcome)
                self.assertEqual(game.screens[0], [])  # the quit frame was read where it was meant to be
                self.assertEqual(game.touches, [])
                self.assertEqual(len(stops(rt)), 1)
                self.assertIs(stops(rt)[0].kwargs["image"], quit_[1])
                self.assertTrue(rt.logs[-1].endswith("本项未完成。"), rt.logs)

    def test_titles_are_compared_whole_with_whitespace_aside(self):
        game = Game(titles={"dig": "美鸭梨 挖掘", "menu": "美 鸭梨"})
        outcome, rt = play(game)
        self.assertTrue(outcome, rt.logs)
        self.assertEqual(game.touches, WAY)
        # The page that is still there with other spacing after its back arrow has not gone: it stops, no second tap.
        game = Game(titles={"stuck": "美鸭梨 挖掘"})
        original = game.action

        def action(name, target=None, source_frame=None, **fields):
            if game.where == "dig" and list(target) == BACK:
                game.where = "stuck"
                game.touches.append("back")
                return True
            return original(name, target=target, source_frame=source_frame, **fields)
        game.action = action
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, ["duck", "entry", "back"])
        self.assertEqual(len(stops(rt)), 1)

    def test_no_user_message_carries_ocr_text(self):
        # A refusing label: the fixed text for the user, the label in the saved diagnostic.
        game = Game(words={"dig": [("再次挖掘吧", [80, 50, 90, 30])]})
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(rt.logs, [f"美鸭梨挖掘：{pages.GUARD_TAP}，本项未完成。"])
        self.assertIn("再次挖掘吧", stops(rt)[0].args[1])
        # Harvest-like words: named in the diagnostic, not in the report.
        game = Game(harvest=("一键收获",))
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertNotIn("一键收获", " ".join(rt.logs))
        self.assertIn("读到像收获的文字", rt.logs[0])
        queue = [c for c in rt.diagnostic.call_args_list if c.args[0] == "android_dig_queue"]
        self.assertIn("一键收获", queue[0].args[1])
        # A page observed at the start, by its title: neither when it does not go nor when it is not safe to leave.
        game = Game(start="mail")
        original = game.action

        def stay(name, target=None, source_frame=None, **fields):
            if game.where == "mail" and list(target) == BACK:
                game.touches.append("back")
                return True
            return original(name, target=target, source_frame=source_frame, **fields)
        game.action = stay
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, ["back"])
        self.assertEqual(len(stops(rt)), 1)
        game_after = Game()
        game_after.screens = {2: [game_after.snapshot("mail")] * 3}
        outcome, rt_after = play(game_after)
        self.assertFalse(outcome)
        for logs in (rt.logs, rt_after.logs):
            self.assertTrue(logs and logs[-1].endswith("本项未完成。"), logs)
            self.assertNotIn("邮件", " ".join(logs))
            self.assertNotIn("8/300", " ".join(logs))

    def test_a_requested_stop_before_the_tasker_stops_reports_nothing(self):
        # stop.request is published before post_stop: rt.stopped (the tasker) is still False meanwhile.
        for name, changes, linger in (("empty", {}, False), ("busy", {"count": "(1/4)"}, False),
                                      ("empty, menu still shown", {}, True), ("busy, menu still shown",
                                                                              {"count": "(1/4)"}, True)):
            with self.subTest(name):
                game = Game(request_after=4, **changes)
                if linger:
                    game.screens = {4: [game.snapshot("menu")] * 3}
                outcome, rt = play(game)
                self.assertFalse(outcome)
                self.assertEqual(game.touches, WAY)  # nothing more sent
                self.assertFalse(rt.stopped)
                self.assertEqual(rt.logs, [])  # neither 「没有可收获的挖掘」 nor 「本项未完成」
                self.assertEqual(stops(rt), [])
        # Requested after the entry: the queue is not reported either, nor the way back sent.
        game = Game(request_after=2)
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, ["duck", "entry"])
        self.assertEqual(rt.logs, [])

    def test_the_frame_a_back_arrow_leads_to_decides_the_next_step(self):
        # 美鸭梨挖掘 -> 邮件 for one frame -> 美鸭梨: stops with the 邮件 frame, no back arrow on the menu.
        game = Game()
        mail = game.snapshot("mail")
        game.screens = {3: [mail]}
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, ["duck", "entry", "back"])
        self.assertEqual(len(stops(rt)), 1)
        self.assertIs(stops(rt)[0].kwargs["image"], mail[1])
        self.assertNotIn("没有可收获的挖掘", " ".join(rt.logs))
        # 美鸭梨 -> the dig page again: not the HUD, stops with that frame.
        game = Game()
        again = game.snapshot("dig")
        game.screens = {4: [again]}
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, WAY)
        self.assertIs(stops(rt)[0].kwargs["image"], again[1])

    def test_positive_evidence_is_no_transition(self):
        game = Game()
        layer = game.snapshot("layer")
        game.screens = {2: [layer, game.snapshot("dig")]}
        harvest = Game()
        revealing = harvest.snapshot("dig", count=None, harvest=("确认收获",))
        harvest.screens = {2: [revealing, harvest.snapshot("dig")]}
        for name, game_, frame in (("reward layer", game, layer[1]), ("harvest text, unreadable counter", harvest,
                                                                       revealing[1])):
            with self.subTest(name):
                outcome, rt = play(game_)
                self.assertFalse(outcome)
                self.assertEqual(game_.touches, ["duck", "entry"])
                self.assertEqual(len(stops(rt)), 1)
                self.assertIs(stops(rt)[0].kwargs["image"], frame)
                self.assertNotIn("没有可收获的挖掘", " ".join(rt.logs))

    def test_queue_evidence_on_the_way_back_overturns_an_empty_read(self):
        clean = Game().snapshot("dig")
        # Two empty reads, then the way back's deciding frame; or a frame waited through after the back arrow.
        for name, at, changes in (("deciding frame, counter", 2, {"count": "(1/4)"}),
                                  ("deciding frame, occupied slot", 2, {"busy_slots": (1,)}),
                                  ("deciding frame, another total", 2, {"count": "(0/8)"}),
                                  ("tap frame, harvest text", 2, {"harvest": ("确认收获",)}),
                                  ("frame waited through", 3, {"count": "(1/4)"})):
            with self.subTest(name):
                game = Game()
                busy = game.snapshot("dig", **changes)
                game.screens = {2: [clean, clean, clean, busy]} if name.startswith("tap") else \
                    {2: [clean, clean, busy]} if at == 2 else {3: [busy]}
                outcome, rt = play(game)
                self.assertFalse(outcome)
                self.assertEqual(game.touches, WAY)
                self.assertEqual(stops(rt), [])
                queue = [c for c in rt.diagnostic.call_args_list if c.args[0] == "android_dig_queue"]
                self.assertEqual(len(queue), 1)
                self.assertIs(queue[0].kwargs["image"], busy[1])
                self.assertEqual(len(rt.logs), 1)
                self.assertIn("还没在设备上见过收获", rt.logs[0])
                self.assertIn("画面已保存", rt.logs[0])
                self.assertNotIn("没有可收获的挖掘", rt.logs[0])
                self.assertNotIn("确认收获", rt.logs[0])

    def test_only_the_surveyed_four_slot_queue_reads_empty(self):
        game = Game(count="(0/8)")
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, WAY)
        self.assertIn("队列 0/8", rt.logs[0])
        self.assertNotIn("没有可收获的挖掘", rt.logs[0])

    def test_the_report_says_saved_only_when_the_frame_was_saved(self):
        for reference, words in (({"image_status": "saved"}, "画面已保存"), ({"image_status": "save_failed"}, "画面没能保存"),
                                 (None, "画面没能保存")):
            with self.subTest(str(reference)):
                game = Game(count="(1/4)")
                rt = runtime(game)
                rt.diagnostic.return_value = reference
                with patch.object(pages, "screen_now", side_effect=game.screen_now), \
                        patch("time.monotonic", side_effect=itertools.count(0, .25).__next__):
                    self.assertFalse(dig.run(rt))
                self.assertEqual(len(rt.logs), 1)
                self.assertIn(words, rt.logs[0])
                self.assertNotIn("画面已保存" if words != "画面已保存" else "画面没能保存", rt.logs[0])

    def test_a_reward_layer_over_a_readable_screen_is_neither_accepted_nor_tapped(self):
        game = Game()
        hud, menu = game.snapshot("hud"), game.snapshot("menu")
        cases = {  # touches before, the frames shown then, the touches expected
            "the duck's confirming frame": (0, [hud, hud, hud], []),
            "the menu awaited after the duck": (1, [], ["duck"]),
            "the entry's confirming frame": (1, [menu, menu], ["duck"]),
            "the dig page's back arrow": (2, [game.snapshot("dig")] * 4, ["duck", "entry"]),
            "the HUD reached at the end": (4, [], WAY),
        }
        for name, (at, before, touches) in cases.items():
            with self.subTest(name):
                game = Game()
                where = {0: "hud", 1: "menu", 2: "dig", 4: "hud"}[at]
                layered = game.snapshot(where, layer=True)
                game.screens = {at: [*before, layered]}
                outcome, rt = play(game)
                self.assertFalse(outcome)
                self.assertEqual(game.screens[at], [])
                self.assertEqual(game.touches, touches)
                self.assertEqual(len(stops(rt)), 1)
                self.assertIs(stops(rt)[0].kwargs["image"], layered[1])
                self.assertNotIn("没有可收获的挖掘", " ".join(rt.logs))
                self.assertTrue(rt.logs and rt.logs[-1].endswith("本项未完成。"), rt.logs)

    def test_later_evidence_is_saved_when_seen_even_if_the_way_back_then_fails(self):
        game = Game()
        clean, busy, mail = game.snapshot("dig"), game.snapshot("dig", count="(1/4)"), game.snapshot("mail")
        game.screens = {2: [clean, clean, busy], 3: [mail]}
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, ["duck", "entry", "back"])
        queue = [c for c in rt.diagnostic.call_args_list if c.args[0] == "android_dig_queue"]
        self.assertEqual(len(queue), 1)
        self.assertIs(queue[0].kwargs["image"], busy[1])
        self.assertEqual(len(stops(rt)), 1)
        self.assertIs(stops(rt)[0].kwargs["image"], mail[1])
        self.assertNotIn("没有可收获的挖掘", " ".join(rt.logs))

    def test_the_dig_words_check_that_cannot_run_refuses(self):
        game = Game()
        _screen, frame, recognize = game.snapshot("dig")
        broken = lambda node, override=None: None if node == dig.DIG_WORDS else recognize(node)  # noqa: E731
        with self.assertRaises(pages.GuardRefusal):
            dig.guarded(actions.BACK_ARROW)(frame, broken)

    def test_a_user_stop_sends_nothing_more_and_reports_no_failure(self):
        game = Game(stop_on="entry")
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, ["duck", "entry"])
        self.assertEqual(rt.logs, [])


class ViewTests(unittest.TestCase):
    def test_counter_text(self):
        for text, expected in (("(0/4)", (0, 4)), ("（1／4）", (1, 4)), ("2/4", (2, 4)), ("( 0 / 4 )", (0, 4)),
                               ("5/4", None), ("0/0", None), ("挖掘队列", None), ("(0/4) 1", None)):
            recognize = lambda node, override=None, t=text: result([row(t, COUNT)])  # noqa: E731
            self.assertEqual(dig.read_count(recognize), expected, text)

    def test_the_title_must_be_exact(self):
        game = Game()
        _screen, frame, recognize = game.snapshot("dig")
        for title in ("美鸭梨", "美鸭梨挖掘 1", "挖掘"):
            screen = Screen("page", {"back": BACK}, title=title)
            self.assertIsNone(dig.read_view(screen, frame, recognize), title)


class EntryTests(unittest.TestCase):
    def rt(self):
        rt = SimpleNamespace(stopped=False, logs=[], ui=SimpleNamespace(find_menu_entry=Mock(return_value=False)),
                             main=Mock(return_value=True), diagnostic=Mock(),
                             context=SimpleNamespace(get_node_data=Mock(return_value={})))
        rt.log = lambda text, **kwargs: rt.logs.append(text)
        return rt

    def test_the_shared_task_branches_to_the_android_flow_on_a_touch_target_only(self):
        from daily.gameplay import Gameplay
        rt = self.rt()
        with patch("host.game_backend", return_value="android"), patch.object(dig, "run", return_value=True) as run:
            self.assertTrue(Gameplay(rt).dig())
        run.assert_called_once_with(rt)
        rt.ui.find_menu_entry.assert_not_called()
        rt.main.assert_not_called()  # the Android flow returns to the HUD itself
        for backend in ("win32", "macos"):
            rt = self.rt()
            with patch("host.game_backend", return_value=backend), patch.object(dig, "run") as run:
                self.assertFalse(Gameplay(rt).dig())  # the PC path as before: no entry found, back to the main view
            run.assert_not_called()
            rt.ui.find_menu_entry.assert_called_once_with(node="MaaNikki_Dig_Open")
            rt.main.assert_called_once_with()
            self.assertIn("未找到美鸭梨挖掘入口。", rt.logs)

    def test_android_never_reads_targets_or_sends_statistics(self):
        from collection import statistics
        from daily.gameplay import Gameplay
        refuse = Mock(side_effect=AssertionError("statistics on Android"))
        for name, changes in (("empty", {}), ("busy", {"count": "(2/4)"})):
            with self.subTest(name):
                game = Game(**changes)
                rt = runtime(game)
                rt.ui = SimpleNamespace(find_menu_entry=refuse)
                rt.main, rt.context = refuse, SimpleNamespace(get_node_data=refuse)
                with patch("host.game_backend", return_value="android"), \
                        patch.object(Gameplay, "observe_dig_targets", refuse), \
                        patch.object(Gameplay, "_read_dig_target_names", refuse), \
                        patch.object(statistics, "upload", refuse), \
                        patch.object(statistics, "sync_task_stats", refuse), \
                        patch.object(statistics, "is_task_stats_enabled", refuse), \
                        patch.object(pages, "screen_now", side_effect=game.screen_now), \
                        patch("time.monotonic", side_effect=itertools.count(0, .25).__next__):
                    outcome = Gameplay(rt).dig()
                self.assertEqual(outcome, name == "empty")
                self.assertEqual(game.touches, WAY)
        refuse.assert_not_called()

    def test_the_module_imports_no_statistics_and_names_no_spending_node(self):
        tree = ast.parse((ROOT / "agent/host/android/dig.py").read_text(encoding="utf-8"))
        docstrings = {id(body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and (body := node.body)
                      and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)}
        imported, names = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                names.add(node.value)
        self.assertFalse({name for name in imported if "collection" in name or "statistics" in name}, imported)
        code = " ".join(names)  # identifiers and string constants, docstrings left out
        for name in ("observe_dig_targets", "_read_dig_target_names", "MaaNikki_Dig_Again", "MaaNikki_Dig_Gather",
                     "MaaNikki_Dig_GatherConfirm", "DigRepeat", "upload", "statistics", "get_node_data"):
            self.assertNotIn(name, code)


class ResourceTests(unittest.TestCase):
    def test_tier_note_nodes_and_words(self):
        tiers = json.loads((ROOT / "agent/host/android/tasks.json").read_text(encoding="utf-8"))
        self.assertIn("MaaNikki_Dig", tiers["experimental"])
        self.assertEqual(tiers["entries"], [])
        note = tiers["notes"]["MaaNikki_Dig"]
        for words in ("没有可收获的挖掘", "手动收获", "不点再次挖掘", "不上传挖掘统计", "从不开始新的挖掘"):
            self.assertIn(words, note)
        if not (ROOT / "resource_android").is_dir():  # the release source archive does not list it (plan risk 13)
            self.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
            return
        nodes = json.loads((ROOT / "resource_android/pipeline/android_dig.json").read_text(encoding="utf-8"))
        self.assertEqual(set(nodes), set(dig.NODES))
        words = re.compile(nodes[dig.DIG_WORDS]["expected"])
        for text in ("挖掘", "挖 掘", "挖掘！", "挖掘!", "挖掘。", "！挖掘", "再次挖掘", "！选择物资", "选择物资", "消耗", "小时",
                     "4小时", "加速", "立即完成", "确认收获", "一键收获", "开始挖掘", "领取", "一键领取"):
            self.assertTrue(words.search(text), text)
        for text in ("美鸭梨挖掘", "挖掘队列", "美鸭梨挖掘！", "美鸭梨", "(0/4)", "返回", "心愿原野", "采集物资：32/56"):
            self.assertFalse(words.search(text), text)
        self.assertEqual(nodes[dig.DIG_WORDS]["roi"], [0, 0, 1280, 720])  # labels read whole, on the full frame
        entry = re.compile(nodes[dig.MENU_DIG]["expected"])
        self.assertTrue(entry.search("美鸭梨挖掘"))
        self.assertFalse(entry.search("美鸭梨快跑") or entry.search("挖掘"))
        harvest = re.compile(nodes[dig.HARVEST_LIKE]["expected"])
        for text in ("确认收获", "一键收获", "可收获", "领取", "已完成", "再次挖掘"):
            self.assertTrue(harvest.search(text), text)
        # The shared list stays as it is: 挖掘 in it would refuse the menu entry 「美鸭梨挖掘」 itself.
        shared = re.compile(json.loads((ROOT / "resource_android/pipeline/android_mailbox.json")
                                       .read_text(encoding="utf-8"))[pages.FORBIDDEN]["expected"])
        for text in ("美鸭梨挖掘", "挖掘", "挖掘队列"):
            self.assertFalse(shared.search(text), text)


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
from host.android import actions, dig, pages, touch_point

fixtures = repo / "scripts/fixtures/android"
crop = lambda name: cv2.imread(str(fixtures / name))
# UID-free crops of the saved frames (menu-p1, dig), put back where they were (1280x720).
menu_title, menu_row = crop("redeem_menu_title.png"), crop("menu_dig_row.jpg")
title, queue, controls, icon = crop("dig_title.png"), crop("dig_queue.jpg"), crop("dig_controls.jpg"), \
    crop("dig_material_icon.png")

def compose(marks):
    frame = np.full((720, 1280, 3), (70, 62, 55), np.uint8)
    for image, x, y in marks:
        frame[y:y + image.shape[0], x:x + image.shape[1]] = image
    return frame

page = [(title, 0, 0), (queue, 1120, 70), (controls, 560, 100)]  # controls: the dial, 消耗, the hours, 挖掘
frames = {"menu": compose([(menu_title, 0, 0), (menu_row, 60, 280)]), "dig": compose(page),
          "dig slot 1 filled": compose(page + [(icon, 1159, 159)])}
current, results = {}, {}

class Frame(CustomController):
    def connect(self): return True
    def request_uuid(self): return "android-dig-test"
    def screencap(self): return current["frame"]
    def refuse(self, *args): return False
    start_app = stop_app = click = swipe = touch_down = touch_move = touch_up = refuse
    click_key = input_text = key_down = key_up = refuse

def gap(a, b):
    # How far apart two boxes are (0 when they touch or overlap): the larger of the x and y gaps.
    return max(b[0] - (a[0] + a[2]), a[0] - (b[0] + b[2]), b[1] - (a[1] + a[3]), a[1] - (b[1] + b[3]), 0)

class Judge(CustomAction):
    def run(self, context, argv):
        image = context.tasker.controller.post_screencap().wait().get()
        def recognize(node, override=None):
            return context.run_recognition(node, image, {node: override} if override else None)
        screen = actions.read_screen(recognize)
        out = {"kind": screen.kind, "title": screen.title}
        words = recognize(dig.DIG_WORDS)
        out["words"] = [[r.text, list(r.box)] for r in (words.filtered_results or [])] if actions._hit(words) else []
        targets = {"back": recognize(actions.BACK_ARROW), "entry": recognize(dig.MENU_DIG)}
        out["taps"] = {name: touch_point(list(r.box)) for name, r in targets.items() if actions._hit(r)}
        out["refused"] = {name: [dig.dig_word_near(recognize, list(r.box)), pages._forbidden_near(recognize, list(r.box))]
                          for name, r in targets.items() if actions._hit(r)}
        out["gaps"] = {name: min([gap(list(r.box), box) for _text, box in out["words"]] or [9999])
                       for name, r in targets.items() if actions._hit(r)}
        accept = dig.guarded(actions.BACK_ARROW, dig.dig_ready if screen.title == dig.DIG else None)
        out["back_accepted"] = bool(actions._hit(targets["back"]) and accept(image, recognize))
        out["entry_accepted"] = bool(actions._hit(targets["entry"]) and dig.guarded(dig.MENU_DIG)(image, recognize))
        view = dig.read_view(screen, image, recognize)
        out["view"] = view and [view.count, view.total, list(view.harvest), list(view.busy_slots), view.empty]
        out["slots"] = [round(dig._bright(image, centre), 3) for centre in dig.SLOTS]
        # Points next to the dig page's controls must be refused; the title row must not be.
        out["beside"] = {name: dig.dig_word_near(recognize, box) for name, box in
                         (("button", [835, 600, 1, 1]), ("select", [830, 455, 1, 1]), ("consume", [835, 500, 1, 1]),
                          ("title", [92, 25, 137, 33]), ("queue header", [1153, 88, 80, 19]))}
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
    """The real nodes and OCR, both layers loaded, on frames composed from UID-free crops of the saved menu and dig
    frames (in a fresh interpreter, as in test_android_actions.py)."""

    def test_the_menu_entry_dig_page_queue_and_guard(self):
        if not (ROOT / "resource_android").is_dir():
            self.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
            return
        env = {**os.environ, "MAAFW_BINARY_PATH": str(ROOT / "maafw")}
        # -X utf8: the child prints Chinese text, and -I ignores PYTHONUTF8 (Windows consoles default to cp1252).
        result_ = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", "-c", FRAMEWORK_CHECK, str(ROOT)],
                                 capture_output=True, text=True, encoding="utf-8", timeout=300, env=env,
                                 cwd=tempfile.gettempdir())
        self.assertEqual(result_.returncode, 0, result_.stderr[-2000:])
        outcome = json.loads(result_.stdout.strip().splitlines()[-1])
        menu, page, filled = outcome["menu"], outcome["dig"], outcome["dig slot 1 filled"]
        # The menu: exactly 美鸭梨, the entry's label found where the saved frame shows it, nothing in the way.
        self.assertEqual((menu["kind"], menu["title"]), ("page", "美鸭梨"))
        self.assertLessEqual(max(abs(menu["taps"]["entry"][0] - 379), abs(menu["taps"]["entry"][1] - 359)), 6)
        self.assertEqual(menu["words"], [])
        self.assertEqual(menu["refused"], {"back": [None, None], "entry": [None, None]})
        self.assertTrue(menu["entry_accepted"] and menu["back_accepted"])
        self.assertIsNone(menu["view"])
        # The dig page: its own title, an empty queue on the saved frame, every slot plain.
        self.assertEqual((page["kind"], page["title"]), ("page", "美鸭梨挖掘"))
        self.assertEqual(page["view"], [0, 4, [], [], True])
        self.assertLessEqual(max(page["slots"]), .2)
        self.assertLessEqual(max(abs(page["taps"]["back"][0] - 56), abs(page["taps"]["back"][1] - 41)), 6)
        self.assertNotIn("entry", page["taps"])
        # The guard reads the page's controls as whole labels; the back arrow is far from all of them.
        texts = {text for text, _box in page["words"]}
        self.assertTrue({"挖掘", "消耗"} <= texts, texts)
        self.assertTrue(any("选择物资" in text for text in texts), texts)
        self.assertEqual(page["refused"], {"back": [None, None]})
        self.assertGreaterEqual(page["gaps"]["back"], 400)
        self.assertTrue(page["back_accepted"])
        beside = page["beside"]
        self.assertEqual(beside["button"], "挖掘")
        self.assertIn("选择物资", beside["select"])
        self.assertIsNotNone(beside["consume"])
        self.assertIsNone(beside["title"])
        self.assertIsNone(beside["queue header"])
        # A material icon in the first slot (the queue still reading 0/4): not empty.
        self.assertEqual(filled["view"][3], [1])
        self.assertFalse(filled["view"][4])


if __name__ == "__main__":
    unittest.main()
