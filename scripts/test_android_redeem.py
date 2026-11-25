"""Android 兑换码 (agent/host/android/redeem.py, pages.input_text_on_screen / send_text): python scripts/test_android_redeem.py"""
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
# The entry test imports agent registration, which imports maa: Windows CI's embedded maa has no bin folder.
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402

import host  # noqa: E402
from host.android import actions, pages, redeem  # noqa: E402
from host.android.actions import Screen  # noqa: E402

try:
    import fcntl  # noqa: F401
    HAS_FLOCK = True
except ImportError:
    HAS_FLOCK = False

# Where the saved frames show things (1280x720).
BACK = [37, 22, 40, 40]
DUCK = [30, 18, 54, 54]
DUCK_POINT = [56, 44]  # the duck's touch point (device run 1 tapped (56,44))
GEAR = [610, 644, 47, 47]
OTHER = [79, 598, 55, 34]
IMAGE_HEADER = [282, 91, 98, 28]
HEADER = [260, 529, 100, 34]
HEADER_SCROLLED = [260, 510, 100, 34]
ROW_LABEL = [288, 569, 80, 36]
ROW_USE_FADED = [950, 587, 73, 27]
ROW_USE = [947, 567, 82, 38]
RESET = [1014, 655, 179, 37]
PLACEHOLDER = [416, 328, 205, 26]
CLEAR = [794, 329, 28, 28]
FIELD = [406, 328, 116, 27]
USE = [603, 480, 70, 39]
CLOSE = [929, 177, 36, 36]
BANNER = [569, 197, 137, 28]
GOLD = (40, 150, 220)  # BGR of the filled modal's 使用 face (HSV ~18, 208, 220)
PINK = (215, 190, 245)  # BGR of the result banner (HSV ~177, 57, 245)

HUD = Screen("hud", {"duck": DUCK})
MENU = Screen("page", {"back": BACK}, title="美鸭梨")
SETTINGS = Screen("page", {"back": BACK}, title="设置")
MODAL = Screen("modal", {"close": CLOSE}, title="兑换奖励")
UNKNOWN = Screen("unknown", reason="认不出当前画面")
BANNERS = {"already_used": "兑换码已使用", "expired": "兑换码已过期", "invalid": "兑换码无效",
           "cooldown": "操作过于频繁，请稍后再试", "other": "请输入兑换码"}


def row(text, box):
    return SimpleNamespace(text=text, box=list(box))


def result(rows):
    rows = list(rows)
    if not rows:
        return SimpleNamespace(hit=False, box=None, best_result=None, filtered_results=[])
    return SimpleNamespace(hit=True, box=rows[0].box, best_result=rows[0], filtered_results=rows)


MISS = result([])
QUIT = (Screen("quit_dialog", {"cancel": [494, 480, 20, 20]}), np.full((720, 1280, 3), 7, np.uint8),
        lambda node, override=None: result([row("退出游戏", [560, 200, 160, 40])])
        if node in (actions.QUIT_TITLE, actions.QUIT_CANCEL) else MISS)


