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


try:
    import fcntl  # noqa: F401
    HAS_FLOCK = True
except ImportError:
    HAS_FLOCK = False


REAL_TAKE_OWNER_LOCK = host.take_owner_lock


def injected_owner_lock(path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise host.UnsupportedInput("cannot create the lock directory") from None
    return SimpleNamespace(close=lambda: None)


def fake_fcntl(flock):
    return SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=flock)


class FreshAndroidState(unittest.TestCase):
    """Every test starts with fresh process-wide Android input state (halt flag, focus epoch, frame marks)."""

    def setUp(self):
        import tempfile
        import daily.runtime as runtime_module
        self.runtime_module = runtime_module
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_root = Path(tmp.name)
        if not HAS_FLOCK:  # Windows CI: ownership is injected; the real lock is tested on macOS
            owner = patch.object(host, "take_owner_lock", side_effect=injected_owner_lock)
            owner.start()
            self.addCleanup(owner.stop)
        patcher = patch.object(host, "_DEVICE_INPUT", self.fresh_state())
        self.state = patcher.start()
        self.addCleanup(patcher.stop)

    def fresh_state(self, root=...):
        state = host.DeviceInputState(self.data_root if root is ... else root)
        self.addCleanup(lambda: state._owner.close() if state._owner is not None else None)
        return state


class TaskListTests(unittest.TestCase):
    def test_the_shipped_list_is_well_formed_and_holds_the_check_and_mailbox_on_trial(self):
        verified, experimental, excluded = android.task_tiers()
        self.assertEqual(verified, frozenset())
        self.assertEqual(experimental, frozenset({"MaaNikki_AndroidCheck", "MaaNikki_Mailbox"}))
        self.assertEqual(excluded, {})

    def test_admission_notices_and_refusals_follow_the_android_list(self):
        with android_run():
            self.assertEqual(host.game_backend(), "android")
            self.assertTrue(host.task_entry_supported("MaaNikki_AndroidCheck"))
            for entry in ("MaaNikki_Dig", "MaaNikki_Fishing", None, ""):
                self.assertFalse(host.task_entry_supported(entry))
            self.assertEqual(host.task_refusal_reason("MaaNikki_Dig"), "安卓模拟器暂不开放这个任务，已跳过。")
            self.assertEqual(host.task_start_notice("MaaNikki_AndroidCheck"),
                             "本任务尚未在安卓模拟器上验证（试用）：只截图和识别，不向游戏发送任何输入。")
            self.assertIsNone(host.task_start_notice("MaaNikki_Dig"))

    def test_an_unreadable_list_runs_nothing(self):
        with android_run(), patch.object(android, "_task_list", side_effect=OSError("missing")):
            self.assertFalse(host.task_entry_supported("MaaNikki_AndroidCheck"))
            self.assertEqual(host.task_refusal_reason("MaaNikki_AndroidCheck"), "无法读取安卓任务清单，已跳过。")
            self.assertIsNone(host.task_start_notice("MaaNikki_AndroidCheck"))

    def test_the_mac_development_switch_does_not_open_android_tasks(self):
        with android_run(), patch.dict(os.environ, {"MAANIKKI_MACOS_EXTRA_TASKS": "MaaNikki_Dig"}):
            self.assertFalse(host.task_entry_supported("MaaNikki_Dig"))

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
            for entry in ("MaaNikki_Dig", "MaaNikki_AndroidCheck"):
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


class TargetTests(FreshAndroidState):
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


