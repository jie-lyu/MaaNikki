"""Host backend selection and Win32 target behaviour with fakes: python/python.exe -B scripts/test_host.py.

Runs on any OS: Win32 calls go to a scripted user32, never to a real window.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import os
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))

import host  # noqa: E402
from host.win32.target import Win32Target  # noqa: E402
from navigation.models import NavigationError  # noqa: E402


from contextlib import ExitStack, contextmanager  # noqa: E402


@contextmanager
def backend(name):
    """Pin the platform backend everywhere it is bound by name, so tests behave alike on every OS."""
    import daily.runtime
    import registration
    import tall_window
    with ExitStack() as stack:
        for module in (host, daily.runtime, registration, tall_window):
            stack.enter_context(patch.object(module, "platform_backend", return_value=name))
        yield


class FakeUser32:
    def __init__(self, hwnd=0x10, *, exists=True, iconic=False, foreground=0x10, hung=False):
        self.hwnd, self.exists, self.iconic, self.fg, self.hung_state = hwnd, exists, iconic, foreground, hung
        self.calls = []

    def IsWindow(self, hwnd):
        self.calls.append(("IsWindow", hwnd))
        return int(self.exists)

    def IsIconic(self, hwnd):
        self.calls.append(("IsIconic", hwnd))
        return int(self.iconic)

    def GetForegroundWindow(self):
        self.calls.append(("GetForegroundWindow",))
        return self.fg

    def IsHungAppWindow(self, hwnd):
        self.calls.append(("IsHungAppWindow", hwnd))
        return int(self.hung_state)

    def ShowWindow(self, hwnd, command):
        self.calls.append(("ShowWindow", hwnd, command))
        self.iconic = False
        return 1

    def GetWindowTextW(self, hwnd, buffer, size):
        buffer.value = "Other App"
        return len(buffer.value)


def target(user32: FakeUser32, *, capture_requires_foreground=False) -> Win32Target:
    item = Win32Target.__new__(Win32Target)
    item.handle = item.hwnd = user32.hwnd
    item.user32 = user32
    item.capture_requires_foreground = capture_requires_foreground
    return item


class CaptureBoundsTests(unittest.TestCase):
    """Click-target bounds: Win32 work-area clipping where the backend has it, the frame elsewhere."""

    def setUp(self):
        import tall_window
        self.tall_window = tall_window
        patcher = patch.object(tall_window, "platform_backend", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_without_work_area_clipping_the_frame_is_the_bound(self):
        tw = self.tall_window
        self.assertTrue(tw.is_capture_point_safe(None, [0, 0]))
        self.assertTrue(tw.is_capture_point_safe(None, [1279, 719]))
        self.assertTrue(tw.is_capture_point_safe(None, [600, 340, 80, 40]))  # rect centre
        for target in ([1280, 10], [10, 720], [-1, 10], [float("nan"), 1], [1, 2, 3], "x", None, ["a", 1]):
            with self.subTest(target=target):
                self.assertFalse(tw.is_capture_point_safe(None, target))
        self.assertEqual(tw.clamp_capture_park_point(None, [-5, 900]), [0, 719])
        self.assertEqual(tw.safe_capture_y_range(None, 720), (0, 719))

    def test_tall_frame_keeps_clicks_inside_the_16_9_base_like_win32_without_a_window(self):
        tw = self.tall_window
        tall = (tw.TALL_CAPTURE_WIDTH, tw.TALL_CAPTURE_HEIGHT)
        self.assertTrue(tw.is_capture_point_safe(None, [10, 719], capture_size=tall))
        self.assertFalse(tw.is_capture_point_safe(None, [10, 720], capture_size=tall))
        from host.win32 import window_layout
        for size in ((1280, 720), tall, (1, 1)):
            with self.subTest(size=size):
                self.assertEqual(tw._frame_bounds(size), window_layout.safe_capture_bounds(None, size))

    def test_frame_fallback_never_loads_the_window_layout(self):
        tw = self.tall_window
        with patch.object(tw, "_layout", side_effect=AssertionError("window layout loaded")):
            self.assertTrue(tw.is_capture_point_safe(None, [5, 5]))
            tw.clamp_capture_park_point(None, [5, 5])
            tw.safe_capture_y_range(None, 720)

    def test_win32_delegates_to_the_window_layout_unchanged(self):
        tw = self.tall_window
        layout = Mock()
        layout.is_capture_point_safe.return_value = "safe"
        with patch.object(tw, "platform_backend", return_value="win32"), \
                patch.dict(sys.modules, {"host.win32.window_layout": layout}), \
                patch("host.win32.window_layout", layout, create=True):
            self.assertEqual(tw.is_capture_point_safe(7, [1, 2], margin_px=4, require_work_area=True), "safe")
            tw.clamp_capture_park_point(7, [0, 0], capture_size=(1280, 720), margin_px=4)
            tw.safe_capture_y_range(7, 720, margin_px=8)
        layout.is_capture_point_safe.assert_called_once_with(7, [1, 2], margin_px=4, require_work_area=True)
        layout.clamp_capture_park_point.assert_called_once_with(7, [0, 0], capture_size=(1280, 720), margin_px=4)
        layout.safe_capture_y_range.assert_called_once_with(7, 720, margin_px=8)


class WindowsRuleTests(unittest.TestCase):
    """Windows-only rules decided by the host backend instead of by callers."""

    SEIZE = {"type": "Win32", "win32": {"mouse": "Seize", "keyboard": "SeizeWithBlockInput"}}

    def test_foreground_input_rule_is_the_win32_backends(self):
        import client_config
        with patch.object(host, "platform_backend", return_value="win32"):
            self.assertTrue(client_config.supports_foreground_inputs(self.SEIZE))
            for controller in ({"type": "Win32", "win32": {"mouse": "Seize", "keyboard": "PostMessage"}},
                               {"type": "MacOS", "macos": {}}, {"type": "Win32"}, None, "Win32"):
                with self.subTest(controller=controller):
                    self.assertFalse(client_config.supports_foreground_inputs(controller))
        with patch.object(host, "platform_backend", return_value=None):
            self.assertFalse(client_config.supports_foreground_inputs(self.SEIZE))

    @unittest.skipUnless(os.name == "nt", "the Win32 overlay backend only imports on Windows")
    def test_window_only_capture_rule(self):
        from maa.define import MaaWin32ScreencapMethodEnum as Method
        from host.win32.overlays import capture_is_window_only
        self.assertTrue(capture_is_window_only({"screencap_method": int(Method.FramePool)}))
        self.assertTrue(capture_is_window_only({"screencap_method": int(Method.PrintWindow)}))
        for info in ({"screencap_method": int(Method.GDI)}, {"screencap_method": int(Method.FramePool) | int(Method.GDI)},
                     {"screencap_method": 0}, {"screencap_method": True}, {}, None):
            with self.subTest(info=info):
                self.assertFalse(capture_is_window_only(info))

    def test_launch_probes_follow_the_launch_capability(self):
        with patch.object(host, "platform_backend", return_value=None):
            self.assertIsNone(host.launch_window_probes())
        with patch.object(host, "platform_backend", return_value="win32"):
            probes = host.launch_window_probes()
        self.assertTrue(callable(probes._is_window_hung) and callable(probes.start_program))
        hung = Mock(IsHungAppWindow=Mock(return_value=1))
        self.assertTrue(probes._is_window_hung(hung, 0x10))
        self.assertFalse(probes._is_window_hung(None, 0x10))
        self.assertFalse(probes._is_window_hung(hung, 0))

    def test_hud_needs_the_overlay_capability(self):
        import game_hud
        with patch("host.overlay_backend", return_value=None):
            self.assertIsNone(game_hud.acquire_game_hud(1, 0x10))


class OverlayThemeTests(unittest.TestCase):
    def test_theme_resolves_without_a_windows_registry(self):
        import overlay_style
        with patch.dict(sys.modules, {"winreg": None}), patch.object(overlay_style, "_SYSTEM_DARK_CACHE", None), \
                patch.object(overlay_style, "_THEME_CACHE", None):
            self.assertFalse(overlay_style._system_prefers_dark())
            self.assertIsNotNone(overlay_style.resolve_overlay_theme())


GOOD_MAC_INFO = {"type": "macos", "window_id": 4242, "screencap_method": 2, "input_method": 3, "maanikki_patch": 2,
                 "geometry_changed": False}


class FakeQuartz:
    """host.macos.quartz without the window server."""

    def __init__(self, *, pid=700, title="Infinity Nikki  ", bundle="com.papegames.infinitynikki", front=700):
        from host.macos.quartz import WindowInfo
        self.WindowInfo = WindowInfo
        self.windows = {4242: WindowInfo(4242, pid, title, (0.0, 0.0, 1920.0, 1080.0), True)}
        self.bundle, self.front, self.posted = bundle, front, []
        self.pointer = (100.0, 100.0)  # the system cursor, global points

    def window(self, window_id):
        return self.windows.get(window_id)

    def bundle_identifier(self, pid):
        return self.bundle

    def front_pid(self):
        return self.front

    def process_path(self, pid):
        return "/Applications/Other.app/Contents/MacOS/Other"

    def post_key(self, pid, keycode, *, down):
        self.posted.append((pid, keycode, down))
        return True

    def cursor(self):
        return self.pointer


class MacOSBackendTests(unittest.TestCase):
    def mac_target(self, quartz, info=GOOD_MAC_INFO, controller=None):
        from host.macos import target as module
        with patch.dict(sys.modules, {"host.macos.quartz": quartz}), patch("host.macos.quartz", quartz, create=True):
            return module.MacOSTarget(info, controller)

    def test_key_table_keeps_unmapped_apart_from_key_code_zero(self):
        from host.macos.keys import UnmappedKey, VK_TO_MACOS, translate
        self.assertEqual(translate(0x41), 0)       # A is kVK_ANSI_A = 0
        self.assertEqual(translate(0x4D), 46)      # M, measured on the device
        self.assertEqual(translate(0x57), 13)      # W, measured on the device
        self.assertEqual(translate(27), 53)        # Esc
        self.assertEqual(translate(18), 58)        # Alt -> Option
        for vk in (0, 45, 91, 255, 0x41 + 0.0, None, "A"):  # Insert and the Windows key have no Mac key
            with self.subTest(vk=vk), self.assertRaises(UnmappedKey):
                translate(vk)
        letters = [VK_TO_MACOS[vk] for vk in range(0x41, 0x5B)]
        self.assertEqual(len(set(letters)), 26)

    def test_native_key_is_unchanged_on_windows_and_translated_on_macos(self):
        with patch.object(host, "platform_backend", return_value="win32"):
            self.assertEqual([host.native_key(k) for k in (18, 77, 45, 999)], [18, 77, 45, 999])
        with patch.object(host, "platform_backend", return_value="macos"):
            self.assertEqual(host.native_key(77), 46)
            with self.assertRaisesRegex(NavigationError, "没有对应的键位"):
                host.native_key(45)

    def test_runtime_native_actions_carry_controller_key_codes(self):
        from daily.runtime import native_key_fields
        with patch.object(host, "platform_backend", return_value="macos"):
            self.assertEqual(native_key_fields("ClickKey", {"key": 27}), {"key": 53})
            self.assertEqual(native_key_fields("KeyDown", {"key": [17, 65]}), {"key": [59, 0]})
            self.assertEqual(native_key_fields("LongPressKey", {"key": 87, "duration": 5}), {"key": 13, "duration": 5})
            self.assertEqual(native_key_fields("Click", {"target": [1, 2]}), {"target": [1, 2]})
        with patch.object(host, "platform_backend", return_value="win32"):
            fields = {"key": 27}
            self.assertEqual(native_key_fields("ClickKey", fields), {"key": 27})

    def test_foreground_input_sends_controller_codes_and_tracks_key_ids(self):
        from navigation.controller import ForegroundInput
        from game_keys import Binding
        controller = Mock()
        controller.post_key_down.return_value.wait.return_value.succeeded = True
        controller.post_key_up.return_value.wait.return_value.succeeded = True
        fake_target = Mock(handle=1, capture_requires_foreground=False)
        fake_target.foreground.return_value = True
        with patch.object(host, "platform_backend", return_value="macos"), \
                patch("navigation.controller.connect_target", return_value=fake_target), \
                patch("task_control.active_session", return_value=None):
            inputs = ForegroundInput(controller, lambda: False)
            inputs._down(Binding("key", 87))
            self.assertEqual(inputs.held, {87})
            controller.post_key_down.assert_called_once_with(13)
            inputs.release()
            controller.post_key_up.assert_called_once_with(13)

    def test_controller_info_must_come_from_the_patched_unit(self):
        from host.macos.target import check_controller_info
        self.assertEqual(check_controller_info(GOOD_MAC_INFO), 4242)
        cases = {
            "官方 Mac 控制模块": {**GOOD_MAC_INFO, "maanikki_patch": None},
            "版本过旧": {**GOOD_MAC_INFO, "maanikki_patch": 1},
            "截图或输入方式": {**GOOD_MAC_INFO, "input_method": 2},
            "没有报告窗口几何状态": {**GOOD_MAC_INFO, "geometry_changed": None},
            "没有有效的 Mac 游戏窗口": {**GOOD_MAC_INFO, "window_id": 0},
        }
        for message, info in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(NavigationError, message):
                check_controller_info(info)
        for info in ({"type": "win32", "hwnd": 1}, None, {**GOOD_MAC_INFO, "window_id": "1"}):
            with self.subTest(info=info), self.assertRaises(NavigationError):
                check_controller_info(info)

    def test_target_verifies_bundle_id_and_trimmed_title(self):
        target = self.mac_target(FakeQuartz())
        self.assertEqual((target.handle, target.pid), (4242, 700))
        for quartz, message in ((FakeQuartz(bundle="com.example.other"), "不是游戏客户端"),
                                (FakeQuartz(title=""), "不是无限暖暖"),
                                (FakeQuartz(title="无限暖暖 启动器"), "不是无限暖暖")):
            with self.subTest(message=message), self.assertRaisesRegex(NavigationError, message):
                self.mac_target(quartz)
        closed = FakeQuartz()
        closed.windows.clear()
        from navigation.models import GameWindowClosed
        with self.assertRaises(GameWindowClosed):
            self.mac_target(closed)

    def test_target_focus_and_existence(self):
        quartz = FakeQuartz()
        target = self.mac_target(quartz)
        self.assertTrue(target.exists() and target.foreground() and not target.minimized())
        quartz.front = 1
        self.assertFalse(target.foreground())
        self.assertFalse(target.bring_to_foreground(Mock()))  # never activates the game itself
        quartz.front = 700
        quartz.windows[4242] = quartz.WindowInfo(4242, 700, "Infinity Nikki", (0.0, 0.0, 1920.0, 1080.0), False)
        self.assertTrue(target.minimized() and not target.foreground())
        quartz.windows[4242] = quartz.WindowInfo(4242, 901, "Infinity Nikki", (0.0, 0.0, 1920.0, 1080.0), True)
        self.assertFalse(target.exists())  # the id now belongs to another process

    def test_input_releases_by_posting_to_the_game_process(self):
        quartz = FakeQuartz()
        target = self.mac_target(quartz)
        native = target.input()
        native.force_key_up(87)
        native.force_key_up(45)  # unmapped: nothing to release
        self.assertEqual(quartz.posted, [(700, 13, False)])
        self.assertFalse(native.press_mouse(0) or native.post_window_key(87, down=True) or native.relative_move(1, 0))
        with self.assertRaisesRegex(NavigationError, "路线录制"):
            native.physical_keys()

    def test_scene_buttons_go_through_the_unit_from_patch_level_3(self):
        import inspect
        from unittest.mock import create_autospec
        from host.macos.target import SCENE_PRESS_PRESSURE

        class Controller:
            """The maafw 5.16.1 binding's signatures (checked against maa.controller where it imports)."""
            def post_touch_down(self, x, y, contact=0, pressure=1): ...
            def post_touch_up(self, contact=0): ...

        try:
            from maa.controller import Controller as Binding
        except (ImportError, OSError):  # Windows CI's packaged runtime has no maa/bin
            Binding = None
        if Binding is not None:
            for method in ("post_touch_down", "post_touch_up"):
                self.assertEqual(list(inspect.signature(getattr(Binding, method)).parameters),
                                 list(inspect.signature(getattr(Controller, method)).parameters))

        def bound(method, call):
            # Bind against the binding's signature: a positional slip would press another button.
            return inspect.signature(getattr(Controller, method)).bind(None, *call.args, **call.kwargs).arguments

        for button in (0, 1, 2):
            with self.subTest(button=button):
                controller = create_autospec(Controller, instance=True)
                controller.post_touch_down.return_value.wait.return_value.succeeded = True
                native = self.mac_target(FakeQuartz(), {**GOOD_MAC_INFO, "maanikki_patch": 3}, controller).input()
                self.assertTrue(native.press_mouse(button))
                down = bound("post_touch_down", controller.post_touch_down.call_args)
                self.assertEqual((down["contact"], down["pressure"]), (button, SCENE_PRESS_PRESSURE))
                native.force_mouse_up(button)
                self.assertEqual(bound("post_touch_up", controller.post_touch_up.call_args)["contact"], button)
        self.assertFalse(native.press_mouse(3))  # the extra buttons have no unit contact
        older = self.mac_target(FakeQuartz(), GOOD_MAC_INFO, controller).input()
        self.assertFalse(older.press_mouse(1))  # a patch-2 unit would hover-move and turn the camera
        self.assertEqual(controller.post_touch_down.call_count, 1)

    def test_alt_before_camera_turns_only_where_the_backend_requires_it(self):
        from navigation.engine import Navigator
        def run(name, *, window_only=False, succeeded=True, reset=False):
            controller = Mock()
            controller.post_click_key.return_value.wait.return_value.succeeded = succeeded
            nav = SimpleNamespace(_cursor_released=False, inputs=SimpleNamespace(controller=controller, window_only=window_only))
            with backend(name):
                Navigator._ensure_cursor_released(nav)
                Navigator._ensure_cursor_released(nav)  # once per release state
                if reset:  # a pause or a point action clears the state
                    nav._cursor_released = False
                    Navigator._ensure_cursor_released(nav)
            return nav._cursor_released, [call.args for call in controller.post_click_key.call_args_list]
        # Windows, unchanged: Alt once, again after a reset, never for window-only input, a failure raises.
        self.assertEqual(run("win32"), (True, [(18,)]))
        self.assertEqual(run("win32", reset=True), (True, [(18,), (18,)]))
        self.assertEqual(run("win32", window_only=True), (False, []))
        with self.assertRaisesRegex(NavigationError, "光标模式"):
            run("win32", succeeded=False)
        # macOS: no key at all, also after a reset; the next camera move is not swallowed.
        self.assertEqual(run("macos"), (True, []))
        self.assertEqual(run("macos", reset=True), (True, []))
        self.assertTrue(host.capabilities("win32").requires_alt_before_camera)
        self.assertFalse(host.capabilities("macos").requires_alt_before_camera)

    def test_macos_capabilities_and_saved_controller_rule(self):
        capabilities = host.capabilities("macos")
        self.assertEqual(capabilities.backend, "macos")
        self.assertTrue(capabilities.foreground_input)
        self.assertTrue(capabilities.clipboard)  # NSPasteboard text for redeem codes
        self.assertFalse(any((capabilities.background_input, capabilities.overlay, capabilities.tall_window,
                              capabilities.work_area_bounds, capabilities.launch_game, capabilities.close_game,
                              capabilities.credential_store)))
        import client_config
        mac = {"type": "MacOS", "macos": {"screencap": "DisplayWindow", "input": "Hybrid"}}
        with patch.object(host, "platform_backend", return_value="macos"):
            self.assertTrue(client_config.supports_foreground_inputs(mac))
            for controller in ({"type": "MacOS", "macos": {"screencap": "ScreenCaptureKit", "input": "PostToPid"}},
                               {"type": "Win32", "win32": {"mouse": "Seize", "keyboard": "Seize"}}, None):
                with self.subTest(controller=controller):
                    self.assertFalse(client_config.supports_foreground_inputs(controller))