class Game:
    """A scripted game: HUD -> 美鸭梨 -> 设置 (图像, or ``tab``) -> 其他 (row faded) -> swiped -> 兑换奖励 modal. ``results``:
    what each 使用 tap brings ("layer": a 「恭喜获得」 layer, a BANNERS key: that banner over the modal for
    ``banner_reads`` frames, then the modal again with the code still in the field, "nothing": no change, "popup": an
    unknown dialog). ``field``: what the modal's field holds when it opens. ``typed``: what the field reads after a text
    input (default: the text). ``screens``: frames shown instead, keyed by the number of touches sent. ``stop_on``: the
    touch after which the run is stopped. ``start``: where the game is when the task starts. ``settle_after``: the faded
    row settles by itself after that many 其他 frames. ``close_to``: where the modal's ✕ leads. ``fade_reads``: frames of a
    fading banner (pink band half gone, no text read, the modal's title readable again) after it. ``result_field``: what
    the field reads while the banner shows. ``with_banner``: what else shows with the banner ("layer": the reward
    layer's texts, "price": a 「¥6」 label, "close": another popup's close mark)."""

    def __init__(self, results=(), *, field="", typed=None, forbidden=(), screens=None, stop_on=None, start="hud",
                 banner_reads=2, tab="settings", settle_after=None, close_to="scrolled", fade_reads=0,
                 result_field=None, with_banner=None, layer_stays=False, dismiss_field=None, layer_text=False,
                 layer_strip=False):
        self.where, self.field, self.focused, self.banner, self.banner_left = start, field, False, "", 0
        self.results, self.typed, self.forbidden = list(results), typed, list(forbidden)
        self.screens, self.stop_on, self.banner_reads, self.tab = dict(screens or {}), stop_on, banner_reads, tab
        self.settle_after, self.other_reads, self.close_to = settle_after, 0, close_to
        self.touches, self.texts, self.rt = [], [], None
        self.fade_reads, self.fade_left, self.result_field, self.with_banner = fade_reads, 0, result_field, with_banner
        # The success layer: ``layer_stays`` (the dismissal tap does nothing), ``dismiss_field`` (what the field reads
        # once the modal is back), ``layer_text`` (text read around the dismissal point), ``layer_strip`` (the input
        # strip still open under the layer).
        self.layer_stays, self.dismiss_field, self.layer_text, self.layer_strip = (layer_stays, dismiss_field,
                                                                                   layer_text, layer_strip)

    def frame(self):
        frame = np.zeros((720, 1280, 3), np.uint8)
        if self.where in ("modal", "banner", "fade"):
            if self.focused:
                frame[665:720, :] = 255
            if self.field:
                x, y, w, h = redeem.USE_FACE
                frame[y:y + h, x:x + w] = GOLD
        if self.where == "layer" and self.layer_strip:
            frame[665:720, :] = 255
        if self.where == "banner":
            frame[192:232, 310:970] = PINK
        if self.where == "fade":  # about half the band still pink: neither a banner nor gone
            x, y, w, h = redeem.BANNER_BAND
            frame[y:y + h, x:x + w // 2] = PINK
        return frame

    def recognize(self, node, override=None):
        where = self.where
        modal, body = where in ("modal", "fade"), where in ("modal", "banner", "fade")
        settings = where in ("settings", "control", "other", "scrolled") or body
        if node == pages.FORBIDDEN:
            rows = list(self.forbidden) + ([("全部恢复默认", RESET)] if settings else [])
            if where == "banner" and self.with_banner == "price":
                rows.append(("¥6", [700, 420, 60, 30]))
            expected = (override or {}).get("expected", pages.FORBIDDEN_WORDS)
            return result([row(text, box) for text, box in rows if re.search(expected, text)])
        if node in (actions.QUIT_TITLE, actions.QUIT_CANCEL):
            return MISS
        if node == actions.MODAL_CLOSE:
            if where == "banner" and self.with_banner == "close":
                return result([row("", [1100, 120, 36, 36])])
            return result([row("", CLOSE)]) if modal or where == "popup" else MISS
        if node == actions.HUD_DUCK:
            return result([row("", DUCK)]) if where == "hud" else MISS
        if node == actions.BACK_ARROW:
            return result([row("", BACK)]) if where in ("menu", "settings", "control", "other", "scrolled") else MISS
        if node == redeem.MENU_SETTINGS:
            return result([row("", GEAR)]) if where == "menu" else MISS
        if node == redeem.OTHER_TAB:
            return result([row("其他", OTHER)]) if settings else MISS
        if node == redeem.IMAGE_CONTENT:
            return result([row("基础设置", IMAGE_HEADER)]) if where == "settings" else MISS
        if node == redeem.ROW_LABEL:
            return result({"other": [row("兑换码", HEADER)],
                           "scrolled": [row("兑换码", HEADER_SCROLLED), row("兑换码", ROW_LABEL)]}.get(where, []))
        if node == redeem.ROW_USE:
            return result({"other": [row("使用", ROW_USE_FADED)], "scrolled": [row("使用", ROW_USE)]}.get(where, []))
        if node == redeem.PLACEHOLDER:
            return result([row("点击此处输入兑换码", PLACEHOLDER)]) if body and not self.field else MISS
        if node == redeem.CLEAR:
            return result([row("", CLEAR)]) if body and self.field else MISS
        if node == redeem.FIELD:
            return result([row(self.field, FIELD)]) if body and self.field else MISS
        if node == redeem.USE:
            return result([row("使用", USE)]) if body else MISS
        if node == redeem.BANNER:
            return result([row(self.banner, BANNER)]) if where == "banner" else MISS
        if node == actions.PAGE_TITLE and (override or {}).get("roi") == redeem.DISMISS_CLEAR:
            return result([row("活动", [560, 670, 60, 24])]) if where == "layer" and self.layer_text else MISS
        if node in (redeem.LAYER_TITLE, redeem.LAYER_HINT):
            layer = where == "layer" or (where == "banner" and self.with_banner == "layer")
            return result([row("恭喜获得", [570, 165, 136, 42])]) if layer else MISS
        return MISS

    def snapshot(self, where):
        """The frame the game would show at ``where`` (for scripted screens)."""
        saved, self.where = self.where, where
        try:
            screen = {"hud": HUD, "menu": MENU, "settings": SETTINGS, "control": SETTINGS, "other": SETTINGS,
                      "scrolled": SETTINGS, "modal": MODAL, "fade": MODAL}.get(where, UNKNOWN)
            return screen, self.frame(), (lambda w: lambda node, override=None: self._at(w, node, override))(where)
        finally:
            self.where = saved

    def _at(self, where, node, override):
        saved, self.where = self.where, where
        try:
            return self.recognize(node, override)
        finally:
            self.where = saved

    def screen_now(self, rt):
        if rt.stopped:
            return None, None, None
        if self.screens.get(len(self.touches)):
            return self.screens[len(self.touches)].pop(0)
        if self.where == "other":
            self.other_reads += 1
            if self.settle_after is not None and self.other_reads > self.settle_after:
                self.where = "scrolled"
        elif self.where == "banner":
            if self.banner_left <= 0:
                self.where, self.banner, self.fade_left = "fade", "", self.fade_reads
                self.field = self.restore_field
            self.banner_left -= 1
        if self.where == "fade":
            if self.fade_left <= 0:
                self.where = "modal"
            self.fade_left -= 1
        return self.snapshot(self.where)

    def touched(self, what):
        self.touches.append(what)
        if self.stop_on == what:
            self.rt.stopped = True

    def action(self, name, source_frame=None, **fields):
        assert source_frame is not None
        if name == "Swipe":
            assert self.where == "other" and fields["duration"] == redeem.SWIPE_MS
            self.where = "scrolled"
            self.touched(("swipe", tuple(fields["begin"]), tuple(fields["end"])))
            return True
        target = list(fields["target"])
        if target == DUCK_POINT and self.where == "hud":
            self.where, what = "menu", "duck"
        elif target == GEAR and self.where == "menu":
            self.where, what = self.tab, "gear"
        elif target == OTHER and self.where == "settings":
            self.where, what = "other", "other"
        elif target == ROW_USE and self.where == "scrolled":
            self.where, what = "modal", "row"
        elif target == PLACEHOLDER and self.where == "modal":
            self.focused, what = True, "field"
        elif target == CLEAR and self.where == "modal":
            self.field, self.focused, what = "", False, "clear"
        elif target == USE and self.where == "modal":
            what, outcome = "use", self.results.pop(0)
            if outcome in ("layer", "popup"):
                self.where = outcome
            elif outcome in BANNERS:
                self.where, self.banner, self.banner_left = "banner", BANNERS[outcome], self.banner_reads
                self.restore_field = self.field
                self.field = self.result_field or self.field
        elif target == list(redeem.DISMISS_POINT) and self.where == "layer":
            what = "dismiss"
            if not self.layer_stays:
                self.where = "modal"
                self.field = self.dismiss_field or self.field
        elif target == CLOSE and self.where == "modal":
            self.where, what = self.close_to, "close"
        elif target == BACK and self.where in ("scrolled", "menu"):
            self.where, what = {"scrolled": "menu", "menu": "hud"}[self.where], "back"
        else:
            raise AssertionError(f"unexpected tap {target} on {self.where}")
        self.touched(what)
        return True

    def send_text(self, rt, text, source_frame):
        assert self.where == "modal" and self.focused and not self.field and source_frame is not None
        self.texts.append(text)
        self.field = self.typed or text
        return True


def play(game, codes=("ABC123",), record=None):
    rt = SimpleNamespace(stopped=False, logs=[], key=Mock(side_effect=AssertionError("shared named action")),
                         _frame_still_valid=Mock(return_value=True), pause=Mock(return_value=True),
                         diagnostic=Mock(), TOUCH_KEY_ATTEMPTS=3, action=game.action)
    rt.log = rt.logs.append
    game.rt = rt
    root = Path(tempfile.mkdtemp())
    if record:
        path = root.joinpath(*redeem.RECORD_FILE)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"items": {code: {"outcome": outcome, "confirmed_at": "2026-11-26T00:00:00+00:00"}
                                              for code, outcome in record.items()}}), encoding="utf-8")
    with patch.object(pages, "screen_now", side_effect=game.screen_now), \
            patch.object(pages, "send_text", side_effect=game.send_text), \
            patch("app_paths.DATA_ROOT", root), \
            patch("time.monotonic", side_effect=itertools.count(0, .25).__next__):
        outcome = redeem.run(rt, [(code, False) for code in codes])
    rt.record = redeem.Record(root).outcomes()
    return outcome, rt


