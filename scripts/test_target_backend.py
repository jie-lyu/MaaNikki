"""Game-target backend selection (host.game_backend) and the paths it must leave unchanged:
python/python.exe -B scripts/test_target_backend.py.

The game target follows the controller the client submitted (PI_CONTROLLER); the desktop backend
(host.platform_backend) is pinned per test, so these run alike on every OS. The Win32 cases are positive:
they show Windows behaviour is unchanged through the real admission guard, key dispatch and asset lookup,
not only that other controllers are refused.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import glob
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))

import host  # noqa: E402
from navigation.models import NavigationError  # noqa: E402

INTERFACE = json.loads((ROOT / "interface.json").read_text(encoding="utf-8"))
WINDOWS_CONTROLLER = next(c for c in INTERFACE["controller"] if c["name"] == "nikki_windows")
MACOS_CONTROLLER = next(c for c in json.loads((ROOT / "build/macos/interface-overlay.json").read_text(
    encoding="utf-8"))["controller"] if c["name"] == "nikki_macos")
ADB_CONTROLLER = {"name": "nikki_android", "type": "Adb", "display_short_side": 720}


def task_entries() -> list[str]:
    entries = set()
    for path in glob.glob(str(ROOT / "tasks" / "*.json")):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for task in data.get("task", []) if isinstance(data, dict) else []:
            if task.get("entry"):
                entries.add(task["entry"])
    return sorted(entries)


@contextmanager
def desktop(name, controller=None, raw=None):
    """This process's desktop backend pinned to `name`, with `controller` (or a raw string) as PI_CONTROLLER."""
    env = dict(os.environ)
    env.pop("PI_CONTROLLER", None)
    env.pop("MAANIKKI_MACOS_EXTRA_TASKS", None)
    if raw is not None:
        env["PI_CONTROLLER"] = raw
    elif controller is not None:
        env["PI_CONTROLLER"] = json.dumps(controller, ensure_ascii=False)
    with patch.dict(os.environ, env, clear=True), patch.object(host, "platform_backend", return_value=name):
        yield


def probe_action(name):
    """A registered agent action that records whether its task code ran (the real admission guard)."""
    import registration
    ran = []
    token = registration._LOCAL_REGISTRATION.set(True)
    try:
        @registration.agent_action(name)
        class Probe:
            def run(self, context, argv):
                ran.append(argv.task_detail.entry)
                return True
    finally:
        registration._LOCAL_REGISTRATION.reset(token)
    return Probe, ran


def run_probe(probe, entry):
    context = Mock()
    argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=11, entry=entry), node_name=entry)
    with patch("registration.task_session", return_value=nullcontext(None)):
        return probe().run(context, argv), context


class GameBackendSelectionTests(unittest.TestCase):
    def test_without_a_submitted_controller_it_is_the_desktop_backend(self):
        for name in ("win32", "macos", None):
            with desktop(name):
                self.assertEqual(host.game_backend(), name)
                self.assertIs(host.submitted_controller_type(), host.NO_SUBMITTED_CONTROLLER)

    def test_a_desktop_controller_selects_its_backend_only_on_its_own_desktop(self):
        with desktop("win32", WINDOWS_CONTROLLER):
            self.assertEqual(host.game_backend(), "win32")
        with desktop("macos", MACOS_CONTROLLER):
            self.assertEqual(host.game_backend(), "macos")
        with desktop("macos", WINDOWS_CONTROLLER):
            self.assertIsNone(host.game_backend())
        with desktop("win32", MACOS_CONTROLLER):
            self.assertIsNone(host.game_backend())

    def test_other_controller_types_keep_the_desktop_rules(self):
        for name in ("win32", "macos", None):
            for kind in ("Gamepad", "PlayCover", "WlRoots"):
                with self.subTest(name=name, kind=kind), desktop(name, {"name": "x", "type": kind}):
                    self.assertEqual(host.game_backend(), name)
                    self.assertEqual(host.task_entry_supported("MaaNikki_Fishing"),
                                     name != "macos")  # what the desktop alone decides
        with desktop(None, WINDOWS_CONTROLLER):  # Linux: as before only for types that are not targets
            self.assertFalse(host.task_entry_supported("MaaNikki_Mailbox"))

    def test_adb_selects_android_on_macos_only_and_malformed_selects_none(self):
        with desktop("macos", ADB_CONTROLLER):
            self.assertEqual(host.game_backend(), "android")
        for name in ("win32", None):
            with desktop(name, ADB_CONTROLLER):
                self.assertIsNone(host.game_backend())
                self.assertEqual(host.capabilities(host.game_backend()), host.UNSUPPORTED)
        for name in ("win32", "macos"):
            for raw in ("{", "[]", '{"type": 3}', '{"name": "x"}', ""):
                with self.subTest(raw=raw), desktop(name, raw=raw):
                    self.assertIsNone(host.game_backend())
                    self.assertFalse(host.task_entry_supported("MaaNikki_Mailbox"))