class MacOSPreviewPathTests(unittest.TestCase):
    """The daily runtime and ForegroundInput paths the Mac preview depends on, over fakes."""

    def runtime(self):
        from daily.runtime import Runtime
        context = SimpleNamespace(
            tasker=SimpleNamespace(stopping=False, controller=SimpleNamespace()),
            get_node_data=Mock(return_value={}),
            run_action=Mock(return_value=SimpleNamespace(success=True)),
        )
        rt = Runtime(context)
        rt._frame_validated = True
        rt.pause = Mock(return_value=True)
        rt.log = Mock()
        return rt

    def dispatched(self, rt):
        return [call.kwargs["pipeline_override"]["MaaNikki_Daily_Action"] for call in rt.context.run_action.call_args_list]

    def claim_click(self, name):
        from daily.mailbox import Mailbox
        rt = self.runtime()
        mailbox = Mailbox(rt)
        mailbox.button_state = Mock(return_value=("available", [300, 670, 128, 25], object()))
        rt.image_current = Mock(return_value=True)
        if name == "win32":
            rt.ui.unhover = Mock(return_value=True)  # its own cursor parking is covered elsewhere
        with backend(name):
            self.assertTrue(mailbox.click_claim())
        return self.dispatched(rt)

    def test_mailbox_claim_on_macos_sends_one_centred_click_without_hover(self):
        self.assertEqual(self.claim_click("macos"), [{"action": "Click", "target": [364, 682]}])

    def scrolled(self, name):
        rt = self.runtime()
        with backend(name):
            self.assertTrue(rt.ui.scroll([100, 100, 200, 200], -480))
        return self.dispatched(rt)

    def test_scroll_on_macos_leaves_the_hover_to_the_control_unit(self):
        self.assertEqual(self.scrolled("macos"), [{"action": "Scroll", "target": [292, 292], "dx": 0, "dy": -480}])

    def test_scroll_on_windows_still_moves_into_the_list_first(self):
        self.assertEqual(self.scrolled("win32"), [{"action": "TouchMove", "target": [292, 292]},
                                                  {"action": "Scroll", "target": [292, 292], "dx": 0, "dy": -480}])

    def test_mailbox_claim_on_windows_still_hovers_first(self):
        actions = self.claim_click("win32")
        self.assertEqual(actions[:2], [{"action": "TouchMove", "target": [364, 682]},
                                       {"action": "Click", "target": [364, 682]}])

    def foreground_input(self, *, release_ignores_focus, foreground):
        from navigation.controller import ForegroundInput
        controller = Mock()
        controller.post_key_down.return_value.wait.return_value.succeeded = True
        controller.post_key_up.return_value.wait.return_value.succeeded = True
        target = Mock(handle=1, capture_requires_foreground=False, release_ignores_focus=release_ignores_focus)
        target.foreground.return_value = foreground
        with patch("navigation.controller.connect_target", return_value=target), \
                patch("task_control.active_session", return_value=None):
            inputs = ForegroundInput(controller, lambda: False)
        return inputs, controller, target

    def test_macos_releases_through_the_controller_after_focus_loss(self):
        from game_keys import Binding
        with backend("macos"):
            inputs, controller, target = self.foreground_input(release_ignores_focus=True, foreground=True)
            inputs._down(Binding("key", 87))
            target.foreground.return_value = False
            inputs.release()
            controller.post_key_up.assert_called_once_with(13)
            target.input.return_value.force_key_up.assert_not_called()
            # A release the controller could not deliver goes straight to the game; the next press
            # still goes through the controller (the unit posts it again even if it counts W as down).
            target.foreground.return_value = True
            inputs._down(Binding("key", 87))
            controller.post_key_up.return_value.wait.return_value.succeeded = False
            inputs.release()
            target.input.return_value.force_key_up.assert_called_once_with(87)
            inputs._down(Binding("key", 87))
            self.assertEqual(controller.post_key_down.call_count, 3)

    def test_windows_still_releases_directly_after_focus_loss(self):
        from game_keys import Binding
        with backend("win32"):
            inputs, controller, target = self.foreground_input(release_ignores_focus=False, foreground=True)
            inputs._down(Binding("key", 87))
            target.foreground.return_value = False
            inputs.release()
            controller.post_key_up.assert_not_called()
            target.input.return_value.force_key_up.assert_called_once_with(87)

    def test_unmapped_key_is_never_recorded_as_held(self):
        from game_keys import Binding
        with backend("macos"):
            inputs, controller, _ = self.foreground_input(release_ignores_focus=True, foreground=True)
            for _ in range(2):
                with self.assertRaisesRegex(NavigationError, "没有对应的键位"):
                    inputs._down(Binding("key", 255))
            self.assertEqual(inputs.held, set())
            controller.post_key_down.assert_not_called()

    def test_wrong_frame_size_skips_window_resizing_without_the_capability(self):
        import numpy as np
        from game_view import GameFrameSizeError
        rt = self.runtime()
        job = Mock(succeeded=True)
        job.get.return_value = np.zeros((700, 1280, 3), dtype=np.uint8)
        job.wait.return_value = job
        rt.context.tasker.controller = rt.controller = Mock()
        rt.controller.post_screencap.return_value = job
        with backend("macos"), \
                patch("tall_window.ensure_16_9_window", side_effect=AssertionError("resized")):
            with self.assertRaises(GameFrameSizeError):
                rt.capture()


