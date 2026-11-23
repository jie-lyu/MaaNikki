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
        with patch.object(host.os, "name", "posix"):
            with self.assertRaisesRegex(RuntimeError, "不支持关闭游戏"):
                host.game_close_api()
        with patch.object(host.os, "name", "nt"), patch("host.win32.close.Win32Api") as binding:
            self.assertIs(host.game_close_api(), binding.return_value)

    def test_win32_capabilities(self):
        capabilities = host.capabilities("win32")
        self.assertEqual(capabilities.backend, "win32")
        self.assertTrue(capabilities.foreground_input and capabilities.overlay and capabilities.tall_window)

    def test_non_windows_host_rejects_with_the_previous_message(self):
        controller = Mock()
        with patch.object(host.os, "name", "posix"):
            with self.assertRaisesRegex(NavigationError, "前台导航仅支持 Windows 游戏窗口。"):
                host.connect_target(controller)

    def test_unreadable_info_and_invalid_window(self):
        class Broken:
            @property
            def info(self):
                raise RuntimeError("closed")
        with patch.object(host.os, "name", "nt"):
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
        with patch.object(host.os, "name", "posix"):
            with self.assertRaisesRegex(NavigationError, "前台导航仅支持 Windows 游戏窗口。"):
                BackgroundInput(Mock(), lambda: False)
        user32 = Mock(hwnd=0x10)
        with patch.object(host.os, "name", "nt"), \
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