def stops(rt):
    return [c for c in rt.diagnostic.call_args_list if c.args[0].startswith("android_redeem_stop_")]


ENTRY = ["duck", "gear", "other", ("swipe", (700, 520), (700, 300)), "row", "field"]


class FlowTests(unittest.TestCase):
    def test_each_code_is_typed_read_back_and_submitted_once_and_each_banner_is_recorded(self):
        codes = ("USED1", "OLD2", "BAD3", "BUSY4")
        game = Game(["already_used", "expired", "invalid", "cooldown"])
        outcome, rt = play(game, codes)
        self.assertTrue(outcome, rt.logs)
        self.assertEqual(game.touches[:6], ENTRY)
        self.assertEqual(game.texts, list(codes))
        self.assertEqual(game.touches.count("use"), len(codes))  # never a second submit, a cooldown included
        self.assertEqual(game.touches[-3:], ["close", "back", "back"])
        self.assertEqual(rt.record, {"USED1": "already_used", "OLD2": "expired", "BAD3": "expired",
                                     "BUSY4": "cooldown"})
        for words in ("兑换码「USED1」已使用过", "兑换码「OLD2」已过期或无效", "兑换码「BUSY4」提示操作太频繁，本次不再提交它",
                      "兑换码输入框读回「USED1」，与兑换码一致。"):
            self.assertTrue(any(words in line for line in rt.logs), (words, rt.logs))
        # Each later code: after the banner has gone, the field still holds the previous code: its X, then the field.
        uses = [i for i, t in enumerate(game.touches) if t == "use"]
        for before, after in zip(uses, uses[1:]):
            self.assertEqual(game.touches[before + 1:after], ["clear", "field"])
        self.assertEqual(stops(rt), [])
        rt.key.assert_not_called()  # the shared named action (which taps 取消 and tries again) is not used

    def test_the_device_run_banner_is_waited_out_before_the_next_code(self):
        game = Game(["already_used", "already_used"], banner_reads=8)
        outcome, rt = play(game, ("拾光二载暖赴新章", "NEXT"))
        self.assertTrue(outcome, rt.logs)
        self.assertEqual(rt.record, {"拾光二载暖赴新章": "already_used", "NEXT": "already_used"})

    def test_a_banner_that_never_goes_stops_before_the_next_code(self):
        game = Game(["already_used", "already_used"], banner_reads=10 ** 6)
        outcome, rt = play(game, ("USED1", "NEXT"))
        self.assertFalse(outcome)
        self.assertEqual(game.touches.count("use"), 1)
        self.assertEqual(game.touches[-1], "use")
        self.assertEqual(len(stops(rt)), 1)

    def test_recorded_codes_are_skipped_as_on_the_pc(self):
        done = {"A": "redeemed", "B": "already_used", "C": "expired", "D": "cooldown"}
        self.assertEqual(redeem.pending_codes([("A", False), ("B", True), ("C", False), ("C2", False), ("D", False)],
                                              done), ["C2", "D"])
        self.assertEqual(redeem.pending_codes([("C", True)], done), ["C"])  # an expired code entered by hand
        game = Game()
        outcome, rt = play(game, ("A", "B"), record=done)
        self.assertTrue(outcome)
        self.assertEqual(game.touches, [])
        self.assertIn("兑换码领取：暂无待兑换的新兑换码，跳过。", rt.logs)

    def test_with_nothing_pending_a_leftover_modal_is_left_the_verified_way(self):
        game = Game(start="modal", field="USED1")
        outcome, rt = play(game, ("A",), record={"A": "already_used"})
        self.assertTrue(outcome, rt.logs)
        self.assertEqual(game.touches, ["close", "back", "back"])
        for start in ("layer", "popup"):
            with self.subTest(start=start):
                game = Game(start=start)
                outcome, rt = play(game, ("A",), record={"A": "already_used"})
                self.assertFalse(outcome)
                self.assertEqual(game.touches, [])
                self.assertEqual(len(stops(rt)), 1)

    def test_a_filled_field_is_cleared_by_its_x_before_the_field_is_tapped(self):
        game = Game(["already_used"], field="LEFTOVER")
        outcome, rt = play(game)
        self.assertTrue(outcome, rt.logs)
        self.assertEqual(game.touches[4:7], ["row", "clear", "field"])
        self.assertEqual(game.texts, ["ABC123"])

    def test_a_field_that_reads_other_text_is_never_submitted(self):
        for typed in ("ABCI23", "ABC 123"):  # inner whitespace is not stripped away either
            with self.subTest(typed=typed):
                game = Game(["already_used"], typed=typed)
                outcome, rt = play(game)
                self.assertFalse(outcome)
                self.assertNotIn("use", game.touches)
                self.assertEqual(game.touches[-1], "field")  # nothing after the input, not even the way back
                self.assertEqual(len(stops(rt)), 1)
                self.assertIn(f"输入框读回「{typed}」，不是兑换码「ABC123」，没有提交", stops(rt)[0].args[1])
                self.assertEqual(rt.record, {})

    def test_an_unknown_result_or_a_reward_layer_stops_with_that_frame_and_records_nothing(self):
        for results, words in ((["nothing"], "没有看到认得的提示条"), (["other"], "提示条认不出：「请输入兑换码」"),
                               (["popup"], "认不出的画面")):
            with self.subTest(results=results):
                game = Game(results)
                outcome, rt = play(game, ("ABC123", "NEXT"))
                self.assertFalse(outcome)
                self.assertEqual(game.touches[-1], "use")
                self.assertEqual(game.texts, ["ABC123"])
                self.assertEqual(len(stops(rt)), 1)
                self.assertIn(words, stops(rt)[0].args[1])
                self.assertIsNotNone(stops(rt)[0].kwargs["image"])
                self.assertEqual(rt.record, {})

    def test_a_stop_before_the_text_input_sends_no_text(self):
        game = Game(["already_used"], stop_on="field")
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.texts, [])
        self.assertEqual(game.touches[-1], "field")

    def test_a_forbidden_word_next_to_the_row_refuses_the_tap_and_ends_all_input(self):
        game = Game(["already_used"], forbidden=[("退出登录", [940, 615, 100, 30])])
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertNotIn("row", game.touches)
        self.assertEqual(game.touches[-1], ("swipe", (700, 520), (700, 300)))
        self.assertTrue(any("退出登录" in line for line in rt.logs), rt.logs)
        self.assertEqual(len(stops(rt)), 1)  # the refusing frame is saved
        self.assertIsNotNone(stops(rt)[0].kwargs["image"])

    def test_a_row_that_settles_before_the_swipe_is_tapped_without_a_swipe(self):
        game = Game(["already_used"], settle_after=1)
        outcome, rt = play(game)
        self.assertTrue(outcome, rt.logs)
        self.assertEqual(game.touches[:5], ["duck", "gear", "other", "row", "field"])

    def test_another_settings_tab_stops_on_its_first_frame(self):
        game = Game(["already_used"], tab="control")
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, ["duck", "gear"])
        self.assertEqual(len(stops(rt)), 1)
        game = Game(["already_used"], close_to="settings")  # the ✕ led to 图像, not 其他
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches[-1], "close")
        self.assertEqual(len(stops(rt)), 1)

    def test_a_quit_dialog_on_an_entry_or_return_tap_frame_stops_with_that_frame(self):
        game = Game(["already_used"])
        hud = game.snapshot("hud")
        game.screens = {0: [hud, hud, hud, QUIT]}  # the duck tap's confirming frame
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, [])
        self.assertIs(stops(rt)[0].kwargs["image"], QUIT[1])
        game = Game(["already_used"])
        scrolled = game.snapshot("scrolled")
        game.screens = {8: [scrolled, scrolled, scrolled, QUIT]}  # after the ✕: the back tap's confirming frame
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches[-1], "close")
        self.assertIs(stops(rt)[0].kwargs["image"], QUIT[1])

    def test_a_success_layer_is_dismissed_once_and_recorded_after_the_modal_shows_the_code_again(self):
        game = Game(["already_used", "layer", "expired"])
        outcome, rt = play(game, ("USED1", "为暖暖攒下生日祝福", "OLD3"))
        self.assertTrue(outcome, rt.logs)
        self.assertEqual(rt.record, {"USED1": "already_used", "为暖暖攒下生日祝福": "redeemed", "OLD3": "expired"})
        self.assertEqual(game.touches.count("dismiss"), 1)
        at = game.touches.index("dismiss")
        self.assertEqual(game.touches[at - 1:at + 3], ["use", "dismiss", "clear", "field"])  # then the next code
        self.assertIn("兑换码「为暖暖攒下生日祝福」兑换成功。", rt.logs)
        self.assertIn("兑换码领取：成功兑换 1 个兑换码。", rt.logs)

    def test_a_success_layer_that_cannot_be_confirmed_records_nothing(self):
        for options, touched, words in (
                ({"layer_stays": True}, True, "关闭「恭喜获得」奖励层"),
                ({"dismiss_field": "OTHER"}, True, "不是刚提交的「ABC123」，没有记录"),
                ({"layer_text": True}, False, "奖励层在点空白处之前变了"),
                ({"layer_strip": True}, False, "奖励层在点空白处之前变了"),
                ({"with_banner": "layer"}, False, "奖励层和提示条同时出现")):
            with self.subTest(options=options):
                results = ["already_used"] if "with_banner" in options else ["layer"]
                game = Game(results, **options)
                outcome, rt = play(game, ("ABC123", "NEXT"))
                self.assertFalse(outcome)
                self.assertEqual(rt.record, {})
                self.assertEqual(game.touches.count("dismiss"), int(touched))
                self.assertEqual(game.touches[-1], "dismiss" if touched else "use")
                self.assertEqual(len(stops(rt)), 1)
                self.assertIn(words, stops(rt)[0].args[1])

    def test_a_fading_banner_is_waited_out_before_any_tap_on_the_modal(self):
        game = Game(["already_used", "expired"], fade_reads=3)  # the 「清空」 tap on a fading frame would raise
        outcome, rt = play(game, ("USED1", "OLD2"))
        self.assertTrue(outcome, rt.logs)
        self.assertEqual(rt.record, {"USED1": "already_used", "OLD2": "expired"})
        tap = redeem.banner_pink
        half = np.zeros((720, 1280, 3), np.uint8)
        x, y, w, h = redeem.BANNER_BAND
        half[y:y + h, x:x + w // 2] = PINK
        self.assertFalse(redeem.quiet(half))  # 0.5: not a banner to read, not gone either
        self.assertLess(tap(half), redeem.BANNER_PINK)

    def test_the_pink_score_counts_both_hue_ranges_together(self):
        import cv2
        x, y, w, h = redeem.BANNER_BAND
        frame = np.zeros((720, 1280, 3), np.uint8)
        hsv = np.zeros((h, w, 3), np.uint8)
        hsv[:, :] = (100, 10, 120)  # grey
        hsv[:, :round(w * .28)] = (172, 80, 240)  # pink just below red's wrap-around
        hsv[:, round(w * .28):round(w * .54)] = (4, 80, 240)  # pink just above it
        frame[y:y + h, x:x + w] = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
        self.assertAlmostEqual(redeem.banner_pink(frame), .54, delta=.02)
        self.assertFalse(redeem.quiet(frame))  # each range alone (0.28, 0.26) would pass as gone

    def test_a_banner_is_recorded_only_with_the_submitted_code_still_in_the_field(self):
        game = Game(["already_used"], result_field="PROBE123")
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(rt.record, {})
        self.assertIn("不是刚提交的「ABC123」，没有记录", stops(rt)[0].args[1])

    def test_a_banner_with_a_reward_layer_another_popup_or_prices_is_not_recorded(self):
        for with_banner, words in (("close", "别的弹窗的关闭标记"), ("price", "价格或购买")):
            with self.subTest(with_banner=with_banner):
                game = Game(["already_used"], with_banner=with_banner)
                outcome, rt = play(game)
                self.assertFalse(outcome)
                self.assertEqual(rt.record, {})
                self.assertEqual(game.touches[-1], "use")
                self.assertIn(words, stops(rt)[0].args[1])

    def test_the_first_settings_frame_after_the_gear_decides_the_tab(self):
        game = Game(["already_used"])
        control = game.snapshot("control")
        game.screens = {2: [control]}  # one 控制 frame, then 图像
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, ["duck", "gear"])
        self.assertIs(stops(rt)[0].kwargs["image"], control[1])

    def test_a_settings_title_with_more_than_settings_is_refused(self):
        game = Game(["already_used"], start="control")
        _screen, frame, recognize = game.snapshot("control")
        odd = (Screen("page", {"back": BACK}, title="设置 控制"), frame, recognize)
        game.screens = {0: [odd]}
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, [])
        self.assertIn("「设置 控制」", stops(rt)[0].args[1])

    def test_a_user_stop_ends_the_task_without_a_failure_line(self):
        rt = SimpleNamespace(stopped=False, log=Mock())
        self.assertFalse(redeem.fail(rt, "没能回到大世界"))
        rt.log.assert_called_once()
        rt.log.reset_mock()
        with patch("host.android_user_stop", return_value=True):
            self.assertFalse(redeem.fail(rt, "没能回到大世界"))
        rt.log.assert_not_called()

    def test_an_unexpected_screen_while_opening_stops_with_that_frame(self):
        popup = (UNKNOWN, np.zeros((720, 1280, 3), np.uint8),
                 lambda node, override=None: result([row("", CLOSE)]) if node == actions.MODAL_CLOSE else MISS)
        game = Game(["already_used"], screens={2: [popup]})  # after the gear: an unknown dialog, not 设置
        outcome, rt = play(game)
        self.assertFalse(outcome)
        self.assertEqual(game.touches, ["duck", "gear"])
        self.assertEqual(len(stops(rt)), 1)
        self.assertIs(stops(rt)[0].kwargs["image"], popup[1])