class TaskAllowlistTests(unittest.TestCase):
    def test_windows_runs_every_task_and_macos_only_the_preview_allowlist(self):
        import glob
        entries = [task["entry"] for path in glob.glob(str(ROOT / "tasks/*.json"))
                   for task in json.loads(Path(path).read_text(encoding="utf-8")).get("task", [])]
        with patch.dict(os.environ), backend("win32"):
            os.environ.pop("MAANIKKI_MACOS_EXTRA_TASKS", None)
            self.assertTrue(all(host.task_entry_supported(entry) for entry in entries + ["MaaNikki_GameKeysRead", None]))
        with patch.dict(os.environ), backend("macos"):
            os.environ.pop("MAANIKKI_MACOS_EXTRA_TASKS", None)
            verified = {"MaaNikki_Mailbox", "MaaNikki_WeeklyFreeGifts", "MaaNikki_MonthlyPass",
                        "MaaNikki_CollectStarlightCrystals"}
            experimental = {"MaaNikki_Home", "MaaNikki_Xinghai", "MaaNikki_Zhaoxi", "MaaNikki_Crown",
                            "MaaNikki_RealmChallenge", "MaaNikki_Dig", "MaaNikki_RedeemCodes", "MaaNikki_OpenWorldCollect",
                            "MaaNikki_RouteRun", "MaaNikki_MinigameBling", "MaaNikki_PersistentAbilities"}
            self.assertEqual({entry for entry in entries if host.task_entry_supported(entry)}, verified | experimental)
            from host.macos import task_tier, task_tiers
            self.assertEqual({e for e in verified | experimental if task_tier(e) == "verified"}, verified)
            for excluded in ("MaaNikki_MonthlyCrystalExchange", "MaaNikki_RouteTest", "MaaNikki_CloseGame",
                             "MaaNikki_StartGame", "MaaNikki_SubtaskTest_xinghai_chat"):
                self.assertFalse(host.task_entry_supported(excluded), excluded)
                self.assertIn(excluded, task_tiers()[2])  # with a reason for the user
            # every task file entry is classified somewhere
            classified = verified | experimental | set(task_tiers()[2])
            self.assertEqual([e for e in entries if e not in classified], [])
            self.assertFalse(host.task_entry_supported("MaaNikki_GameKeysRead"))
            self.assertFalse(host.task_entry_supported(None))

    def test_the_tier_list_is_parsed_strictly(self):
        from host.macos import parse_task_tiers
        self.assertEqual(parse_task_tiers({"entries": ["A"]}), (frozenset({"A"}), frozenset(), {}))  # older list
        self.assertEqual(parse_task_tiers({"entries": ["A"], "experimental": ["B"], "excluded": {"C": "why"}}),
                         (frozenset({"A"}), frozenset({"B"}), {"C": "why"}))
        for bad in (None, [], {}, {"entries": "A"}, {"entries": ["A", 1]}, {"entries": ["A", "A"]}, {"entries": [""]},
                    {"entries": ["A"], "experimental": "B"}, {"entries": ["A"], "experimental": ["A"]},
                    {"entries": ["A"], "excluded": {"A": "why"}}, {"entries": ["A"], "excluded": {"B": ""}},
                    {"entries": ["A"], "excluded": ["B"]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_task_tiers(bad)

    def test_the_list_file_refuses_duplicate_keys_and_nan(self):
        from host.macos import load_task_list, parse_task_tiers
        for raw in ('{"entries":["A"],"notes":NaN}', '{"entries":["A"],"excluded":{"A":"why"},"excluded":{}}',
                    '{"entries":["A"],"entries":["B"]}', '{"entries":["A"],"excluded":{"B":"x","B":"y"}}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_task_tiers(load_task_list(raw))
        self.assertEqual(parse_task_tiers(load_task_list('{"entries":["A"]}'))[0], frozenset({"A"}))

    def test_a_malformed_list_refuses_everything_on_macos(self):
        with patch.dict(os.environ), backend("macos"), \
                patch("host.macos.task_tiers", side_effect=ValueError("bad")):
            os.environ.pop("MAANIKKI_MACOS_EXTRA_TASKS", None)
            self.assertFalse(host.task_entry_supported("MaaNikki_Mailbox"))
            self.assertFalse(host.task_entry_supported("MaaNikki_Home"))

    def test_background_mode_falls_back_to_pause_without_window_input(self):
        from task_control import focus_lost_mode
        with backend("win32"):
            self.assertEqual([focus_lost_mode(m) for m in ("background", "return", "pause", "x", None)],
                             ["background", "return", "pause", "pause", "pause"])
        with backend("macos"):
            self.assertEqual([focus_lost_mode(m) for m in ("background", "return", "pause")],
                             ["pause", "return", "pause"])

    def test_runtime_background_mode_follows_the_capability(self):
        from daily.runtime import Runtime
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False, controller=SimpleNamespace()))
        rt = Runtime(context)
        with patch("daily.runtime.active_session", return_value=None), \
                patch("daily.runtime.parameters", return_value={"mode": "background"}):
            with backend("win32"):
                self.assertTrue(rt.background_mode())
            with backend("macos"):
                self.assertFalse(rt.background_mode())

    def test_dev_switch_adds_exact_entries_on_macos_only(self):
        with patch.dict(os.environ, {"MAANIKKI_MACOS_EXTRA_TASKS": " MaaNikki_Dig ,, MaaNikki_MonthlyPass"}):
            with backend("macos"):
                self.assertTrue(host.task_entry_supported("MaaNikki_Dig"))
                self.assertTrue(host.task_entry_supported("MaaNikki_MonthlyPass"))
                self.assertTrue(host.task_entry_supported("MaaNikki_Mailbox"))
                self.assertFalse(host.task_entry_supported("MaaNikki_Fishing"))
                self.assertFalse(host.task_entry_supported(None))
                self.assertFalse(host.task_entry_supported(""))
            with backend("win32"):
                self.assertTrue(host.task_entry_supported("MaaNikki_Fishing"))  # unchanged: every task

    def test_unsupported_entry_is_refused_before_any_task_code(self):
        import registration
        ran = []
        token = registration._LOCAL_REGISTRATION.set(True)
        try:
            @registration.agent_action("test.allowlist_gate")
            class Probe:
                def run(self, context, argv):
                    ran.append(argv.node_name)
                    return True
        finally:
            registration._LOCAL_REGISTRATION.reset(token)
        context = Mock()
        argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=7, entry="MaaNikki_Fishing"), node_name="MaaNikki_Fishing")
        with backend("macos"):
            self.assertFalse(Probe().run(context, argv))
        self.assertEqual(ran, [])
        message = context.run_task.call_args.args[1]["MaaNikki_Daily_Message"]["focus"]["Node.Action.Starting"]
        self.assertIn("MaaNikki_Fishing", message)
        self.assertIn("不在当前系统开放", message)
        argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=7, entry=None), node_name="X")
        with backend("macos"):
            self.assertFalse(Probe().run(context, argv))
        self.assertEqual(ran, [])

    def test_an_experimental_task_says_so_in_its_own_log_once_before_its_first_action(self):
        import registration
        from contextlib import nullcontext
        events = []
        token = registration._LOCAL_REGISTRATION.set(True)
        try:
            @registration.agent_action("test.start_notice")
            class Probe:
                def run(self, context, argv):
                    events.append(("run", argv.task_detail.task_id))
                    return True
        finally:
            registration._LOCAL_REGISTRATION.reset(token)

        def context():
            # Each task's messages go through the context MaaFramework hands that task's actions,
            # so a scheduled or background instance logs its own notice.
            ctx = Mock()
            ctx.run_task.side_effect = lambda entry, override: events.append(
                ("notice", override["MaaNikki_Daily_Message"]["focus"]["Node.Action.Starting"]))
            return ctx

        def run(ctx, task_id, entry):
            argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=task_id, entry=entry), node_name=entry)
            return Probe().run(ctx, argv)

        notes = json.loads((ROOT / "agent/host/macos/preview_tasks.json").read_text(encoding="utf-8"))["notes"]
        with patch.dict(os.environ), patch("registration.task_session", return_value=nullcontext(None)), \
                patch.object(registration, "_START_NOTICES", registration.deque(maxlen=256)):
            os.environ.pop("MAANIKKI_MACOS_EXTRA_TASKS", None)
            with backend("macos"):
                dig = context()
                self.assertTrue(run(dig, 9001, "MaaNikki_Dig"))
                self.assertTrue(run(dig, 9001, "MaaNikki_Dig"))  # a later action of the same task
                self.assertEqual(events, [
                    ("notice", f"本任务尚未在 Mac 上验证（试用）：{notes['MaaNikki_Dig']}。"),
                    ("run", 9001), ("run", 9001)])
                events.clear()
                self.assertTrue(run(context(), 9002, "MaaNikki_Dig"))  # the next submission warns again
                self.assertEqual([kind for kind, _ in events], ["notice", "run"])
                events.clear()
                self.assertTrue(run(context(), 9003, "MaaNikki_Mailbox"))  # verified: no notice
                self.assertEqual(events, [("run", 9003)])
                events.clear()
                failing = Mock()
                failing.run_task.side_effect = RuntimeError("tasker gone")
                with patch("registration.traceback.print_exc"):
                    self.assertTrue(run(failing, 9004, "MaaNikki_Home"))  # a failed notice never blocks the task
                self.assertEqual(events, [("run", 9004)])
                events.clear()
                with patch("host.macos._task_list", side_effect=ValueError("bad")):
                    self.assertIsNone(host.task_start_notice("MaaNikki_Home"))
            with backend("win32"):
                win = context()
                self.assertTrue(run(win, 9005, "MaaNikki_Dig"))
                self.assertEqual(events, [("run", 9005)])  # Windows: unchanged, nothing extra in the log
                win.run_task.assert_not_called()
                self.assertIsNone(host.task_start_notice("MaaNikki_Dig"))