class Win32UnchangedTests(unittest.TestCase):
    """Windows with its own controller submitted behaves exactly as with none."""

    def test_every_task_is_admitted(self):
        entries = task_entries()
        self.assertGreater(len(entries), 20)
        with desktop("win32", WINDOWS_CONTROLLER):
            self.assertEqual([e for e in entries if not host.task_entry_supported(e)], [])
            self.assertIsNone(host.task_start_notice("MaaNikki_Dig"))

    def test_the_admission_guard_runs_task_code(self):
        probe, ran = probe_action("test.target_backend.win32_admission")
        with desktop("win32", WINDOWS_CONTROLLER):
            for entry in ("MaaNikki_Fishing", "MaaNikki_StartGame", "MaaNikki_Mailbox"):
                result, context = run_probe(probe, entry)
                self.assertTrue(result, entry)
                context.run_task.assert_not_called()  # no refusal message, no start notice
        self.assertEqual(ran, ["MaaNikki_Fishing", "MaaNikki_StartGame", "MaaNikki_Mailbox"])

    def test_integer_keys_reach_the_controller_unchanged(self):
        from daily.runtime import Runtime, native_key_fields
        with desktop("win32", WINDOWS_CONTROLLER):
            self.assertEqual([host.native_key(k) for k in (27, 18, 77, 45, 999)], [27, 18, 77, 45, 999])
            self.assertEqual(native_key_fields("ClickKey", {"key": 27}), {"key": 27})
            self.assertEqual(native_key_fields("KeyDown", {"key": [17, 65]}), {"key": [17, 65]})
            rt = Runtime.__new__(Runtime)
            rt.context = SimpleNamespace(run_action=Mock(return_value=SimpleNamespace(success=True)),
                                         tasker=SimpleNamespace(stopping=False))
            rt.check_focus, rt.ensure_frame, rt.invalidate_frame = Mock(), Mock(return_value=True), Mock()
            rt._evidence = SimpleNamespace(observe=Mock())
            rt.navigation_halted = False
            rt._focus_generation = 0
            rt._active_inputs = Mock(return_value=None)
            rt._use_window_input = Mock(return_value=False)
            with patch("daily.runtime.active_session", return_value=None):
                self.assertTrue(rt.action("ClickKey", key=27))
        override = rt.context.run_action.call_args.kwargs["pipeline_override"]["MaaNikki_Daily_Action"]
        self.assertEqual(override, {"action": "ClickKey", "key": 27})

    def test_capabilities_and_window_services_are_the_win32_ones(self):
        with desktop("win32"):
            before = host.capabilities(host.game_backend())
        with desktop("win32", WINDOWS_CONTROLLER):
            self.assertEqual(host.capabilities(host.game_backend()), before)
            self.assertTrue(host.capabilities(host.game_backend()).tall_window)

    def test_assets_resolve_in_the_main_resource_only(self):
        # The GameUI a real Runtime builds, with the root it chooses, not one chosen by the test.
        from app_paths import RESOURCE_ROOT
        from daily.runtime import Runtime
        from daily.ui import catalog
        resource = RESOURCE_ROOT / "resource"
        with desktop("win32", WINDOWS_CONTROLLER), patch("daily.runtime.active_session", return_value=None):
            ui = Runtime(SimpleNamespace(tasker=SimpleNamespace(controller=None))).ui
            self.assertEqual(ui.resource, resource)
            self.assertEqual(ui.asset_path("game/ui/AreaDialog.png"),
                             (resource / "image" / "game/ui/AreaDialog.png").resolve())
            self.assertEqual(catalog(resource)["assets"]["AreaDialog"]["template"], "game/ui/AreaDialog.png")
            with self.assertRaises(ValueError):
                ui.asset_path("../../interface.json")

    def test_connect_target_accepts_the_win32_window(self):
        target = SimpleNamespace(name="win32-target")
        with desktop("win32", WINDOWS_CONTROLLER), patch("host.win32.target.Win32Target", return_value=target) as make:
            info = {"type": "win32", "hwnd": 66}
            self.assertIs(host.connect_target(SimpleNamespace(info=info)), target)
            make.assert_called_once_with(info)  # the connected window's info, passed on unchanged
        with desktop("win32", WINDOWS_CONTROLLER), \
                self.assertRaisesRegex(NavigationError, "没有有效的 Windows 游戏窗口"):  # the Win32 target's own check
            host.connect_target(SimpleNamespace(info={"type": "adb", "adb_serial": "127.0.0.1:5555"}))