class UnreachableDeviceTests(FreshAndroidState):
    def test_repeated_shell_failures_stop_with_adb_restart_advice(self):
        controller = FakeShellController({"mCurrentFocus": FOCUS_GAME})
        target = AndroidTarget(INFO, controller)
        self.assertTrue(target.foreground())
        controller.outputs.clear()  # the ADB server stops answering
        self.assertFalse(target.foreground())
        self.assertFalse(target.foreground())
        with self.assertRaises(host.DeviceUnreachable) as caught:
            target.foreground()
        self.assertIn("adb kill-server", str(caught.exception))
        controller.outputs["mCurrentFocus"] = FOCUS_GAME
        self.assertTrue(target.foreground())  # a success starts the count again
        controller.outputs.clear()
        self.assertFalse(target.foreground())
        controller.outputs["mCurrentFocus"] = FOCUS_GAME
        self.assertTrue(target.foreground())

    def test_an_unreachable_device_makes_earlier_frames_stale(self):
        import numpy as np
        controller = FakeShellController({"mCurrentFocus": FOCUS_GAME})
        target = AndroidTarget(INFO, controller)
        frame = np.zeros((720, 1280, 3), np.uint8)
        self.state.mark(frame, 0)
        controller.outputs.clear()
        self.assertIs(target.exists(), True)  # unreadable is not "gone" (first failure)
        target.exists()                        # second failure
        with self.assertRaises(host.DeviceUnreachable):
            target.foreground()                # third: raised before any focus reading was recorded
        self.assertNotEqual(self.state.marks_of(frame), (0, self.state.epoch))  # the old frame no longer counts

    def test_shell_reads_hold_the_device_input_lock(self):
        owned = []

        class Device(FakeShellController):
            def post_shell(inner, command, timeout):
                owned.append(self.state.lock._is_owned())
                return super().post_shell(command, timeout)
        target = AndroidTarget(INFO, Device({"mCurrentFocus": FOCUS_GAME, "pidof": "4242\n__exit=0\n"}))
        target.exists()
        target.foreground()
        self.assertEqual(owned, [True, True])  # pidof as well as the focus read


class SessionTests(FreshAndroidState):
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


