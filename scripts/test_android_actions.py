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
from test_android_backend import FreshAndroidState, android_run  # noqa: E402


def hit(box, text=""):
    return SimpleNamespace(hit=True, box=box, best_result=SimpleNamespace(text=text))


MISS = SimpleNamespace(hit=False, box=None, best_result=None)

# The label row of the observed HUD: label -> (centre x, width), text top at y=64.
LABELS = {"商城": (657, 36), "共鸣": (727, 36), "奇迹之旅": (798, 70), "活动大厅": (868, 70), "奇想日历": (939, 70),
          "无限之心": (1010, 70), "任务": (1081, 36), "设计图": (1152, 54), "换装": (1222, 36)}


class Scripted:
    """A recogniser over a described screen: fixed nodes hit or miss; the label OCR reads every label that lies
    wholly inside its region (as one merged box, like the real OCR)."""

    def __init__(self, *, quit=False, cancel=False, redeem=False, duck=False, arrow=False, labels=LABELS,
                 errors=()):
        self.fixed = {actions.QUIT_TITLE: quit and hit([560, 180, 160, 40], "退出游戏"),
                      actions.QUIT_CANCEL: cancel and hit([466, 463, 60, 40], "取消"),
                      actions.REDEEM_TITLE: redeem and hit([580, 176, 118, 38], "兑换奖励"),
                      actions.HUD_DUCK: duck and hit([30, 18, 54, 54]),
                      actions.BACK_ARROW: arrow and hit([37, 22, 40, 40])}
        self.labels, self.errors, self.calls = labels, set(errors), []

    def __call__(self, node, override=None):
        self.calls.append((node, override))
        if node in self.errors:
            return None
        if node != actions.HUD_LABEL:
            return self.fixed[node] or MISS
        x, y, w, h = override["roi"]
        inside = sorted((cx, label, width) for label, (cx, width) in self.labels.items()
                        if x <= cx - width / 2 and cx + width / 2 <= x + w)
        text = "".join(label for _cx, label, _w in inside)
        if not inside or override["expected"] not in text:
            return MISS
        left = inside[0][0] - inside[0][2] / 2
        right = inside[-1][0] + inside[-1][2] / 2
        return hit([round(left), 64, round(right - left), 22], text)


class ScreenTests(unittest.TestCase):
    def test_screens(self):
        cases = {"quit_dialog": Scripted(quit=True, cancel=True, duck=True),  # the HUD shows behind the dialog
                 "redeem_modal": Scripted(redeem=True, arrow=True),
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
                 "duck and arrow": Scripted(duck=True, arrow=True),
                 "nothing": Scripted()}
        for case, recognize in cases.items():
            with self.subTest(case=case):
                screen = actions.read_screen(recognize)
                self.assertEqual(screen.kind, "unknown")
                self.assertTrue(screen.reason)

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
                self.assertEqual(self.plan(action, Scripted(quit=True, cancel=True, duck=True)),
                                 ([466, 463, 60, 40], False))

    def test_menu_keeps_the_esc_meaning(self):
        self.assertEqual(self.plan("menu", Scripted(duck=True)), ([30, 18, 54, 54], True))
        self.assertEqual(self.plan("menu", Scripted(arrow=True)), ([37, 22, 40, 40], True))
        self.assertEqual(self.plan("menu", Scripted(redeem=True, arrow=True)), (actions.REDEEM_CLOSE, True))

    def test_hud_shortcuts_tap_the_icon_above_their_label(self):
        expected = {"shop": 657, "monthly": 798, "calendar": 939, "dress": 1222}
        for action, x in expected.items():
            with self.subTest(action=action):
                target, completes = self.plan(action, Scripted(duck=True))
                self.assertTrue(completes)
                self.assertLessEqual(abs(target[0] - x), 3)
                self.assertEqual(target[1], 64 - actions.ICON_ABOVE_LABEL)
        self.assertEqual(self.plan("map", Scripted(duck=True)), (actions.MINIMAP, True))

    def test_hud_actions_are_refused_off_the_hud(self):
        for action in ("map", "shop", "monthly", "calendar", "dress"):
            for screen in (Scripted(arrow=True), Scripted(redeem=True, arrow=True)):
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
        rt._device_target = SimpleNamespace(foreground=Mock(return_value=True), exists=Mock(return_value=True))
        screens = list(screens)
        self.frames = []

        def capture():
            frame = np.zeros((720, 1280, 3), np.uint8)
            self.state.mark(frame, rt._focus_generation)
            self.frames.append((frame, screens.pop(0)))
            return frame
        rt.capture = Mock(side_effect=capture)

        def recognize(node, fields=None, image=None):
            for frame, screen in self.frames:
                if frame is image:
                    return screen(node, fields)
            raise AssertionError("recognised on a frame that was not captured for this decision")
        rt.recognize = Mock(side_effect=recognize)
        return rt

    def sent(self, rt):
        return [(call.args[0], call.args[1].target) for call in rt.context.run_action_direct.call_args_list]

    def test_menu_on_the_hud_taps_the_duck_on_the_decision_frame(self):
        rt = self.runtime([Scripted(duck=True)])
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.key("menu"))
        self.assertEqual(self.sent(rt), [("Click", [56, 44])])

    def test_the_quit_guard_cancels_then_decides_again(self):
        rt = self.runtime([Scripted(quit=True, cancel=True, duck=True), Scripted(duck=True)])
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.key("calendar"))
        sent = self.sent(rt)
        self.assertEqual(sent[0], ("Click", [495, 482]))  # 取消, never 确认
        self.assertEqual(sent[1][0], "Click")
        self.assertLessEqual(abs(sent[1][1][0] - 939), 3)
        self.assertEqual(rt.capture.call_count, 2)

    def test_an_unknown_screen_sends_nothing_and_stops_with_the_reason(self):
        from navigation.models import NavigationError
        rt = self.runtime([Scripted(duck=True, arrow=True)])
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                self.assertRaises(NavigationError) as caught:
            rt.key("menu")
        self.assertIn("没有发送任何输入", str(caught.exception))
        rt.context.run_action_direct.assert_not_called()

    def test_a_frame_made_stale_while_deciding_is_decided_again(self):
        rt = self.runtime([Scripted(duck=True), Scripted(duck=True)])
        original = rt.recognize.side_effect
        seen = []

        def recognize(node, fields=None, image=None):
            if not seen:
                self.state.observed_loss()  # focus lost while the first frame is being recognised
            seen.append(node)
            return original(node, fields, image)
        rt.recognize.side_effect = recognize
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.key("menu"))
        self.assertEqual(rt.capture.call_count, 2)
        self.assertEqual(len(self.sent(rt)), 1)

    def test_other_named_actions_and_holds_are_refused(self):
        rt = self.runtime([])
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            for name in ("jump", "interact", "skip", "exit", "backpack"):
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


