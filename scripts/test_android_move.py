"""Android movement backend (host.android.move, host.android.minimap) offline:
python/python.exe -B scripts/test_android_move.py.

Runs on any OS: no emulator, adb or controller. The walk loop runs against a scripted world (the character, the
camera yaw, what each captured frame shows) on a fake clock; the minimap reader runs on small crops of real device
frames (scripts/fixtures/android/move_minimap_*.jpg, cut from .build/android/phase-e-measure m2-goto2 and m2-fb10,
positions as the device session logged them); the narration band runs through the real MaaFramework nodes on frames
composed from device crops. scripts/android/move_frames.py repeats the frame checks on the full saved frames
(local only: .build/ is not in the repository).
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))

from host.android import move  # noqa: E402
from host.android.minimap import Fix  # noqa: E402
from host.android.move import Goal, Observation, Walker  # noqa: E402

FIXTURES = ROOT / "scripts/fixtures/android"


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Frame:
    """What one capture showed: the minimap position (lagging right after a lift when the world says so)."""

    def __init__(self, number, position, kind="hud", present=True, reason=""):
        self.number, self.position, self.kind, self.present, self.reason = number, position, kind, present, reason

    def __repr__(self):
        return f"Frame({self.number}, {self.position}, {self.kind})"


class World:
    """The character (map units), the camera yaw, and how a step moves it: full deflection covers
    move.expected_distance(seconds) * ``gain`` along yaw + angle - 270 (unless ``blocked``)."""

    def __init__(self, start, yaw=30.0, gain=1.0, blocked=False, lag=0.0):
        self.position, self.yaw, self.gain, self.blocked, self.lag = tuple(start), yaw, gain, blocked, lag
        self.before = self.position  # where the minimap still was when the last step began


class FakeIo:
    CAPTURE_S = 0.3  # reading + capture + classification
    OVERHEAD_S = 0.1  # lift confirmation after the gesture's own duration

    def __init__(self, world, clock, kinds=None, step_results=None):
        self.world, self.clock = world, clock
        self.kinds = list(kinds or [])  # kinds of the next observations (then "hud")
        self.step_results = list(step_results or [])
        self.steps, self.pauses, self.saved, self.logs, self.records, self.bells = [], [], [], [], [], []
        self.observed, self.frames = 0, []
        self.lift_at = None
        self.stop_after_steps = None

    def stopped(self):
        return self.stop_after_steps is not None and len(self.steps) >= self.stop_after_steps

    def observe(self):
        at = self.clock()
        self.clock.t += self.CAPTURE_S
        kind = self.kinds.pop(0) if self.kinds else "hud"
        reason = ""
        if isinstance(kind, tuple):
            kind, reason = kind
        x, y = self.world.position
        if self.world.lag and self.lift_at is not None and at - self.lift_at < move.SETTLE_S:
            bx, by = self.world.before
            x, y = bx + (x - bx) * (1 - self.world.lag), by + (y - by) * (1 - self.world.lag)
        self.observed += 1
        frame = Frame(self.observed, (x, y), kind, reason=reason)
        self.frames.append(frame)
        return Observation(frame, kind, at, reason)

    def step(self, frame, angle, seconds, jump_at):
        assert frame is self.frames[-1], "a step must be decided on the frame just captured"
        self.steps.append({"frame": frame, "angle": angle, "seconds": seconds, "jump_at": jump_at,
                           "position": self.world.position})
        ok = self.step_results.pop(0) if self.step_results else True
        self.world.before = self.world.position
        if ok and not self.world.blocked:
            heading = math.radians((self.world.yaw + angle - 270) % 360)
            distance = move.expected_distance(seconds) * self.world.gain
            x, y = self.world.position
            self.world.position = (x + distance * math.sin(heading), y - distance * math.cos(heading))
        self.clock.t += seconds + self.OVERHEAD_S
        self.lift_at = self.clock()
        return ok

    def bell(self, frame):
        assert frame is self.frames[-1]
        self.bells.append(frame)
        self.clock.t += 4.1
        self.lift_at = self.clock()
        return True

    def pause(self, seconds):
        self.pauses.append((len(self.steps), round(seconds, 3)))
        self.clock.t += seconds
        return not self.stopped()

    def save(self, stage, reason, frame, details=None):
        self.saved.append((stage, reason, frame))
        return True

    def log(self, text, *, detail=False):
        self.logs.append((detail, text))

    def record(self, kind, record, frame):
        self.records.append((kind, record))
        return None if frame is None else f"frame-{getattr(frame, 'number', 'real')}"


class FakeReader:
    """Reads the frame's minimap position exactly, the world's yaw as the camera heading; ``bad`` frames (by
    number) read as a weak or misplaced match."""

    def __init__(self, world, *, score=0.75, bad=None, heading_missing=()):
        self.world, self.score, self.bad, self.heading_missing = world, score, dict(bad or {}), set(heading_missing)
        self.calls = []

    def present(self, frame):
        return frame.present

    def fix(self, frame, hint, search_radius):
        self.calls.append((frame.number, hint, search_radius))
        x, y = frame.position
        heading = None if frame.number in self.heading_missing else self.world.yaw
        if frame.number in self.bad:
            return self.bad[frame.number]
        return Fix(x, y, self.score, heading, 1.0 if heading is not None else 0.0, self.score >= 0.55, "luma")


def walker(world, *, kinds=None, step_results=None, reader=None, **options):
    clock = Clock()
    io = FakeIo(world, clock, kinds, step_results)
    return Walker(io, reader or FakeReader(world), clock=clock, **options), io


class GeometryTests(unittest.TestCase):
    def test_world_bearing_convention(self):
        self.assertEqual([round(move.bearing(*d)) for d in ((0, -1), (1, 0), (0, 1), (-1, 0))], [0, 90, 180, 270])

    def test_screen_angle_from_world_bearing_and_camera_yaw(self):
        cases = [  # (bearing, camera yaw) -> screen angle (0 right, 90 down)
            ((0, 0), 270),      # walk where the camera looks: stick up
            ((90, 90), 270),
            ((90, 0), 0),       # 90 deg right of the camera: stick right
            ((327.4, 147.4), 90),  # straight back: stick down
            ((57.4, 147.4), 180),  # 90 deg left of the camera: stick left
            ((235.4, 69.7), 75.7),  # m2-goto2 step 1 on the device; it moved along ~238 deg
            ((233.4, 69.7), 73.7),  # m2-goto1
        ]
        for (world, yaw), expected in cases:
            self.assertAlmostEqual(move.screen_angle(world, yaw), expected, places=6, msg=(world, yaw))

    def test_duration_is_clamped_and_follows_the_measured_model(self):
        self.assertEqual(move.step_seconds(0), move.MIN_STEP_S)
        self.assertEqual(move.step_seconds(0.3), move.MIN_STEP_S)  # 0.3 + 0.05 is under the minimum
        self.assertAlmostEqual(move.step_seconds(4.34), 1.0, places=2)  # m2-fb10: 1.0 s covered 4.2-4.8 u
        self.assertEqual(move.step_seconds(7.44), 1.5)
        self.assertEqual(move.step_seconds(100), move.MAX_STEP_S)
        self.assertEqual(move.MAX_STEP_S, 1.5)
        self.assertAlmostEqual(move.expected_distance(1.5), 7.44)
        self.assertAlmostEqual(move.expected_distance(0.5), 1.24)  # m2: 0.5 s covered ~0.7-2.0 u
        self.assertEqual(move.expected_distance(0.2), 0.0)


class StartTests(unittest.TestCase):
    def test_no_hint_refuses_before_any_capture_or_input(self):
        walk, io = walker(World((0, 0)))
        result = walk.walk([Goal(5, 0)], None, 60)
        self.assertEqual(result.outcome, "refused")
        self.assertIn("提示", result.message)
        self.assertEqual((io.observed, io.steps, io.saved), (0, [], []))

    def test_a_weak_hinted_match_refuses_with_the_frame_saved(self):
        world = World((0, 0))
        reader = FakeReader(world, bad={1: Fix(0, 0, 0.41, 30.0, 1.0, False)})
        walk, io = walker(world, reader=reader)
        result = walk.walk([Goal(5, 0)], (0.5, 0.5), 60)
        self.assertEqual(result.outcome, "refused")
        self.assertEqual(io.steps, [])
        self.assertEqual([frame.number for _stage, _reason, frame in io.saved], [1])
        self.assertIn("画面已保存", result.message)
        self.assertEqual(reader.calls[0][1], (0.5, 0.5))  # matched around the hint, never unhinted

    def test_a_fix_far_from_the_hint_refuses(self):
        world = World((0, 0))
        reader = FakeReader(world, bad={1: Fix(60, 0, 0.8, 30.0, 1.0, True)})
        walk, io = walker(world, reader=reader)
        self.assertEqual(walk.walk([Goal(5, 0)], (0, 0), 60).outcome, "refused")
        self.assertEqual(io.steps, [])

    def test_no_minimap_at_the_pivot_refuses(self):
        world = World((0, 0))
        walk, io = walker(world)
        original = io.observe

        def observe():
            obs = original()
            obs.frame.present = False
            return obs
        io.observe = observe
        result = walk.walk([Goal(5, 0)], (0, 0), 60)
        self.assertEqual(result.outcome, "refused")
        self.assertIn("小地图", result.message)
        self.assertEqual(io.steps, [])

    def test_a_bell_point_is_refused_up_front_unless_enabled(self):
        walk, io = walker(World((0, 0)))
        result = walk.walk([Goal(3, 0), Goal(6, 0, action="bell")], (0, 0), 60)
        self.assertEqual((result.outcome, io.observed, io.steps), ("refused", 0, []))


class WalkTests(unittest.TestCase):
    def test_already_there_arrives_without_input(self):
        walk, io = walker(World((10, 10)))
        result = walk.walk([Goal(10.5, 10.4)], (10, 10), 60)
        self.assertEqual(result.outcome, "arrived")
        self.assertEqual(io.steps, [])

    def test_walks_to_a_point_with_full_deflection_steps_and_arrives_on_a_settled_frame(self):
        world = World((100, 100), yaw=30.0, lag=0.3)
        walk, io = walker(world)
        result = walk.walk([Goal(100, 94)], (100.5, 100), 60)  # 6 u straight up the map
        self.assertTrue(result.ok, result.message)
        first = io.steps[0]
        self.assertAlmostEqual(first["angle"], move.screen_angle(0, 30.0))  # 240: up-left of the stick
        self.assertAlmostEqual(first["seconds"], move.step_seconds(6))
        self.assertLessEqual(math.dist(world.position, (100, 94)), move.ARRIVAL_U)
        # Arrival was judged on a frame captured at least SETTLE_S after the last lift (a settle wait happened).
        self.assertTrue(any(after == len(io.steps) and seconds > 0 for after, seconds in io.pauses))
        self.assertTrue(all(step["seconds"] <= 1.5 for step in io.steps))
        self.assertTrue(all(step["frame"].kind == "hud" for step in io.steps))

    def test_far_steps_do_not_wait_for_the_minimap_to_settle(self):
        world = World((0, 0), yaw=200.0, lag=0.3)
        walk, io = walker(world)
        result = walk.walk([Goal(0, -30)], (0, 0), 120)
        self.assertTrue(result.ok, result.message)
        far = [index for index, step in enumerate(io.steps) if math.dist(step["frame"].position, (0, -30))
               - move.LAG_U > move.REACH_U]
        self.assertGreaterEqual(len(far), 2)
        self.assertFalse([after for after, _seconds in io.pauses if after in far and after > 0 and after - 1 in far])
        self.assertEqual({io.steps[i]["seconds"] for i in far}, {1.5})

    def test_steps_are_replanned_with_the_camera_yaw_of_each_frame(self):
        world = World((0, 0), yaw=10.0)
        walk, io = walker(world)
        original = io.step

        def step(frame, angle, seconds, jump_at):
            ok = original(frame, angle, seconds, jump_at)
            world.yaw = (world.yaw + 40) % 360  # the camera swings while strafing (m2-rl10b)
            return ok
        io.step = step
        result = walk.walk([Goal(20, 0)], (0, 0), 120)
        self.assertTrue(result.ok, result.message)
        for number, item in enumerate(io.steps):
            yaw = (10.0 + 40 * number) % 360
            x, y = item["frame"].position
            self.assertAlmostEqual(item["angle"], move.screen_angle(move.bearing(20 - x, 0 - y), yaw), places=6)

    def test_three_steps_without_displacement_stop_as_blocked_with_the_frame_saved(self):
        world = World((0, 0), blocked=True)
        walk, io = walker(world)
        result = walk.walk([Goal(0, -6)], (0, 0), 60)
        self.assertEqual(result.outcome, "blocked")
        self.assertEqual(len(io.steps), move.STALL_STEPS)
        self.assertEqual(len(io.saved), 1)
        self.assertIs(io.saved[0][2], io.frames[-1])
        self.assertIn("画面已保存", result.message)

    def test_timeout_stops_with_the_frame_saved(self):
        world = World((0, 0), gain=0.3)  # slow, but moving: not blocked
        walk, io = walker(world)
        result = walk.walk([Goal(0, -200)], (0, 0), 5)
        self.assertEqual(result.outcome, "timeout")
        self.assertEqual(len(io.saved), 1)

    def test_route_points_are_passed_and_the_last_is_reached(self):
        world = World((0, 0), yaw=90.0)
        walk, io = walker(world)
        goals = [Goal(5, 0, 1.25), Goal(10, 0, 1.25), Goal(15, 0, 1.25), Goal(15, -5, 1.25)]
        result = walk.walk(goals, (0, 0), 120)
        self.assertTrue(result.ok, result.message)
        self.assertLessEqual(math.dist(world.position, (15, -5)), 1.25)
        passed = [text for detail, text in io.logs if detail and text.startswith("passed point")]
        self.assertEqual(passed, ["passed point 0", "passed point 1", "passed point 2"])

    def test_a_wait_point_pauses_without_input(self):
        world = World((0, 0), yaw=90.0)
        walk, io = walker(world)
        result = walk.walk([Goal(3, 0, 1.6, action="wait", params={"seconds": 0.9}), Goal(8, 0)], (0, 0), 60)
        self.assertTrue(result.ok, result.message)
        self.assertIn(0.9, [seconds for _after, seconds in io.pauses])

    def test_the_bell_is_pressed_once_on_a_settled_hud_frame_when_enabled(self):
        world = World((0, 0), yaw=90.0, lag=0.3)
        walk, io = walker(world, allow_bell=True)
        result = walk.walk([Goal(4, 0, 1.2, action="bell")], (0, 0), 60)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(len(io.bells), 1)
        self.assertEqual(io.bells[0].kind, "hud")

    def test_leaving_the_leash_stops(self):
        world = World((0, 0), yaw=0.0, gain=3.0)  # overshoots wildly
        walk, io = walker(world, leash=(0, 0, 8))
        result = walk.walk([Goal(0, -7)], (0, 0), 60)
        self.assertIn(result.outcome, ("leash", "lost"))
        self.assertEqual(len(io.steps), 1)


class ScreenTests(unittest.TestCase):
    def test_another_screen_after_a_step_stops_with_that_frame_saved_and_nothing_tapped(self):
        world = World((0, 0))
        walk, io = walker(world, kinds=["hud", ("other", "另一个页面")])
        result = walk.walk([Goal(0, -20)], (0, 0), 60)
        self.assertEqual(result.outcome, "screen")
        self.assertEqual(len(io.steps), 1)
        self.assertEqual(io.saved[0][2].number, 2)
        self.assertIn("另一个页面", result.message)
        self.assertNotIn("bell", [k for k, _r in io.records])

    def test_not_recognised_yet_is_waited_through_briefly(self):
        world = World((0, 0), yaw=0.0)
        walk, io = walker(world, kinds=["hud", "transition", "transition", "hud"])
        result = walk.walk([Goal(0, -6)], (0, 0), 60)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(io.saved, [])

    def test_not_recognised_for_too_long_stops_with_the_frame_saved(self):
        world = World((0, 0))
        walk, io = walker(world, kinds=["hud"] + ["transition"] * 20)
        result = walk.walk([Goal(0, -20)], (0, 0), 60)
        self.assertEqual(result.outcome, "screen")
        self.assertEqual(len(io.steps), 1)
        self.assertEqual(io.saved[0][2].kind, "transition")

    def test_narration_on_the_hud_pauses_input_then_the_walk_continues(self):
        world = World((0, 0), yaw=0.0)
        walk, io = walker(world, kinds=["hud", "narration", "narration", "narration", "hud"])
        result = walk.walk([Goal(0, -12)], (0, 0), 60)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(all(step["frame"].kind == "hud" for step in io.steps))
        self.assertEqual([step["frame"].number for step in io.steps][:2], [1, 5])  # nothing sent on frames 2-4
        messages = [text for detail, text in io.logs if not detail]
        self.assertEqual(messages, ["画面上有一行环境旁白，暂停移动，等它消失。", "环境旁白已消失，继续移动。"])
        self.assertEqual(io.saved, [])

    def test_narration_that_does_not_go_stops(self):
        world = World((0, 0))
        walk, io = walker(world, kinds=["hud"] + ["narration"] * 100)
        result = walk.walk([Goal(0, -20)], (0, 0), 120)
        self.assertEqual(result.outcome, "screen")
        self.assertEqual(len(io.steps), 1)
        self.assertEqual(len(io.saved), 1)


class StopTests(unittest.TestCase):
    def test_a_gesture_that_returns_false_ends_the_walk_with_nothing_further(self):
        world = World((0, 0))
        walk, io = walker(world, step_results=[True, False])
        result = walk.walk([Goal(0, -40)], (0, 0), 120)
        self.assertEqual(result.outcome, "stopped")
        self.assertEqual(len(io.steps), 2)
        self.assertEqual(io.observed, 2)  # no capture after the stopped gesture either
        self.assertEqual((io.saved, io.bells), ([], []))

    def test_a_run_stopped_after_a_step_sends_nothing_further(self):
        world = World((0, 0))
        walk, io = walker(world)
        io.stop_after_steps = 2
        result = walk.walk([Goal(0, -40)], (0, 0), 120)
        self.assertEqual(result.outcome, "stopped")
        self.assertEqual(len(io.steps), 2)
        self.assertEqual(io.saved, [])

    def test_a_bad_fix_gets_one_wider_retry_then_continues(self):
        world = World((0, 0), yaw=0.0)
        reader = FakeReader(world, bad={2: Fix(40, 40, 0.8, 0.0, 1.0, True)})  # an implausible jump
        walk, io = walker(world, reader=reader)
        result = walk.walk([Goal(0, -12)], (0, 0), 60)
        self.assertTrue(result.ok, result.message)
        searches = [radius for number, _hint, radius in reader.calls if number in (2, 3)]
        self.assertEqual(searches, [move.TRACK_SEARCH, move.WIDE_SEARCH])

    def test_two_bad_fixes_in_a_row_stop_as_lost(self):
        world = World((0, 0), yaw=0.0)
        weak = Fix(0, 0, 0.3, 0.0, 1.0, False)
        reader = FakeReader(world, bad={2: weak, 3: weak})
        walk, io = walker(world, reader=reader)
        result = walk.walk([Goal(0, -12)], (0, 0), 60)
        self.assertEqual(result.outcome, "lost")
        self.assertEqual(len(io.steps), 1)
        self.assertEqual(io.saved[0][2].number, 3)

    def test_an_unreadable_camera_yaw_twice_stops(self):
        world = World((0, 0))
        walk, io = walker(world, reader=FakeReader(world, heading_missing={1, 2}))
        result = walk.walk([Goal(0, -12)], (0, 0), 60)
        self.assertEqual(result.outcome, "lost")
        self.assertEqual(io.steps, [])


def _frame(name):
    import cv2
    import numpy as np
    crop = cv2.imread(str(FIXTURES / name))
    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[11:211, 60:260] = crop  # where it was cut from (minimap pivot (159.6, 110.8))
    return frame


class RealFrameTests(unittest.TestCase):
    """The minimap reader on crops of the saved m2-goto2 frames (positions and camera yaw as the device session read
    them with the offline adapter, .build/android/phase-e-measure/m2-goto2/results.jsonl)."""

    reader = None

    @classmethod
    def setUpClass(cls):
        from host.android.minimap import MinimapReader
        cls.reader = MinimapReader("miraland", ROOT / "resource")

    def test_the_first_fix_with_a_hint_reads_the_logged_position_and_camera_yaw(self):
        fix = self.reader.fix(_frame("move_minimap_goto_start.jpg"), (9878.3, 10011.5), move.FIRST_FIX_RADIUS)
        self.assertTrue(fix.reliable)
        self.assertLessEqual(math.dist((fix.x, fix.y), (9886.88, 10005.58)), 0.3)
        self.assertAlmostEqual(fix.heading, 69.7, delta=2)
        other = self.reader.fix(_frame("move_minimap_plaza_south.jpg"), (9880, 10010), move.FIRST_FIX_RADIUS)
        self.assertLessEqual(math.dist((other.x, other.y), (9879.02, 10012.23)), 0.3)
        self.assertAlmostEqual(other.heading, 147.4, delta=2)

    def test_a_wrong_hint_is_refused_not_walked_from(self):
        frame = _frame("move_minimap_goto_start.jpg")
        world = World((0, 0))
        clock = Clock()
        io = FakeIo(world, clock)
        io.observe = lambda: Observation(frame, "hud", clock())
        result = Walker(io, self.reader, clock=clock).walk([Goal(10300, 10005)], (10290, 10005), 60)
        self.assertEqual(result.outcome, "refused")
        self.assertEqual(io.steps, [])

    def test_no_hint_means_no_search(self):
        from navigation.models import NavigationError
        with self.assertRaises(NavigationError):
            self.reader.fix(_frame("move_minimap_goto_start.jpg"), None, 75)

    def test_the_chevron_check(self):
        import numpy as np
        self.assertTrue(self.reader.present(_frame("move_minimap_goto_start.jpg")))
        self.assertFalse(self.reader.present(np.full((720, 1280, 3), 90, np.uint8)))

    def test_a_step_decided_and_tracked_on_real_frames(self):
        """From the m2-goto2 start toward the measurement start (9878.3, 10011.5): the first step's angle and
        duration, then the frame captured right after the device's step tracks as a plausible fix."""
        frames = [_frame("move_minimap_goto_start.jpg"), _frame("move_minimap_goto_step0_after.jpg"),
                  _frame("move_minimap_goto_step0_settled.jpg"), _frame("move_minimap_goto_step1_settled.jpg")]
        clock = Clock()
        io = FakeIo(World((0, 0)), clock, step_results=[True, False])
        queue = list(frames)

        def observe():
            frame = queue.pop(0)
            io.frames.append(frame)
            at = clock()
            clock.t += 0.3
            return Observation(frame, "hud", at)
        io.observe = observe
        io.step = lambda frame, angle, seconds, jump_at: (io.steps.append((angle, seconds)), clock.__setattr__(
            "t", clock.t + seconds + 0.1), len(io.steps) < 2)[-1]
        result = Walker(io, self.reader, clock=clock).walk([Goal(9878.3, 10011.5)], (9886, 10006), 60)
        angle, seconds = io.steps[0]
        self.assertAlmostEqual(angle, 75.7, delta=1.5)  # what the device run sent toward the same point
        self.assertEqual(seconds, 1.5)  # 10.4 u away: a full-length step
        moved = [record for kind, record in io.records if kind == "moved"]
        self.assertLessEqual(math.dist((moved[0]["after"]["x"], moved[0]["after"]["y"]), (9884.08, 10007.73)), 0.3)
        self.assertEqual(result.outcome, "stopped")  # the second gesture "returned False": nothing further