class Win32FocusLostModeTests(unittest.TestCase):
    def test_saved_choices_are_unchanged_on_windows_and_macos(self):
        from task_control import focus_lost_mode
        for name, controller in (("win32", None), ("win32", WINDOWS_CONTROLLER), ("macos", None), ("macos", MACOS_CONTROLLER)):
            with self.subTest(name=name, submitted=bool(controller)), desktop(name, controller):
                self.assertEqual(focus_lost_mode("return"), "return")
                self.assertEqual(focus_lost_mode("pause"), "pause")
                self.assertEqual(focus_lost_mode("background"), "background" if name == "win32" else "pause")


class Win32CaptureAndMouseUnchangedTests(unittest.TestCase):
    def test_capture_never_consults_device_focus_and_scene_buttons_stay_available(self):
        import numpy as np
        from daily.runtime import Runtime
        from host.win32.target import Win32Target
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
        rt._device_focus_ready = Mock(side_effect=AssertionError("device focus consulted"))
        for controller in (None, WINDOWS_CONTROLLER):
            with desktop("win32", controller), patch("daily.runtime.active_session", return_value=None):
                self.assertIs(rt._capture(), frame)
        win32 = Win32Target.__new__(Win32Target)
        win32.handle = win32.hwnd = 1
        win32.user32 = Mock()
        native = win32.input()
        self.assertNotEqual(getattr(native, "supports_scene_buttons", True), False)


class Win32DispatchUnchangedTests(unittest.TestCase):
    def test_child_task_overrides_and_actions_are_not_filtered_on_windows(self):
        from daily.runtime import Runtime
        rt = Runtime.__new__(Runtime)
        rt.navigation_halted = False
        rt.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False), clone=Mock())
        rt.context.clone.return_value.run_task.return_value = SimpleNamespace(status=SimpleNamespace(succeeded=True))
        rt.invalidate_frame = Mock()
        with desktop("win32", WINDOWS_CONTROLLER), patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.run("X", {"X": {"action": "ClickKey", "key": 87}}))
            self.assertFalse(host.capabilities(host.game_backend()).touch_actions_only)


class Win32SessionStartupTests(unittest.TestCase):
    def test_the_first_foreground_wait_still_nudges_focus_with_alt_up(self):
        from navigation.controller import ForegroundInput
        from host.win32.target import Win32Target
        from task_control import task_session, _SESSIONS
        for controller_def in (None, WINDOWS_CONTROLLER):
            state = SimpleNamespace(focused=False, now=0.0)
            controller = Mock()

            def key_up(key):
                state.focused = True  # Seize's Alt key-up activates the game
                return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=True))
            controller.post_key_up.side_effect = key_up
            context = SimpleNamespace(tasker=SimpleNamespace(controller=controller, stopping=False), run_task=Mock(),
                                      run_recognition=Mock(), get_node_data=Mock(return_value={}))
            inputs = ForegroundInput.__new__(ForegroundInput)
            inputs.controller, inputs.hwnd = controller, 1
            inputs.target = Win32Target.__new__(Win32Target)
            inputs.target.handle = inputs.target.hwnd = 1
            inputs.target.user32 = Mock()
            inputs.target.user32.IsWindow.return_value = True
            inputs.target.user32.IsIconic.return_value = False
            inputs.target.user32.IsHungAppWindow.return_value = False
            inputs.target.capture_requires_foreground = False
            inputs.native = inputs.target.input()
            inputs.stopped = lambda: False
            inputs.foreground = lambda: state.focused
            inputs.held, inputs.buttons = set(), set()

            def sleep(seconds):
                state.now += max(seconds, 1e-9)
            _SESSIONS.clear()
            try:
                with self.subTest(submitted=bool(controller_def)), desktop("win32", controller_def), \
                        task_session(context, 41) as session, \
                        patch("task_control.wall_time.monotonic", side_effect=lambda: state.now), \
                        patch("task_control.wall_time.sleep", side_effect=sleep):
                    session.adapter = inputs
                    session.register(inputs)
                    session.ensure_hud = Mock(return_value=None)  # the Win32 HUD overlay needs real Windows
                    inputs.check()
            finally:
                _SESSIONS.clear()
            controller.post_key_up.assert_called_once_with(18)