class NoKeyboardOrMouseTests(FreshAndroidState):
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
        import numpy as np
        rt = self.runtime()
        rt.context.run_action_direct = Mock(return_value=SimpleNamespace(success=success))
        rt.context.tasker.post_stop = Mock()
        self.frame = np.zeros((720, 1280, 3), np.uint8)
        self.state.mark(self.frame, 0)  # the decision's frame, captured at focus generation 0
        rt._validate_pointer_targets = Mock(return_value=(1280, 720))
        rt.pause = Mock(return_value=True)
        rt.ui = SimpleNamespace(unhover=Mock(return_value=True))  # without pointer hover it returns at once
        rt._device_target = SimpleNamespace(foreground=Mock(return_value=focused), exists=Mock(return_value=True),
                                            serial=INFO["adb_serial"])
        return rt

    def test_a_tap_is_dispatched_with_device_focus_confirmed_right_before(self):
        rt = self.tap_runtime()
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame))
        rt.context.run_action.assert_not_called()  # no pipeline node: nothing inherited
        action, param = rt.context.run_action_direct.call_args.args[:2]
        self.assertEqual(action, "Click")
        self.assertEqual((param.target, param.target_offset, param.contact), ([119, 209], (0, 0, 0, 0), 0))
        rt._device_target.foreground.assert_called()

    def test_a_swipe_is_sent_fully_specified(self):
        rt = self.tap_runtime()
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.action("Swipe", begin=[100, 100], end=[300, 100], duration=300, source_frame=self.frame))
        action, param = rt.context.run_action_direct.call_args.args[:2]
        self.assertEqual(action, "Swipe")
        self.assertEqual((param.begin, param.end, param.duration, param.only_hover, param.contact,
                          param.begin_offset, param.end_offset),
                         ([100, 100], [[300, 100]], [300], False, 0, (0, 0, 0, 0), [(0, 0, 0, 0)]))

    def test_swipe_rectangles_use_a_pixel_inside_them(self):
        rt = self.tap_runtime()
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.action("Swipe", begin=[1270, 10, 10, 10], end=[1279, 719, 1, 1], duration=300,
                                      source_frame=self.frame))
        param = rt.context.run_action_direct.call_args.args[1]
        self.assertEqual((param.begin, param.end), ([1274, 14], [[1279, 719]]))

    def test_each_touch_is_on_disk_before_it_is_sent_and_cleared_once_confirmed(self):
        rt = self.tap_runtime()
        marker = self.state.marker()
        seen = []
        rt.context.run_action_direct.side_effect = lambda *a: (seen.append(marker.is_file()),
                                                               SimpleNamespace(success=True))[1]
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame))
            self.assertTrue(rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame))
        self.assertEqual(seen, [True, True])
        self.assertFalse(marker.exists())
        self.assertEqual(marker.parent, self.data_root / "config" / "android-input")  # not under the clearable cache

    def test_every_sent_touch_is_logged_with_its_outcome(self):
        rt = self.tap_runtime()
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame))
            self.assertTrue(rt.action("Swipe", begin=[100, 100], end=[300, 100], duration=300,
                                      source_frame=self.frame))
        log = self.data_root / "logs" / "android-input.jsonl"
        entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(e["action"], e["outcome"]) for e in entries],
                         [("Click", "sending"), ("Click", "confirmed"), ("Swipe", "sending"), ("Swipe", "confirmed")])
        self.assertEqual(entries[0]["target"], [119, 209])  # the pixel actually tapped, logged before it was sent
        self.assertEqual((entries[3]["begin"], entries[3]["end"], entries[3]["duration"]), ([100, 100], [300, 100], 300))
        rt.context.run_action_direct.return_value = SimpleNamespace(success=False)
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                self.assertRaises(host.InputUnconfirmed):
            rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame)
        self.assertEqual(json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["outcome"], "unconfirmed")

    def test_stop_or_focus_loss_during_the_intent_write_sends_nothing(self):
        for change in ("stopping", "focus"):
            with self.subTest(change=change):
                rt = self.tap_runtime()
                original = self.state.record_touch

                def record(action, fields, outcome, rt=rt, change=change, original=original):
                    if outcome == "sending":
                        if change == "stopping":
                            rt.context.tasker.stopping = True
                        else:
                            rt._device_target.foreground.return_value = False
                    return original(action, fields, outcome)
                with patch.object(self.state, "record_touch", side_effect=record), android_run(), \
                        patch("daily.runtime.active_session", return_value=None):
                    try:
                        self.assertFalse(rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame))
                    except Exception as error:  # focus loss raises ForegroundChanged
                        self.assertEqual(type(error).__name__, "ForegroundChanged")
                rt.context.run_action_direct.assert_not_called()
                self.assertFalse(self.state.marker().exists())
                log = (self.data_root / "logs" / "android-input.jsonl").read_text(encoding="utf-8").splitlines()
                self.assertEqual(json.loads(log[-1])["outcome"], "cancelled")

    def test_a_touch_whose_intent_cannot_be_logged_is_not_sent(self):
        (self.data_root / "logs").write_text("")  # a file where the log directory goes
        rt = self.tap_runtime()
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                self.assertRaises(host.UnsupportedInput):
            rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame)
        rt.context.run_action_direct.assert_not_called()
        self.assertFalse(self.state.halted)
        self.assertFalse(self.state.marker().exists())  # the reservation was released: nothing was sent

    def test_an_unconfirmed_touch_halts_the_next_agent_process_under_any_serial(self):
        rt = self.tap_runtime(success=False)
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                self.assertRaises(host.InputUnconfirmed):
            rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame)
        self.assertTrue(self.state.marker().is_file())
        self.state._owner.close()  # this process exits
        for serial in ("127.0.0.1:5555", "emulator-5554"):  # one BlueStacks instance under two serials
            with self.subTest(serial=serial), patch.object(host, "_DEVICE_INPUT", self.fresh_state()) as state:
                self.state = state
                nxt = self.tap_runtime()
                nxt._device_target.serial = serial
                with android_run(), patch("daily.runtime.active_session", return_value=None):
                    with self.assertRaises(host.InputUnconfirmed):
                        nxt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame)
                    self.assertTrue(state.halted)
                    self.assertTrue(nxt.stopped)
                nxt.context.run_action_direct.assert_not_called()
                nxt.context.tasker.post_stop.assert_called_once()
                self.assertTrue(state.marker().is_file())  # still there for the next one
                state._owner.close()

    @unittest.skipUnless(HAS_FLOCK, "flock is macOS/POSIX only; Android hosts are macOS for now")
    def test_only_one_agent_process_uses_android_input(self):
        import subprocess
        import textwrap
        lock = self.data_root / "config" / "android-input" / "owner.lock"
        lock.parent.mkdir(parents=True)
        holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import fcntl, sys
            handle = open({str(lock)!r}, "a+b")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            print("held", flush=True)
            sys.stdin.read()
        """)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.stdin.close)
        self.addCleanup(holder.stdout.close)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        rt = self.tap_runtime()
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            with self.assertRaises(host.UnsupportedInput):
                rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame)
            with self.assertRaises(host.UnsupportedInput):
                self.state.claim()  # capture claims the same way
        rt.context.run_action_direct.assert_not_called()
        self.assertFalse(self.state.marker().exists())
        self.assertFalse(self.state.halted)

    def test_a_touch_that_cannot_be_recorded_is_not_sent(self):
        import stat
        blocker = self.data_root / "blocker"
        blocker.write_text("")
        locked = self.data_root / "locked"
        for case in ("no data root", "data root is a file", "directory not writable", "write fails"):
            if case == "directory not writable" and os.name == "nt":
                continue  # a read-only attribute does not stop file creation in a Windows directory
            with self.subTest(case=case):
                root = {"no data root": None, "data root is a file": blocker}.get(case, self.data_root)
                if case == "directory not writable":
                    root = locked
                state = self.fresh_state(root)
                if case == "directory not writable":
                    state.claim()
                    state.directory().chmod(stat.S_IRUSR | stat.S_IXUSR)
                    self.addCleanup(state.directory().chmod, stat.S_IRWXU)
                with patch.object(host, "_DEVICE_INPUT", state):
                    self.state = state
                    rt = self.tap_runtime()
                    write = patch("host.os.write", side_effect=OSError(28, "No space left on device")) \
                        if case == "write fails" else nullcontext()
                    with android_run(), patch("daily.runtime.active_session", return_value=None), write, \
                            self.assertRaises(host.UnsupportedInput):
                        rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame)
                    rt.context.run_action_direct.assert_not_called()
                    self.assertFalse(state.halted)
                    if case == "write fails":
                        self.assertFalse(state.marker().exists())  # the partial reservation was removed
                        with android_run(), patch("daily.runtime.active_session", return_value=None):
                            self.assertTrue(rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame))
                if state._owner is not None:
                    state._owner.close()

    def test_owner_lock_failures_are_refusals(self):
        def contended(fd, flags):
            raise BlockingIOError(35, "Resource temporarily unavailable")
        lock = self.data_root / "elsewhere" / "owner.lock"
        cases = (("no fcntl", None, nullcontext()),
                 ("held elsewhere", fake_fcntl(contended), nullcontext()),
                 ("open fails", fake_fcntl(Mock()), patch("builtins.open", side_effect=BlockingIOError(35, "busy"))),
                 ("flock fails", fake_fcntl(Mock(side_effect=OSError(5, "I/O error"))), nullcontext()))
        for case, module, opening in cases:
            with self.subTest(case=case), patch.dict(sys.modules, {"fcntl": module}), opening, \
                    self.assertRaises(host.UnsupportedInput):
                REAL_TAKE_OWNER_LOCK(lock)

    def test_a_process_that_does_not_own_android_input_reads_no_focus(self):
        controller = FakeShellController({"mCurrentFocus": FOCUS_LAUNCHER})
        target = AndroidTarget(INFO, controller)
        with patch.object(host, "take_owner_lock", side_effect=host.UnsupportedInput("owned elsewhere")), \
                patch.object(host, "_DEVICE_INPUT", self.fresh_state()) as state:
            with self.assertRaises(host.UnsupportedInput):
                target.foreground()
            details = target.focus_details()
            self.assertIsNone(details["focused_package"])
            self.assertEqual(state.epoch, 0)  # no reading, so no loss recorded where the owner cannot see it
        self.assertFalse(any("mCurrentFocus" in command for command in controller.commands))

    def test_stop_pressed_during_the_final_focus_query_sends_nothing(self):
        rt = self.tap_runtime()

        def focus_while_stop_is_pressed():
            rt.context.tasker.stopping = True
            return True
        rt._device_target.foreground = focus_while_stop_is_pressed
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertFalse(rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame))
        rt.context.run_action_direct.assert_not_called()
        self.assertFalse(self.state.marker().exists())
        session = Mock()
        session.stopped.side_effect = lambda fresh=False: fresh  # the cached answer is stale
        with android_run(), patch("daily.runtime.active_session", return_value=session):
            self.assertTrue(self.tap_runtime()._stopped_now())
        session.stopped.assert_called_with(fresh=True)

    def test_the_last_checks_follow_the_reservation(self):
        from task_control import ForegroundChanged
        for change in ("focus", "stopping", "halted"):
            with self.subTest(change=change):
                rt = self.tap_runtime()
                original = self.state.begin_touch

                def begin_touch_while_something_changes(change=change, rt=rt, original=original):
                    original()
                    if change == "focus":
                        rt._device_target.foreground.return_value = False
                    elif change == "stopping":
                        rt.context.tasker.stopping = True
                    else:
                        self.state.halted = True
                with patch.object(self.state, "begin_touch", begin_touch_while_something_changes), \
                        android_run(), patch("daily.runtime.active_session", return_value=None):
                    if change == "focus":
                        with self.assertRaises(ForegroundChanged):
                            rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame)
                    else:
                        self.assertFalse(rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame))
                rt.context.run_action_direct.assert_not_called()
                self.assertFalse(self.state.marker().exists())  # nothing was sent: the reservation is released
                self.state.halted = False

    def test_without_a_chosen_data_root_android_input_is_refused(self):
        env = {k: v for k, v in os.environ.items() if k != "MAANIKKI_DATA_ROOT"}
        with patch.object(host, "_DEVICE_INPUT", None), patch.dict(os.environ, env, clear=True):
            state = host.device_input()
            self.assertIsNone(state.root)
            with self.assertRaises(host.UnsupportedInput):
                state.claim()
        with patch.object(host, "_DEVICE_INPUT", None), \
                patch.dict(os.environ, {"MAANIKKI_DATA_ROOT": str(self.data_root)}):
            self.assertIsNotNone(host.device_input().root)

    def test_edge_rectangles_tap_a_pixel_inside_them(self):
        from host.android import touch_point
        self.assertEqual(touch_point([1279, 719, 1, 1]), [1279, 719])
        self.assertEqual(touch_point([1200, 200, 80, 20]), [1239, 209])
        self.assertEqual(touch_point([5, 6]), [5, 6])
        rt = self.tap_runtime()
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.action("Click", target=[1279, 719, 1, 1], source_frame=self.frame))
        self.assertEqual(rt.context.run_action_direct.call_args.args[1].target, [1279, 719])

    def test_focus_is_read_inside_the_dispatch_lock(self):
        from task_control import ForegroundChanged
        rt = self.tap_runtime()
        readings = []

        def foreground():
            readings.append(self.state.lock._is_owned())  # RLock: owned by this thread while reading
            return False
        rt._device_target.foreground = foreground
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                self.assertRaises(ForegroundChanged):
            rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame)
        self.assertEqual(readings, [True])
        rt.context.run_action_direct.assert_not_called()

    def test_a_tap_needs_its_source_frame(self):
        rt = self.tap_runtime()
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                self.assertRaises(host.UnsupportedInput):
            rt.action("Click", target=[100, 200, 40, 20])
        rt.context.run_action_direct.assert_not_called()

    def test_a_fresh_capture_does_not_authorise_a_target_from_an_older_frame(self):
        import numpy as np
        from task_control import ForegroundChanged
        rt = self.tap_runtime()
        old = self.frame
        self.state.observed_loss()                 # seen by any Runtime
        new = np.ones((720, 1280, 3), np.uint8)
        self.state.mark(new, 0)                     # captured after focus came back
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            with self.assertRaises(ForegroundChanged):
                rt.action("Click", target=[100, 200, 40, 20], source_frame=old)
            self.assertTrue(rt.action("Click", target=[100, 200, 40, 20], source_frame=new))
        self.assertEqual(rt.context.run_action_direct.call_count, 1)

    def test_a_focus_loss_seen_by_another_runtime_makes_frames_stale(self):
        from task_control import ForegroundChanged
        rt = self.tap_runtime()
        other = self.tap_runtime()
        other._device_target.foreground = Mock(return_value=False)
        frame = self.frame
        self.state.mark(frame, 0)
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            self.assertFalse(other._device_focus_now())
            with self.assertRaises(ForegroundChanged):
                rt.action("Click", target=[100, 200, 40, 20], source_frame=frame)

    def test_a_focus_loss_observed_after_the_capture_cancels_a_tap_without_a_token(self):
        from task_control import ForegroundChanged
        rt = self.tap_runtime()
        readings = iter([False, True, True])  # lost, then back: the frame from before is stale
        rt._device_target.foreground = Mock(side_effect=lambda: next(readings))
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                patch("daily.runtime.wall_time.sleep"):
            self.assertFalse(rt._device_focus_ready())
            with self.assertRaises(ForegroundChanged):
                rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame)
        rt.context.run_action_direct.assert_not_called()

    def test_a_tap_without_device_focus_or_on_an_old_frame_is_cancelled(self):
        from task_control import ForegroundChanged
        rt = self.tap_runtime(focused=False)
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                self.assertRaises(ForegroundChanged):
            rt.action("Click", target=[100, 200, 40, 20], source_frame=self.frame)
        rt = self.tap_runtime()
        rt._focus_generation = 8  # the decision was made on generation 7
        with android_run(), patch("daily.runtime.active_session", return_value=None), \
                self.assertRaises(ForegroundChanged):
            rt.action("Click", target=[100, 200, 40, 20], focus_generation=7, source_frame=self.frame)
        rt.context.run_action_direct.assert_not_called()

    def test_an_unconfirmed_tap_halts_every_runtime_and_the_queue(self):
        for failure in ("rejected", "raised"):
            with self.subTest(failure=failure), \
                    patch.object(host, "_DEVICE_INPUT", self.fresh_state(self.data_root / failure)) as state:
                self.state = state
                rt = self.tap_runtime(success=False)
                if failure == "raised":
                    rt.context.run_action_direct.side_effect = RuntimeError("result unreadable")
                other = self.tap_runtime()
                with android_run(), patch("daily.runtime.active_session", return_value=None):
                    with self.assertRaises((host.InputUnconfirmed, RuntimeError)):
                        rt.action("Swipe", begin=[100, 100], end=[300, 100], duration=300, source_frame=self.frame)
                    self.assertTrue(state.halted)
                    self.assertTrue(rt.stopped)
                    self.assertTrue(other.stopped)  # another Runtime in the same process
                    self.assertFalse(other.action("Click", target=[10, 10, 4, 4], source_frame=self.frame))
                rt.context.tasker.post_stop.assert_called_once()  # later tasks of the batch do not start
                other.context.run_action_direct.assert_not_called()
        session = SimpleNamespace(input_unconfirmed=False)
        rt = self.tap_runtime(success=False)
        with android_run(), patch("daily.runtime.active_session", return_value=session):
            rt._halt_unconfirmed()
        self.assertTrue(session.input_unconfirmed)  # with a session, the whole run stops through it

    def test_only_plain_targets_inside_the_frame(self):
        rt = self.tap_runtime()
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            for action, fields in (("Click", {"target": [50, 50, 4, 4], "target_offset": [200, 100, 0, 0]}),
                                   ("Swipe", {"begin": [1, 1], "end": [5, 5], "only_hover": True}),
                                   ("Click", {"target": [50, 50], "contact": 1}),
                                   ("Click", {"target": [1270, 700, 40, 40]}),
                                   ("Click", {"target": [1280, 10]}),
                                   ("Click", {"target": [20, 720]}),
                                   ("Click", {"target": [5, 5, 0, 10]}),

                                   ("Click", {"target": [-1, 5]}),
                                   ("Click", {}),
                                   ("Swipe", {"begin": [1, 1], "end": [5, 5], "duration": -1})):
                with self.subTest(action=action, fields=fields), self.assertRaises(host.UnsupportedInput):
                    rt.action(action, source_frame=self.frame, **fields)
        rt.context.run_action_direct.assert_not_called()

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


class DeviceFocusCaptureTests(FreshAndroidState):
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

    def test_a_loss_observed_after_the_last_check_is_not_stamped_onto_the_frame(self):
        rt, inputs = self.runtime([True, True, True, True])
        calls = []

        def validate(image):
            if not calls:
                self.state.observed_loss()  # e.g. another caller sees the launcher while this frame is validated
            calls.append(image)
        rt.validate_frame = validate
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            image = rt._capture()
        self.assertEqual(int(image[0, 0, 0]), 2)  # the first frame was discarded, not marked with the new epoch
        self.assertIsNone(self.state.marks_of(calls[0]))

    def test_a_loss_between_the_focus_check_and_the_capture_discards_the_frame(self):
        rt, inputs = self.runtime([True, True, True, True])
        original = rt._device_focus_ready
        calls = []

        def ready():
            result = original()
            calls.append(result)
            if len(calls) == 1:
                self.state.observed_loss()  # another caller sees the launcher right after this positive reading
            return result
        rt._device_focus_ready = ready
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            image = rt._capture()
        self.assertEqual(int(image[0, 0, 0]), 2)
        self.assertEqual(rt.controller.post_screencap.call_count, 2)

    def test_validation_and_mark_are_one_locked_step(self):
        rt, inputs = self.runtime([True, True])
        owned = []
        rt.validate_frame = lambda image: owned.append(self.state.lock._is_owned())
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            image = rt._capture()
        self.assertEqual(owned, [True])  # no loss can be recorded between validating and marking
        self.assertEqual(self.state.marks_of(image), (0, self.state.epoch))

    def test_a_loss_before_validation_skips_the_validator(self):
        rt, inputs = self.runtime([True, True, True, True])
        validated = []
        rt.validate_frame = validated.append
        original = rt._device_focus_ready
        calls = []

        def ready():
            result = original()
            calls.append(result)
            if len(calls) == 2:
                self.state.observed_loss()  # after the post-capture check, before validation
            return result
        rt._device_focus_ready = ready
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            image = rt._capture()
        self.assertEqual(len(validated), 1)  # the first frame never reached the validator (nor its evidence)
        self.assertIs(validated[0], image)
        self.assertEqual(int(image[0, 0, 0]), 2)

    def test_only_an_accepted_frame_becomes_evidence(self):
        rt, inputs = self.runtime([True, True, True, True])
        rt.validate_frame = Mock(side_effect=lambda image: self.state.observed_loss()
                                 if rt.validate_frame.call_count == 1 else None)
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            image = rt._capture()
        frames = [c.args[0] for c in rt._evidence.remember_frame.call_args_list]
        self.assertEqual(len(frames), 1)
        self.assertIs(frames[0], image)

    def test_a_focus_loss_seen_elsewhere_during_the_capture_discards_the_frame(self):
        rt, inputs = self.runtime([True, True, True, True])
        original = rt.controller.post_screencap.side_effect

        def capture_while_another_runtime_sees_the_launcher():
            if rt.controller.post_screencap.call_count == 1:
                self.state.observed_loss()
            return original()
        rt.controller.post_screencap.side_effect = capture_while_another_runtime_sees_the_launcher
        with android_run(), patch("daily.runtime.active_session", return_value=None):
            image = rt._capture()
        self.assertEqual(int(image[0, 0, 0]), 2)
        self.assertEqual(self.state.marks_of(image), (0, 1))

    def test_a_frame_whose_focus_was_lost_by_the_end_is_discarded(self):
        image, rt, inputs = self.capture([True, False, True, True])
        self.assertEqual(int(image[0, 0, 0]), 2)  # the first frame was thrown away
        self.assertEqual(rt.controller.post_screencap.call_count, 2)


class DeviceInputStateTests(FreshAndroidState):
    def test_the_target_records_every_negative_reading(self):
        target = AndroidTarget(INFO, FakeShellController({"mCurrentFocus": FOCUS_LAUNCHER}))
        before = self.state.epoch
        self.assertFalse(target.foreground())
        self.assertFalse(target.foreground())
        self.assertEqual(self.state.epoch, before + 2)
        self.assertTrue(AndroidTarget(INFO, FakeShellController({"mCurrentFocus": FOCUS_GAME})).foreground())
        self.assertEqual(self.state.epoch, before + 2)

    def test_a_session_adapter_reading_makes_frames_stale(self):
        import numpy as np
        controller = FakeShellController({"mCurrentFocus": FOCUS_LAUNCHER, "pidof": "4242\n__exit=0\n"})
        controller.info = INFO
        from navigation.controller import ForegroundInput
        with android_run():
            inputs = ForegroundInput(controller, lambda: False)
        frame = np.zeros((720, 1280, 3), np.uint8)
        self.state.mark(frame, 0)
        self.assertFalse(inputs.capturable())
        self.assertNotEqual(self.state.marks_of(frame), (0, self.state.epoch))

    def test_focus_reads_and_loss_records_are_one_locked_step(self):
        owned = []

        class Device(FakeShellController):
            def post_shell(inner, command, timeout):
                owned.append(self.state.lock._is_owned())
                return super().post_shell(command, timeout)
        target = AndroidTarget(INFO, Device({"mCurrentFocus": FOCUS_LAUNCHER}))
        before = self.state.epoch
        self.assertFalse(target.foreground())
        self.assertEqual(target.focus_details()["focused_package"], "com.bluestacks.launcher")
        self.assertEqual(self.state.epoch, before + 2)  # the diagnostic reading counts too
        self.assertEqual(owned, [True, True])

    def test_the_state_is_created_once_across_threads(self):
        import threading
        import time
        seen = []
        original = host.DeviceInputState.__init__

        def slow_init(inner, root=None):
            time.sleep(0.05)
            original(inner, root)
        with patch.object(host, "_DEVICE_INPUT", None), patch.object(host.DeviceInputState, "__init__", slow_init):
            threads = [threading.Thread(target=lambda: seen.append(host.device_input())) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len({id(state) for state in seen}), 1)

    def test_desktop_runs_never_create_or_read_the_state(self):
        from daily.runtime import Runtime
        rt = Runtime.__new__(Runtime)
        rt.navigation_halted = False
        rt.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        with patch.object(host, "_DEVICE_INPUT", None), patch.object(host, "platform_backend", return_value="win32"), \
                patch.dict(os.environ, {"PI_CONTROLLER": json.dumps({"type": "Win32"})}), \
                patch("daily.runtime.active_session", return_value=None):
            self.assertFalse(rt.stopped)
            self.assertIsNone(host._DEVICE_INPUT)

    def test_desktop_capture_never_touches_the_state(self):
        import numpy as np
        from daily.runtime import Runtime
        frame = np.full((720, 1280, 3), 5, np.uint8)
        rt = Runtime.__new__(Runtime)
        rt.controller = SimpleNamespace(post_screencap=Mock(return_value=SimpleNamespace(
            wait=lambda: None, succeeded=True, get=lambda: frame)))
        rt.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        rt.navigation_halted, rt._focus_generation = False, 0
        rt.check_focus, rt.invalidate_frame, rt.validate_frame = Mock(), Mock(), Mock()
        rt.observation_generation = Mock(return_value=0)
        rt._evidence = SimpleNamespace(remember_frame=Mock())
        rt._active_inputs = Mock(return_value=None)
        for backend, controller in (("win32", {"type": "Win32"}), ("macos", {"type": "MacOS"})):
            with self.subTest(backend=backend), patch.object(host, "_DEVICE_INPUT", None), \
                    patch.object(host, "device_input", side_effect=AssertionError("desktop touched Android state")), \
                    patch("daily.runtime.device_input", side_effect=AssertionError("desktop touched Android state")), \
                    patch.object(host, "platform_backend", return_value=backend), \
                    patch.dict(os.environ, {"PI_CONTROLLER": json.dumps(controller)}), \
                    patch("daily.runtime.active_session", return_value=None):
                self.assertIs(rt._capture(), frame)
                self.assertIsNone(host._DEVICE_INPUT)


class SessionlessCaptureTests(FreshAndroidState):
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


class SessionlessExitTests(FreshAndroidState):
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


class MessageAndSessionTests(unittest.TestCase):
    def test_messages_pin_their_node_on_android(self):
        from task_control import message_override
        with android_run():
            node = message_override({"Node.Action.Starting": "x"})["MaaNikki_Daily_Message"]
        self.assertEqual(node, {"focus": {"Node.Action.Starting": "x"}, "recognition": "DirectHit",
                                "action": "DoNothing", "next": [], "on_error": []})

    def test_an_unconfirmed_touch_stops_the_session(self):
        from task_control import ForegroundSession
        session = ForegroundSession(SimpleNamespace(tasker=SimpleNamespace(stopping=False)))
        self.assertFalse(session.stopped(fresh=True))
        session.input_unconfirmed = True
        self.assertTrue(session.stopped())


class FocusLostModeTests(unittest.TestCase):
    def test_return_to_the_game_behaves_like_pause(self):
        from task_control import focus_lost_mode as normalize
        with android_run():
            self.assertEqual(normalize("return"), "pause")
            self.assertEqual(normalize("background"), "pause")
            self.assertEqual(normalize("pause"), "pause")


if __name__ == "__main__":
    unittest.main()