class GeometryLatchTests(unittest.TestCase):
    def session(self, info):
        from task_control import ForegroundSession as TaskSession
        session = TaskSession.__new__(TaskSession)
        session._frame_failure = None
        session.frame_validated = False
        session.inputs = []
        session.generation = 0
        session.evidence = Mock()
        session.notify = Mock()
        session.context = Mock()
        session._controller = Mock(info=info)
        return session

    def test_macos_geometry_change_stops_the_queue_like_a_bad_frame(self):
        import numpy as np
        from task_control import TaskStopped
        session = self.session({"type": "macos", "geometry_changed": True})
        with backend("macos"), patch("task_control.write_message"):
            with self.assertRaisesRegex(TaskStopped, "重新连接"):
                session.validate_frame(np.zeros((720, 1280, 3), dtype=np.uint8))
        session.context.tasker.post_stop.assert_called_once()
        session.evidence.save.assert_called_once()
        unchanged = self.session({"type": "macos", "geometry_changed": False})
        with backend("macos"):
            unchanged.validate_frame(np.zeros((720, 1280, 3), dtype=np.uint8))
        self.assertTrue(unchanged.frame_validated)

    def test_unreadable_latch_pauses_instead_of_passing(self):
        import numpy as np
        from task_control import TaskStopped
        for info in ({"type": "macos"}, {"type": "macos", "geometry_changed": "no"}, None):
            with self.subTest(info=info):
                session = self.session(info)
                with backend("macos"), patch("task_control.write_message"):
                    with self.assertRaisesRegex(TaskStopped, "无法读取"):
                        session.validate_frame(np.zeros((720, 1280, 3), dtype=np.uint8))
        broken = Mock()
        type(broken).info = property(lambda self: (_ for _ in ()).throw(RuntimeError("gone")))
        with backend("macos"):
            self.assertIn("无法读取", host.connection_geometry_problem(lambda: broken))

    def test_windows_never_looks_up_the_controller(self):
        get = Mock()
        with backend("win32"):
            self.assertIsNone(host.connection_geometry_problem(get))
        get.assert_not_called()


