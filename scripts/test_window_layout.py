"""16:9 window handoff and restore with a fake user32: python/python.exe -B scripts/test_window_layout.py.

The start-game pretask persists the windows it resized, the agent adopts
them at startup, and exit cleanup restores them. Runs on any OS.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))

from host.win32 import window_layout as layout  # noqa: E402

ORIGINAL_HANDOFF = layout.HANDOFF_FILE

SNAPSHOT = {
    "hwnd": 0x10, "style": 0x14CF0000, "ex_style": 0x100,
    "placement": {"flags": 0, "showCmd": 1, "ptMinPosition": [-1, -1], "ptMaxPosition": [-1, -1],
                  "rcNormalPosition": [100, 80, 1700, 980]},
    "rect": [100, 80, 1700, 980], "client_size": [1584, 861],
}


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.handoff = Path(self.folder.name) / "logs/startup/window_restore.json"
        self.user32 = Mock()
        self.user32.IsWindow.return_value = True
        for patcher in (patch.object(layout, "HANDOFF_FILE", self.handoff),
                        patch.object(layout, "_user32", return_value=self.user32),
                        patch.object(layout, "_ensure_atexit_registered")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.clear)
        self.clear()

    @staticmethod
    def clear():
        for state in (layout._SESSION_16_9_SNAPSHOT, layout._SESSION_NOTIFIED, layout._HWND_STATE, layout._TALL_FAILED):
            state.clear()

    def test_pretask_persists_agent_adopts_and_exit_restores(self):
        # Pretask: a resized window is handed off and the local state cleared, so it does not restore itself.
        layout._SESSION_16_9_SNAPSHOT[0x10] = layout._snapshot_from_dict(SNAPSHOT)
        layout.persist_session_16_9_handoff()
        self.assertEqual(layout._SESSION_16_9_SNAPSHOT, {})
        payload = json.loads(self.handoff.read_text(encoding="utf-8"))
        self.assertEqual(payload["windows"], [SNAPSHOT])
        self.assertLess(abs(payload["saved_at"] - time.time()), 60)

        # Agent startup: adopts the snapshot once and consumes the file.
        layout.adopt_session_16_9_handoff()
        self.assertFalse(self.handoff.exists())
        self.assertEqual(layout._snapshot_to_dict(layout._SESSION_16_9_SNAPSHOT[0x10]), SNAPSHOT)
        self.assertIn(0x10, layout._SESSION_NOTIFIED)

        # Exit cleanup: restores exactly that window once.
        with patch.object(layout, "_restore_snapshot") as restore:
            layout.restore_session_16_9_windows()
            layout.restore_session_16_9_windows()
        restore.assert_called_once()
        self.assertEqual(layout._snapshot_to_dict(restore.call_args.args[1]), SNAPSHOT)

    def test_stale_or_closed_handoffs_are_ignored(self):
        self.handoff.parent.mkdir(parents=True)
        self.handoff.write_text(json.dumps({"saved_at": time.time() - 301, "windows": [SNAPSHOT]}), encoding="utf-8")
        layout.adopt_session_16_9_handoff()
        self.assertEqual(layout._SESSION_16_9_SNAPSHOT, {})
        self.assertFalse(self.handoff.exists())
        self.user32.IsWindow.return_value = False
        self.handoff.write_text(json.dumps({"saved_at": time.time(), "windows": [SNAPSHOT]}), encoding="utf-8")
        layout.adopt_session_16_9_handoff()
        self.assertEqual(layout._SESSION_16_9_SNAPSHOT, {})

    def test_handoff_path_is_under_the_data_root(self):
        import app_paths
        self.assertEqual(ORIGINAL_HANDOFF, Path(app_paths.DATA_ROOT) / "logs" / "startup" / "window_restore.json")


class GeometryTests(unittest.TestCase):
    def test_client_and_work_area_in_screen_pixels(self):
        from types import SimpleNamespace
        from host.win32.geometry import Win32Geometry
        user32 = Mock()
        user32.SetThreadDpiAwarenessContext.return_value = 0

        def client_rect(hwnd, ref):
            ref._obj.left, ref._obj.top, ref._obj.right, ref._obj.bottom = 0, 0, 1920, 1080
            return 1

        def to_screen(hwnd, ref):
            ref._obj.x, ref._obj.y = 100, 50
            return 1

        def monitor(handle, ref):
            work = ref._obj.rcWork
            work.left, work.top, work.right, work.bottom = 0, 0, 2560, 1400
            return 1
        user32.GetClientRect.side_effect = client_rect
        user32.ClientToScreen.side_effect = to_screen
        user32.MonitorFromWindow.return_value = 1
        user32.GetMonitorInfoW.side_effect = monitor
        geometry = Win32Geometry(SimpleNamespace(hwnd=0x10))
        with patch.object(layout, "_user32", return_value=user32):
            self.assertEqual(geometry.client_size(), (1920, 1080))
            self.assertEqual(geometry.client_screen_rect(), (100, 50, 2020, 1130))
            self.assertEqual(geometry.monitor_work_area(), (0, 0, 2560, 1400))
            user32.MonitorFromWindow.return_value = 0
            self.assertIsNone(geometry.monitor_work_area())


class FacadeTests(unittest.TestCase):
    def test_without_the_backend_lifecycle_hooks_do_nothing_and_layout_calls_fail(self):
        import host
        import tall_window
        with patch.object(host.os, "name", "posix"), patch.object(layout, "restore_session_16_9_windows") as restore:
            self.assertIsNone(tall_window.adopt_session_16_9_handoff())
            self.assertIsNone(tall_window.persist_session_16_9_handoff())
            self.assertIsNone(tall_window.restore_session_16_9_windows())
            restore.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "不支持调整游戏窗口"):
                tall_window.window_hwnd()

    def test_on_windows_calls_pass_through_unchanged(self):
        import host
        import tall_window
        with patch.object(host.os, "name", "nt"):
            for name, args, kwargs in (
                ("clamp_capture_park_point", (0x10, [5, 6]), {"capture_size": (1280, 720), "margin_px": 8}),
                ("safe_capture_y_range", (0x10, 5120), {"capture_width": 1280}),
                ("is_capture_point_safe", (0x10, (1, 2)), {}),
                ("heal_16_9_window", (Mock(),), {"hwnd": 0x10}),
                ("ensure_16_9_window", (Mock(),), {"notify": None, "write_log": False}),
                ("is_resolution_adjustment_blocked", (0x10,), {}),
                ("tall_window", (Mock(),), {}),
                ("restore_session_16_9_windows", (), {}),
            ):
                with self.subTest(name=name), patch.object(layout, name, return_value="sentinel") as backend:
                    self.assertEqual(getattr(tall_window, name)(*args, **kwargs), "sentinel")
                    backend.assert_called_once_with(*args, **kwargs)
            with patch.object(layout, "window_hwnd", return_value=0x10) as backend:
                self.assertEqual(tall_window.window_hwnd("rt"), 0x10)
                backend.assert_called_once_with(rt="rt", controller=None)

    def test_shared_settings_and_sizes_are_defined_once(self):
        import tall_window
        self.assertIs(layout.is_auto_adjust_resolution_enabled, tall_window.is_auto_adjust_resolution_enabled)
        self.assertEqual((layout.TALL_CAPTURE_WIDTH, layout.TALL_CAPTURE_HEIGHT), (1280, 5120))


if __name__ == "__main__":
    unittest.main()
