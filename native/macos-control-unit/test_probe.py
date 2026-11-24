"""Offline tests of probe.py's scene-button-check: python -B native/macos-control-unit/test_probe.py

The probe sends input to the game, so it can only run with the maintainer at the machine. These
tests replace the controller, captures and cursor with fakes and check what it would send, in which
order, and that every press and W are released whatever fails. No game, window or maafw needed.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import cv2  # noqa: F401  loaded once here: patch.dict(sys.modules) would drop a half-imported cv2
import numpy as np

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("maanikki_probe", HERE / "probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

PRESSURE = probe.SCENE_PRESS_PRESSURE


class Job:
    def __init__(self, succeeded, interrupt=False):
        self.succeeded = succeeded
        self.interrupt = interrupt

    def wait(self):
        if self.interrupt:
            raise KeyboardInterrupt  # Ctrl+C while waiting: the request was already submitted
        return self


class FakeController:
    """Records input; refuses the presses whose 1-based index is in refuse_presses."""

    def __init__(self, *, refuse_presses=(), key_up_ok=True, touch_up_ok=True, patch=3, interrupt=(), refuse_keys=()):
        self.events = []
        self.inputs = 0
        self.presses = 0
        self.refuse_presses = set(refuse_presses)
        self.key_up_ok, self.touch_up_ok = key_up_ok, touch_up_ok
        self.info = {"type": "macos", "maanikki_patch": patch}
        self.interrupt = set(interrupt)  # event kinds whose wait() raises KeyboardInterrupt
        self.refuse_keys = set(refuse_keys)  # key codes whose key_down is refused
        self.inactive_ok = True

    def _record(self, event, ok=True):
        self.events.append(event)
        self.inputs += 1
        return Job(ok, interrupt=event[0] in self.interrupt)

    def post_relative_move(self, dx, dy):
        return self._record(("move", dx, dy))

    def post_touch_down(self, *, x, y, contact, pressure):
        self.presses += 1
        return self._record(("down", contact, pressure, x, y), self.presses not in self.refuse_presses)

    def post_touch_up(self, *, contact):
        return self._record(("up", contact), self.touch_up_ok)

    def post_key_down(self, key):
        return self._record(("key_down", key), key not in self.refuse_keys)

    def post_key_up(self, key):
        return self._record(("key_up", key), self.key_up_ok)

    def post_inactive(self):
        return self._record(("inactive",), self.inactive_ok)

    def post_click_key(self, key):
        return self._record(("click_key", key))


def run(argv, controller, abilities=("insect",), cursor=(100.0, 100.0), command="scene-button-check", sleep=None):
    """Run scene-button-check with fakes; returns (exit code or "interrupted", stdout, saved frame names).

    The HUD reads ``abilities`` in turn and keeps the last one for any further reading.
    """
    readings = list(abilities)

    def selected_ability(image):
        return (readings.pop(0) if len(readings) > 1 else readings[0]), {}

    def grab(ctrl):
        # Every input changes the whole frame; two captures with no input between them are identical.
        return np.full((720, 1280, 3), (ctrl.inputs * 37) % 256, dtype=np.uint8), 1.0

    tasker = types.ModuleType("maa.tasker")
    tasker.Tasker = types.SimpleNamespace(set_log_dir=lambda path: None)
    toolkit = types.ModuleType("maa.toolkit")
    toolkit.Toolkit = object()
    with tempfile.TemporaryDirectory() as out, \
            mock.patch.dict(sys.modules, {"maa": types.ModuleType("maa"), "maa.tasker": tasker, "maa.toolkit": toolkit}), \
            mock.patch.object(probe, "_pick_window", lambda args, tk: 7), \
            mock.patch.object(probe, "_bundle_pid", lambda bundle: 42), \
            mock.patch.object(probe, "_front_pid", lambda: 42), \
            mock.patch.object(probe, "_connect", lambda window, raw: (controller, True, 1.0)), \
            mock.patch.object(probe, "_grab", grab), \
            mock.patch.object(probe, "_map_back_score", lambda image: 0.5), \
            mock.patch.object(probe, "_cursor", cursor if callable(cursor) else (lambda: cursor)), \
            mock.patch.object(probe, "_game_window_bounds", lambda window: (0, 0, 1920, 1080)), \
            mock.patch.object(probe.time, "sleep", sleep or (lambda seconds: None)), \
            mock.patch.object(sys, "argv", ["probe.py", command, "--profile", "maanikki", "--game",
                                            "--out", str(Path(out) / "run"), *argv]), \
            mock.patch.object(probe, "_ensure_runtime", lambda runtime: None), \
            mock.patch.object(probe, "_selected_ability", selected_ability):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            try:
                code = probe.main()
            except SystemExit as error:
                code = error.code
            except KeyboardInterrupt:
                code = "interrupted"
        frames = sorted(p.name for p in (Path(out) / "run").glob("*.png"))
    return code, stdout.getvalue(), frames


def down(contact):
    return ("down", contact, PRESSURE, 0, 0)


class SceneButtonCheckTests(unittest.TestCase):
    def test_middle_turns_away_then_presses_once(self):
        controller = FakeController()
        code, output, _ = run([], controller)
        self.assertEqual(code, 0, output)
        self.assertEqual(controller.events, [("move", 400, 0), down(2), ("up", 2)])
        self.assertIn("needs_review", output)

    def test_left_and_right_press_without_a_turn(self):
        for button, contact, extra in (("left", 0, []), ("right", 1, ["--expect-ability", "insect"])):
            with self.subTest(button=button):
                controller = FakeController()
                code, output, _ = run(["--button", button, *extra], controller)
                self.assertEqual(code, 0, output)
                self.assertEqual(controller.events, [down(contact), ("up", contact)])
                self.assertIn(probe.SCENE_BUTTON_EXPECTED[button], output)

    def test_repeat_while_walking_holds_w_around_every_press(self):
        controller = FakeController()
        code, output, _ = run(["--button", "right", "--expect-ability", "insect", "--repeat", "3", "--with-forward"],
                             controller)
        self.assertEqual(code, 0, output)
        self.assertEqual(controller.events, [("key_down", probe.KEY_W),
                                             down(1), ("up", 1), down(1), ("up", 1), down(1), ("up", 1),
                                             ("key_up", probe.KEY_W)])

    def test_a_long_hold_saves_a_frame_while_the_button_is_down(self):
        controller = FakeController()
        code, output, frames = run(["--button", "left", "--hold", "1.5"], controller)
        self.assertEqual(code, 0, output)
        self.assertTrue(any("held-1" in name for name in frames), frames)
        code, output, frames = run(["--button", "left"], FakeController())
        self.assertFalse(any("held" in name for name in frames), frames)

    def test_a_refused_press_stops_and_still_releases_it_and_w(self):
        controller = FakeController(refuse_presses={2})
        code, output, _ = run(["--button", "left", "--repeat", "3", "--with-forward"], controller)
        self.assertEqual(code, 1)
        self.assertIn("refused the scene press", output)
        self.assertEqual(controller.events, [("key_down", probe.KEY_W), down(0), ("up", 0), down(0), ("up", 0),
                                             ("key_up", probe.KEY_W)])

    def test_an_unconfirmed_w_release_asks_inactive(self):
        controller = FakeController(key_up_ok=False)
        code, output, _ = run(["--button", "left", "--with-forward"], controller)
        self.assertEqual(code, 1)
        self.assertEqual(controller.events[-2:], [("key_up", probe.KEY_W), ("inactive",)])
        self.assertIn("release of W was not confirmed", output)

    def test_an_unconfirmed_button_release_asks_inactive_then_releases_w(self):
        controller = FakeController(touch_up_ok=False)
        code, output, _ = run(["--button", "right", "--expect-ability", "insect", "--with-forward"], controller)
        self.assertEqual(code, 1)
        self.assertEqual(controller.events, [("key_down", probe.KEY_W), down(1), ("up", 1), ("inactive",),
                                             ("key_up", probe.KEY_W)])

    def test_right_needs_the_expected_ability_and_presses_nothing_else(self):
        controller = FakeController()
        code, output, _ = run(["--button", "right", "--expect-ability", "insect"], controller, abilities=("fishing",))
        self.assertEqual(code, 1)
        self.assertIn("the HUD shows fishing, not insect; no button pressed", output)
        self.assertIn("--select-slot", output)
        self.assertEqual(controller.events, [])

    def test_right_selects_the_slot_then_presses(self):
        controller = FakeController()
        code, output, _ = run(["--button", "right", "--expect-ability", "insect", "--select-slot", "3"], controller,
                              abilities=(None, "insect"))
        self.assertEqual(code, 0, output)
        self.assertEqual(controller.events, [("key_down", 0x14), ("key_up", 0x14), down(1), ("up", 1)])

    def test_right_presses_nothing_when_the_slot_holds_another_ability(self):
        controller = FakeController()
        code, output, _ = run(["--button", "right", "--expect-ability", "insect", "--select-slot", "2"], controller,
                              abilities=("fishing", "star_collect"))
        self.assertEqual(code, 1)
        self.assertIn("the HUD shows star_collect, not insect; no button pressed", output)
        self.assertEqual(controller.events, [("key_down", 0x13), ("key_up", 0x13)])

    def test_right_keeps_an_already_selected_ability(self):
        controller = FakeController()
        code, output, _ = run(["--button", "right", "--expect-ability", "insect", "--select-slot", "5"], controller)
        self.assertEqual(code, 0, output)
        self.assertEqual(controller.events, [down(1), ("up", 1)])

    def test_ability_options_are_checked_before_anything_runs(self):
        for argv in (["--button", "right"], ["--button", "left", "--expect-ability", "insect"],
                     ["--button", "middle", "--select-slot", "1"]):
            with self.subTest(argv=argv):
                controller = FakeController()
                with contextlib.redirect_stderr(io.StringIO()):
                    code, _, _ = run(argv, controller)
                self.assertEqual(code, 2)
                self.assertEqual(controller.events, [])

    def test_an_interrupted_slot_key_is_still_released(self):
        controller = FakeController(interrupt={"key_down"})
        code, _, _ = run(["--button", "right", "--expect-ability", "insect", "--select-slot", "2"], controller,
                         abilities=("fishing",))
        self.assertEqual(code, "interrupted")
        self.assertEqual(controller.events, [("key_down", 0x13), ("key_up", 0x13)])

    def test_an_unconfirmed_slot_key_release_asks_inactive_before_anything_else(self):
        controller = FakeController(key_up_ok=False)
        code, output, frames = run(["--button", "right", "--expect-ability", "insect", "--select-slot", "2"],
                                   controller, abilities=("fishing", "insect"))
        self.assertEqual(code, 1)
        self.assertEqual(controller.events, [("key_down", 0x13), ("key_up", 0x13), ("inactive",)])
        self.assertFalse(any("after-select" in name for name in frames), frames)
        self.assertIn("release of slot key 2 was not confirmed", output)

    def test_an_interrupt_while_releasing_asks_inactive(self):
        for kind, argv in (("up", ["--button", "left"]), ("key_up", ["--button", "left", "--with-forward"])):
            with self.subTest(release=kind):
                controller = FakeController(interrupt={kind})
                code, output, _ = run(argv, controller)
                self.assertEqual(code, 1)
                self.assertIn(("inactive",), controller.events)
                self.assertIn("was not confirmed", output)

    def test_the_cursor_is_checked_before_any_slot_key(self):
        controller = FakeController()
        code, output, _ = run(["--button", "right", "--expect-ability", "insect", "--select-slot", "2"], controller,
                              abilities=("fishing", "insect"), cursor=(5000.0, 100.0))
        self.assertEqual(code, 1)
        self.assertIn("not over the game window", output)
        self.assertEqual(controller.events, [])

    def test_the_ability_is_confirmed_on_the_last_frame_before_the_press(self):
        controller = FakeController()
        code, output, _ = run(["--button", "right", "--expect-ability", "insect"], controller,
                              abilities=("insect", "fishing"))
        self.assertEqual(code, 1)
        self.assertIn("the HUD now shows fishing", output)
        self.assertEqual(controller.events, [])

    def test_a_long_hold_is_measured(self):
        controller = FakeController()
        code, output, _ = run(["--button", "left", "--hold", "1.5"], controller)
        self.assertEqual(code, 0, output)
        self.assertIn('"measured_hold_s"', output)

    def test_an_old_unit_gets_nothing(self):
        controller = FakeController(patch=2)
        code, output, _ = run(["--button", "left"], controller)
        self.assertEqual(code, 1)
        self.assertEqual(controller.events, [])

    def test_hold_and_repeat_are_bounded(self):
        for argv in (["--hold", "0.01"], ["--hold", "3.5"], ["--hold", "nan"], ["--repeat", "0"], ["--repeat", "9"]):
            with self.subTest(argv=argv):
                controller = FakeController()
                with contextlib.redirect_stderr(io.StringIO()):
                    code, _, _ = run(argv, controller)
                self.assertEqual(code, 2)
                self.assertEqual(controller.events, [])


class KeySequenceCheckTests(unittest.TestCase):
    def sequence(self, name, controller):
        return run(["--sequence", name], controller, command="key-sequence-check")

    def test_face_camera_releases_w_before_ctrl(self):
        controller = FakeController()
        code, output, _ = self.sequence("face-camera", controller)
        self.assertEqual(code, 0, output)
        self.assertEqual(controller.events, [("key_down", 59), ("key_down", 13), ("key_up", 13), ("key_up", 59)])
        self.assertIn(probe.SEQUENCE_EXPECTED["face-camera"], output)

    def test_starsea_fall_is_space_then_q(self):
        controller = FakeController()
        code, output, _ = self.sequence("starsea-fall", controller)
        self.assertEqual(code, 0, output)
        self.assertEqual(controller.events, [("key_down", 49), ("key_up", 49), ("key_down", 12), ("key_up", 12)])

    def test_a_refused_key_stops_and_releases_everything_pressed(self):
        controller = FakeController(refuse_keys={13})
        code, output, _ = self.sequence("face-camera", controller)
        self.assertEqual(code, 1)
        self.assertIn("refused key down 13", output)
        self.assertEqual(controller.events, [("key_down", 59), ("key_down", 13), ("key_up", 13), ("key_up", 59)])

    def test_an_interrupted_press_is_still_released(self):
        controller = FakeController(interrupt={"key_down"})
        code, _, _ = self.sequence("starsea-fall", controller)
        self.assertEqual(code, "interrupted")
        self.assertEqual(controller.events, [("key_down", 49), ("key_up", 49)])

    def test_a_refused_release_mid_sequence_is_retried_then_asks_inactive(self):
        controller = FakeController(key_up_ok=False)
        code, output, _ = self.sequence("starsea-fall", controller)
        self.assertEqual(code, 1)
        self.assertEqual(controller.events, [("key_down", 49), ("key_up", 49), ("key_up", 49), ("inactive",)])

    def test_an_interrupted_release_is_retried(self):
        controller = FakeController(interrupt={"key_up"})
        code, output, _ = self.sequence("starsea-fall", controller)
        self.assertEqual(code, 1)
        self.assertEqual(controller.events[:3], [("key_down", 49), ("key_up", 49), ("key_up", 49)])
        self.assertIn(("inactive",), controller.events)

    def test_an_unconfirmed_cleanup_release_asks_inactive(self):
        controller = FakeController(refuse_keys={13}, key_up_ok=False)
        code, output, _ = self.sequence("face-camera", controller)
        self.assertEqual(code, 1)
        # Every remaining key is tried (W, then Ctrl) before inactive() is asked.
        self.assertEqual(controller.events, [("key_down", 59), ("key_down", 13), ("key_up", 13), ("key_up", 59),
                                             ("inactive",)])
        self.assertIn("release of key(s) [13, 59] was not confirmed", output)

    def test_an_interrupted_w_with_failing_releases_still_tries_ctrl_before_inactive(self):
        class InterruptW(FakeController):
            def post_key_down(self, key):
                job = super().post_key_down(key)
                job.interrupt = key == 13  # Ctrl+C while waiting for W's press
                return job

            def post_key_up(self, key):
                return self._record(("key_up", key), key != 13)  # W's release fails

        controller = InterruptW()
        controller.inactive_ok = False
        code, output, _ = self.sequence("face-camera", controller)
        self.assertEqual(controller.events, [("key_down", 59), ("key_down", 13), ("key_up", 13), ("key_up", 59),
                                             ("inactive",)])
        self.assertEqual(code, 1)
        self.assertIn("release of key(s) [13] was not confirmed", output)
        self.assertIn("NOT confirmed", output)

    def test_face_camera_counts_the_submission_time_toward_the_80_ms_pulse(self):
        clock = {"t": 0.0}
        slept = []

        class SlowAck(FakeController):
            def post_key_down(self, key):
                clock["t"] += 0.06  # a 60 ms acknowledgement
                return super().post_key_down(key)

        def sleep(seconds):
            slept.append(round(seconds, 3))
            clock["t"] += seconds
        controller = SlowAck()
        with mock.patch.object(probe.time, "perf_counter", lambda: clock["t"]):
            code, output, frames = run(["--sequence", "face-camera"], controller, command="key-sequence-check",
                                       sleep=sleep)
        self.assertEqual(code, 0, output)
        self.assertIn(0.02, slept)  # 80 ms from before the W submission, not 80 ms after its acknowledgement
        self.assertIn(0.4, slept)
        self.assertTrue(any("settled" in name for name in frames), frames)


class CameraCheckCursorTests(unittest.TestCase):
    def dragging_cursor(self, controller):
        # The game moving the real pointer by dx/2 points, as seen on the device.
        return lambda: (100.0 + sum(e[1] for e in controller.events if e[0] == "move") / 2, 100.0)

    def test_record_cursor_keeps_going_and_records_each_displacement(self):
        controller = FakeController()
        code, output, _ = run(["--record-cursor"], controller, cursor=self.dragging_cursor(controller),
                              command="camera-check")
        self.assertEqual(code, 0, output)
        self.assertEqual(controller.events, [("move", 400, 0), ("move", -400, 0)])
        self.assertIn('"cursor_moved_by": [\n      200.0', output)
        self.assertIn('"cursor_moved_by": [\n      -200.0', output)

    def test_strict_mode_judges_against_the_baseline_so_small_drifts_add_up(self):
        controller = FakeController()

        def creeping():  # 0.8 points per move: each step alone is within 1 point, the total is not
            return (100.0 + 0.8 * sum(1 for e in controller.events if e[0] == "move"), 100.0)
        code, output, _ = run([], controller, cursor=creeping, command="camera-check")
        self.assertEqual(code, 1)
        self.assertIn("the real cursor moved after relative move -400", output)

    def test_record_cursor_with_option_records_the_absorbing_move(self):
        controller = FakeController()
        code, output, _ = run(["--record-cursor", "--option"], controller, cursor=self.dragging_cursor(controller),
                              command="camera-check")
        self.assertEqual(code, 0, output)
        self.assertEqual(controller.events[1], ("move", 1, 0))
        self.assertIn('"step": "absorbing move +1"', output)
        self.assertIn('"cursor_moved_by": [\n      0.5', output)

    def test_without_the_flag_a_moved_cursor_still_stops_the_check(self):
        controller = FakeController()
        code, output, _ = run([], controller, cursor=self.dragging_cursor(controller), command="camera-check")
        self.assertEqual(code, 1)
        self.assertEqual(controller.events, [("move", 400, 0)])
        self.assertIn("the real cursor moved", output)


class TimingTests(unittest.TestCase):
    def test_percentiles_are_ordered_nearest_rank(self):
        durations = iter([0.050, 0.030, 0.040, 0.100, 0.035])
        clock = {"t": 0.0}

        def sleep(seconds):
            clock["t"] += next(durations)
        with mock.patch.object(probe.time, "sleep", sleep), mock.patch.object(probe.time, "perf_counter", lambda: clock["t"]), \
                mock.patch.object(probe, "_front_bundle_id", lambda: "x"), \
                mock.patch.object(sys, "argv", ["probe.py", "timing", "--samples", "5"]), \
                mock.patch("subprocess.run", return_value=types.SimpleNamespace(stdout="")):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(probe.main(), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual((result["p50_ms"], result["p95_ms"], result["max_ms"]), (40.0, 100.0, 100.0))
        with contextlib.redirect_stderr(io.StringIO()), mock.patch.object(sys, "argv", ["probe.py", "timing", "--samples", "0"]):
            with self.assertRaises(SystemExit):
                probe.main()


class AbilityReadingTests(unittest.TestCase):
    """The HUD reading against a synthetic frame: each icon drawn where and how large the HUD shows it."""

    def hud(self, name):
        pipeline, node = probe.ABILITY_NODES[name]
        definition = json.loads((probe.RESOURCE / "pipeline" / pipeline).read_text(encoding="utf-8"))[node]
        icon = cv2.imread(str(probe.RESOURCE / "image" / definition["template"]))
        icon = cv2.resize(icon, None, fx=.73 * 2 / 3, fy=.73 * 2 / 3, interpolation=cv2.INTER_AREA)
        frame = np.full((720, 1280, 3), 40, dtype=np.uint8)
        x, y, w, h = definition["roi"]
        ih, iw = icon.shape[:2]
        top, left = y + (h - ih) // 2, x + (w - iw) // 2
        frame[top:top + ih, left:left + iw] = icon
        return frame

    def test_each_known_ability_is_read_as_itself(self):
        for name in probe.ABILITY_NODES:
            with self.subTest(ability=name):
                ability, scores = probe._selected_ability(self.hud(name))
                self.assertEqual(ability, name, scores)

    def test_scores_are_compared_unrounded(self):
        with mock.patch.object(probe, "_ability_scores", return_value={"insect": 0.7804, "fishing": 0.1}):
            self.assertEqual(probe._selected_ability(None), ("insect", {"insect": 0.78, "fishing": 0.1}))
        with mock.patch.object(probe, "_ability_scores", return_value={"insect": 0.78, "fishing": 0.1}):
            self.assertIsNone(probe._selected_ability(None)[0])

    def test_an_empty_button_is_no_known_ability(self):
        ability, _ = probe._selected_ability(np.full((720, 1280, 3), 40, dtype=np.uint8))
        self.assertIsNone(ability)


if __name__ == "__main__":
    unittest.main()