class SelectionTests(unittest.TestCase):
    def test_backend_follows_platform_and_controller_type(self):
        with patch.object(host.os, "name", "nt"):
            self.assertEqual(host.platform_backend(), "win32")
            self.assertEqual(host.controller_backend({"type": "win32", "hwnd": 1}), "win32")
            self.assertIsNone(host.controller_backend({"type": "macos"}))
            self.assertIsNone(host.controller_backend(None))
        with patch.object(host.os, "name", "posix"), patch.object(host.sys, "platform", "linux"):
            self.assertIsNone(host.platform_backend())
            self.assertIsNone(host.controller_backend({"type": "win32", "hwnd": 1}))
            self.assertEqual(host.capabilities(host.platform_backend()), host.UNSUPPORTED)
            self.assertIsNone(host.overlay_backend())
        with patch.object(host.os, "name", "posix"), patch.object(host.sys, "platform", "darwin"):
            self.assertEqual(host.platform_backend(), "macos")
            self.assertEqual(host.controller_backend({"type": "macos", "window_id": 1}), "macos")
            self.assertIsNone(host.controller_backend({"type": "win32", "hwnd": 1}))
            self.assertIsNone(host.overlay_backend())

    def test_optional_adapters_follow_capabilities(self):
        with patch.object(host, "platform_backend", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "不支持关闭游戏"):
                host.game_close_api()
        with patch.object(host, "platform_backend", return_value="win32"), patch("host.win32.close.Win32Api") as binding:
            self.assertIs(host.game_close_api(), binding.return_value)
        with patch.object(host, "platform_backend", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "剪贴板"):
                host.clipboard()
            with self.assertRaisesRegex(RuntimeError, "登录信息"):
                host.credential_store()
        with patch.object(host, "platform_backend", return_value="win32"):
            self.assertTrue(callable(host.clipboard().get_clipboard_text))
            self.assertTrue(callable(host.clipboard().set_clipboard_text))
            self.assertEqual(host.clipboard().__name__, "host.win32.clipboard")
            self.assertTrue(callable(host.credential_store().credentials))
        with patch.object(host, "platform_backend", return_value="macos"):
            # Importing the module loads nothing native; the runtime is bound on first use.
            self.assertEqual(host.clipboard().__name__, "host.macos.clipboard")
            with self.assertRaisesRegex(RuntimeError, "登录信息"):
                host.credential_store()

    @unittest.skipUnless(sys.platform == "darwin", "NSPasteboard exists only on macOS")
    def test_macos_clipboard_round_trips_text_on_a_private_pasteboard(self):
        # A uniquely named pasteboard, released afterwards: the user's clipboard is never touched.
        import uuid
        from host.macos import clipboard as module
        rt = module._runtime()
        cf = rt["cf"]
        name = f"com.maanikki.test.{uuid.uuid4()}".encode()
        ref = cf.CFStringCreateWithBytes(None, name, len(name), module._UTF8, False)
        send, sel = rt["named"]
        board = send(rt["class"], sel, ref)
        self.assertTrue(board)
        try:
            with patch.object(module, "_board", lambda rt: board):
                self.assertEqual(module.get_clipboard_text(), (False, ""))  # no text: nothing to restore
                for text in ("ABCD1234", "兑换码 测试 ✓😀", "left\0right", ""):
                    self.assertTrue(module.set_clipboard_text(text))
                    self.assertEqual(module.get_clipboard_text(), (True, text))
        finally:
            send, sel = rt["release_globally"]
            send(board, sel)
            cf.CFRelease(ref)

    def test_task_code_reaches_clipboard_and_credentials_through_the_host(self):
        from daily import redeem_codes
        from map_mask import pearpal
        module = SimpleNamespace(get_clipboard_text=Mock(return_value=(True, "x")),
                                 set_clipboard_text=Mock(return_value=True),
                                 credentials=Mock(return_value={"token": "t", "openid": "o"}))
        with patch("host.clipboard", return_value=module), patch("host.credential_store", return_value=module):
            self.assertEqual(redeem_codes.get_clipboard_text(), (True, "x"))
            self.assertTrue(redeem_codes.set_clipboard_text("CODE"))
            self.assertEqual(pearpal.credentials("login.bin"), {"token": "t", "openid": "o"})
        module.set_clipboard_text.assert_called_once_with("CODE")
        module.credentials.assert_called_once_with("login.bin")

    def test_process_helpers_are_windows_only_and_delegate_unchanged(self):
        import runtime_threads
        with patch.object(runtime_threads, "platform_backend", return_value=None), \
                patch("host.win32.process.start_parent_exit_watchdog") as watchdog, \
                patch("host.win32.process.cap_loaded_maafw_opencv") as cap:
            self.assertFalse(runtime_threads.start_parent_exit_watchdog(print))
            self.assertFalse(runtime_threads.cap_loaded_maafw_opencv(2))
            watchdog.assert_not_called()
            cap.assert_not_called()
        with patch.object(runtime_threads, "platform_backend", return_value="win32"), \
                patch("host.win32.process.start_parent_exit_watchdog", return_value=True) as watchdog, \
                patch("host.win32.process.cap_loaded_maafw_opencv", return_value=True) as cap:
            self.assertTrue(runtime_threads.start_parent_exit_watchdog(print))
            self.assertTrue(runtime_threads.cap_loaded_maafw_opencv(3))
        watchdog.assert_called_once_with(print)
        cap.assert_called_once_with(3)

    def test_win32_capabilities(self):
        capabilities = host.capabilities("win32")
        self.assertEqual(capabilities.backend, "win32")
        self.assertTrue(capabilities.foreground_input and capabilities.overlay and capabilities.tall_window)
        self.assertTrue(capabilities.work_area_bounds)

    def test_non_windows_host_rejects_with_the_previous_message(self):
        controller = Mock()
        with patch.object(host, "platform_backend", return_value=None):
            with self.assertRaisesRegex(NavigationError, "前台导航仅支持 Windows 游戏窗口。"):
                host.connect_target(controller)

    def test_unreadable_info_and_invalid_window(self):
        class Broken:
            @property
            def info(self):
                raise RuntimeError("closed")
        with patch.object(host, "platform_backend", return_value="win32"):
            with self.assertRaisesRegex(NavigationError, "无法读取当前连接的游戏窗口"):
                host.connect_target(Broken())
            for info in ({"type": "win32", "hwnd": 0}, {"type": "adb"}, {"type": "win32", "hwnd": "1"}, None):
                with self.subTest(info=info), self.assertRaisesRegex(NavigationError, "没有有效的 Windows 游戏窗口"):
                    host.connect_target(SimpleNamespace(info=info))


class Win32TargetTests(unittest.TestCase):
    def test_focus_queries(self):
        self.assertTrue(target(FakeUser32()).foreground())
        self.assertFalse(target(FakeUser32(foreground=0x20)).foreground())
        self.assertFalse(target(FakeUser32(iconic=True)).foreground())
        self.assertFalse(target(FakeUser32(exists=False)).foreground())
        self.assertTrue(target(FakeUser32(hung=True)).hung())
        self.assertFalse(target(FakeUser32(exists=False, hung=True)).hung())
        self.assertTrue(target(FakeUser32(iconic=True)).minimized())
        self.assertFalse(target(FakeUser32(exists=False)).exists())

    def test_focus_details(self):
        details = target(FakeUser32(foreground=0x20)).focus_details()
        self.assertEqual(details, {"game_hwnd": 0x10, "foreground_hwnd": 0x20, "foreground_title": "Other App"})

    def test_bring_to_foreground_restores_then_releases_alt_through_maa(self):
        user32 = FakeUser32(iconic=True, foreground=0x20)
        controller = Mock()

        def key_up(key):
            user32.fg = 0x10  # the Alt key-up through Maa activates the game
            return Mock()
        controller.post_key_up.side_effect = key_up
        self.assertTrue(target(user32).bring_to_foreground(controller))
        self.assertIn(("ShowWindow", 0x10, 9), user32.calls)
        controller.post_key_up.assert_called_once_with(18)

    def test_bring_to_foreground_is_a_noop_when_focused(self):
        controller = Mock()
        self.assertTrue(target(FakeUser32()).bring_to_foreground(controller))
        controller.post_key_up.assert_not_called()


class Win32InputTests(unittest.TestCase):
    def native(self):
        user32 = Mock()
        user32.MapVirtualKeyW.return_value = 0x11  # scan code for W, not extended
        from host.win32.input import Win32Input
        return Win32Input(SimpleNamespace(user32=user32, hwnd=0x10)), user32

    def test_force_releases_bypass_the_agent_channel(self):
        native, user32 = self.native()
        native.force_key_up(87)
        native.force_key_up(38)  # arrow keys are extended
        native.force_mouse_up(4)
        self.assertEqual(user32.keybd_event.call_args_list[0].args, (87, 0, 0x0002, 0))
        self.assertEqual(user32.keybd_event.call_args_list[1].args, (38, 0, 0x0003, 0))
        user32.mouse_event.assert_called_once_with(0x0100, 0, 0, 2, 0)

    def test_press_and_relative_move_use_send_input_without_moving_the_cursor(self):
        native, user32 = self.native()
        with patch("host.win32.input.send_mouse", return_value=True) as send:
            self.assertTrue(native.press_mouse(3))
            self.assertTrue(native.relative_move(5, -2))
        self.assertEqual(send.call_args_list[0].args, (user32, 0x0080))
        self.assertEqual(send.call_args_list[0].kwargs, {"data": 1})
        self.assertEqual(send.call_args_list[1].kwargs, {"dx": 5, "dy": -2})

    def test_window_keys_post_matching_down_and_up(self):
        native, user32 = self.native()
        user32.PostMessageW.return_value = 1
        self.assertTrue(native.post_window_key(87, down=True))
        self.assertTrue(native.post_window_key(87, down=False))
        self.assertTrue(native.post_window_key(18, down=True))  # Alt uses WM_SYSKEYDOWN
        down, up, alt = (call.args for call in user32.PostMessageW.call_args_list)
        self.assertEqual(down, (0x10, 0x0100, 87, 1 | (0x11 << 16)))
        self.assertEqual(up, (0x10, 0x0101, 87, 1 | (0x11 << 16) | 0xC0000000))
        self.assertEqual(alt[1], 0x0104)


    def test_physical_keys_read_the_high_bit_of_async_key_state(self):
        from host.win32.input import Win32PhysicalKeys
        user32 = Mock()
        user32.GetAsyncKeyState.side_effect = lambda key: {87: -32768, 32: 1}.get(key, 0)
        with patch("host.win32.input.ctypes.WinDLL", create=True, return_value=user32):
            keys = Win32PhysicalKeys()
        self.assertTrue(keys.down(87))
        self.assertFalse(keys.down(32))  # only the "pressed since last call" bit
        self.assertFalse(keys.down(65))

    def test_mouse_guard_lives_in_the_backend(self):
        import importlib.util
        from host.win32 import mouse_guard
        self.assertTrue(callable(mouse_guard.guarded_borrowed_cursor))
        self.assertTrue(callable(mouse_guard.yield_for_user_mouse))
        self.assertIsNone(importlib.util.find_spec("mouse_guard"))

    def test_route_recorder_polls_keys_through_the_input_backend(self):
        from daily.route_studio import KeyWatcher
        inputs = SimpleNamespace(native=Mock())
        watcher = KeyWatcher(inputs, {87: "key:forward"})
        self.assertIs(watcher.keys, inputs.native.physical_keys.return_value)