class DevEntryTests(unittest.TestCase):
    """The dev-only walk entry: run by the runner only, through the agent's Runtime, never offered to a user."""

    ENTRY = "MaaNikki_AndroidWalkProbe"

    def test_the_agent_allows_the_entry_only_when_the_runner_names_it(self):
        from unittest.mock import patch
        import host.android as android
        self.assertIn(self.ENTRY, android.DEV_ENTRIES)
        verified, experimental, excluded = android.task_tiers()
        self.assertNotIn(self.ENTRY, set(verified) | set(experimental) | set(excluded))
        env = {k: v for k, v in os.environ.items() if k != android.DEV_ENTRIES_ENV}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(android.task_allowed(self.ENTRY))
        for value, allowed in ((self.ENTRY, True), (f"MaaNikki_Other, {self.ENTRY}", True),
                               ("MaaNikki_Other", False), ("", False)):
            with self.subTest(value=value), patch.dict(os.environ, {**env, android.DEV_ENTRIES_ENV: value}, clear=True):
                self.assertEqual(android.task_allowed(self.ENTRY), allowed)
                self.assertFalse(android.task_allowed("MaaNikki_Other"))  # only names of the fixed set count

    def test_no_interface_task_list_or_package_offers_it(self):
        for path in ("interface.json", "build/android/interface-overlay.json", "agent/host/android/tasks.json",
                     "scripts/fixtures/windows-package-files.txt"):
            self.assertNotIn(self.ENTRY, (ROOT / path).read_text(encoding="utf-8"), path)
        self.assertNotIn("android_walk_probe", (ROOT / "scripts/fixtures/windows-package-files.txt").read_text())

    def test_the_runner_turns_options_into_the_parameters_and_enables_only_that_agent(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        sys.path.insert(0, str(ROOT / "scripts" / "android"))
        import runner
        override = runner.dev_override(self.ENTRY, {"hint": "9880.5,10010.6", "target": "9884.5,10016.5",
                                                    "timeout": "45", "leash": "12"})
        self.assertEqual(override, [{self.ENTRY: {"custom_action_param": {
            "kind": "android_walk_probe", "map": "miraland", "hint": [9880.5, 10010.6], "target": [9884.5, 10016.5],
            "timeout": 45.0, "leash": 12.0}}}])
        for bad in ({"target": "1,2"}, {"hint": "1,2", "target": "3"}, {"hint": "1,2", "target": "3,4", "x": "1"},
                    {"hint": "1,2", "target": "3,4", "timeout": "soon"}):
            with self.subTest(options=bad), self.assertRaises(runner.RunnerError):
                runner.dev_override(self.ENTRY, bad)
        with self.assertRaises(runner.RunnerError):  # not an interface task: the ordinary path refuses it
            runner.task_override(runner.android_interface(), self.ENTRY, {})
        tasks, _options = runner.interface_options(runner.android_interface())
        self.assertNotIn(self.ENTRY, [task.get("entry") for task in tasks])
        controller = {"name": "nikki_android"}
        from unittest.mock import patch
        with patch.dict(os.environ, {runner.DEV_ENTRIES_ENV: self.ENTRY}):
            self.assertNotIn(runner.DEV_ENTRIES_ENV, runner.agent_environment(controller, Path("/tmp/d")))
            self.assertEqual(runner.agent_environment(controller, Path("/tmp/d"), self.ENTRY)[runner.DEV_ENTRIES_ENV],
                             self.ENTRY)
        import host.android as android
        self.assertEqual(runner.DEV_ENTRIES_ENV, android.DEV_ENTRIES_ENV)

    def test_gameplay_runs_it_only_for_its_entry_and_kind_and_only_on_android(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from action import gameplay
        from host.android import walk_probe
        runtime = SimpleNamespace(stopped=False, log=Mock(), main=Mock())
        param = json.dumps({"kind": "android_walk_probe", "hint": [1, 2], "target": [3, 4]})

        def run(entry, raw, backend="android"):
            argv = SimpleNamespace(custom_action_param=raw, task_detail=SimpleNamespace(entry=entry))
            with patch.object(gameplay, "Executors", return_value=runtime), \
                    patch("host.game_backend", return_value=backend), \
                    patch.object(walk_probe, "run", return_value=True) as walk:
                return gameplay.GameplayAction.run.__wrapped__(None, None, argv), walk
        result, walk = run(self.ENTRY, param)
        self.assertTrue(result)
        walk.assert_called_once()
        self.assertEqual(walk.call_args.args[1]["target"], [3, 4])
        for entry, raw, backend in ((self.ENTRY, json.dumps({"kind": "main"}), "android"),
                                    ("MaaNikki_Mailbox", param, "android"), (self.ENTRY, param, "win32"),
                                    (self.ENTRY, param, "macos")):
            with self.subTest(entry=entry, raw=raw, backend=backend):
                result, walk = run(entry, raw, backend)
                self.assertFalse(result)
                walk.assert_not_called()
        runtime.main.assert_not_called()

    def test_the_parameters_are_checked_before_anything_runs(self):
        from host.android.walk_probe import Params
        good = {"map": "miraland", "hint": [9880.5, 10010.6], "target": [9884.5, 10016.5]}
        self.assertEqual((Params(good).timeout, Params(good).leash, Params(good).radius), (60.0, 12.0, 1.8))
        for change in ({"map": ""}, {"hint": [1]}, {"target": [1, float("nan")]}, {"timeout": 500},
                       {"leash": 50}, {"radius": 9}, {"target": [9900, 10010.6]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                Params({**good, **change})

    def test_off_android_it_refuses_before_anything_else(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from host.android import walk_probe
        rt = SimpleNamespace(log=Mock(), stopped=False)
        with patch("host.game_backend", return_value="win32"), \
                patch("host.android.minimap.MinimapReader", side_effect=AssertionError("no map load")):
            self.assertFalse(walk_probe.run(rt, {"map": "miraland", "hint": [1, 2], "target": [3, 4]}))
        rt.log.assert_called_once()

    def test_each_step_and_its_frame_go_to_the_debug_folder_with_the_uid_masked(self):
        import numpy as np
        import cv2
        from host.android.walk_probe import DiagnosticIo
        with tempfile.TemporaryDirectory() as folder:
            io = DiagnosticIo(object(), Path(folder) / "walk")
            frame = np.full((720, 1280, 3), 200, np.uint8)
            name = io.record("step", {"step": 0, "fix": {"x": 1.0}, "angle": 75.7, "seconds": 1.5}, frame)
            io.record("moved", {"step": 0, "moved": 4.2}, None)
            saved = cv2.imread(str(Path(folder) / "walk" / name))
            self.assertEqual(int(saved[710, 10].max()), 0)  # the UID corner is blacked out
            self.assertEqual(int(saved[360, 640].min()), 200)
            lines = [json.loads(line) for line in (Path(folder) / "walk/walk.jsonl").read_text(encoding="utf-8")
                     .splitlines()]
            self.assertEqual([(line["kind"], line["frame"]) for line in lines], [("step", name), ("moved", None)])
            self.assertEqual(lines[0]["angle"], 75.7)


FRAMEWORK_CHECK = r"""
import json, sys
from pathlib import Path
import cv2
import numpy as np
from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.resource import Resource
from maa.tasker import Tasker

repo = Path(sys.argv[1])
sys.path.insert(0, str(repo / "agent"))
from host.android import actions, move

fixtures = repo / "scripts/fixtures/android"
crop = lambda name: cv2.imread(str(fixtures / name))
duck = cv2.imread(str(repo / "resource_android/image/android/menu_duck.png"))
row, remark, settings = crop("hud_label_row.png"), crop("move_companion_remark.jpg"), crop("page_title.png")

def hud():
    frame = np.full((720, 1280, 3), 90, np.uint8)
    frame[18:72, 30:84], frame[50:100, 600:1280] = duck, row
    return frame

frames = {"hud": hud(), "blank": np.full((720, 1280, 3), 90, np.uint8)}
frames["hud with a companion remark"] = hud()
frames["hud with a companion remark"][505:592, 380:900] = remark
frames["page"] = np.full((720, 1280, 3), 90, np.uint8)
frames["page"][10:10 + settings.shape[0], 0:settings.shape[1]] = settings
current, results = {}, {}

class Frame(CustomController):
    def connect(self): return True
    def request_uuid(self): return "android-move-test"
    def screencap(self): return current["frame"]
    def refuse(self, *args): return False
    start_app = stop_app = click = swipe = touch_down = touch_move = touch_up = refuse
    click_key = input_text = key_down = key_up = refuse

class Judge(CustomAction):
    def run(self, context, argv):
        image = context.tasker.controller.post_screencap().wait().get()
        def recognize(node, override=None):
            return context.run_recognition(node, image, {node: override} if override else None)
        results[current["name"]] = list(move.classify(actions.read_screen(recognize), recognize))
        return CustomAction.RunResult(success=True)

resource = Resource()
for bundle in (repo / "resource", repo / "resource_android"):
    assert resource.post_bundle(bundle).wait().succeeded, bundle
action = Judge()
assert resource.register_custom_action("judge", action)
controller = Frame()
assert controller.post_connection().wait().succeeded
tasker = Tasker()
assert tasker.bind(resource, controller)
entry = {"Judge": {"recognition": "DirectHit", "action": "Custom", "custom_action": "judge", "next": []}}
for name, frame in frames.items():
    current.update(name=name, frame=frame)
    tasker.post_task("Judge", entry).wait()
print(json.dumps(results, ensure_ascii=False))
"""


@unittest.skipUnless((ROOT / "maafw").exists(), "needs the MaaFramework runtime in ./maafw")
class FrameworkTests(unittest.TestCase):
    """The narration band and the screen classes through the real nodes and OCR (both layers), on frames composed from
    device crops: the HUD label row and duck, the companion remark 「大喵」 line from a saved frame (it appeared over
    the calendar there; no frame of a remark over the HUD was saved, so its place over the HUD is assumed the same)."""

    def test_the_hud_the_narration_band_and_other_screens(self):
        if not (ROOT / "resource_android").is_dir():
            self.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
            return
        env = {**os.environ, "MAAFW_BINARY_PATH": str(ROOT / "maafw")}
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", "-c", FRAMEWORK_CHECK, str(ROOT)],
                                capture_output=True, text=True, encoding="utf-8", timeout=300, env=env,
                                cwd=tempfile.gettempdir())
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(outcome["hud"], ["hud", ""])
        self.assertEqual(outcome["hud with a companion remark"], ["narration", ""])
        self.assertEqual(outcome["blank"], ["transition", ""])
        self.assertEqual(outcome["page"][0], "other")
        self.assertTrue(outcome["page"][1].startswith("另一个页面"))

    def test_the_narration_node(self):
        nodes = json.loads((ROOT / "resource_android/pipeline/android_move.json").read_text(encoding="utf-8"))
        self.assertEqual(list(nodes), [move.NARRATION])
        node = nodes[move.NARRATION]
        self.assertEqual((node["recognition"], node["action"]), ("OCR", "DoNothing"))
        x, y, w, h = node["roi"]
        self.assertGreaterEqual(y, 470)  # below the HUD label row and the quest tracker
        self.assertLessEqual(y + h, 600)  # above the bottom HUD text (心誓, at y ~680)


if __name__ == "__main__":
    unittest.main()
