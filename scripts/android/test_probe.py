"""Offline tests of the Android probe: python -I scripts/android/test_probe.py

No device, adb or game: the controller is a fake that records what the probe would send. Kept out
of scripts/test_*.py so the Windows offline gate (scripts/run_offline_tests.py) does not pick it up.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

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
FOCUS_NULL_BUT_GAME_APP = ("  mCurrentFocus=null\n"
                           "  mFocusedApp=ActivityRecord{1 u0 com.papegames.infinitynikki/com.epicgames.unreal.GameActivity t5}\n")


class FakeController:
    def __init__(self, *, focus=FOCUS_GAME, fail=(), interrupt=()):
        self.events, self.focus = [], focus
        self.fail, self.interrupt = set(fail), set(interrupt)

    def _job(self, event):
        self.events.append(event)
        return Job(event[0] not in self.fail, interrupt=event[0] in self.interrupt)

    def post_shell(self, command, timeout):
        if "dumpsys window" in command:
            # the real command greps mCurrentFocus only
            return Job(True, "".join(l + "\n" for l in self.focus.splitlines() if "mCurrentFocus" in l))
        return Job(True, "")

    def post_click(self, x, y, contact, pressure):
        return self._job(("click", x, y, contact))

    def post_swipe(self, x1, y1, x2, y2, ms, contact=0, pressure=1):
        return self._job(("swipe", x1, y1, x2, y2, ms, contact))

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
        return probe.Device(controller, probe.InputLog(self.log_path, "test purpose")), controller

    def records(self, phase="done"):
        records = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        return [r for r in records if phase is None or r.get("phase") == phase]

    def test_focused_package_reads_current_focus(self):
        self.assertEqual(probe.focused_package(lambda _: FOCUS_GAME), probe.GAME_PACKAGE)
        self.assertEqual(probe.focused_package(lambda _: FOCUS_LAUNCHER), "com.bluestacks.launcher")
        self.assertIsNone(probe.focused_package(lambda _: None))
        self.assertIsNone(probe.focused_package(lambda _: "mCurrentFocus=null"))
        # mFocusedApp still names the game while nothing (or a dialog) has focus; it must not count
        self.assertIsNone(probe.focused_package(lambda _: FOCUS_NULL_BUT_GAME_APP))

    def test_guard_refuses_input_when_the_game_is_not_focused(self):
        device, controller = self.device(focus=FOCUS_LAUNCHER)
        for send in (lambda: device.tap(1, 2), lambda: device.key(4), lambda: device.down(1, 2),
                     lambda: device.swipe(1, 2, 3, 4, 10), lambda: device.text("x")):
            with self.assertRaises(probe.ProbeError):
                send()
        self.assertEqual(controller.events, [])
        self.assertEqual(len(self.records(phase=None)), 5)  # every refusal is logged
        self.assertTrue(all(r["refused"] for r in self.records(phase=None)))

    def test_focus_is_checked_before_each_new_input_in_a_steps_file(self):
        device, controller = self.device()
        original = device.key

        def key_then_lose_focus(code):
            ok = original(code)
            controller.focus = FOCUS_LAUNCHER  # e.g. HOME: the launcher now has focus
            return ok
        device.key = key_then_lose_focus
        with self.assertRaises(probe.ProbeError):
            probe.run_steps(device, [{"key": 3}, {"tap": [1, 2]}], out=None)
        self.assertEqual(controller.events, [("key", 3)])

    def test_a_recent_focus_check_covers_the_moves_of_a_held_contact(self):
        device, controller = self.device()
        device.down(1, 2)
        controller.focus = FOCUS_LAUNCHER
        self.assertTrue(device.move(3, 4))
        self.assertTrue(device.up(0))
        self.assertEqual(controller.events, [("down", 1, 2, 0), ("move", 3, 4, 0), ("up", 0)])

    def test_failed_release_fails_the_run_and_keeps_the_contact_marked(self):
        device, controller = self.device(fail={"up"})
        with self.assertRaises(probe.ProbeError):
            probe.run_steps(device, [{"down": [1, 2]}], out=None)
        self.assertEqual(device.held, {0})
        self.assertEqual([e for e in controller.events if e[0] == "up"], [("up", 0), ("up", 0)])  # one retry

    def test_failed_click_or_swipe_sends_an_explicit_up(self):
        device, controller = self.device(fail={"click", "swipe"})
        self.assertFalse(device.tap(1, 2, contact=1))
        self.assertFalse(device.swipe(1, 2, 3, 4, 10, contact=2))
        self.assertIn(("up", 1), controller.events)
        self.assertIn(("up", 2), controller.events)
        self.assertEqual(device.held, set())

    def test_durations_are_validated_before_any_input(self):
        device, controller = self.device()
        for send in (lambda: device.swipe(1, 2, 3, 4, -1), lambda: device.tap(1, 2, hold_ms=-5),
                     lambda: device.drag(1, 2, 3, 4, 10 ** 9)):
            with self.assertRaises(probe.ProbeError):
                send()
        with self.assertRaises(probe.ProbeError):
            probe.run_steps(device, [{"wait": -1}], out=None)
        self.assertEqual(controller.events, [])

    def test_input_is_logged_before_it_is_sent(self):
        device, controller = self.device()
        self.log_path.mkdir()  # the log path is a directory: the intent cannot be written
        with self.assertRaises(OSError):
            device.tap(1, 2)
        self.assertEqual(controller.events, [])

    def test_swipe_step_passes_its_contact(self):
        device, controller = self.device()
        probe.run_steps(device, [{"swipe": [1, 2, 3, 4], "ms": 10, "contact": 1}], out=None)
        self.assertEqual(controller.events, [("swipe", 1, 2, 3, 4, 10, 1)])

    def test_recognition_error_fails_the_step_before_later_input(self):
        device, controller = self.device()

        class Recognizer:
            def run_many(self, names):
                return [{"node": n, "hit": False, "error": "capture failed"} for n in names]
        with self.assertRaises(probe.ProbeError):
            probe.run_steps(device, [{"reco": ["X"]}, {"tap": [1, 2]}], out=None, recognizer=Recognizer())
        self.assertEqual(controller.events, [])

    def test_shell_offers_only_fixed_diagnostics(self):
        parser = probe.build_parser()
        self.assertEqual(parser.parse_args(["shell", "focus", "--grep", "mCurrentFocus"]).name, "focus")
        for bad in ("input tap 1 2", "dumpsys batterystats --reset", "wm size 100x100"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["shell", bad])
        self.assertTrue(all(" " not in c or c.split()[0] in {"dumpsys", "getevent", "wm", "ps", "cmd"}
                            for c in probe.DIAGNOSTICS.values()))

    def test_input_step_allows_one_key_event_or_axis(self):
        for ok in ("gamepad keyevent KEYCODE_BUTTON_A", "dpad keyevent KEYCODE_DPAD_UP", "joystick motionevent MOVE 0 -1"):
            self.assertTrue(probe.INPUT_STEP.fullmatch(ok), ok)
        device, controller = self.device()
        for bad in ("motionevent DOWN 100 100", "keyevent 3 4", "touchscreen motionevent DOWN 1 2",
                    "gamepad keyevent KEYCODE_BUTTON_A KEYCODE_HOME", "joystick motionevent DOWN 0 0"):
            with self.assertRaises(probe.ProbeError, msg=bad):
                device.shell_input(bad)
        self.assertEqual(controller.events, [])

    def test_text_refuses_line_breaks(self):
        device, controller = self.device()
        for bad in ("a\nd 0 10 10 50", "x\ry", "tab\there"):
            with self.assertRaises(probe.ProbeError):
                device.text(bad)
        self.assertEqual(controller.events, [])

    def test_moves_and_ups_of_contacts_not_put_down_are_refused(self):
        device, controller = self.device()
        with self.assertRaises(probe.ProbeError):
            device.move(1, 2, contact=3)
        with self.assertRaises(probe.ProbeError):
            probe.run_steps(device, [{"up": 2}], out=None)
        self.assertEqual(controller.events, [])

    def test_long_gesture_rechecks_focus_before_moves(self):
        device, controller = self.device()
        device.down(1, 2)
        controller.focus = FOCUS_LAUNCHER
        device._focus_checked -= 1  # the last check is older than the recheck interval
        with self.assertRaises(probe.ProbeError):
            device.move(3, 4)
        self.assertEqual(device.release_all(), [])
        self.assertEqual(controller.events, [("down", 1, 2, 0), ("up", 0)])

    def test_interrupted_click_is_released(self):
        device, controller = self.device(interrupt={"click"})
        with self.assertRaises(KeyboardInterrupt):
            device.tap(1, 2, contact=1)
        self.assertEqual(device.held, {1})
        self.assertEqual(device.release_all(), [])
        self.assertEqual(controller.events[-1], ("up", 1))

    def test_release_proceeds_when_the_log_cannot_be_written(self):
        device, controller = self.device()
        device.down(1, 2)
        self.log_path.unlink()
        self.log_path.mkdir()  # every further log write fails
        self.assertEqual(device.release_all(), [])
        self.assertEqual(controller.events[-1], ("up", 0))

    def test_covered_fraction(self):
        self.assertEqual(probe.covered_fraction((0, 0, 80, 80), []), 0.0)
        self.assertEqual(probe.covered_fraction((0, 0, 80, 80), [(-10, -10, 200, 200)]), 1.0)
        self.assertAlmostEqual(probe.covered_fraction((0, 0, 80, 80), [(0, 0, 40, 80), (20, 0, 20, 80)]), 0.5)

    def _background(self, *, hud=True, opened=True, quit_dialog=False, calendar=True, fail=()):
        device, controller = self.device(fail=set(fail))
        frames = iter([np.zeros((720, 1280, 3), np.uint8), np.full((720, 1280, 3), 3, np.uint8)])
        device.capture = lambda: next(frames)

        class Recognizer:
            def run_many(self, names, adhoc=None):
                hits = {"AndroidProbe_HudLabels": (hud, [1204, 63, 37, 23]),
                        "AndroidProbe_QuitDialog": (quit_dialog, [585, 180, 110, 30]),
                        "AndroidProbe_QuitCancel": (quit_dialog, [466, 468, 60, 30])}
                out = []
                for n in names:
                    if n.startswith("AndroidProbe_CalendarLabel"):
                        out.append({"node": n, "hit": calendar, "box": [905, 65, 70, 18] if calendar else None,
                                    "detail": {"text": "奇想日历"}})
                    else:
                        out.append({"node": n, "hit": hits[n][0], "box": hits[n][1] if hits[n][0] else None,
                                    "detail": {}})
                return out

            def run(self, name):
                return {"node": name, "hit": opened}
        with mock.patch.object(probe, "mac_window_state", return_value={"available": False}), \
                mock.patch.object(probe.time, "sleep"):
            return probe.background_sample(device, Recognizer()), controller

    def test_background_sample_sends_back_only_after_the_calendar_is_recognised(self):
        sample, controller = self._background()
        self.assertEqual(controller.events, [("click", 940, 50, 0), ("key", 4)])  # tap above the located label
        self.assertEqual(sample["change_1s"], 3.0)
        sample, controller = self._background(opened=False)
        self.assertEqual(controller.events, [("click", 940, 50, 0)])
        self.assertFalse(sample["calendar_opened"])
        for kwargs in ({"hud": False}, {"calendar": False}):
            sample, controller = self._background(**kwargs)
            self.assertEqual(controller.events, [], kwargs)

    def test_calendar_shortcut_needs_windows_that_agree(self):
        def recognizer(boxes):
            class Recognizer:
                def run_many(self, names, adhoc=None):
                    windows = iter(boxes)
                    out = []
                    for n in names:
                        box = next(windows, None) if n.startswith("AndroidProbe_CalendarLabel") else None
                        hit = n == "AndroidProbe_HudLabels" or box is not None
                        out.append({"node": n, "hit": hit, "box": box or ([1204, 63, 37, 23] if hit else None),
                                    "detail": {"text": "厅奇想日历" if box else ""}})
                    return out
            return Recognizer()
        # "厅奇想日历": the label starts one character in, so its centre is 3/5 of the way along the box
        state = probe.hud_state(recognizer([[880, 65, 100, 18], [885, 65, 100, 18]]))
        self.assertTrue(935 <= state["calendar_icon"][0] <= 945, state)
        self.assertNotIn("calendar_icon", probe.hud_state(recognizer([[880, 65, 100, 18], [1100, 65, 100, 18]])))
        self.assertNotIn("calendar_icon", probe.hud_state(recognizer([[880, 65, 100, 18]])))  # one window is not enough

    def test_background_sample_cancels_the_quit_dialog_and_sends_nothing_else(self):
        sample, controller = self._background(quit_dialog=True)  # the HUD labels show behind the dialog
        self.assertFalse(sample["hud"])
        self.assertEqual(controller.events, [("click", 496, 483, 0)])  # 取消 only
        self.assertTrue(sample["quit_cancelled"])

    def test_background_sample_stops_after_a_failed_tap(self):
        sample, controller = self._background(fail={"click"})
        self.assertFalse(sample["tap_ok"])
        self.assertNotIn(("key", 4), controller.events)

    def test_bgcheck_fails_when_a_release_cannot_be_confirmed(self):
        device, controller = self.device(fail={"up"})

        def sample(device_, recognizer):
            device_.held.add(0)  # e.g. an interrupted click
            return {"time": "t", "window": {}}
        args = probe.build_parser().parse_args(["bgcheck", "--purpose", "t", "--out", str(Path(self.tmp.name) / "b.jsonl"),
                                                "--samples", "3", "--interval", "0"])
        with mock.patch.object(probe, "connect", return_value=device), mock.patch.object(probe, "Recognizer"), \
                mock.patch.object(probe, "background_sample", side_effect=sample), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(probe.cmd_bgcheck(args), 2)
        self.assertEqual(len((Path(self.tmp.name) / "b.jsonl").read_text().splitlines()), 1)  # stopped after one

    def test_bgcheck_refuses_raw_frames(self):
        args = probe.build_parser().parse_args(["--raw", "bgcheck", "--purpose", "t", "--out", "x"])
        with self.assertRaises(probe.ProbeError):
            probe.cmd_bgcheck(args)

    def test_custom_recognition_errors_are_kept(self):
        class Reco:
            hit, box, algorithm, best_result = False, None, "And", None
            all_results = []
            raw_detail = {"sub_results": [{"name": "x", "detail": {"all": [{"detail": {"error": "missing template"}}]}}]}
        result = probe.judged("N", Reco())
        self.assertEqual(result["detail"]["error"], "missing template")

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
        focus = controller.post_shell
        controller.post_shell = lambda command, timeout: (
            Job(True, "__exit=0\n") if command.startswith("input ") else focus(command, timeout))
        self.assertTrue(device.shell_input("gamepad keyevent KEYCODE_BUTTON_A"))
        controller.post_shell = lambda command, timeout: (
            Job(True, "Error: Unknown command\n__exit=1\n") if command.startswith("input ") else focus(command, timeout))
        self.assertFalse(device.shell_input("keyboard keyevent KEYCODE_BOGUS"))
        records = self.records()
        self.assertEqual([r["command"] for r in records], ["input", "input"])
        self.assertEqual([r["ok"] for r in records], [True, False])
        with self.assertRaises(probe.ProbeError):
            device.shell_input("keyboard keyevent KEYCODE_BACK; reboot")
        self.assertEqual(len(self.records()), 2)

    def test_full_roi_override(self):
        whole = [0, 0, 1280, 720]
        ocr = {"recognition": {"type": "OCR", "param": {"roi": [1, 2, 3, 4], "expected": ["x"]}}}
        self.assertEqual(probe.full_roi_override(ocr)["recognition"]["param"]["roi"], whole)
        custom = {"recognition": {"type": "Custom", "param": {
            "custom_recognition": "nikki.ui_template",
            "custom_recognition_param": {"roi": [1, 2, 3, 4], "fallback_all": [{"roi": [5, 6, 7, 8]}]}}}}
        self.assertEqual(probe.full_roi_override(ocr, 1600, 720)["recognition"]["param"]["roi"], [0, 0, 1600, 720])
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
