"""Android action table (agent/host/android/actions.py) and its Runtime hook: python scripts/test_android_actions.py

Screen classification, the quit guard, the HUD shortcut search and the taps Runtime.key sends on an Android target,
with a scripted recogniser; the pipeline nodes and templates in resource_android/ with MaaFramework itself on
synthetic frames (frames from the device show the account UID and are not committed; the real-frame check is
`scripts/android/probe.py screen --frame ...`).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))

import host  # noqa: E402
from host.android import actions  # noqa: E402
from test_android_backend import FreshAndroidState, advancing_snapshots, android_run  # noqa: E402


def hit(box, text=""):
    return SimpleNamespace(hit=True, box=box, best_result=SimpleNamespace(text=text))


MISS = SimpleNamespace(hit=False, box=None, best_result=None)

# The label row of the observed HUD: label -> (centre x, width), text top at y=64.
LABELS = {"商城": (657, 36), "共鸣": (727, 36), "奇迹之旅": (798, 70), "活动大厅": (868, 70), "奇想日历": (939, 70),
          "无限之心": (1010, 70), "任务": (1081, 36), "设计图": (1152, 54), "换装": (1222, 36)}


class Scripted:
    """A recogniser over a described screen: fixed nodes hit or miss; the label OCR reads every label that lies
    wholly inside its region (as one merged box, like the real OCR). ``ocr=False`` models an OCR engine that cannot
    run: every OCR node reports an ordinary miss (what MaaFramework 5.16.1 does without its model)."""

    OCR_NODES = {actions.QUIT_TITLE, actions.QUIT_CANCEL, actions.REDEEM_TITLE, actions.HUD_LABEL, actions.PAGE_TITLE}

    def __init__(self, *, quit=False, cancel=False, modal=False, redeem=True, duck=False, arrow=False, lit=None,
                 title="设置", labels=LABELS, ocr=True, errors=()):
        # A dialog dims what is behind it; without one the HUD and page icons are at full brightness.
        lit = not (quit or modal) if lit is None else lit
        self.fixed = {actions.QUIT_TITLE: quit and hit([560, 180, 160, 40], "退出游戏"),
                      actions.QUIT_CANCEL: cancel and hit([459, 461, 72, 40], "取消"),
                      actions.MODAL_CLOSE: (modal or quit) and hit([929, 177, 36, 36]),
                      actions.REDEEM_TITLE: modal and redeem and hit([580, 176, 118, 38], "兑换奖励"),
                      actions.HUD_DUCK: duck and hit([30, 18, 54, 54]),
                      actions.HUD_DUCK_LIT: duck and lit and hit([30, 18, 54, 54]),
                      actions.BACK_ARROW: arrow and hit([37, 22, 40, 40]),
                      actions.BACK_ARROW_LIT: arrow and lit and hit([37, 22, 40, 40]),
                      actions.PAGE_TITLE: arrow and title and hit([96, 26, 30 * len(title), 30], title)}
        self.labels, self.ocr, self.errors, self.calls = labels, ocr, set(errors), []

    def __call__(self, node, override=None):
        self.calls.append((node, override))
        if node in self.errors:
            return None
        if node in self.OCR_NODES and not self.ocr:
            return MISS
        if node != actions.HUD_LABEL:
            return self.fixed[node] or MISS
        if not self.fixed[actions.HUD_DUCK]:
            return MISS  # the label row only shows with the HUD
        x, y, w, h = (override or {}).get("roi", actions.LABEL_ROW)
        expected = override["expected"]
        inside = sorted((cx, label, width) for label, (cx, width) in self.labels.items()
                        if x <= cx - width / 2 and cx + width / 2 <= x + w)
        text = "".join(label for _cx, label, _w in inside)
        if not inside or not any(part in text for part in expected.split("|")):
            return MISS
        left = inside[0][0] - inside[0][2] / 2
        right = inside[-1][0] + inside[-1][2] / 2
        return hit([round(left), 64, round(right - left), 22], text)


class ScreenTests(unittest.TestCase):
    def test_screens(self):
        cases = {"quit_dialog": Scripted(quit=True, cancel=True, duck=True),  # the HUD shows behind the dialog
                 "modal": Scripted(modal=True, arrow=True),
                 "hud": Scripted(duck=True),
                 "page": Scripted(arrow=True)}
        for kind, recognize in cases.items():
            with self.subTest(kind=kind):
                self.assertEqual(actions.read_screen(recognize).kind, kind)

    def test_anything_uncertain_is_unknown(self):
        cases = {"quit title alone": Scripted(quit=True, duck=True),
                 "a 取消 alone": Scripted(cancel=True, arrow=True),
                 "quit check failed": Scripted(duck=True, errors={actions.QUIT_TITLE}),
                 "cancel check failed": Scripted(duck=True, errors={actions.QUIT_CANCEL}),
                 "modal check failed": Scripted(duck=True, errors={actions.MODAL_CLOSE}),
                 "duck check failed": Scripted(duck=True, errors={actions.HUD_DUCK}),
                 "arrow check failed": Scripted(duck=True, errors={actions.BACK_ARROW}),
                 "duck and arrow": Scripted(duck=True, arrow=True),
                 "HUD without readable labels": Scripted(duck=True, labels={}),
                 "page without a readable title": Scripted(arrow=True, title=""),
                 "page title not text": Scripted(arrow=True, title="L6"),
                 "a modal that is not identified": Scripted(modal=True, redeem=False, arrow=True),
                 "a dimmed HUD (an unrecognised dialog)": Scripted(duck=True, lit=False),
                 "a dimmed page (an unrecognised dialog)": Scripted(arrow=True, lit=False),
                 "nothing": Scripted()}
        for case, recognize in cases.items():
            with self.subTest(case=case):
                screen = actions.read_screen(recognize)
                self.assertEqual(screen.kind, "unknown")
                self.assertTrue(screen.reason)

    def test_without_ocr_the_quit_confirmation_is_never_the_hud(self):
        recognize = Scripted(quit=True, cancel=True, duck=True, ocr=False)
        screen = actions.read_screen(recognize)
        self.assertEqual(screen.kind, "unknown")  # a close mark, but no identified modal: nothing is tapped
        for action in sorted(actions.ACTIONS):
            with self.subTest(action=action), self.assertRaises(actions.ScreenUnrecognised):
                actions.plan_tap(action, screen, recognize)
        # Even with the dialog's texts and close mark hidden, the dimmed duck keeps it from being the HUD.
        masked = Scripted(duck=True, lit=False)
        self.assertEqual(actions.read_screen(masked).kind, "unknown")
        for screen in (Scripted(duck=True, ocr=False), Scripted(arrow=True, ocr=False)):
            self.assertEqual(actions.read_screen(screen).kind, "unknown")

    def test_brightness_is_checked_on_the_matched_icon(self):
        for kind, icon, lit, box in (("hud", "duck", actions.HUD_DUCK_LIT, [44, 30, 54, 54]),
                                     ("page", "arrow", actions.BACK_ARROW_LIT, [60, 40, 40, 40])):
            with self.subTest(kind=kind):
                recognize = Scripted(**{icon: True})
                recognize.fixed[actions.HUD_DUCK if icon == "duck" else actions.BACK_ARROW] = hit(box)
                self.assertEqual(actions.read_screen(recognize).kind, kind)
                self.assertIn((lit, {"roi": box}), recognize.calls)  # not a fixed rectangle

    def test_the_quit_guard_is_checked_before_anything_else(self):
        recognize = Scripted(quit=True, cancel=True, duck=True)
        actions.read_screen(recognize)
        self.assertEqual([node for node, _ in recognize.calls], [actions.QUIT_TITLE, actions.QUIT_CANCEL])


class PlanTests(unittest.TestCase):
    def plan(self, action, recognize):
        return actions.plan_tap(action, actions.read_screen(recognize), recognize)

    def test_the_quit_guard_cancels_whatever_was_asked(self):
        for action in sorted(actions.ACTIONS):
            with self.subTest(action=action):
                plan = self.plan(action, Scripted(quit=True, cancel=True, duck=True))
                self.assertEqual((plan.target, plan.key), ([459, 461, 72, 40], "cancel"))
                self.assertEqual(plan.completes, action == "leave")

    def test_menu_only_opens_and_leave_only_leaves(self):
        self.assertEqual(self.plan("menu", Scripted(duck=True)).target, [30, 18, 54, 54])
        for screen in (Scripted(arrow=True), Scripted(modal=True, arrow=True)):
            with self.assertRaises(actions.ScreenUnrecognised):
                self.plan("menu", screen)  # never "the menu key" on a page: that intent is leave
        self.assertEqual(self.plan("leave", Scripted(arrow=True)).target, [37, 22, 40, 40])
        self.assertEqual(self.plan("leave", Scripted(modal=True, arrow=True)).target, [929, 177, 36, 36])
        plan = self.plan("leave", Scripted(duck=True))
        self.assertEqual((plan.target, plan.completes), (None, True))  # already in the open world: no tap

    def test_hud_shortcuts_tap_the_icon_above_their_label(self):
        expected = {"shop": 657, "monthly": 798, "calendar": 939, "dress": 1222}
        for action, x in expected.items():
            with self.subTest(action=action):
                plan = self.plan(action, Scripted(duck=True))
                self.assertTrue(plan.completes)
                self.assertLessEqual(abs(plan.target[0] - x), 3)
                self.assertEqual(plan.target[1], 64 - actions.ICON_ABOVE_LABEL)
        self.assertEqual(self.plan("map", Scripted(duck=True)).target, actions.MINIMAP)

    def test_hud_actions_are_refused_off_the_hud(self):
        for action in ("menu", "map", "shop", "monthly", "calendar", "dress"):
            for screen in (Scripted(arrow=True), Scripted(modal=True, arrow=True)):
                with self.subTest(action=action), self.assertRaises(actions.ScreenUnrecognised):
                    self.plan(action, screen)

    def test_an_unknown_screen_gets_no_tap(self):
        for action in sorted(actions.ACTIONS):
            with self.subTest(action=action), self.assertRaises(actions.ScreenUnrecognised):
                self.plan(action, Scripted(duck=True, arrow=True))

    def test_a_shortcut_that_cannot_be_placed_is_refused(self):
        moved = dict(LABELS)
        del moved["商城"]
        with self.assertRaises(actions.ScreenUnrecognised):
            self.plan("shop", Scripted(duck=True, labels=moved))  # customised HUD: no fallback position

    def test_windows_that_disagree_or_a_failed_confirmation_refuse(self):
        recognize = Scripted(duck=True)
        original = recognize.__call__

        def drifting(node, override=None):
            result = original(node, override)
            if node == actions.HUD_LABEL and result.hit and override["roi"][0] == 620:
                return hit([700, 64, 36, 22], "商城")  # one window places the label 40 px away
            return result
        self.assertIsNone(actions.locate_shortcut(drifting, "商城"))
        checks = []

        def unconfirmed(node, override=None):
            result = original(node, override)
            if node == actions.HUD_LABEL and override["roi"][2] == 18 * 2 + 24:
                checks.append(override["roi"])
                return MISS  # the centred window does not read the label
            return result
        self.assertIsNone(actions.locate_shortcut(unconfirmed, "商城"))
        self.assertEqual(len(checks), 1)
        self.assertLessEqual(abs(checks[0][0] + checks[0][2] / 2 - 657), 3)

    def test_the_tap_is_confirmed_on_a_fresh_frame(self):
        decided = Scripted(duck=True)
        screen = actions.read_screen(decided)
        shop = actions.plan_tap("shop", screen, decided)
        self.assertEqual(actions.confirm_tap(shop, screen, Scripted(duck=True)), shop.target)
        self.assertIsNone(actions.confirm_tap(shop, screen, Scripted(arrow=True)))  # now a page
        moved = {**LABELS, "商城": (727, 36), "共鸣": (657, 36)}
        self.assertIsNone(actions.confirm_tap(shop, screen, Scripted(duck=True, labels=moved)))
        lower = {label: (cx, width) for label, (cx, width) in LABELS.items()}

        class Lowered(Scripted):
            def __call__(inner, node, override=None):  # the label strip 16 px lower: the label reads, elsewhere
                result = Scripted.__call__(inner, node, override)
                if node == actions.HUD_LABEL and result.hit:
                    return hit([result.box[0], result.box[1] + 16, result.box[2], result.box[3]],
                               result.best_result.text)
                return result
        self.assertIsNone(actions.confirm_tap(shop, screen, Lowered(duck=True, labels=lower)))
        page = Scripted(arrow=True)
        back = actions.plan_tap("leave", actions.read_screen(page), page)
        self.assertEqual(actions.confirm_tap(back, actions.read_screen(page), Scripted(arrow=True)), back.target)
        self.assertIsNone(actions.confirm_tap(back, actions.read_screen(page), Scripted(modal=True, arrow=True)))
        self.assertIsNone(actions.confirm_tap(back, actions.read_screen(page), Scripted(arrow=True, title="地图")))

    def test_an_unchanged_strip_confirms_even_when_ocr_reads_by_window_position(self):
        # cam360/c05 with the real OCR: the scan put 奇想日历 at x=943; the window centred there read it at 933, and a
        # window centred at 933 read it at 942 (9 px from the plan). Confirming must reuse the deciding window.
        def reading(window_centre):
            return round(933 - 0.9 * (window_centre - 943))

        class C05(Scripted):
            def __call__(inner, node, override=None):
                result = Scripted.__call__(inner, node, override)
                width = 18 * len("奇想日历") + 24
                if node == actions.HUD_LABEL and result.hit and override and "roi" in override \
                        and override["expected"] == "奇想日历" and override["roi"][2] == width:
                    centre = reading(override["roi"][0] + width / 2)
                    return hit([centre - 35, *result.box[1:3], result.box[3]], "奇想日历")
                return result
        labels = {**LABELS, "奇想日历": (943, 70)}
        decided = C05(duck=True, labels=labels)
        screen = actions.read_screen(decided)
        plan = actions.plan_tap("calendar", screen, decided)
        self.assertEqual(plan.target[0], 933)
        self.assertEqual(actions.confirm_tap(plan, screen, C05(duck=True, labels=labels)), plan.target)
        self.assertEqual(plan.window_x, 943)
        self.assertEqual(reading(933), 942)  # what re-centring on the target would have read: refused at 8 px

    def test_boxes_are_compared_where_they_would_be_tapped(self):
        quit = Scripted(quit=True, cancel=True, duck=True)
        screen = actions.read_screen(quit)
        plan = actions.plan_tap("leave", screen, quit)
        wider = Scripted(quit=True, cancel=True, duck=True)
        wider.fixed[actions.QUIT_CANCEL] = hit([467, 461, 98, 40], "取消")  # corner 8 px away, centre 21 px away
        self.assertIsNone(actions.confirm_tap(plan, screen, wider))
        nudged = Scripted(quit=True, cancel=True, duck=True)
        nudged.fixed[actions.QUIT_CANCEL] = hit([463, 463, 72, 40], "取消")
        self.assertEqual(actions.confirm_tap(plan, screen, nudged), [463, 463, 72, 40])

    def test_unknown_actions_are_not_planned(self):
        with self.assertRaises(ValueError):
            actions.plan_tap("jump", actions.read_screen(Scripted(duck=True)), Scripted(duck=True))


class RuntimeKeyTests(FreshAndroidState):
    def runtime(self, screens):
        import numpy as np
        from daily.runtime import Runtime
        rt = Runtime.__new__(Runtime)
        rt.context = SimpleNamespace(run_action=Mock(), tasker=SimpleNamespace(stopping=False, post_stop=Mock()))
        rt.context.run_action_direct = Mock(return_value=SimpleNamespace(success=True))
        rt.navigation_halted, rt._focus_generation = False, 0
        rt.check_focus, rt.invalidate_frame, rt.ensure_frame = Mock(), Mock(), Mock(return_value=True)
        rt._validate_pointer_targets = Mock(return_value=(1280, 720))
        rt._use_window_input = Mock(return_value=False)
        rt._evidence = SimpleNamespace(observe=Mock(), remember_frame=Mock())
        rt._active_inputs = Mock(return_value=None)
        rt.pause = Mock(return_value=True)
        rt.ui = SimpleNamespace(unhover=Mock(return_value=True))
        rt._device_target = SimpleNamespace(foreground=Mock(return_value=True), exists=Mock(return_value=True),
                                            serial="127.0.0.1:5555", identity=Mock(return_value=None),
                                            touch_snapshot=Mock(side_effect=advancing_snapshots()))
        screens = list(screens)
        self.frames = []

        def capture():
            frame = np.zeros((720, 1280, 3), np.uint8)
            self.state.mark(frame, rt._focus_generation)
            self.frames.append((frame, screens.pop(0)))
            return frame
        rt.capture = Mock(side_effect=capture)

        def run_recognition(node, image, override=None):
            for frame, screen in self.frames:
                if frame is image:
                    return screen(node, (override or {}).get(node))
            raise AssertionError("recognised on a frame that was not captured for this decision")
        rt.context.run_recognition = Mock(side_effect=run_recognition)
        return rt

    def sent(self, rt):
        return [(call.args[0], call.args[1].target) for call in rt.context.run_action_direct.call_args_list]

    def test_menu_on_the_hud_taps_the_duck_confirmed_on_a_fresh_frame(self):
        rt = self.runtime([Scripted(duck=True), Scripted(duck=True)])
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.key("menu"))
        self.assertEqual(self.sent(rt), [("Click", [56, 44])])
        self.assertLessEqual(rt.check_focus.call_count, 2)  # at dispatch only, not once per recognition

    def test_a_screen_that_changed_after_the_decision_is_not_tapped(self):
        rt = self.runtime([Scripted(duck=True), Scripted(arrow=True),   # HUD decided, a page by confirmation time
                           Scripted(arrow=True), Scripted(arrow=True)])
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                patch("daily.runtime.Runtime._dispatch_touch", wraps=rt._dispatch_touch) as dispatch:
            from navigation.models import NavigationError
            with self.assertRaises(NavigationError):
                rt.key("shop")  # the second decision sees a page, where 商城 means nothing
        rt.context.run_action_direct.assert_not_called()
        dispatch.assert_not_called()

    def test_the_tap_carries_the_confirming_frame(self):
        rt = self.runtime([Scripted(duck=True), Scripted(duck=True)])
        sources = []
        original = rt._dispatch_touch
        rt._dispatch_touch = lambda action, fields, generation, source: (sources.append(source),
                                                                           original(action, fields, generation,
                                                                                    source))[1]
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.key("calendar"))
        self.assertIs(sources[0], self.frames[1][0])

    def test_the_quit_guard_cancels_then_decides_again(self):
        rt = self.runtime([Scripted(quit=True, cancel=True, duck=True)] * 2 + [Scripted(duck=True)] * 2)
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.key("calendar"))
        sent = self.sent(rt)
        self.assertEqual(sent[0], ("Click", [494, 480]))  # 取消, never 确认
        self.assertLessEqual(abs(sent[1][1][0] - 939), 3)
        self.assertEqual(rt.capture.call_count, 4)

    def test_an_unknown_screen_sends_nothing_and_says_so(self):
        from navigation.models import NavigationError
        rt = self.runtime([Scripted(duck=True, arrow=True)])
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                self.assertRaises(NavigationError) as caught:
            rt.key("menu")
        self.assertIn("没有发送任何输入", str(caught.exception))
        rt.context.run_action_direct.assert_not_called()

    def test_a_guard_tap_already_sent_is_reported(self):
        from navigation.models import NavigationError
        rt = self.runtime([Scripted(quit=True, cancel=True, duck=True)] * 2 + [Scripted(duck=True, arrow=True)])
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                self.assertRaises(NavigationError) as caught:
            rt.key("map")
        self.assertIn("已点过 1 次退出确认框的「取消」", str(caught.exception))
        self.assertEqual(len(self.sent(rt)), 1)

    def test_a_focus_loss_while_confirming_means_deciding_again(self):
        rt = self.runtime([Scripted(duck=True)] * 4)
        original = rt.context.run_recognition.side_effect
        lost = []

        def recognize(node, image, override=None):
            if image is self.frames[-1][0] and len(self.frames) == 2 and not lost:
                lost.append(node)
                self.state.observed_loss()  # focus lost while the confirming frame is being recognised
            return original(node, image, override)
        rt.context.run_recognition.side_effect = recognize
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.key("menu"))
        self.assertEqual(rt.capture.call_count, 4)  # decide, confirm (stale), decide, confirm
        self.assertEqual(len(self.sent(rt)), 1)

    def test_a_focus_loss_while_deciding_voids_the_decision(self):
        for name, screens in (("shop", [Scripted(duck=True)] * 4), ("leave", [Scripted(duck=True)] * 2)):
            with self.subTest(name=name):
                rt = self.runtime(screens)
                original = rt.context.run_recognition.side_effect
                lost = []

                def recognize(node, image, override=None, rt=rt, original=original, lost=lost):
                    if len(self.frames) == 1 and not lost:
                        lost.append(node)
                        self.state.observed_loss()  # while the deciding frame is being recognised
                    return original(node, image, override)
                rt.context.run_recognition.side_effect = recognize
                with android_run(), patch("daily.runtime.active_session", return_value=None):
                    self.assertTrue(rt.leave_page() if name == "leave" else rt.key(name))
                # The first decision was discarded: a second frame was decided on (and, for shop, confirmed).
                self.assertEqual(rt.capture.call_count, 2 if name == "leave" else 3)
                self.assertEqual(len(self.sent(rt)), 0 if name == "leave" else 1)
                if name == "shop":
                    source = rt.context.run_action_direct.call_args
                    self.assertIsNotNone(source)

    def test_escape_is_esc_on_the_desktop_and_leave_on_android(self):
        from daily.runtime import Runtime
        rt = Runtime.__new__(Runtime)
        rt.key, rt.leave_page = Mock(return_value=True), Mock(return_value=True)
        for backend, controller in (("win32", "Win32"), ("macos", "MacOS")):
            with self.subTest(backend=backend), patch.object(host, "platform_backend", return_value=backend), \
                    patch.dict(os.environ, {"PI_CONTROLLER": json.dumps({"type": controller})}):
                self.assertTrue(rt.escape(.35))
                rt.key.assert_called_with("menu", .35)
        rt.leave_page.assert_not_called()
        with android_run():
            self.assertTrue(rt.escape(0))
        rt.leave_page.assert_called_once_with(0)
        self.assertEqual(rt.key.call_count, 2)

    def test_focus_lost_at_dispatch_is_decided_again_within_the_limit(self):
        rt = self.runtime([Scripted(duck=True)] * 4)
        rt._device_target.foreground = Mock(side_effect=[False, True, True, True])
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                patch("daily.runtime.wall_time.sleep"):
            self.assertTrue(rt.key("menu"))
        self.assertEqual(len(self.sent(rt)), 1)
        self.assertEqual(rt.capture.call_count, 4)

    def test_leave_page(self):
        rt = self.runtime([Scripted(arrow=True), Scripted(arrow=True)])
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.leave_page())
        self.assertEqual(self.sent(rt), [("Click", [56, 41])])
        rt = self.runtime([Scripted(duck=True)])
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.leave_page())  # already in the open world
        rt.context.run_action_direct.assert_not_called()
        self.assertEqual(rt.capture.call_count, 1)

    def test_other_named_actions_and_holds_are_refused(self):
        rt = self.runtime([])
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            for name in ("jump", "interact", "skip", "exit", "backpack", "Leave", "MENU"):
                with self.subTest(name=name), self.assertRaises(host.UnsupportedInput):
                    rt.key(name)
            with self.assertRaises(host.UnsupportedInput):
                rt.hold("forward", 1)
        rt.capture.assert_not_called()
        rt.context.run_action_direct.assert_not_called()

    def test_desktop_keys_do_not_take_the_touch_path(self):
        from daily.runtime import Runtime
        for backend, controller in (("win32", "Win32"), ("macos", "MacOS")):
            with self.subTest(backend=backend):
                rt = Runtime.__new__(Runtime)
                rt.navigation_halted = False
                rt.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
                rt.game_keys = SimpleNamespace(get=Mock(return_value=SimpleNamespace(kind="key", code=27)))
                rt.action, rt.pause, rt._touch_key = Mock(return_value=True), Mock(return_value=True), Mock()
                with patch.object(host, "platform_backend", return_value=backend), \
                        patch.dict(os.environ, {"PI_CONTROLLER": json.dumps({"type": controller})}), \
                        patch("daily.runtime.active_session", return_value=None):
                    self.assertTrue(rt.key("menu"))
                rt._touch_key.assert_not_called()
                rt.action.assert_called_once_with("ClickKey", key=27)


def android_layer_present(test) -> bool:
    """A git checkout (CI, development) must have resource_android/; the release source archive does not list it
    yet (plan risk 13), so there the layer's own checks have nothing to check."""
    if (ROOT / "resource_android").is_dir():
        return True
    test.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
    return False