class MacOSUnchangedTests(unittest.TestCase):
    def test_tiers_notices_and_keys_match_the_unsubmitted_case(self):
        entries = task_entries()
        with desktop("macos"):
            before = ([host.task_entry_supported(e) for e in entries], [host.task_start_notice(e) for e in entries],
                      [host.task_refusal_reason(e) for e in entries], host.native_key(77))
        with desktop("macos", MACOS_CONTROLLER):
            after = ([host.task_entry_supported(e) for e in entries], [host.task_start_notice(e) for e in entries],
                     [host.task_refusal_reason(e) for e in entries], host.native_key(77))
        self.assertEqual(after, before)
        self.assertEqual(after[3], 46)  # still translated


class AdbOnWindowsRefusedTests(unittest.TestCase):
    """The Windows package does not ship the Android backend yet: an Adb controller runs nothing there."""

    def test_no_task_runs_and_the_reason_says_why(self):
        probe, ran = probe_action("test.target_backend.adb_refusal")
        with desktop("win32", ADB_CONTROLLER):
            self.assertIsNone(host.game_backend())
            for entry in ("MaaNikki_Mailbox", "MaaNikki_AndroidCheck"):
                self.assertFalse(host.task_entry_supported(entry))
                self.assertIn("安卓", host.task_refusal_reason(entry))
                self.assertIsNone(host.task_start_notice(entry))
            result, context = run_probe(probe, "MaaNikki_AndroidCheck")
            self.assertFalse(result)
            message = context.run_task.call_args.args[1]["MaaNikki_Daily_Message"]["focus"]["Node.Action.Starting"]
            self.assertIn("安卓", message)
        self.assertEqual(ran, [])

    def test_no_window_services_for_a_controller_without_a_window(self):
        with desktop("win32", ADB_CONTROLLER):
            self.assertIsNone(host.overlay_backend())
            self.assertIsNone(host.launch_window_probes())
            with self.assertRaises(RuntimeError):
                host.game_close_api()
            with self.assertRaises(NavigationError):
                host.background_input_class()
            with self.assertRaises(NavigationError):
                host.connect_target(SimpleNamespace(info={"type": "adb", "adb_serial": "127.0.0.1:5555"}))

    def test_the_windows_package_leaves_the_android_backend_out(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import release
        finally:
            sys.path.remove(str(ROOT / "scripts"))
        host_dir = str(ROOT / "agent" / "host")
        self.assertIn("android", release.runtime_ignored(host_dir, ["android", "macos", "win32", "__init__.py"]))
        self.assertNotIn("win32", release.runtime_ignored(host_dir, ["android", "macos", "win32", "__init__.py"]))


class CallSiteGuardTests(unittest.TestCase):
    """platform_backend() is for desktop services only; game-target code asks game_backend()."""

    DESKTOP_ONLY = {"agent/host/__init__.py", "agent/runtime_threads.py"}

    def test_only_desktop_modules_ask_for_the_desktop_backend(self):
        offenders = []
        for path in sorted((ROOT / "agent").rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if relative in self.DESKTOP_ONLY or "/host/win32/" in relative or "/host/macos/" in relative:
                continue
            if re.search(r"\bplatform_backend\b", path.read_text(encoding="utf-8")):
                offenders.append(relative)
        self.assertEqual(offenders, [])

    def test_host_uses_the_desktop_backend_only_for_desktop_services(self):
        source = (ROOT / "agent/host/__init__.py").read_text(encoding="utf-8")
        functions = re.findall(r"^def (\w+)\(.*?(?=^def |\Z)", source, flags=re.S | re.M)
        bodies = dict(zip(functions, re.split(r"^def \w+\(", source, flags=re.M)[1:]))
        users = sorted(name for name, body in bodies.items() if re.search(r"\bplatform_backend\(\)", body))
        self.assertEqual(users, ["clipboard", "controller_backend", "credential_store", "game_backend"])


if __name__ == "__main__":
    unittest.main()