class TextInputTests(unittest.TestCase):
    """pages.send_text: the last checks, the log and the reservation around Controller.post_input_text."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        if not HAS_FLOCK:  # Windows CI: ownership is injected (as in test_android_backend.py); macOS tests the lock
            owner = patch.object(host, "take_owner_lock", side_effect=lambda path: (
                path.parent.mkdir(parents=True, exist_ok=True), SimpleNamespace(close=lambda: None))[1])
            owner.start()
            self.addCleanup(owner.stop)
        self.state = host.DeviceInputState(self.root)
        self.frame = np.zeros((4, 4, 3), np.uint8)
        self.state.mark(self.frame, 0, SimpleNamespace(state="none", boot_id="boot", latest=1))
        self.posted = []
        job = SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=self.succeeded))
        self.succeeded = True
        self.rt = SimpleNamespace(
            stopped=False, _focus_generation=0, check_focus=Mock(), _sessionless_target=lambda: SimpleNamespace(
                serial="127.0.0.1:5555"), _device_focus_now=Mock(return_value=True),
            _stopped_now=Mock(return_value=False), _halt_unconfirmed=Mock(side_effect=self.state.halt),
            _release_touch=lambda state: state.release_touch(),
            controller=SimpleNamespace(post_input_text=lambda text: self.posted.append(text) or job))
        self.patch = patch("host.device_input", return_value=self.state)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(lambda: self.state._owner and self.state._owner.close())

    def log(self):
        path = self.root / "logs/android-input.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return [(entry["action"], entry.get("text"), entry["outcome"]) for entry in map(json.loads, lines)]

    def test_the_intent_is_logged_before_and_the_outcome_after(self):
        self.assertTrue(pages.send_text(self.rt, "ABC123", self.frame))
        self.assertEqual(self.posted, ["ABC123"])
        self.assertEqual(self.log(), [("InputText", "ABC123", "sending"), ("InputText", "ABC123", "sent")])
        self.assertFalse(self.state.marker().exists())

    def test_a_stop_request_cancels_the_input_after_the_intent(self):
        self.state.stop_request().parent.mkdir(parents=True, exist_ok=True)
        self.state.stop_request().write_text("")
        self.assertFalse(pages.send_text(self.rt, "ABC123", self.frame))
        self.assertEqual(self.posted, [])
        self.assertEqual(self.log(), [("InputText", "ABC123", "sending"), ("InputText", "ABC123", "cancelled")])
        self.assertFalse(self.state.marker().exists())

    def test_nothing_is_sent_or_logged_when_input_is_halted_or_the_run_stopped(self):
        self.rt.stopped = True
        self.assertFalse(pages.send_text(self.rt, "ABC123", self.frame))
        self.rt.stopped = False
        self.state.halt()
        self.assertFalse(pages.send_text(self.rt, "ABC123", self.frame))
        self.assertEqual((self.posted, self.log()), ([], []))

    def test_a_stale_frame_or_a_held_contact_sends_nothing(self):
        from task_control import ForegroundChanged
        self.state.observed_loss()
        with self.assertRaises(ForegroundChanged):
            pages.send_text(self.rt, "ABC123", self.frame)
        held = np.zeros((4, 4, 3), np.uint8)
        self.state.mark(held, 0, SimpleNamespace(state="injected", boot_id="boot", latest=1))
        with self.assertRaises(host.InputUnconfirmed):
            pages.send_text(self.rt, "ABC123", held)
        self.assertEqual(self.posted, [])
        self.assertTrue(self.state.halted)

    def test_a_user_stop_cancels_the_input_also_with_an_unreadable_touch_reading(self):
        unread = np.zeros((4, 4, 3), np.uint8)
        self.state.mark(unread, 0, None)  # the touch reading before the frame could not be read
        self.state.stop_request().parent.mkdir(parents=True, exist_ok=True)
        self.state.stop_request().write_text("stop")
        with patch("host.game_backend", return_value="android"):
            self.assertFalse(pages.send_text(self.rt, "ABC123", unread))
        self.assertEqual(self.posted, [])
        self.assertFalse(self.state.halted)  # a stop, not an unconfirmed contact
        self.assertEqual(self.log(), [("InputText", "ABC123", "sending"), ("InputText", "ABC123", "cancelled")])
        self.state.stop_request().unlink()
        with self.assertRaises(host.InputUnconfirmed):  # without a stop the unreadable reading halts all input
            pages.send_text(self.rt, "ABC123", unread)
        self.assertTrue(self.state.halted)

    def test_an_exception_after_the_send_is_an_unconfirmed_input(self):
        for error, stop in ((OSError("adb pipe"), False), (RuntimeError("stopping"), True)):
            with self.subTest(error=type(error).__name__):
                self.setUp()
                self.rt.controller = SimpleNamespace(post_input_text=lambda text: SimpleNamespace(
                    wait=Mock(side_effect=error)))
                with patch("host.game_backend", return_value="android"):
                    if stop:  # the stop is published while the input is in flight
                        self.rt.controller = SimpleNamespace(post_input_text=lambda text: (
                            self.state.stop_request().parent.mkdir(parents=True, exist_ok=True),
                            self.state.stop_request().write_text("stop"),
                            SimpleNamespace(wait=Mock(side_effect=error)))[2])
                    with self.assertRaises(host.InputUnconfirmed) as caught:
                        pages.send_text(self.rt, "ABC123", self.frame)
                self.assertIs(caught.exception.__cause__, error)
                self.assertTrue(self.state.halted and self.state.marker().exists())
                self.assertEqual(self.log()[-1], ("InputText", "ABC123", f"raised {type(error).__name__}"))

    def test_a_failed_input_halts_all_input_and_keeps_the_reservation(self):
        self.succeeded = False
        with self.assertRaises(host.InputUnconfirmed):
            pages.send_text(self.rt, "ABC123", self.frame)
        self.assertEqual(self.log()[-1], ("InputText", "ABC123", "failed"))
        self.assertTrue(self.state.halted and self.state.marker().exists())

    def test_the_input_is_decided_and_confirmed_on_two_frames_that_accept_takes(self):
        frames = [(MODAL, np.zeros((2, 2, 3), np.uint8), None) for _ in range(2)]
        rt = SimpleNamespace(TOUCH_KEY_ATTEMPTS=3, _frame_still_valid=Mock(return_value=True))
        with patch.object(pages, "screen_now", side_effect=frames), \
                patch.object(pages, "send_text", return_value=True) as send:
            self.assertTrue(pages.input_text_on_screen(rt, "X1", "输入框", lambda s, f, r: True))
        send.assert_called_once_with(rt, "X1", frames[1][1])
        refused = [(MODAL, np.zeros((2, 2, 3), np.uint8), None), (UNKNOWN, np.ones((2, 2, 3), np.uint8), None)]
        with patch.object(pages, "screen_now", side_effect=refused), patch.object(pages, "send_text") as send:
            with self.assertRaises(pages.FrameMismatch) as caught:
                pages.input_text_on_screen(rt, "X1", "输入框", lambda s, f, r: s.kind == "modal")
        send.assert_not_called()
        self.assertIs(caught.exception.frame, refused[1][1])

    def test_runtime_input_text_still_refuses_on_android(self):
        from daily.runtime import Runtime
        with patch("daily.runtime.game_backend", return_value="android"), self.assertRaises(host.UnsupportedInput):
            Runtime.input_text(SimpleNamespace(stopped=False), "ABC123")


class EntryTests(unittest.TestCase):
    def test_the_entry_branches_to_the_android_flow_before_the_pc_class_and_its_clipboard(self):
        from action import gameplay
        runtime = SimpleNamespace(stopped=False, log=Mock(), main=Mock())
        argv = SimpleNamespace(custom_action_param=json.dumps({"kind": "redeem_codes"}),
                               task_detail=SimpleNamespace(entry="MaaNikki_RedeemCodes"))
        pc = Mock()
        pc.return_value.run.return_value = True
        refuse = Mock(side_effect=AssertionError("host clipboard"))
        with patch.object(gameplay, "Executors", return_value=runtime), patch.object(gameplay, "Gameplay"), \
                patch("daily.redeem_codes.RedeemCodes", pc):
            with patch("host.game_backend", return_value="android"), patch.object(redeem, "run", return_value=True) \
                    as run, patch("daily.redeem_codes.get_clipboard_text", refuse), \
                    patch("daily.redeem_codes.set_clipboard_text", refuse), patch("host.clipboard", refuse):
                self.assertTrue(gameplay.GameplayAction.run.__wrapped__(None, None, argv))
            run.assert_called_once_with(runtime)
            pc.assert_not_called()  # not even built: its constructor reads the UID the PC way
            with patch("host.game_backend", return_value="win32"), patch.object(redeem, "run") as run:
                self.assertTrue(gameplay.GameplayAction.run.__wrapped__(None, None, argv))
            run.assert_not_called()
            pc.assert_called_once_with(runtime)
            pc.return_value.run.assert_called_once_with()

    def test_the_android_flow_never_uses_the_host_clipboard(self):
        refuse = Mock(side_effect=AssertionError("host clipboard"))
        with patch("daily.redeem_codes.get_clipboard_text", refuse), \
                patch("daily.redeem_codes.set_clipboard_text", refuse), patch("host.clipboard", refuse):
            outcome, _rt = play(Game(["already_used", "expired"]), ("A1", "B2"))
        self.assertTrue(outcome)
        refuse.assert_not_called()

    def test_the_codes_come_from_the_shared_collection_with_the_default_options(self):
        rt = SimpleNamespace(context=Mock(), log=Mock())
        with patch("daily.runtime.parameters", return_value={}), \
                patch("daily.redeem_codes.collect_candidate_codes", return_value=[("X", False)]) as collect:
            self.assertEqual(redeem.candidates(rt), [("X", False)])
        collect.assert_called_once_with(online=True, extra_raw="", log=rt.log)


class ResourceTests(unittest.TestCase):
    def test_nodes_tier_and_guard_words(self):
        tiers = json.loads((ROOT / "agent/host/android/tasks.json").read_text(encoding="utf-8"))
        self.assertIn("MaaNikki_RedeemCodes", tiers["experimental"])
        note = tiers["notes"]["MaaNikki_RedeemCodes"]
        for words in ("兑换码已使用", "兑换成功过一次", "还没在设备上跑过", "就不记录", "停下并保存截图"):
            self.assertIn(words, note)
        if not (ROOT / "resource_android").is_dir():  # the release source archive does not list it (plan risk 13)
            self.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
            return
        nodes = json.loads((ROOT / "resource_android/pipeline/android_redeem.json").read_text(encoding="utf-8"))
        self.assertEqual(set(nodes), set(redeem.NODES))
        self.assertTrue(nodes[redeem.MENU_SETTINGS]["green_mask"])
        for name in ("menu_settings.png", "redeem_clear.png"):
            self.assertTrue((ROOT / "resource_android/image/android" / name).is_file())
        forbidden = json.loads((ROOT / "resource_android/pipeline/android_mailbox.json")
                               .read_text(encoding="utf-8"))[pages.FORBIDDEN]["expected"]
        self.assertEqual(forbidden, pages.FORBIDDEN_WORDS)
        import re
        for text in ("全部恢复默认", "恢复默认图像", "退出登录", "切换账号", "注销账号"):
            self.assertTrue(re.search(forbidden, text), text)
        for text in ("兑换码", "使用", "其他", "查看", "用户协议", "兑换奖励", "点击此处输入兑换码"):
            self.assertFalse(re.search(forbidden, text), text)


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
from host.android import actions, pages, redeem, touch_point

fixtures = repo / "scripts/fixtures/android"
crop = lambda name: cv2.imread(str(fixtures / name))
# UID-free crops of the saved frames (menu-p1, settings-other, settings-other-scrolled, c3-settings-open,
# redeem-modal, text-2-typed, device run 1's stop frame), put back where they were.
menu_title, bar, settings = crop("redeem_menu_title.png"), crop("redeem_menu_bar.png"), crop("page_title.png")
tabs_other, tabs_image, reset = crop("redeem_tabs_other.png"), crop("redeem_tabs_image.png"), crop("redeem_reset.png")
image_header = crop("redeem_image_header.png")  # 「基础设置」, the 图像 tab's first header (c3-settings-open)
faded, scrolled = crop("redeem_list.jpg"), crop("redeem_list_scrolled.jpg")
empty, typed = crop("redeem_modal_empty.jpg"), crop("redeem_modal_typed.jpg")
banner = crop("redeem_modal_banner.jpg")  # device run 1's stop frame: 「兑换码已使用」 over the modal's title bar
success = crop("redeem_success_layer.jpg")  # device run 2's stop frame, x >= 230, y >= 140: the 「恭喜获得」 layer
layer_title, layer_hint = crop("reward_layer_title.png"), crop("reward_layer_hint.png")

def compose(marks, dim=False, strip=False):
    frame = np.full((720, 1280, 3), (70, 62, 55), np.uint8)
    for image, x, y in marks:
        frame[y:y + image.shape[0], x:x + image.shape[1]] = image
    if dim:
        frame = (frame * 0.45).astype(np.uint8)
    if strip:
        frame[668:720, :] = 255
    return frame

page = [(settings, 0, 10), (reset, 990, 640)]
other = page + [(tabs_other, 0, 560)]
frames = {
    "menu": compose([(menu_title, 0, 0), (bar, 560, 620)]),
    "settings image": compose(page + [(tabs_image, 0, 560), (image_header, 262, 78)]),
    "other faded": compose(other + [(faded, 250, 500)]),
    "other scrolled": compose(other + [(scrolled, 250, 490)]),
    "reset under the row": compose(other + [(scrolled, 250, 490), (reset, 920, 612)]),
}
under = frames["other scrolled"]
frames["modal empty"] = compose([(under, 0, 0)], dim=True); frames["modal empty"][160:560, 300:980] = empty
frames["modal focused"] = frames["modal empty"].copy(); frames["modal focused"][668:720, :] = 255
frames["modal typed"] = compose([(under, 0, 0)], dim=True); frames["modal typed"][160:560, 300:980] = typed
frames["modal typed"][668:720, :] = 255
frames["modal banner"] = compose([(under, 0, 0)], dim=True); frames["modal banner"][160:560, 300:980] = banner
frames["success layer"] = compose([(success, 230, 140)])
frames["layer"] = (frames["modal typed"] * 0.45).astype(np.uint8)
frames["layer"][140:220, 540:740], frames["layer"][620:680, 520:760] = layer_title, layer_hint
current, results = {}, {}

class Frame(CustomController):
    def connect(self): return True
    def request_uuid(self): return "android-redeem-test"
    def screencap(self): return current["frame"]
    def refuse(self, *args): return False
    start_app = stop_app = click = swipe = touch_down = touch_move = touch_up = refuse
    click_key = input_text = key_down = key_up = refuse

def target(recognize, node):
    result = recognize(node)
    if not actions._hit(result):
        return None
    box = actions._box(result)
    return [touch_point(box), pages._forbidden_near(recognize, box)]

class Judge(CustomAction):
    def run(self, context, argv):
        image = context.tasker.controller.post_screencap().wait().get()
        def recognize(node, override=None):
            return context.run_recognition(node, image, {node: override} if override else None)
        screen = actions.read_screen(recognize)
        out = {"kind": screen.kind, "title": screen.title, "gear": target(recognize, redeem.MENU_SETTINGS),
               "other_view": redeem.other_view(screen, recognize), "other_tab": target(recognize, redeem.OTHER_TAB),
               "layer": redeem.is_reward_layer(screen, recognize), "image": redeem.image_view(screen, recognize),
               "banner": redeem.banner(screen, image, recognize), "blank": redeem.blank_at_dismiss(recognize),
               "dismiss": pages._forbidden_near(recognize, [*redeem.DISMISS_POINT, 1, 1])}
        use = redeem.row_use(recognize)
        out["row_use"] = use and [touch_point(use), pages._forbidden_near(recognize, use)]
        out["faded"] = redeem.faded_row(recognize)
        out["swipe"] = [pages._forbidden_near(recognize, [*p, 1, 1]) for p in redeem.LIST_SWIPE]
        if screen.kind == "modal" or out["banner"]:
            out.update(field=list(redeem.read_field(image, recognize)), strip=redeem.strip_open(image),
                       gold=round(redeem.use_gold(image), 3),
                       clear=target(recognize, redeem.CLEAR), placeholder=target(recognize, redeem.PLACEHOLDER),
                       use=target(recognize, redeem.USE), close=target(recognize, actions.MODAL_CLOSE))
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
    """The real nodes and OCR, both layers loaded, on frames composed from UID-free crops of the saved 设置 and 兑换奖励
    frames (in a fresh interpreter, as in test_android_weekly.py)."""

    def test_the_nodes_targets_and_guards_on_composed_frames(self):
        if not (ROOT / "resource_android").is_dir():
            self.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
            return
        env = {**os.environ, "MAAFW_BINARY_PATH": str(ROOT / "maafw")}
        # -X utf8: the child prints Chinese text, and -I ignores PYTHONUTF8 (Windows consoles default to cp1252).
        completed = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", "-c", FRAMEWORK_CHECK, str(ROOT)],
                                   capture_output=True, text=True, encoding="utf-8", timeout=300, env=env,
                                   cwd=tempfile.gettempdir())
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        out = json.loads(completed.stdout.strip().splitlines()[-1])
        kinds = {name: (value["kind"], value["title"]) for name, value in out.items()}
        self.assertEqual(kinds, {"menu": ("page", "美鸭梨"), "settings image": ("page", "设置"),
                                 "other faded": ("page", "设置"), "other scrolled": ("page", "设置"),
                                 "reset under the row": ("page", "设置"), "modal empty": ("modal", "兑换奖励"),
                                 "modal focused": ("modal", "兑换奖励"), "modal typed": ("modal", "兑换奖励"),
                                 "modal banner": ("unknown", ""), "success layer": ("unknown", ""),
                                 "layer": ("unknown", "")})
        # The gear only on the menu, where the survey tapped (633,668).
        self.assertEqual(out["menu"]["gear"], [[633, 667], None])
        self.assertEqual([name for name, value in out.items() if value["gear"]], ["menu"])
        # 其他: its content only on that tab; the tab label tappable on 图像.
        self.assertFalse(out["settings image"]["other_view"])
        self.assertEqual([name for name, value in out.items() if value["image"]], ["settings image"])
        self.assertEqual(out["settings image"]["other_tab"][1], None)
        self.assertTrue(out["other faded"]["other_view"] and out["other scrolled"]["other_view"])
        # The row's 使用: not lined up while the list is faded (one swipe first), lined up after it; refused once a
        # forbidden label (全部恢复默认) comes within the margin below it.
        self.assertIsNone(out["other faded"]["row_use"])
        self.assertTrue(out["other faded"]["faded"])
        self.assertFalse(out["other scrolled"]["faded"])
        self.assertEqual(out["other faded"]["swipe"], [None, None])
        tap, refused = out["other scrolled"]["row_use"]
        self.assertLessEqual(max(abs(tap[0] - 987), abs(tap[1] - 588)), 8)  # phase A tapped (987,588)
        self.assertIsNone(refused)
        self.assertEqual(out["reset under the row"]["row_use"][1], "全部恢复默认")
        # The modal: field states, focus by the strip, the gold 使用 once filled, no hint, targets and guards.
        empty, focused, typed = out["modal empty"], out["modal focused"], out["modal typed"]
        self.assertEqual((empty["field"], empty["strip"], empty["gold"]), (["empty", ""], False, 0.0))
        self.assertEqual((focused["field"], focused["strip"]), (["empty", ""], True))
        self.assertEqual((typed["field"], typed["strip"]), (["filled", "PROBE123"], True))
        self.assertGreaterEqual(typed["gold"], redeem.USE_GOLD)
        self.assertEqual(empty["placeholder"], [[518, 340], None])
        self.assertIsNone(empty["clear"])
        self.assertEqual(typed["clear"][1], None)
        self.assertLessEqual(max(abs(typed["clear"][0][0] - 808), abs(typed["clear"][0][1] - 342)), 3)
        for value in (empty, focused, typed):
            self.assertEqual(value["use"][1], None)
            self.assertLessEqual(max(abs(value["use"][0][0] - 637), abs(value["use"][0][1] - 499)), 3)
            self.assertEqual(value["close"][1], None)
        self.assertTrue(out["layer"]["layer"])
        # The result banner (device run 1): read over the modal's body, the field still holding the code.
        self.assertEqual([name for name, value in out.items() if value["banner"]], ["modal banner"])
        self.assertEqual(out["modal banner"]["banner"], "兑换码已使用")
        self.assertEqual(out["modal banner"]["field"], ["filled", "拾光二载暖赴新章"])
        self.assertEqual([name for name, value in out.items() if value["layer"]], ["success layer", "layer"])
        # The success layer of device run 2: its dismissal point blank on it and on the modal and 其他 frames beneath.
        for name in ("success layer", "modal empty", "modal banner", "other scrolled"):
            self.assertTrue(out[name]["blank"], name)
            self.assertIsNone(out[name]["dismiss"], name)


if __name__ == "__main__":
    unittest.main()