class ResourceTests(unittest.TestCase):
    def test_every_node_is_defined_with_its_template(self):
        nodes = json.loads((ROOT / "resource_android" / "pipeline" / "android.json").read_text(encoding="utf-8"))
        self.assertEqual(set(nodes), set(actions.NODES))
        for name, node in nodes.items():
            with self.subTest(node=name):
                self.assertEqual(node["action"], "DoNothing")
                if node["recognition"] == "TemplateMatch":
                    self.assertTrue((ROOT / "resource_android" / "image" / node["template"]).is_file())
                    self.assertGreaterEqual(node["threshold"], 0.8)
        self.assertEqual(nodes[actions.HUD_LABEL]["roi"], list(actions.LABEL_ROW))


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
frames = {}
for name, marks in (("blank", []), ("duck", [(duck, 30, 18)]), ("arrow", [(arrow, 37, 22)]),
                    ("both", [(duck, 30, 18), (arrow, 100, 60)])):
    frame = np.full((720, 1280, 3), 90, np.uint8)
    for image, x, y in marks:
        frame[y:y + image.shape[0], x:x + image.shape[1]] = image
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
        results[current["name"]] = {"kind": screen.kind, "targets": screen.targets, "ran": nodes}
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
    """The real nodes and templates, both layers loaded, on synthetic frames (in a fresh interpreter, see
    test_resource_layers.py)."""

    def test_the_nodes_load_run_and_tell_the_hud_from_a_page(self):
        import subprocess
        env = {**os.environ, "MAAFW_BINARY_PATH": str(ROOT / "maafw")}
        result = subprocess.run([sys.executable, "-I", "-B", "-c", FRAMEWORK_CHECK, str(ROOT)], capture_output=True,
                                text=True, timeout=300, env=env, cwd=tempfile.gettempdir())
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual({name: value["kind"] for name, value in outcome.items()},
                         {"blank": "unknown", "duck": "hud", "arrow": "page", "both": "unknown"})
        self.assertEqual(outcome["duck"]["targets"], {"duck": [30, 18, 54, 54]})
        self.assertEqual(outcome["arrow"]["targets"], {"back": [37, 22, 40, 40]})
        for name, value in outcome.items():
            self.assertTrue(all(value["ran"].values()), (name, value["ran"]))  # every node ran (OCR included)


if __name__ == "__main__":
    unittest.main()
