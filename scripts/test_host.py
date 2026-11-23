"""Host backend selection and Win32 target behaviour with fakes: python/python.exe -B scripts/test_host.py.

Runs on any OS: Win32 calls go to a scripted user32, never to a real window.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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


class SelectionTests(unittest.TestCase):
    def test_backend_follows_platform_and_controller_type(self):
        with patch.object(host.os, "name", "nt"):
            self.assertEqual(host.platform_backend(), "win32")
            self.assertEqual(host.controller_backend({"type": "win32", "hwnd": 1}), "win32")
            self.assertIsNone(host.controller_backend({"type": "macos"}))
            self.assertIsNone(host.controller_backend(None))
        with patch.object(host.os, "name", "posix"):
            self.assertIsNone(host.platform_backend())
            self.assertIsNone(host.controller_backend({"type": "win32", "hwnd": 1}))
            self.assertEqual(host.capabilities(host.platform_backend()), host.UNSUPPORTED)
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
            self.assertTrue(callable(host.credential_store().credentials))

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


if __name__ == "__main__":
    unittest.main()