class ResourceTests(unittest.TestCase):
    def test_every_node_is_defined_with_its_template(self):
        if not android_layer_present(self):
            return
        nodes = json.loads((ROOT / "resource_android" / "pipeline" / "android.json").read_text(encoding="utf-8"))
        self.assertEqual(set(nodes), set(actions.NODES))
        for name, node in nodes.items():
            with self.subTest(node=name):
                self.assertEqual(node["action"], "DoNothing")
                if node["recognition"] == "TemplateMatch":
                    self.assertTrue((ROOT / "resource_android" / "image" / node["template"]).is_file())
                    self.assertGreaterEqual(node["threshold"], 0.8)
        self.assertEqual(nodes[actions.HUD_LABEL]["roi"], list(actions.LABEL_ROW))
        # The corner icons are matched on their own pixels only: the background around them changes with the scene
        # (an unmasked duck scored 0.64 against dark trees on the device). Masked pixels are pure green.
        import cv2
        for name in (actions.HUD_DUCK, actions.BACK_ARROW):
            self.assertIs(nodes[name].get("green_mask"), True, name)
            image = cv2.imread(str(ROOT / "resource_android" / "image" / nodes[name]["template"]))
            corners = [image[0, 0], image[0, -1], image[-1, 0], image[-1, -1]]
            self.assertTrue(all(tuple(int(v) for v in c) == (0, 255, 0) for c in corners), name)


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
from host.android import actions