class LaunchTests(unittest.TestCase):
    def test_start_game_window_helpers_live_in_the_backend(self):
        import start_game
        from host.win32 import launch, window_layout
        for name in ("LauncherWindows", "ensure_unminimized", "focus_window", "game_window"):
            self.assertIs(getattr(start_game, name), getattr(launch, name))
        self.assertIs(window_layout.game_window, launch.game_window)

    def test_ensure_unminimized_restores_without_activating_first(self):
        from host.win32 import launch
        user32 = Mock()
        user32.IsWindow.return_value = True
        state = {"iconic": True}
        user32.IsIconic.side_effect = lambda hwnd: state["iconic"]

        def show(hwnd, command):
            if command == 4:  # SW_SHOWNOACTIVATE
                state["iconic"] = False
        user32.ShowWindow.side_effect = show
        with patch("host.win32.launch.ctypes.WinDLL", create=True, return_value=user32), \
                patch("host.win32.launch.time.sleep"):
            self.assertTrue(launch.ensure_unminimized(0x10))
        self.assertEqual([call.args for call in user32.ShowWindow.call_args_list], [(0x10, 4)])

    def test_launcher_background_click_maps_capture_to_client_and_posts_messages(self):
        from contextlib import contextmanager
        from host.win32 import launch
        user32 = Mock()
        user32.IsWindow.return_value, user32.IsIconic.return_value = 1, 0

        def client_rect(hwnd, ref):
            ref._obj.left, ref._obj.top, ref._obj.right, ref._obj.bottom = 0, 0, 2000, 1000
            return 1

        def to_screen(hwnd, ref):
            ref._obj.x += 300
            ref._obj.y += 200
            return 1
        user32.GetClientRect.side_effect = client_rect
        user32.ClientToScreen.side_effect = to_screen
        user32.SendMessageTimeoutW.return_value = 1
        leases = []

        @contextmanager
        def lease(x, y, **kwargs):
            leases.append((x, y, kwargs["clip_rect"]))
            yield True
        windows = launch.LauncherWindows.__new__(launch.LauncherWindows)
        windows.user32 = user32
        with patch("host.win32.mouse_guard.guarded_borrowed_cursor", lease), patch("host.win32.launch.time.sleep"):
            self.assertTrue(windows.background_click(0x10, (500, 1000, 3), (100, 50, 20, 10)))
        lparam = (110 << 16) | 220  # centre (110, 55) in a 1000x500 capture -> (220, 110) client pixels
        messages = [call.args[1:4] for call in user32.SendMessageTimeoutW.call_args_list]
        self.assertEqual(messages, [(0x0006, 1, 0), (0x0200, 0, lparam), (0x0201, 1, lparam), (0x0202, 0, lparam)])
        self.assertEqual(leases, [(520, 310, (520, 310, 521, 311))])
        user32.IsIconic.return_value = 1
        self.assertFalse(windows.background_click(0x10, (500, 1000, 3), (100, 50, 20, 10)))

    def test_startup_probes_without_a_window_binding(self):
        from host.win32 import launch
        if os.name != "nt":
            self.assertIsNone(launch._startup_user32())
        self.assertFalse(launch._is_window_process_alive(None, 0x10))
        self.assertFalse(launch._is_window_process_alive(Mock(), 0))


class ForegroundInputDelegationTests(unittest.TestCase):
    def make(self, user32, **kwargs):
        from navigation.controller import ForegroundInput
        with patch("navigation.controller.connect_target", return_value=target(user32, **kwargs)):
            return ForegroundInput(Mock(), lambda: False)

    def test_window_handle_and_focus_come_from_the_target(self):
        inputs = self.make(FakeUser32())
        self.assertEqual(inputs.hwnd, 0x10)
        self.assertTrue(inputs.foreground())
        self.assertTrue(inputs.capturable())
        self.assertFalse(inputs.hung())

    def test_capturable_requires_foreground_only_for_screen_capture(self):
        background = FakeUser32(foreground=0x20)
        self.assertTrue(self.make(background).capturable())
        self.assertFalse(self.make(background, capture_requires_foreground=True).capturable())
        self.assertFalse(self.make(FakeUser32(iconic=True)).capturable())

    def test_background_input_comes_from_the_host_backend(self):
        from navigation.controller import BackgroundInput
        with patch.object(host, "platform_backend", return_value=None):
            with self.assertRaisesRegex(NavigationError, "前台导航仅支持 Windows 游戏窗口。"):
                BackgroundInput(Mock(), lambda: False)
        user32 = Mock(hwnd=0x10)
        with patch.object(host, "platform_backend", return_value="win32"), \
                patch("navigation.controller.connect_target", return_value=target(user32)):
            inputs = BackgroundInput(Mock(), lambda: False)
        from host.win32.background_input import BackgroundInput as Win32BackgroundInput
        self.assertIsInstance(inputs, Win32BackgroundInput)
        self.assertTrue(inputs.window_only)
        self.assertIs(inputs.user32, user32)
        self.assertEqual(inputs.hwnd, 0x10)

    def test_session_brings_the_game_back_through_the_target(self):
        from task_control import ForegroundSession
        adapter = SimpleNamespace(target=Mock(), controller=Mock())
        adapter.target.bring_to_foreground.return_value = True
        self.assertTrue(ForegroundSession._bring_to_foreground(adapter))
        adapter.target.bring_to_foreground.assert_called_once_with(adapter.controller)


class MacTextInputTests(unittest.TestCase):
    """macOS: text goes to the unit one character per request so that a Stop ends it at the next character."""

    def runtime(self, stop_after=None):
        from daily.runtime import Runtime
        rt = Runtime.__new__(Runtime)
        rt.navigation_halted = False
        state = {"stopping": False, "sent": []}
        rt.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))

        def post_input_text(text):
            state["sent"].append(text)
            if stop_after is not None and len(state["sent"]) >= stop_after:
                rt.context.tasker.stopping = True
            return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=True))
        rt.controller = SimpleNamespace(post_input_text=post_input_text)
        rt.check_focus = Mock(return_value=False)
        rt.log = Mock()
        rt.ensure_frame = Mock(return_value=True)
        rt.invalidate_frame = Mock()
        rt._active_inputs = Mock(return_value=None)
        rt._use_window_input = Mock(return_value=False)
        rt.pause = lambda seconds: not rt.context.tasker.stopping
        return rt, state

    def test_mac_sends_one_character_per_request(self):
        rt, state = self.runtime()
        with patch("daily.runtime.platform_backend", return_value="macos"), \
                patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.input_text("b1星"))
        self.assertEqual(state["sent"], ["b", "1", "星"])

    def test_a_stop_ends_mac_text_at_the_next_character(self):
        rt, state = self.runtime(stop_after=1)
        with patch("daily.runtime.platform_backend", return_value="macos"), \
                patch("daily.runtime.active_session", return_value=None):
            self.assertFalse(rt.input_text("abcdef"))
        self.assertEqual(state["sent"], ["a"])

    def test_a_focus_return_part_way_ends_mac_text(self):
        rt, state = self.runtime()
        rt.log = Mock()
        rt.check_focus = Mock(side_effect=lambda: len(state["sent"]) == 1)  # lost and back after the first character
        with patch("daily.runtime.platform_backend", return_value="macos"), \
                patch("daily.runtime.active_session", return_value=None):
            self.assertFalse(rt.input_text("abc"))
        self.assertEqual(state["sent"], ["a"])

    def test_a_resume_before_the_first_character_ends_mac_text(self):
        def resumed_once(rt):
            calls = []

            def check_focus():
                calls.append(1)
                if len(calls) == 1:  # the pause ended here: the resume is consumed by this call
                    rt._focus_generation += 1
                    return True
                return False
            return check_focus

        for where in ("first check", "ensure_frame"):
            with self.subTest(where=where):
                rt, state = self.runtime()
                rt._focus_generation = 4
                if where == "first check":
                    rt.check_focus = resumed_once(rt)
                else:
                    rt.ensure_frame = Mock(side_effect=lambda: setattr(rt, "_focus_generation", 5) or True)
                with patch("daily.runtime.platform_backend", return_value="macos"), \
                        patch("daily.runtime.active_session", return_value=None):
                    self.assertFalse(rt.input_text("abc"))
                self.assertEqual(state["sent"], [])
                rt.log.assert_called_once()
        rt, state = self.runtime()
        rt._focus_generation = 4
        rt.check_focus = resumed_once(rt)
        with patch("daily.runtime.platform_backend", return_value="win32"), \
                patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.input_text("abc"))  # Windows: unchanged
        self.assertEqual(state["sent"], ["abc"])

    def test_windows_still_sends_the_whole_string_at_once(self):
        rt, state = self.runtime()
        with patch("daily.runtime.platform_backend", return_value="win32"), \
                patch("daily.runtime.active_session", return_value=None):
            self.assertTrue(rt.input_text("abc"))
        self.assertEqual(state["sent"], ["abc"])


