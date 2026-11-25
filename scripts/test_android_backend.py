"""Android emulator backend (host.android) with fakes: python/python.exe -B scripts/test_android_backend.py.

Runs on any OS: the controller's ADB shell is scripted, no emulator or adb is needed. The desktop backend is
pinned to macOS, the only host that selects the Android backend for now.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))

import host  # noqa: E402
from host import android  # noqa: E402
from host.android.target import AndroidTarget, NoInput, check_controller_info, focused_package, process_state  # noqa: E402
from navigation.models import NavigationError  # noqa: E402

ADB_CONTROLLER = {"name": "nikki_android", "type": "Adb", "display_short_side": 720}
INFO = {"type": "adb", "adb_serial": "127.0.0.1:5555", "adb_path": "/opt/homebrew/bin/adb",
        "input_methods": 4, "screencap_methods": 8}
FOCUS_GAME = "  mCurrentFocus=Window{fe7cc5b u0 com.papegames.infinitynikki/com.epicgames.unreal.GameActivity}\n"
FOCUS_LAUNCHER = "  mCurrentFocus=Window{1 u0 com.bluestacks.launcher/com.android.launcher3.Launcher}\n"


@contextmanager
def android_run():
    env = {k: v for k, v in os.environ.items() if k not in ("PI_CONTROLLER", "MAANIKKI_MACOS_EXTRA_TASKS")}
    env["PI_CONTROLLER"] = json.dumps(ADB_CONTROLLER)
    with patch.dict(os.environ, env, clear=True), patch.object(host, "platform_backend", return_value="macos"):
        yield


class FakeShellController:
    def __init__(self, outputs):
        self.outputs, self.commands = outputs, []

    def post_shell(self, command, timeout):
        self.commands.append(command)
        output = next((value for key, value in self.outputs.items() if key in command), None)
        return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=output is not None, get=lambda: output))


class TaskListTests(unittest.TestCase):
    def test_the_shipped_list_is_well_formed_and_holds_only_the_read_only_check(self):
        verified, experimental, excluded = android.task_tiers()
        self.assertEqual(verified, frozenset())
        self.assertEqual(experimental, frozenset({"MaaNikki_AndroidCheck"}))
        self.assertEqual(excluded, {})

    def test_admission_notices_and_refusals_follow_the_android_list(self):
        with android_run():
            self.assertEqual(host.game_backend(), "android")
            self.assertTrue(host.task_entry_supported("MaaNikki_AndroidCheck"))
            for entry in ("MaaNikki_Mailbox", "MaaNikki_Fishing", None, ""):
                self.assertFalse(host.task_entry_supported(entry))
            self.assertEqual(host.task_refusal_reason("MaaNikki_Mailbox"), "安卓模拟器暂不开放这个任务，已跳过。")
            self.assertEqual(host.task_start_notice("MaaNikki_AndroidCheck"),
                             "本任务尚未在安卓模拟器上验证（试用）：只截图和识别，不向游戏发送任何输入。")
            self.assertIsNone(host.task_start_notice("MaaNikki_Mailbox"))

    def test_an_unreadable_list_runs_nothing(self):
        with android_run(), patch.object(android, "_task_list", side_effect=OSError("missing")):
            self.assertFalse(host.task_entry_supported("MaaNikki_AndroidCheck"))
            self.assertEqual(host.task_refusal_reason("MaaNikki_AndroidCheck"), "无法读取安卓任务清单，已跳过。")
            self.assertIsNone(host.task_start_notice("MaaNikki_AndroidCheck"))

    def test_the_mac_development_switch_does_not_open_android_tasks(self):
        with android_run(), patch.dict(os.environ, {"MAANIKKI_MACOS_EXTRA_TASKS": "MaaNikki_Mailbox"}):
            self.assertFalse(host.task_entry_supported("MaaNikki_Mailbox"))

    def test_the_admission_guard_refuses_unlisted_tasks_before_task_code(self):
        import registration
        ran = []
        token = registration._LOCAL_REGISTRATION.set(True)
        try:
            @registration.agent_action("test.android_backend.admission")
            class Probe:
                def run(self, context, argv):
                    ran.append(argv.task_detail.entry)
                    return True
        finally:
            registration._LOCAL_REGISTRATION.reset(token)
        notices = []
        context = Mock()
        context.run_task.side_effect = lambda entry, override: notices.append(
            override["MaaNikki_Daily_Message"]["focus"]["Node.Action.Starting"])
        with android_run(), patch("registration.task_session", return_value=nullcontext(None)), \
                patch.object(registration, "_START_NOTICES", registration.deque(maxlen=256)):
            for entry in ("MaaNikki_Mailbox", "MaaNikki_AndroidCheck"):
                argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=21, entry=entry), node_name=entry)
                Probe().run(context, argv)
        self.assertEqual(ran, ["MaaNikki_AndroidCheck"])
        self.assertIn("不在当前系统开放", notices[0])
        self.assertTrue(notices[1].startswith("本任务尚未在安卓模拟器上验证（试用）"))


class CapabilityTests(unittest.TestCase):
    def test_no_desktop_window_services(self):
        with android_run():
            caps = host.capabilities(host.game_backend())
            self.assertEqual(caps.backend, "android")
            enabled = {name for name in caps.__dataclass_fields__ if name != "backend" and getattr(caps, name)}
            self.assertEqual(enabled, {"capture_needs_device_focus", "touch_actions_only"})
            self.assertIsNone(host.overlay_backend())
            self.assertIsNone(host.launch_window_probes())
            with self.assertRaises(RuntimeError):
                host.game_close_api()
            with self.assertRaises(NavigationError):
                host.background_input_class()
            with self.assertRaises(host.UnsupportedInput):
                host.native_key(77)  # no keyboard on an emulator


class TargetTests(unittest.TestCase):
    def test_focus_parsing_counts_only_the_current_focus(self):
        self.assertEqual(focused_package(FOCUS_GAME), "com.papegames.infinitynikki")
        self.assertEqual(focused_package(FOCUS_LAUNCHER), "com.bluestacks.launcher")
        self.assertIsNone(focused_package("  mCurrentFocus=null\n"))
        self.assertIsNone(focused_package(None))
        self.assertIsNone(focused_package(FOCUS_GAME + FOCUS_LAUNCHER))  # ambiguous

    def test_only_an_adb_connection_is_accepted(self):
        self.assertEqual(check_controller_info(INFO), "127.0.0.1:5555")
        for info in ({"type": "win32", "hwnd": 1}, {"type": "adb"}, {"type": "adb", "adb_serial": ""}, None, []):
            with self.subTest(info=info), self.assertRaises(NavigationError):
                check_controller_info(info)

    def test_process_state_needs_a_confirmed_answer(self):
        self.assertTrue(process_state("4242\n__exit=0\n"))
        self.assertTrue(process_state("4242 4243\n__exit=0\n"))
        self.assertFalse(process_state("__exit=1\n"))
        for output in (None, "", "__exit=0\n", "mCurrentFocus=x\n__exit=0\n", "4242\n", "4242\n__exit=1\n"):
            self.assertIsNone(process_state(output), output)

    def test_connect_target_reads_the_device(self):
        controller = FakeShellController({"mCurrentFocus": FOCUS_GAME, "pidof": "4242\n__exit=0\n"})
        controller.info = INFO
        with android_run():
            target = host.connect_target(controller)
        self.assertIsInstance(target, AndroidTarget)
        self.assertTrue(target.exists())
        self.assertTrue(target.foreground())
        self.assertFalse(target.minimized())
        self.assertFalse(target.hung())
        self.assertFalse(target.bring_to_foreground(controller))
        self.assertEqual(target.focus_details()["focused_package"], "com.papegames.infinitynikki")
        self.assertTrue(target.capture_requires_foreground)  # another Android app in front is not the game
        self.assertTrue(target.release_ignores_focus)

    def test_a_dialog_or_launcher_in_front_is_not_the_game(self):
        for outputs in ({"mCurrentFocus": FOCUS_LAUNCHER, "pidof": "4242\n__exit=0\n"},
                        {"mCurrentFocus": "  mCurrentFocus=null\n", "pidof": "4242\n__exit=0\n"},
                        {}):  # shell failed
            target = AndroidTarget(INFO, FakeShellController(outputs))
            self.assertFalse(target.foreground(), outputs)
        self.assertFalse(AndroidTarget(INFO, FakeShellController({"pidof": "__exit=1\n"})).exists())
        self.assertTrue(AndroidTarget(INFO, FakeShellController({})).exists())  # unreadable is not "closed"

    def test_shell_requests_are_serialised_through_reading_the_result(self):
        import threading
        order, gate = [], threading.Event()

        class SharedOutputController:
            """Like MaaFramework: one shell output per controller, replaced by every request."""
            output = None

            def post_shell(self, command, timeout):
                order.append(("post", command))
                SharedOutputController.output = command

                def wait():
                    gate.wait(1)
                    return SimpleNamespace(succeeded=True, get=lambda: SharedOutputController.output)
                return SimpleNamespace(wait=wait)
        target = AndroidTarget(INFO, SharedOutputController())
        results = {}
        threads = [threading.Thread(target=lambda c=c: results.setdefault(c, target._shell(c))) for c in ("a", "b")]
        for thread in threads:
            thread.start()
        gate.set()
        for thread in threads:
            thread.join(2)
        self.assertEqual(results, {"a": "a", "b": "b"})

    def test_a_shell_result_that_raises_is_unreadable(self):
        class Raising:
            def post_shell(self, command, timeout):
                return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=True, get=Mock(side_effect=RuntimeError)))
        self.assertIsNone(AndroidTarget(INFO, Raising())._shell("x"))

    def test_input_primitives_send_nothing_and_there_is_no_window_geometry(self):
        target = AndroidTarget(INFO, FakeShellController({}))
        native = target.input()
        self.assertIsInstance(native, NoInput)
        self.assertIsNone(native.force_key_up(87))
        self.assertIsNone(native.force_mouse_up(1))
        self.assertFalse(native.supports_scene_buttons)
        with self.assertRaises(host.UnsupportedInput):
            native.press_mouse(1)
        self.assertFalse(native.post_window_key(87, down=True))
        with self.assertRaises(host.UnsupportedInput):
            native.relative_move(5, 5)
        with self.assertRaises(NavigationError):
            target.geometry()


class SessionTests(unittest.TestCase):
    """The real task session with an Android target: waits for device focus and sends no key on its own."""

    def test_first_wait_with_the_launcher_in_front_sends_nothing_and_pauses_until_the_game_has_focus(self):
        from navigation.controller import ForegroundInput
        from task_control import task_session, _SESSIONS
        state = SimpleNamespace(now=0.0, focus=FOCUS_LAUNCHER)

        class Device(FakeShellController):
            def post_shell(self, command, timeout):
                self.outputs["mCurrentFocus"] = state.focus
                return super().post_shell(command, timeout)
        controller = Device({"pidof": "4242\n__exit=0\n"})
        controller.info = INFO
        controller.post_key_up = Mock()
        controller.post_key_down = Mock()
        controller.post_touch_up = Mock()
        controller.post_click_key = Mock()
        context = SimpleNamespace(tasker=SimpleNamespace(controller=controller, stopping=False), run_task=Mock(),
                                  run_recognition=Mock(), get_node_data=Mock(return_value={}))

        def sleep(seconds):
            state.now += max(seconds, 1e-9)
            if state.now >= 1:
                state.focus = FOCUS_GAME  # the player switches back to the game on the device
        _SESSIONS.clear()
        try:
            with android_run(), task_session(context, 31) as session, \
                    patch("task_control.wall_time.monotonic", side_effect=lambda: state.now), \
                    patch("task_control.wall_time.sleep", side_effect=sleep):
                inputs = ForegroundInput(controller, lambda: context.tasker.stopping)
                session.adapter = inputs
                inputs.check()  # first wait: requires the game in front
                self.assertGreaterEqual(state.now, 1)
                self.assertTrue(inputs.capturable())
        finally:
            _SESSIONS.clear()
        for method in (controller.post_key_up, controller.post_key_down, controller.post_touch_up,
                       controller.post_click_key):
            method.assert_not_called()
        self.assertTrue(all(c.startswith(("dumpsys window", "pidof")) for c in controller.commands))


class NoKeyboardOrMouseTests(unittest.TestCase):
    """No key code, mouse button or relative move reaches the device from an Android run."""

    def test_every_key_is_refused(self):
        from daily.runtime import native_key_fields
        with android_run():
            for key in (27, 18, 87, 999):
                with self.assertRaises(host.UnsupportedInput):
                    host.native_key(key)
            with self.assertRaises(host.UnsupportedInput):
                native_key_fields("ClickKey", {"key": 27})
            self.assertEqual(native_key_fields("Click", {"target": [1, 2, 3, 4]}), {"target": [1, 2, 3, 4]})

    def runtime(self):
        from daily.runtime import Runtime
        rt = Runtime.__new__(Runtime)
        rt.context = SimpleNamespace(run_action=Mock(), tasker=SimpleNamespace(stopping=False))
        rt.check_focus, rt.ensure_frame, rt.invalidate_frame = Mock(), Mock(return_value=True), Mock()
        rt._evidence = SimpleNamespace(observe=Mock(), remember_frame=Mock())
        rt.navigation_halted, rt._focus_generation = False, 0
        rt._active_inputs = Mock(return_value=None)
        rt._use_window_input = Mock(return_value=False)
        return rt

    def test_integer_key_actions_are_refused_before_dispatch(self):
        rt = self.runtime()
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            for action, fields in (("ClickKey", {"key": 27}), ("KeyDown", {"key": 87}), ("LongPressKey", {"key": 70})):
                with self.subTest(action=action), self.assertRaises(host.UnsupportedInput):
                    rt.action(action, **fields)
        rt.context.run_action.assert_not_called()

    def test_only_taps_and_swipes_are_dispatched_in_any_spelling(self):
        rt = self.runtime()
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            for action in ("Key", "clickkey", "KeyUp", "TouchDown", "TouchMove", "TouchUp", "Scroll", "InputText",
                           "StartApp", "click", "swipe"):
                with self.subTest(action=action), self.assertRaises(host.UnsupportedInput):
                    rt.action(action, key=27, target=[11, 21, 4, 4], contact=1)
        rt.context.run_action.assert_not_called()

    def tap_runtime(self, *, focused=True, success=True):
        rt = self.runtime()
        rt.context.run_action.return_value = SimpleNamespace(success=success)
        rt._target_point = lambda target, name: (target[0] + target[2] // 2, target[1] + target[3] // 2)
        rt._validate_pointer_targets = Mock(return_value=(1280, 720))
        rt.pause = Mock(return_value=True)
        rt.ui = SimpleNamespace(unhover=Mock(return_value=True))  # without pointer hover it returns at once
        rt._device_target = SimpleNamespace(foreground=Mock(return_value=focused), exists=Mock(return_value=True))
        return rt

    def test_a_tap_is_dispatched_with_device_focus_confirmed_right_before(self):
        rt = self.tap_runtime()
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.action("Click", target=[100, 200, 40, 20]))
        override = rt.context.run_action.call_args.kwargs["pipeline_override"]["MaaNikki_Daily_Action"]
        self.assertEqual(override, {"action": "Click", "target": (120, 210)})
        rt._device_target.foreground.assert_called()

    def test_a_tap_without_device_focus_or_on_an_old_frame_is_cancelled(self):
        from task_control import ForegroundChanged
        rt = self.tap_runtime(focused=False)
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                self.assertRaises(ForegroundChanged):
            rt.action("Click", target=[100, 200, 40, 20])
        rt = self.tap_runtime()
        rt._focus_generation = 8  # the decision was made on generation 7
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                self.assertRaises(ForegroundChanged):
            rt.action("Click", target=[100, 200, 40, 20], focus_generation=7)
        rt.context.run_action.assert_not_called()

    def test_an_unconfirmed_tap_halts_the_run(self):
        rt = self.tap_runtime(success=False)
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            with self.assertRaises(host.InputUnconfirmed):
                rt.action("Swipe", begin=[100, 100], end=[300, 100], duration=300)
            self.assertTrue(rt.navigation_halted)
            self.assertFalse(rt.action("Click", target=[10, 10, 4, 4]))  # stopped: nothing more is sent
        self.assertEqual(rt.context.run_action.call_count, 1)

    def test_only_plain_targets_inside_the_frame(self):
        rt = self.tap_runtime()
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            for action, fields in (("Click", {"target": [50, 50, 4, 4], "target_offset": [200, 100, 0, 0]}),
                                   ("Swipe", {"begin": [1, 1], "end": [5, 5], "only_hover": True}),
                                   ("Click", {"target": [50, 50], "contact": 1}),
                                   ("Click", {"target": [1270, 700, 40, 40]}),
                                   ("Click", {"target": [-1, 5]}),
                                   ("Click", {}),
                                   ("Swipe", {"begin": [1, 1], "end": [5, 5], "duration": -1})):
                with self.subTest(action=action, fields=fields), self.assertRaises(host.UnsupportedInput):
                    rt.action(action, **fields)
        rt.context.run_action.assert_not_called()

    def test_child_tasks_do_not_run(self):
        rt = self.runtime()
        rt.context.clone = Mock()
        with android_run():
            for override in (None, {}, {"X": {"next": []}}, {"X": {"action": "ClickKey", "key": 87}}):
                with self.subTest(override=override), self.assertRaises(host.UnsupportedInput):
                    rt.run("X", override)
        rt.context.clone.assert_not_called()

    def test_shipped_pipelines_use_only_actions_a_child_task_may_run(self):
        import glob
        kinds = set()

        def walk(value):
            if isinstance(value, dict):
                action = value.get("action")
                kind = action.get("type") if isinstance(action, dict) else action
                if isinstance(kind, str):
                    kinds.add(kind)
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        for path in glob.glob(str(ROOT / "resource*/pipeline/*.json")) + glob.glob(str(ROOT / "tasks/*.json")):
            walk(json.loads(Path(path).read_text(encoding="utf-8")))
        self.assertTrue(kinds)
        self.assertLessEqual(kinds, {"DoNothing", "Custom"})

    def test_text_input_and_camera_moves_are_refused_before_posting(self):
        from navigation.controller import relative_camera
        rt = self.runtime()
        rt.controller = SimpleNamespace(post_input_text=Mock())
        controller = SimpleNamespace(post_relative_move=Mock())
        with android_run():
            with self.assertRaises(host.UnsupportedInput):
                rt.input_text("CODE")
            with self.assertRaises(host.UnsupportedInput):
                relative_camera(controller, 20, 0)
        rt.controller.post_input_text.assert_not_called()
        controller.post_relative_move.assert_not_called()

    def foreground_input(self):
        from navigation.controller import ForegroundInput
        controller = FakeShellController({"mCurrentFocus": FOCUS_GAME, "pidof": "4242\n__exit=0\n"})
        controller.info = INFO
        controller.post_key_down, controller.post_key_up = Mock(), Mock()
        controller.post_touch_down, controller.post_touch_up = Mock(), Mock()
        with android_run():
            inputs = ForegroundInput(controller, lambda: False)
        return inputs, controller

    def test_named_keys_and_mouse_buttons_are_refused_and_nothing_is_released(self):
        inputs, controller = self.foreground_input()
        with android_run():
            with self.assertRaises(host.UnsupportedInput):
                inputs.hold("forward", .1)
            with self.assertRaises(host.UnsupportedInput):
                inputs.key_down("jump")
            with self.assertRaises(host.UnsupportedInput):
                inputs.click_mouse(1)
            inputs.release()
        self.assertEqual((inputs.held, inputs.buttons), (set(), set()))
        for method in (controller.post_key_down, controller.post_key_up, controller.post_touch_down,
                       controller.post_touch_up):
            method.assert_not_called()

    def test_relative_camera_moves_are_refused(self):
        inputs, _ = self.foreground_input()
        with self.assertRaises(host.UnsupportedInput):
            inputs.native.relative_move(5, 0)


class DeviceFocusCaptureTests(unittest.TestCase):
    """Runtime keeps a frame only when the game had focus on the device right before and right after it."""

    def runtime(self, focus):
        import numpy as np
        from daily.runtime import Runtime
        rt = Runtime.__new__(Runtime)
        frames = iter([np.full((720, 1280, 3), value, np.uint8) for value in (1, 2, 3, 4)])
        rt.controller = SimpleNamespace(post_screencap=Mock(side_effect=lambda: SimpleNamespace(
            wait=lambda: None, succeeded=True, get=lambda frame=next(frames): frame)))
        rt.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        rt.navigation_halted, rt._focus_generation = False, 0
        rt.check_focus, rt.invalidate_frame, rt.validate_frame = Mock(), Mock(), Mock()
        rt.observation_generation = Mock(return_value=0)
        rt._evidence = SimpleNamespace(remember_frame=Mock())
        readings = iter(focus)
        inputs = SimpleNamespace(foreground=lambda: next(readings), check=Mock(), window_only=False)
        rt._active_inputs = Mock(return_value=inputs)
        rt._check_inputs = Mock()
        return rt, inputs

    def capture(self, focus):
        rt, inputs = self.runtime(focus)
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            image = rt._capture()
        return image, rt, inputs

    def test_a_frame_with_focus_before_and_after_is_kept(self):
        image, rt, inputs = self.capture([True, True])
        self.assertEqual(int(image[0, 0, 0]), 1)
        inputs.check.assert_not_called()

    def test_without_focus_before_it_waits_and_captures_only_then(self):
        image, rt, inputs = self.capture([False, True, True])
        self.assertEqual(int(image[0, 0, 0]), 1)
        inputs.check.assert_called_once_with(require_foreground=True)
        self.assertEqual(rt.controller.post_screencap.call_count, 1)

    def test_a_frame_whose_focus_was_lost_by_the_end_is_discarded(self):
        image, rt, inputs = self.capture([True, False, True, True])
        self.assertEqual(int(image[0, 0, 0]), 2)  # the first frame was thrown away
        self.assertEqual(rt.controller.post_screencap.call_count, 2)


class SessionlessCaptureTests(unittest.TestCase):
    def test_without_a_session_capture_still_needs_a_positive_focus_reading(self):
        import numpy as np
        from daily.runtime import Runtime
        state = SimpleNamespace(focus=[FOCUS_LAUNCHER, FOCUS_GAME, FOCUS_GAME])

        class Device(FakeShellController):
            def post_shell(self, command, timeout):
                if "mCurrentFocus" in command:
                    self.outputs["mCurrentFocus"] = state.focus.pop(0) if state.focus else FOCUS_GAME
                return super().post_shell(command, timeout)
        controller = Device({"pidof": "4242\n__exit=0\n"})
        controller.info = INFO
        frame = np.full((720, 1280, 3), 3, np.uint8)
        controller.post_screencap = Mock(return_value=SimpleNamespace(wait=lambda: None, succeeded=True,
                                                                     get=lambda: frame))
        rt = Runtime.__new__(Runtime)
        rt.controller, rt.context = controller, SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        rt.navigation_halted, rt._focus_generation = False, 0
        rt.check_focus, rt.invalidate_frame, rt.validate_frame = Mock(), Mock(), Mock()
        rt.observation_generation = Mock(return_value=0)
        rt._evidence = SimpleNamespace(remember_frame=Mock())
        rt._active_inputs = Mock(return_value=None)
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                patch("daily.runtime.wall_time.sleep") as sleep:
            self.assertIs(rt._capture(), frame)
        sleep.assert_called_once()  # the launcher reading waited instead of capturing
        self.assertEqual(controller.post_screencap.call_count, 1)
        self.assertGreaterEqual(sum("mCurrentFocus" in c for c in controller.commands), 3)


class SessionlessExitTests(unittest.TestCase):
    def test_sessionless_capture_ends_when_the_game_has_exited(self):
        from daily.runtime import Runtime
        from navigation.models import GameWindowClosed
        controller = FakeShellController({"mCurrentFocus": FOCUS_LAUNCHER, "pidof": "__exit=1\n"})
        controller.info = INFO
        controller.post_screencap = Mock()
        rt = Runtime.__new__(Runtime)
        rt.controller, rt.context = controller, SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        rt.navigation_halted, rt._focus_generation = False, 0
        rt.check_focus, rt.invalidate_frame = Mock(), Mock()
        rt.observation_generation = Mock(return_value=0)
        rt._active_inputs = Mock(return_value=None)
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                patch("daily.runtime.wall_time.sleep"), self.assertRaises(GameWindowClosed):
            rt._capture()
        controller.post_screencap.assert_not_called()


class FocusLostModeTests(unittest.TestCase):
    def test_return_to_the_game_behaves_like_pause(self):
        from task_control import focus_lost_mode as normalize
        with android_run():
            self.assertEqual(normalize("return"), "pause")
            self.assertEqual(normalize("background"), "pause")
            self.assertEqual(normalize("pause"), "pause")


if __name__ == "__main__":
    unittest.main()