duck = cv2.imread(str(repo / "resource_android/image/android/menu_duck.png"))
arrow = cv2.imread(str(repo / "resource_android/image/android/back_arrow.png"))
fixtures = repo / "scripts/fixtures/android"
row = cv2.imread(str(fixtures / "hud_label_row.png"))        # the HUD label row, from a device frame
title = cv2.imread(str(fixtures / "page_title.png"))         # back arrow and 设置 title, from a device frame
dialog = cv2.imread(str(fixtures / "quit_dialog.png"))       # the 退出游戏 confirmation, from a device frame
frames = {}
for name, marks in (("blank", []), ("duck only", [(duck, 30, 18)]), ("hud", [(duck, 30, 18), (row, 600, 50)]),
                    ("page", [(title, 0, 10)]), ("arrow only", [(arrow, 37, 22)]),
                    ("quit over hud", [(duck, 30, 18), (row, 600, 50), (dialog, 300, 150)]),
                    ("duck and arrow", [(duck, 30, 18), (arrow, 100, 60), (row, 600, 50)]),
                    ("dimmed hud", [(duck, 30, 18), (row, 600, 50)])):
    frame = np.full((720, 1280, 3), 90, np.uint8)
    for image, x, y in marks:
        frame[y:y + image.shape[0], x:x + image.shape[1]] = image
    if name == "dimmed hud":  # what a dialog's dim layer does to the HUD behind it (texts still readable)
        frame = (frame * 0.45).astype(np.uint8)
    frames[name] = frame
