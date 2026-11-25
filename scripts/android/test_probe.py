"""Offline tests of the Android probe: python -I scripts/android/test_probe.py

No device, adb or game: the controller is a fake that records what the probe would send. Kept out
of scripts/test_*.py so the Windows offline gate (scripts/run_offline_tests.py) does not pick it up.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("android_probe", HERE / "probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


class Job:
    def __init__(self, ok, value=None, interrupt=False):
        self.succeeded, self.value, self.interrupt = ok, value, interrupt

    def wait(self):
        if self.interrupt:
            raise KeyboardInterrupt
        return self

    def get(self):
        return self.value


FOCUS_GAME = ("  mCurrentFocus=Window{fe7cc5b u0 com.papegames.infinitynikki/com.epicgames.unreal.GameActivity}\n"
              "  mFocusedApp=ActivityRecord{1 u0 com.papegames.infinitynikki/com.epicgames.unreal.GameActivity t5}\n")
FOCUS_LAUNCHER = "  mCurrentFocus=Window{1 u0 com.bluestacks.launcher/com.android.launcher3.Launcher}\n"


class FakeController:
    def __init__(self, *, focus=FOCUS_GAME, fail=(), interrupt=()):
        self.events, self.focus = [], focus
        self.fail, self.interrupt = set(fail), set(interrupt)

    def _job(self, event):
        self.events.append(event)
        return Job(event[0] not in self.fail, interrupt=event[0] in self.interrupt)

    def post_shell(self, command, timeout):
        return Job(True, self.focus if "dumpsys window" in command else "")

    def post_click(self, x, y, contact, pressure):
        return self._job(("click", x, y, contact))

    def post_swipe(self, x1, y1, x2, y2, ms):
        return self._job(("swipe", x1, y1, x2, y2, ms))

    def post_touch_down(self, x, y, contact, pressure):
        return self._job(("down", x, y, contact))

    def post_touch_move(self, x, y, contact, pressure):
        return self._job(("move", x, y, contact))

    def post_touch_up(self, contact):
        return self._job(("up", contact))

    def post_click_key(self, code):
        return self._job(("key", code))

    def post_input_text(self, text):
        return self._job(("text", text))

    def post_screencap(self):
        return Job(False)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmp.name) / "log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def device(self, **kwargs):
        controller = FakeController(**kwargs)
        return probe.Device(controller, probe.InputLog(self.log_path, "test purpose"), raw=False), controller

    def records(self):
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]

    def test_focused_package_reads_current_focus(self):
        self.assertEqual(probe.focused_package(lambda _: FOCUS_GAME), probe.GAME_PACKAGE)
        self.assertEqual(probe.focused_package(lambda _: FOCUS_LAUNCHER), "com.bluestacks.launcher")
        self.assertIsNone(probe.focused_package(lambda _: None))
        self.assertIsNone(probe.focused_package(lambda _: "mCurrentFocus=null"))

    def test_guard_refuses_input_when_the_game_is_not_focused(self):
        device, controller = self.device(focus=FOCUS_LAUNCHER)
        with self.assertRaises(probe.ProbeError):
            device.check_foreground()
        self.assertEqual(controller.events, [])

    def test_every_input_is_logged_with_its_purpose(self):
        device, _ = self.device()
        device.tap(10, 20)
        device.key(4)
        records = self.records()
        self.assertEqual([r["command"] for r in records], ["tap", "key"])
        self.assertTrue(all(r["purpose"] == "test purpose" and r["ok"] for r in records))
        self.assertEqual(records[0]["args"], [10, 20, 0])

    def test_steps_lift_held_contacts_when_a_step_fails(self):
        device, controller = self.device(fail={"click"})
        steps = [{"drag": [200, 600, 200, 500], "ms": 0, "steps": 2, "contact": 0},
                 {"tap": [1100, 600], "contact": 1}]
        with self.assertRaises(probe.ProbeError):
            probe.run_steps(device, steps, out=None)
        self.assertEqual(controller.events[-1], ("up", 0))
        self.assertEqual(device.held, set())

    def test_steps_lift_held_contacts_when_interrupted(self):
        device, controller = self.device(interrupt={"move"})
        with self.assertRaises(KeyboardInterrupt):
            probe.run_steps(device, [{"drag": [1, 2, 3, 4], "ms": 0, "steps": 1}], out=None)
        self.assertEqual(controller.events[-1], ("up", 0))
        self.assertIsNone(self.records()[1]["ok"])  # the interrupted move is logged as unknown

    def test_drag_interpolates_and_stays_held(self):
        device, controller = self.device()
        self.assertTrue(device.drag(0, 0, 100, 50, 0, contact=2, steps=2))
        self.assertEqual(controller.events, [("down", 0, 0, 2), ("move", 50, 25, 2), ("move", 100, 50, 2)])
        self.assertEqual(device.held, {2})

    def test_hold_tap_is_down_then_up(self):
        device, controller = self.device()
        device.tap(5, 6, contact=1, hold_ms=1)
        self.assertEqual(controller.events, [("down", 5, 6, 1), ("up", 1)])
        self.assertEqual(device.held, set())

    def test_step_with_two_actions_is_rejected_before_input(self):
        device, controller = self.device()
        with self.assertRaises(probe.ProbeError):
            probe.run_steps(device, [{"tap": [1, 2], "key": 4}], out=None)
        self.assertEqual(controller.events, [])

    def test_shell_input_is_logged_and_checks_the_exit_status(self):
        device, controller = self.device()
        controller.post_shell = lambda command, timeout: Job(True, "__exit=0\n" if command.startswith("input ") else "")
        self.assertTrue(device.shell_input("gamepad keyevent KEYCODE_BUTTON_A"))
        controller.post_shell = lambda command, timeout: Job(True, "Error: Unknown command\n__exit=1\n")
        self.assertFalse(device.shell_input("bogus"))
        records = self.records()
        self.assertEqual([r["command"] for r in records], ["input", "input"])
        self.assertEqual([r["ok"] for r in records], [True, False])
        with self.assertRaises(probe.ProbeError):
            device.shell_input("keyevent 4; reboot")
        self.assertEqual(len(self.records()), 2)

    def test_full_roi_override(self):
        whole = [0, 0, 1280, 720]
        ocr = {"recognition": {"type": "OCR", "param": {"roi": [1, 2, 3, 4], "expected": ["x"]}}}
        self.assertEqual(probe.full_roi_override(ocr)["recognition"]["param"]["roi"], whole)
        custom = {"recognition": {"type": "Custom", "param": {
            "custom_recognition": "nikki.ui_template",
            "custom_recognition_param": {"roi": [1, 2, 3, 4], "fallback_all": [{"roi": [5, 6, 7, 8]}]}}}}
        widened = probe.full_roi_override(custom)["recognition"]["param"]["custom_recognition_param"]
        self.assertEqual(widened["roi"], whole)
        self.assertEqual(widened["fallback_all"][0]["roi"], whole)
        # the node data itself is not modified
        self.assertEqual(custom["recognition"]["param"]["custom_recognition_param"]["roi"], [1, 2, 3, 4])
        self.assertEqual(probe.full_roi_override({"recognition": {"type": "And", "param": {}}}), {})

    def test_recognition_nodes_skip_direct_hit(self):
        names = probe.recognition_nodes("^MaaNikki_Mailbox")
        self.assertIn("MaaNikki_Mailbox_PageReady", names)
        self.assertNotIn("MaaNikki_Mailbox", names)


if __name__ == "__main__":
    unittest.main()