class MacTextGateTests(unittest.TestCase):
    """macOS: sub-options that need typed text refuse before any committing input; Win32 is unchanged."""

    def test_xinghai_chat_is_unavailable_and_refused_before_sitting_down(self):
        from daily.runtime import Executors
        from daily.planner import Card, XINGHAI
        chat = next(rule for rule in XINGHAI if rule.key == "chat")
        rt = Executors.__new__(Executors)
        rt.task_selected = Mock(return_value=True)
        rt.setting = Mock(return_value={"auto_complete": True, "allow_chat": True})
        card = Card(1, "聚会", chat)
        with backend("macos"):
            self.assertFalse(rt.available("xinghai", card))
        with backend("win32"):
            self.assertTrue(rt.available("xinghai", card))
        rt.log, rt.main, rt.key, rt._wait_world_prompt, rt.action = Mock(), Mock(), Mock(), Mock(), Mock()
        with backend("macos"):
            self.assertFalse(rt.chat("xinghai"))
        for untouched in (rt.main, rt.key, rt._wait_world_prompt, rt.action):
            untouched.assert_not_called()

    def test_redeem_stops_the_code_instead_of_typing_it(self):
        from daily import redeem_codes
        codes = redeem_codes.RedeemCodes.__new__(redeem_codes.RedeemCodes)
        codes.rt = SimpleNamespace(action=Mock(return_value=True), pause=Mock(return_value=True),
                                   hit=Mock(return_value=True), input_text=Mock(return_value=True), log=Mock())
        with backend("macos"), patch.object(redeem_codes, "set_clipboard_text", return_value=False):
            self.assertFalse(codes._fill_code("ABCD1234"))
        codes.rt.action.assert_not_called()  # neither the paste button nor the input box
        codes.rt.input_text.assert_not_called()
        with backend("win32"), patch.object(redeem_codes, "set_clipboard_text", return_value=False):
            codes._fill_code("ABCD1234")
        codes.rt.action.assert_any_call("Click", target=redeem_codes.INPUT_BOX_TARGET)

    def test_a_paste_is_only_trusted_when_the_field_reads_the_code(self):
        from daily import redeem_codes
        codes = redeem_codes.RedeemCodes.__new__(redeem_codes.RedeemCodes)
        codes._modal_ready = Mock(return_value=True)
        for read, expected in (("", False), ("ABCD123", False), ("ABCD 1234", True)):
            with self.subTest(read=read):
                codes.rt = SimpleNamespace(action=Mock(return_value=True), pause=Mock(return_value=True),
                                           hit=Mock(return_value=False),  # placeholder missed: looks pasted
                                           text=Mock(return_value=read), input_text=Mock(), log=Mock())
                with backend("macos"), patch.object(redeem_codes, "set_clipboard_text", return_value=True):
                    self.assertIs(codes._fill_code("ABCD1234"), expected)
                codes.rt.input_text.assert_not_called()
        codes.rt = SimpleNamespace(action=Mock(return_value=True), pause=Mock(return_value=True),
                                   hit=Mock(return_value=False), text=Mock(return_value=""), log=Mock())
        with backend("win32"), patch.object(redeem_codes, "set_clipboard_text", return_value=True):
            self.assertTrue(codes._fill_code("ABCD1234"))  # Win32: the placeholder check, as before
        codes.rt.text.assert_not_called()

    def test_redeem_rechecks_the_field_on_a_fresh_frame_after_the_cooldown_before_clicking(self):
        import time
        from contextlib import nullcontext
        from daily import redeem_codes
        button = (600, 450, 80, 40)

        from task_control import ForegroundChanged

        def attempt(platform, *, read="ABCD1234", modal=True, reward=False, use_hit=True, resume_at_click=0,
                    read_after_resume=None):
            events = []
            state = {"generation": 7, "resumes": resume_at_click}

            def action(*args, **fields):
                expected = fields.get("focus_generation")
                if state["resumes"]:
                    # The game left the front and came back between the check and the dispatch.
                    state["resumes"] -= 1
                    state["generation"] += 1
                    codes.rt._focus_generation = state["generation"]
                    if read_after_resume is not None:
                        state["read"] = read_after_resume
                if expected is not None and expected != state["generation"]:
                    events.append("cancelled")
                    raise ForegroundChanged("前台切换后已取消本次输入")
                events.append(("click", tuple(fields["target"])))
                return True
            state["read"] = read
            codes = redeem_codes.RedeemCodes.__new__(redeem_codes.RedeemCodes)
            codes.last_use_at = time.monotonic()  # the previous code was just used: the cooldown applies
            codes._reset_modal_if_dirty = Mock(return_value=True)
            codes._fill_code = Mock(side_effect=lambda code: events.append("filled") or True)
            codes._poll_submit_outcome = Mock(return_value=("redeemed", ""))
            hits = {"MaaNikki_Redeem_ModalReady": modal, "MaaNikki_RewardLayer": reward}
            codes.rt = SimpleNamespace(
                stopped=False, log=Mock(), observe=lambda: nullcontext(), check_focus=Mock(return_value=False),
                _focus_generation=7,
                pause=Mock(side_effect=lambda seconds: events.append("cooldown") or True),
                capture=Mock(side_effect=lambda: events.append("capture") or "fresh frame"),
                hit=lambda node, image=None: hits.get(node, False) and image == "fresh frame",
                text=Mock(side_effect=lambda roi, image=None: state["read"] if image == "fresh frame" else ""),
                recognize=Mock(return_value=SimpleNamespace(hit=use_hit, box=button if use_hit else None)),
                action=Mock(side_effect=action))
            with backend(platform):
                return codes._redeem_one("ABCD1234"), events, codes

        result, events, _ = attempt("macos")
        self.assertEqual(result, ("redeemed", ""))
        self.assertEqual(events, ["filled", "cooldown", "capture", ("click", button)])  # checked after the wait
        for change in ({"read": ""}, {"read": "ABCD123"}, {"modal": False}, {"reward": True}, {"use_hit": False}):
            with self.subTest(change=change):
                result, events, codes = attempt("macos", **change)
                self.assertEqual(result, ("error", "兑换前复核未通过"))
                self.assertNotIn("click", [e[0] for e in events if isinstance(e, tuple)])  # not even [639, 469]
                codes._poll_submit_outcome.assert_not_called()
        # A focus change between the check and the click cancels that click and the check runs again on a fresh frame.
        result, events, _ = attempt("macos", resume_at_click=1)
        self.assertEqual(result, ("redeemed", ""))
        self.assertEqual(events, ["filled", "cooldown", "capture", "cancelled", "capture", ("click", button)])
        result, events, _ = attempt("macos", resume_at_click=1, read_after_resume="")  # field cleared meanwhile
        self.assertEqual(result, ("error", "兑换前复核未通过"))
        self.assertEqual(events, ["filled", "cooldown", "capture", "cancelled", "capture"])
        # Win32 as before: no re-check, and the fixed target when the button template misses.
        result, events, _ = attempt("win32", read="", use_hit=False)
        self.assertEqual(result, ("redeemed", ""))
        self.assertEqual(events, ["filled", "cooldown", ("click", tuple(redeem_codes.MODAL_USE_TARGET))])

    def test_a_resume_inside_the_last_input_check_cancels_a_verified_click(self):
        from daily.runtime import Runtime
        from task_control import ForegroundChanged

        def runtime():
            rt = Runtime.__new__(Runtime)
            rt.navigation_halted = False
            rt.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False),
                                         run_action=Mock(return_value=SimpleNamespace(success=True)))
            session = SimpleNamespace(generation=7, pause_generation=0, wait=lambda *a, **k: None, evidence=Mock(),
                                      stopped=lambda: False)
            rt._focus_generation, rt._pause_generation = 7, 0
            rt.navigation_inputs = None
            rt.ensure_frame, rt.invalidate_frame = Mock(return_value=True), Mock()
            checks = []

            def check(**kwargs):
                checks.append(kwargs)
                if len(checks) == 2:  # the last check before dispatch waits for the game and it comes back
                    session.generation = 8
            rt._active_inputs = Mock(return_value=SimpleNamespace(check=check, window_only=False))
            rt._use_window_input = Mock(return_value=False)
            rt._validate_pointer_targets = Mock(return_value=(1280, 720))
            rt._target_point = lambda target, name: (target[0] + target[2] // 2, target[1] + target[3] // 2)
            return rt, session

        for platform in ("macos", "win32"):
            rt, session = runtime()
            with self.subTest(platform=platform), \
                    patch("daily.runtime.active_session", return_value=session), \
                    patch("daily.runtime.platform_backend", return_value=platform), \
                    patch("daily.runtime.capabilities", return_value=SimpleNamespace(pointer_hover=False)), \
                    patch("daily.runtime.native_key_fields", side_effect=lambda action, fields: fields), \
                    patch.object(Runtime, "pause", return_value=True), \
                    patch.object(Runtime, "ui", SimpleNamespace(unhover=lambda: True), create=True):
                if platform == "macos":
                    with self.assertRaises(ForegroundChanged):
                        rt.action("Click", target=[600, 450, 80, 40], focus_generation=7)
                    rt.context.run_action.assert_not_called()  # the verified frame is from before the resume
                else:
                    self.assertTrue(rt.action("Click", target=[600, 450, 80, 40], focus_generation=7))
                    rt.context.run_action.assert_called_once()  # Win32: its existing order, unchanged

    def runtime_for_amount(self):
        from daily.runtime import Executors
        rt = Executors.__new__(Executors)
        rt.context = SimpleNamespace(get_node_data=lambda node: {"roi": [600, 300, 80, 30]})
        rt.recognize = lambda *a, **k: SimpleNamespace(hit=True, best_result=SimpleNamespace(text="1"))
        rt.frame = Mock(return_value=None)
        rt.action = Mock(return_value=False)
        rt.log, rt.diagnostic, rt.text = Mock(), Mock(), Mock(return_value="")
        rt.pause = Mock(return_value=True)
        rt._active_inputs, rt._use_window_input = Mock(return_value=None), Mock(return_value=False)
        return rt

    def test_a_typed_realm_amount_is_refused_before_the_field_is_clicked(self):
        from host import UnsupportedInput
        rt = self.runtime_for_amount()
        with backend("macos"), self.assertRaisesRegex(UnsupportedInput, "暂不支持输入数量"):
            rt.set_amount("jihua", 0, desired_count=20)  # 1 -> 20: beyond the +/- steps
        rt.action.assert_not_called()
        rt = self.runtime_for_amount()
        with backend("win32"):
            self.assertFalse(rt.set_amount("jihua", 0, desired_count=20))
        self.assertEqual(rt.action.call_args_list[0].args[0], "Click")  # Windows still clicks the field first

    def test_the_realm_queue_spends_nothing_on_an_unsupported_amount(self):
        from action import realm_queue
        from host import UnsupportedInput
        rt = SimpleNamespace(log=Mock(), stopped=False, diagnostic=Mock())
        context = Mock()
        with patch.object(realm_queue, "Executors", return_value=rt), \
                patch.object(realm_queue, "parameters", return_value={"order": "jihua"}), \
                patch.object(realm_queue, "run_double_priority", side_effect=UnsupportedInput("Mac 版暂不支持输入数量")):
            result = realm_queue.RealmQueueAction().run(context, SimpleNamespace(node_name="MaaNikki_RealmQueue"))
        self.assertFalse(result)
        context.override_pipeline.assert_not_called()  # no hand-off to the default realm
        self.assertIn("剩余活跃能量未使用", rt.log.call_args.args[0])

    def test_a_cleanup_error_over_the_refusal_still_spends_nothing(self):
        from action import realm_queue
        from host import UnsupportedInput
        from navigation.models import NavigationError

        def refused_then_cleanup_fails(*args, **kwargs):
            try:
                try:
                    raise UnsupportedInput("Mac 版暂不支持输入数量")
                finally:
                    raise NavigationError("leaving the realm failed")  # inner cleanup (jihua_leave)
            finally:
                raise RuntimeError("reopening the calendar failed")  # outer cleanup (ui.calendar)
        rt = SimpleNamespace(log=Mock(), stopped=False, diagnostic=Mock())
        context = Mock()
        with patch.object(realm_queue, "Executors", return_value=rt), \
                patch.object(realm_queue, "parameters", return_value={"order": "jihua"}), \
                patch.object(realm_queue, "run_double_priority", side_effect=refused_then_cleanup_fails):
            result = realm_queue.RealmQueueAction().run(context, SimpleNamespace(node_name="MaaNikki_RealmQueue"))
        self.assertFalse(result)
        context.override_pipeline.assert_not_called()
        rt.diagnostic.assert_not_called()


class MiddleAlignFallbackTests(unittest.TestCase):
    """macOS: a scene press the unit would refuse (cursor outside the game window) must not stop navigation;
    the optional middle-button alignment is reported unavailable and the keyboard fallbacks run."""

    def mac_input(self, quartz):
        from host.macos.input import MacOSInput
        return MacOSInput(MacOSBackendTests.mac_target(self, quartz))

    def test_the_mac_backend_blocks_only_a_cursor_known_to_be_outside_the_window(self):
        quartz = FakeQuartz()
        native = self.mac_input(quartz)
        for pointer, blocked in (((100.0, 100.0), False), ((1919.0, 1079.0), False), ((1920.0, 500.0), True),
                                 ((-1.0, 500.0), True), ((500.0, 2000.0), True), (None, False)):
            with self.subTest(pointer=pointer):
                quartz.pointer = pointer
                self.assertIs(native.scene_press_blocked(), blocked)
        quartz.pointer = (5000.0, 0.0)
        quartz.windows.clear()  # window gone: unknown, the unit decides
        self.assertFalse(native.scene_press_blocked())

    def test_foreground_input_asks_the_backend_and_win32_never_blocks(self):
        from navigation.controller import ForegroundInput
        inputs = ForegroundInput.__new__(ForegroundInput)
        inputs.native = SimpleNamespace()  # Win32Input has no such notion
        self.assertFalse(inputs.scene_press_blocked())
        inputs.native = SimpleNamespace(scene_press_blocked=lambda: True)
        self.assertTrue(inputs.scene_press_blocked())

    def test_a_required_scene_press_outside_the_window_says_what_to_do_and_presses_nothing(self):
        from navigation.controller import ForegroundInput
        inputs = ForegroundInput.__new__(ForegroundInput)
        inputs.check, inputs.buttons = Mock(), set()
        inputs.native = SimpleNamespace(scene_press_blocked=lambda: True, press_mouse=Mock(return_value=True))
        with self.assertRaisesRegex(NavigationError, "鼠标指针不在游戏画面内"):
            inputs.mouse_down(1)
        inputs.native.press_mouse.assert_not_called()
        self.assertEqual(inputs.buttons, set())
        inputs.native = SimpleNamespace(press_mouse=Mock(return_value=True))  # Win32: no such check
        inputs.mouse_down(1)
        inputs.native.press_mouse.assert_called_once_with(1)
        self.assertEqual(inputs.buttons, {1})

    class Inputs:
        """Foreground inputs whose turns move a shared heading; counts middle presses."""
        window_only = False

        def __init__(self, state, blocked):
            self.state, self.blocked, self.clicks, self.holds = state, blocked, 0, []

        def check(self):
            pass

        def release(self):
            pass

        def ready(self):
            return True

        def foreground(self):
            return True

        def click_mouse(self, button):
            self.clicks += 1
            return True

        def hold(self, key, seconds, restore=True):
            self.holds.append(key)
            self.state["heading"] = (self.state["heading"] + (45 if key == "right" else -45)) % 360

    def navigator(self, blocked, *, has_check=True):
        from navigation.engine import Navigator
        from navigation.vision import Pose
        state = {"heading": 0.0}
        inputs = self.Inputs(state, blocked)
        if has_check:
            inputs.scene_press_blocked = lambda: inputs.blocked
        events = []
        locator = Mock(heading_diagnostics={})
        navigator = Navigator(locator, inputs, Mock(), lambda: False, Mock(return_value=True),
                              event=events.append, wasd_mode=True)
        navigator.motion = None
        pose = lambda: Pose(0, 0, 1, 1, state["heading"], 1)
        navigator.stable_heading = lambda previous, deadline=None: pose()
        return navigator, inputs, events, pose

    def test_the_gate_reports_once_per_blocked_stretch_and_is_unchanged_without_the_check(self):
        navigator, inputs, events, _ = self.navigator(blocked=True)
        self.assertFalse(navigator._can_middle_align())
        self.assertFalse(navigator._can_middle_align())
        self.assertEqual([e["type"] for e in events], ["middle_align_unavailable"])
        inputs.blocked = False
        self.assertTrue(navigator._can_middle_align())
        inputs.blocked = True
        self.assertFalse(navigator._can_middle_align())
        self.assertEqual(len(events), 2)
        navigator, _, events, _ = self.navigator(blocked=True, has_check=False)  # Win32 inputs
        self.assertTrue(navigator._can_middle_align())
        self.assertEqual(events, [])

    def test_a_stationary_turn_falls_back_to_keys_without_pressing(self):
        navigator, inputs, events, pose = self.navigator(blocked=True)
        result = navigator._wasd_turn_stationary(90, pose())
        self.assertEqual(result.heading, 90)
        self.assertEqual(inputs.clicks, 0)
        self.assertEqual(inputs.holds, ["right", "right"])

    def test_a_heading_recheck_probes_with_keys_without_pressing(self):
        from navigation.engine import HeadingUncertain
        navigator, inputs, events, pose = self.navigator(blocked=True)
        navigator.locator.heading_diagnostics = {"method": "heading_uncertain", "position_reliable": True}
        readings = iter([HeadingUncertain("unreadable"), None])

        def stable_heading(previous, deadline=None):
            reading = next(readings)
            if reading is not None:
                raise reading
            return pose()
        navigator.stable_heading = stable_heading
        import time as clock
        self.assertEqual(navigator.recheck_heading(pose(), clock.monotonic() + 10).heading, 45)
        self.assertEqual(inputs.clicks, 0)
        self.assertEqual(inputs.holds, ["right"])


if __name__ == "__main__":
    unittest.main()