current = {}

class Frame(CustomController):
    def connect(self): return True
    def request_uuid(self): return "android-actions-test"
    def screencap(self): return current["frame"]
    def refuse(self, *args): return False
    start_app = stop_app = click = swipe = touch_down = touch_move = touch_up = refuse
    click_key = input_text = key_down = key_up = refuse

results = {}

class Classify(CustomAction):
    def run(self, context, argv):
        image = context.tasker.controller.post_screencap().wait().get()
        def recognize(node, override=None):
            return context.run_recognition(node, image, {node: override} if override else None)
        screen = actions.read_screen(recognize)
        nodes = {node: recognize(node) is not None for node in actions.NODES}
        plans = {}
        for name in sorted(actions.ACTIONS):
            try:
                plan = actions.plan_tap(name, screen, recognize)
                plans[name] = plan.target
            except actions.ScreenUnrecognised:
                plans[name] = "refused"
        results[current["name"]] = {"kind": screen.kind, "targets": screen.targets, "ran": nodes, "plans": plans,
                                    "reason": screen.reason}
        return CustomAction.RunResult(success=True)

resource = Resource()
for bundle in (repo / "resource", repo / "resource_android"):
    assert resource.post_bundle(bundle).wait().succeeded, bundle
action = Classify()
assert resource.register_custom_action("classify", action)
controller = Frame()
assert controller.post_connection().wait().succeeded
tasker = Tasker()
assert tasker.bind(resource, controller)
entry = {"Classify": {"recognition": "DirectHit", "action": "Custom", "custom_action": "classify", "next": []}}
for name, frame in frames.items():
    current.update(name=name, frame=frame)
    tasker.post_task("Classify", entry).wait()
