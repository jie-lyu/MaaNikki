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
from host.android.target import AndroidTarget, check_controller_info, focused_package  # noqa: E402
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

    def test_connect_target_reads_the_device(self):
        controller = FakeShellController({"mCurrentFocus": FOCUS_GAME, "pidof": "4242\n"})
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
        self.assertFalse(target.capture_requires_foreground)
        self.assertTrue(target.release_ignores_focus)

    def test_a_dialog_or_launcher_in_front_is_not_the_game(self):
        for outputs in ({"mCurrentFocus": FOCUS_LAUNCHER, "pidof": "4242\n"},
                        {"mCurrentFocus": "  mCurrentFocus=null\n", "pidof": "4242\n"},
                        {}):  # shell failed
            target = AndroidTarget(INFO, FakeShellController(outputs))
            self.assertFalse(target.foreground(), outputs)
        self.assertFalse(AndroidTarget(INFO, FakeShellController({"pidof": ""})).exists())

    def test_no_input_or_window_geometry_yet(self):
        target = AndroidTarget(INFO, FakeShellController({}))
        with self.assertRaises(NavigationError):
            target.input()
        with self.assertRaises(NavigationError):
            target.geometry()


if __name__ == "__main__":
    unittest.main()
