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
            self.assertFalse(any(getattr(caps, name) for name in caps.__dataclass_fields__ if name != "backend"))
            self.assertIsNone(host.overlay_backend())
            self.assertIsNone(host.launch_window_probes())
            with self.assertRaises(RuntimeError):
                host.game_close_api()
            with self.assertRaises(NavigationError):
                host.background_input_class()
            self.assertEqual(host.native_key(77), 77)  # no macOS translation for an emulator


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
        self.assertFalse(native.press_mouse(1))
        self.assertFalse(native.post_window_key(87, down=True))
        self.assertFalse(native.relative_move(5, 5))
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


if __name__ == "__main__":
    unittest.main()