print(json.dumps(results))
"""


@unittest.skipUnless((ROOT / "maafw").exists(), "needs the MaaFramework runtime in ./maafw")
class FrameworkTests(unittest.TestCase):
    """The real nodes, templates and OCR, both layers loaded, on frames composed from UID-free crops of device frames
    (in a fresh interpreter, see test_resource_layers.py)."""

    def test_the_nodes_load_run_and_tell_the_hud_from_a_page(self):
        import subprocess
        if not android_layer_present(self):
            return
        env = {**os.environ, "MAAFW_BINARY_PATH": str(ROOT / "maafw")}
        result = subprocess.run([sys.executable, "-I", "-B", "-c", FRAMEWORK_CHECK, str(ROOT)], capture_output=True,
                                text=True, timeout=300, env=env, cwd=tempfile.gettempdir())
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual({name: value["kind"] for name, value in outcome.items()},
                         {"blank": "unknown", "duck only": "unknown", "hud": "hud", "page": "page",
                          "arrow only": "unknown", "quit over hud": "quit_dialog", "duck and arrow": "unknown",
                          "dimmed hud": "unknown"})
        self.assertIn("压暗", outcome["dimmed hud"]["reason"])  # refused for the dim layer, not for unreadable text
        hud, page, quit = outcome["hud"]["plans"], outcome["page"]["plans"], outcome["quit over hud"]["plans"]
        self.assertEqual(hud["menu"], [30, 18, 54, 54])
        self.assertIsNone(hud["leave"])
        for action, x in {"shop": 657, "monthly": 798, "calendar": 939, "dress": 1222}.items():
            self.assertLessEqual(abs(hud[action][0] - x), 12, (action, hud[action]))
        self.assertEqual(page["leave"], [37, 22, 40, 40])
        self.assertEqual({page[a] for a in ("menu", "map", "shop")}, {"refused"})
        cancel = outcome["quit over hud"]["targets"]["cancel"]
        self.assertTrue(cancel[0] <= 496 <= cancel[0] + cancel[2] and cancel[1] <= 483 <= cancel[1] + cancel[3])
        self.assertEqual({tuple(target) for target in quit.values()}, {tuple(cancel)})  # whatever was asked
        for name, value in outcome.items():
            self.assertTrue(all(value["ran"].values()), (name, value["ran"]))  # every node ran (OCR included)


if __name__ == "__main__":
    unittest.main()
