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
PROMPT_EXTENT = (747, 324, 1021, 360)  # prompt_verdict's extent on m2-rl10 t00-b-hud
PROMPT_CROP = (740, 310, 1040, 380)  # where each move_prompt_column.jpg crop was cut from its 1280x720 frame


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
        self.stop_after_observations = None
        self.bell_raises = None
        self.captured, self.started, self.not_afters = {}, {}, []
        self.present = True  # the minimap chevron on the next frames
        self.regions = lambda number: ("登上鸣鸣列车", move.TRACKER_DISTANCE)  # the tracker's rows on frame ``number``
        self.name_only = lambda number: False  # Verdict.name_only on frame ``number``
        self.prompts = lambda number: (PROMPT_EXTENT,) if self.kinds_seen.get(number) == "hud_prompt" else ()
        self.fragments = lambda number: ()  # Verdict.fragments: text the noise filters dropped
        self.kinds_seen = {}

    def stopped(self):
        return (self.stop_after_steps is not None and len(self.steps) >= self.stop_after_steps) or (
            self.stop_after_observations is not None and self.observed >= self.stop_after_observations)

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
        frame = Frame(self.observed, (x, y), kind, reason=reason, present=self.present)
        self.frames.append(frame)
        self.captured[frame.number] = self.clock()
        self.started[frame.number] = at
        self.kinds_seen[frame.number] = kind
        return Observation(frame, kind, at, reason, self.clock(), self.regions(self.observed),
                           name_only=self.name_only(self.observed), prompts=self.prompts(self.observed),
                           fragments=self.fragments(self.observed))

    BELL_S = 4.0

    def step(self, frame, angle, seconds, jump_at, *, not_after=None):
        assert frame is self.frames[-1], "a step must be decided on the frame just captured"
        assert not_after is not None and not_after <= self.started[frame.number] + move.MAX_DECISION_AGE_S + 1e-9
        self.not_afters.append(not_after)
        assert not any(kind == "step" and record["step"] == len(self.steps) for kind, record in self.records), \
            "a step's record is written only after its lift"
        ok = self.step_results.pop(0) if self.step_results else True
        if isinstance(ok, BaseException):
            raise ok  # raised before contact-down: nothing sent
        self.steps.append({"frame": frame, "angle": angle, "seconds": seconds, "jump_at": jump_at,
                           "position": self.world.position})
        self.world.before = self.world.position
        if ok and not self.world.blocked:
            heading = math.radians((self.world.yaw + angle - 270) % 360)
            distance = move.expected_distance(seconds) * self.world.gain
            x, y = self.world.position
            self.world.position = (x + distance * math.sin(heading), y - distance * math.cos(heading))
        self.clock.t += seconds + self.OVERHEAD_S
        self.lift_at = self.clock()
        return ok

    def bell(self, frame, *, not_after=None):
        assert frame is self.frames[-1]
        assert not_after is not None and not_after <= self.started[frame.number] + move.MAX_DECISION_AGE_S + 1e-9
        self.not_afters.append(not_after)
        if self.bell_raises is not None:
            error, self.bell_raises = self.bell_raises, None
            raise error
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


def frame_captured(io, frame):
    return io.captured.get(getattr(frame, "number", None), None)


class FakeReader:
    """Reads the frame's minimap position exactly, the world's yaw as the camera heading; ``bad`` frames (by
    number) read as a weak or misplaced match."""

    def __init__(self, world, *, score=0.75, bad=None, heading_missing=(), slow=(), clock=None, bounds=None):
        self.world, self.score, self.bad, self.heading_missing = world, score, dict(bad or {}), set(heading_missing)
        self.slow, self.clock, self.bounds = set(slow), clock, bounds  # slow: frames whose decision takes 1.2 s
        self.calls = []

    def present(self, frame):
        return frame.present

    def within(self, x, y):
        return self.bounds is None or (0 <= x < self.bounds[0] and 0 <= y < self.bounds[1])

    def fix(self, frame, hint, search_radius):
        self.calls.append((frame.number, hint, search_radius))
        if frame.number in self.slow and self.clock is not None:
            self.clock.t += 1.2
        x, y = frame.position
        heading = None if frame.number in self.heading_missing else self.world.yaw
        if frame.number in self.bad:
            return self.bad[frame.number]
        return Fix(x, y, self.score, heading, 1.0 if heading is not None else 0.0, self.score >= 0.55, "luma")


def walker(world, *, kinds=None, step_results=None, reader=None, clock=None, **options):
    clock = clock or Clock()
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
        self.assertEqual(move.step_seconds(6.2), 1.3)
        self.assertEqual(move.step_seconds(7.44), 1.3)  # beyond one step's reach: the longest step
        self.assertEqual(move.step_seconds(100), move.MAX_STEP_S)
        self.assertEqual(move.MAX_STEP_S, 1.3)
        self.assertAlmostEqual(move.REACH_U, 6.2)
        self.assertAlmostEqual(move.REACH_U, move.expected_distance(move.MAX_STEP_S))
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
        self.assertTrue(all(step["seconds"] <= move.MAX_STEP_S for step in io.steps))
        self.assertTrue(all(step["frame"].kind == "hud" for step in io.steps))

    def test_the_longest_hold_leaves_a_dispatch_margin_under_the_gesture_cap(self):
        """On the device a 1500 ms hold (= the cap) overran; 1385 and 1126 ms were confirmed. Every planned hold, and
        the deadline-based not_after, keep HOLD_MARGIN_S for the lift's dispatch latency; the bell too."""
        from host.android import gesture
        self.assertEqual(gesture.GESTURE_MAX_S, 1.5)  # the cap itself is unchanged
        self.assertGreaterEqual(move.HOLD_MARGIN_S, 0.2)
        self.assertLessEqual(move.MAX_STEP_S + move.HOLD_MARGIN_S, gesture.GESTURE_MAX_S + 1e-9)
        self.assertLess(move.MAX_STEP_S, 1.385 + 1e-9)  # no longer than the longest hold confirmed on the device
        self.assertEqual(move.LIFT_MARGIN_S, move.HOLD_MARGIN_S)
        self.assertLessEqual(move.RuntimeIo.BELL_S + move.HOLD_MARGIN_S, gesture.BELL_PRESS_MAX_S)
        world = World((0, 0), yaw=0.0)
        walk, io = walker(world)
        self.assertTrue(walk.walk([Goal(0, -40)], (0, 0), 120).ok)
        self.assertEqual(max(step["seconds"] for step in io.steps), move.MAX_STEP_S)

    def test_a_bell_press_without_the_dispatch_margin_is_refused(self):
        walk, io = walker(World((0, 0), yaw=90.0), allow_bell=True)
        io.BELL_S = 4.4  # under the bell's 4.5 s cap, but not by HOLD_MARGIN_S
        result = walk.walk([Goal(0.5, 0, 1.2, action="bell")], (0, 0), 30)
        self.assertEqual(result.outcome, "refused", result.message)
        self.assertEqual(io.not_afters, [])

    def test_far_steps_do_not_wait_for_the_minimap_to_settle(self):
        world = World((0, 0), yaw=200.0, lag=0.3)
        walk, io = walker(world)
        result = walk.walk([Goal(0, -30)], (0, 0), 120)
        self.assertTrue(result.ok, result.message)
        far = [index for index, step in enumerate(io.steps) if math.dist(step["frame"].position, (0, -30))
               - move.LAG_U > move.REACH_U]
        self.assertGreaterEqual(len(far), 2)
        self.assertFalse([after for after, _seconds in io.pauses if after in far and after > 0 and after - 1 in far])
        self.assertEqual({io.steps[i]["seconds"] for i in far}, {move.MAX_STEP_S})

    def test_steps_are_replanned_with_the_camera_yaw_of_each_frame(self):
        world = World((0, 0), yaw=10.0)
        walk, io = walker(world)
        original = io.step

        def step(frame, angle, seconds, jump_at, **kw):
            ok = original(frame, angle, seconds, jump_at, **kw)
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

    def test_an_unrecognised_frame_ends_the_walk_at_once_with_that_frame_saved(self):
        world = World((0, 0), yaw=0.0)
        walk, io = walker(world, kinds=["hud", ("other", "认不出的画面"), "hud"])
        result = walk.walk([Goal(0, -6)], (0, 0), 60)
        self.assertEqual(result.outcome, "screen")
        self.assertEqual(len(io.steps), 1)
        self.assertEqual(io.observed, 2)  # no grace: the HUD coming back is never looked at
        self.assertEqual(io.saved[0][2].number, 2)

    def test_one_blank_capture_is_tried_again(self):
        world = World((0, 0), yaw=0.0)
        walk, io = walker(world, kinds=["hud", "blank", "hud"])
        result = walk.walk([Goal(0, -6)], (0, 0), 60)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(io.saved, [])

    def test_two_blank_captures_in_a_row_stop_with_the_frame_saved(self):
        world = World((0, 0))
        walk, io = walker(world, kinds=["hud", "blank", "blank", "hud"])
        result = walk.walk([Goal(0, -20)], (0, 0), 60)
        self.assertEqual(result.outcome, "screen")
        self.assertEqual(len(io.steps), 1)
        self.assertEqual(io.saved[0][2].number, 3)

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


class Row:
    def __init__(self, text, box, score=0.99):
        self.text, self.box, self.score = text, box, score


class Ocr:
    def __init__(self, rows):
        self.hit, self.box = bool(rows), (rows[0].box if rows else None)
        self.filtered_results, self.best_result = rows, (rows[0] if rows else None)


def hud_with(*rows, ocr_fails=False):
    """classify() on a positively recognised HUD whose full-frame OCR reads ``rows`` [(text, [x, y, w, h])]."""
    from host.android.actions import Screen

    def recognize(node, override=None):
        assert node == move.SCREEN_TEXT
        return None if ocr_fails else Ocr([Row(*row) for row in rows])
    verdict = move.classify(Screen("hud", {"duck": [30, 18, 54, 54]}), recognize)
    return verdict.kind, verdict.reason


WIND = (("暖暖", [609, 506, 59, 31]), ("起风了——大喵小心，别被吹跑啦。", [446, 557, 374, 30]))  # m2-goto1, as read
BELL = (("暖暖，", [401, 561, 73, 22]), ("星之铃似乎在闪烁呢！摇动它试试吧", [466, 555, 390, 31]),
        ("大喵", [608, 502, 61, 38]))  # phase0/hud-1280x720.png, as read
LABEL_ROW = (("商城", [638, 62, 38, 22]), ("共鸣", [708, 62, 38, 22]),
             ("奇迹之旅活动大厅奇想日历无限之心", [761, 62, 284, 22]), ("任务", [1059, 62, 38, 22]),
             ("设计图", [1125, 62, 54, 22]), ("换装", [1203, 62, 38, 22]))  # where the saved frames read them
HUD_TEXT = (*LABEL_ROW, ("登上鸣鸣列车", [110, 235, 120, 25]), ("1049米", [120, 262, 60, 20]),
            ("心誓5×1", [520, 668, 200, 22]), ("UID 123456789", [0, 700, 90, 18]))


NO_MOVE = ("other", "misread")  # verdicts on which no step is ever decided


class ClassifyTests(unittest.TestCase):
    """What may move: the HUD and nothing else; narration only as one ambient line (rows as OCR read them)."""

    def test_the_hud_alone_and_its_own_text_move(self):
        self.assertEqual(hud_with()[0], "other")  # no positive full-frame evidence: no step
        self.assertEqual(hud_with(*HUD_TEXT), ("hud", ""))
        self.assertEqual(hud_with(*HUD_TEXT, ("口", [640, 610, 20, 20]), ("一飞", [240, 500, 800, 95], 0.52)),
                         ("hud", ""))

    def test_the_two_saved_ambient_remarks_are_narration(self):
        self.assertEqual(hud_with(*WIND, *HUD_TEXT), ("narration", ""))
        self.assertEqual(hud_with(*BELL, *HUD_TEXT), ("narration", ""))

    def test_prompts_dialogue_banners_and_buttons_are_no_narration(self):
        cases = {
            "tap to continue": (("点击继续", [560, 560, 160, 28]),),
            "quest start": (("任务开始", [560, 560, 160, 28]),),
            "remark with a tap prompt": (*WIND, ("点击屏幕继续", [520, 640, 240, 24])),
            "top banner": (("获得新的服装", [500, 140, 280, 40]),),
            "banner over the label row": (("活动开启：星海拾光", [700, 50, 300, 40]),),
            "prompt over the 心誓 bar": (("点击屏幕继续", [520, 660, 240, 24]),),
            "centre banner": (("星海·星梦群屿", [520, 300, 240, 40]),),
            "interaction prompt": (("查看", [794, 329, 55, 31]),),
            "pick-up prompt": (("拾取", [793, 328, 57, 32]),),
            "dialogue choice": (("我知道了", [900, 420, 160, 30]),),
            "two lines": (("暖暖，你看那边", [480, 556, 300, 28]), ("好像有什么东西", [480, 578, 300, 28])),
            "two labels": (("暖暖", [609, 506, 59, 31]), ("大喵", [560, 506, 59, 31]),
                           ("起风了", [560, 557, 120, 30])),
            "label only": (("暖暖", [609, 506, 59, 31]),),
            "line too high": (("起风了", [560, 505, 120, 30]),),
        }
        for name, rows in cases.items():
            with self.subTest(name):
                kind, reason = hud_with(*rows, *HUD_TEXT)
                self.assertIn(kind, NO_MOVE)
                self.assertTrue(reason)
                self.assertFalse(any(text in reason for text, _box in rows))  # no OCR text for the user

    def test_an_ocr_that_could_not_run_or_another_screen_stops(self):
        from host.android.actions import Screen
        self.assertEqual(hud_with(ocr_fails=True)[0], "other")
        self.assertEqual(move.classify(Screen("page", {}, title="设置"), lambda *a: None).kind, "other")
        self.assertEqual(move.classify(Screen("unknown", reason="认不出"), lambda *a: None).kind, "other")


class DeadlineTests(unittest.TestCase):
    def test_arrival_after_the_deadline_is_a_timeout(self):
        world = World((0, 0), yaw=0.0, lag=0.3)
        walk, io = walker(world)
        result = walk.walk([Goal(0, -6)], (0, 0), 1.5)  # the step plus its settle end past 1.5 s
        self.assertEqual(result.outcome, "timeout")
        self.assertEqual(len(io.saved), 1)
        self.assertIs(io.saved[0][2], io.frames[-1])

    def test_continuous_narration_ends_at_the_deadline_not_after_twenty_seconds(self):
        world = World((0, 0))
        walk, io = walker(world, kinds=["hud"] + ["narration"] * 100)
        result = walk.walk([Goal(0, -20)], (0, 0), 3)
        self.assertEqual(result.outcome, "timeout")
        self.assertLess(io.clock.t - 100, 4.5)

    def test_no_bell_press_after_the_deadline(self):
        world = World((0, 0), yaw=90.0)
        walk, io = walker(world, allow_bell=True)
        result = walk.walk([Goal(0.5, 0, 1.2, action="bell")], (0, 0), 0.2)  # arrived, but the time is up
        self.assertEqual(result.outcome, "timeout")
        self.assertEqual(io.bells, [])

    def test_a_final_wait_point_is_judged_again_on_its_fresh_fix(self):
        world = World((0, 0), yaw=90.0)
        walk, io = walker(world)
        original = io.pause

        def pause(seconds):  # something nudges the character during the wait
            if seconds == 0.9:
                world.position = (world.position[0] + 2.5, world.position[1])
            return original(seconds)
        io.pause = pause
        result = walk.walk([Goal(4, 0, 1.6, action="wait", params={"seconds": 0.9})], (0, 0), 60)
        self.assertTrue(result.ok, result.message)
        self.assertLessEqual(math.dist(world.position, (4, 0)), 1.6)
        self.assertEqual([seconds for _after, seconds in io.pauses].count(0.9), 1)  # the wait is not repeated


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


class ResultTests(unittest.TestCase):
    def test_a_stop_seen_right_before_arrival_reports_no_arrival(self):
        world = World((0, 0), yaw=0.0, lag=0.3)
        walk, io = walker(world)
        io.stop_after_observations = 3  # the settled frame that would report arrival
        result = walk.walk([Goal(0, -6)], (0, 0), 60)
        self.assertEqual(result.outcome, "stopped")
        self.assertFalse(result.ok)

    def test_a_successful_walk_reports_its_steps(self):
        world = World((0, 0), yaw=0.0)
        walk, io = walker(world)
        result = walk.walk([Goal(0, -20)], (0, 0), 60)
        self.assertTrue(result.ok)
        self.assertEqual((result.attempted, result.confirmed), (len(io.steps), len(io.steps)))
        self.assertGreaterEqual(result.attempted, 3)
        written = [record for kind, record in io.records if kind == "step"]
        self.assertEqual(len(written), result.attempted)
        self.assertTrue(all(record["frame"].startswith("frame-") for record in result.steps))

    def test_a_step_that_returns_false_counts_as_attempted_not_confirmed(self):
        world = World((0, 0))
        walk, io = walker(world, step_results=[True, False])
        result = walk.walk([Goal(0, -40)], (0, 0), 120)
        self.assertEqual((result.outcome, result.attempted, result.confirmed), ("stopped", 2, 1))

    def test_a_decision_that_aged_out_is_captured_again_and_replanned(self):
        world = World((0, 0), yaw=0.0)
        clock = Clock()
        walk, io = walker(world, clock=clock, reader=FakeReader(world, slow={1}, clock=clock))
        result = walk.walk([Goal(0, -6)], (0, 0), 60)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(io.steps[0]["frame"].number, 2)  # frame 1's decision took 1.2 s: not sent on it

    def test_decisions_that_keep_aging_out_stop(self):
        world = World((0, 0))
        clock = Clock()
        walk, io = walker(world, clock=clock, reader=FakeReader(world, slow=set(range(1, 10)), clock=clock))
        result = walk.walk([Goal(0, -6)], (0, 0), 60)
        self.assertEqual(result.outcome, "screen")
        self.assertEqual(io.steps, [])

    def test_a_settled_fix_that_jumps_past_the_lag_is_a_bad_fix_not_arrival(self):
        world = World((0, 0), yaw=0.0)
        # step 1 (7.44 u) is read right; the settle recheck jumps 4.96 u further, twice: lost, never arrived
        jump = Fix(0, -12.4, 0.78, 0.0, 1.0, True)
        reader = FakeReader(world, bad={3: jump, 4: jump})
        walk, io = walker(world, reader=reader)
        result = walk.walk([Goal(0, -13, 1.0)], (0, 0), 60)
        self.assertEqual(result.outcome, "lost")
        self.assertEqual(len(io.steps), 1)

    def test_coordinates_outside_the_map_are_refused_before_anything(self):
        world = World((10, 10))
        for hint, goal in (((-5, 10), (10, 5)), ((10, 10), (10, 50000))):
            with self.subTest(hint=hint, goal=goal):
                walk, io = walker(world, reader=FakeReader(world, bounds=(1000, 1000)))
                result = walk.walk([Goal(*goal)], hint, 60)
                self.assertEqual((result.outcome, io.observed, io.steps), ("refused", 0, []))
                self.assertIn("地图之外", result.message)

    def test_a_first_fix_outside_the_routes_start_radius_sends_nothing(self):
        world = World((0, 0))
        walk, io = walker(world)
        result = walk.walk([Goal(10, 0), Goal(20, 0)], (0, 0), 60, start_radius=9)
        self.assertEqual(result.outcome, "refused")
        self.assertEqual(io.steps, [])
        self.assertEqual(len(io.saved), 1)
        walk, io = walker(World((0, 0), yaw=90.0))
        self.assertTrue(walk.walk([Goal(8, 0), Goal(12, 0)], (0, 0), 60, start_radius=9).ok)

    def test_walk_route_passes_the_start_radius(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        route = SimpleNamespace(map_id="starsea", points=(SimpleNamespace(x=1, y=2, radius=1.2, jump=False,
                                                                          action="", params={}),),
                                timeout=900.0, start_radius=9.0)
        with patch("host.android.minimap.MinimapReader"), patch.object(move.Walker, "walk") as walk:
            move.walk_route(object(), route, (1, 2))
        self.assertEqual(walk.call_args.kwargs["start_radius"], 9.0)


TRACKER = ("登上鸣鸣列车", move.TRACKER_DISTANCE)


class ReviewRound2Tests(unittest.TestCase):
    """Movement review round 2: each finding's scenario, replayed (rows as OCR would read them)."""

    # 1: the HUD's text regions must keep their own content
    def test_1_prompts_in_the_hud_regions_or_below_the_floor_stop(self):
        cases = {
            "点击继续 in the UID corner": (("点击继续", [20, 690, 120, 24]),),
            "新任务 in the label row": (("新任务", [1000, 60, 80, 24]),),
            "a standalone tap marker": (("▼", [900, 600, 20, 20]),),
            "a combat label": (("Lv. 40", [600, 200, 80, 24], 0.95),),
            "查看 read at 0.79": (("查看", [794, 329, 55, 31], 0.79),),
            "a prompt in the 心誓 bar": (("心誓5 点击领取", [520, 668, 240, 22]),),
        }
        for name, rows in cases.items():
            with self.subTest(name):
                self.assertIn(hud_with(*rows, *HUD_TEXT)[0], NO_MOVE)

    def test_1_only_the_observed_forms_of_the_huds_own_text_move(self):
        observed = (("LID123456789", [0, 700, 90, 18]), ("D123456789", [0, 700, 90, 18]), ("46m", [80, 700, 30, 18]),
                    ("45mm", [80, 700, 30, 18]), ("心誓5や♡", [520, 668, 120, 22]), ("33333×1", [600, 668, 80, 22]),
                    ("2A1", [966, 535, 30, 20], 0.79), ("ittit", [1017, 95, 30, 14], 0.69))
        for row in observed:
            with self.subTest(row[0]):
                self.assertEqual(hud_with(*HUD_TEXT, row), ("hud", ""))
        unobserved = (("活动人厅", [833, 62, 72, 22]), ("共吗", [708, 62, 38, 22]), ("23Gms", [80, 700, 40, 18]),
                      ("UID1234567A9", [0, 700, 90, 18]), ("商城", [900, 62, 38, 22]),  # 商城 out of its place
                      ("126", [320, 40, 30, 16]), ("1234", [600, 668, 60, 22]))
        for row in unobserved:
            with self.subTest(row[0]):
                self.assertIn(hud_with(*HUD_TEXT, row)[0], NO_MOVE)

    def test_1_the_first_frame_must_be_a_clean_hud(self):
        for name, kinds, regions in (("quest start in the tracker", ["hud"], lambda n: ("任务开始",)),
                                     ("two rows in the tracker", ["hud"], lambda n: ("登上鸣鸣列车", "新的委托")),
                                     ):
            with self.subTest(name):
                walk, io = walker(World((0, 0)), kinds=kinds)
                io.regions = regions
                result = walk.walk([Goal(0, -6)], (0, 0), 60)
                self.assertEqual((result.outcome, io.steps), ("refused", []))
                self.assertEqual(io.saved[0][2].number, 1)

    def test_1_a_tracker_change_stops_but_its_distance_may_go_unread(self):
        walk, io = walker(World((0, 0), yaw=0.0))
        io.regions = lambda n: TRACKER if n != 2 else ("登上鸣鸣列车",)
        self.assertTrue(walk.walk([Goal(0, -12)], (0, 0), 60).ok)
        for changed in (("登上鸣鸣列车", "任务开始"), ("离开鸣鸣列车", move.TRACKER_DISTANCE),
                        ("登上鸣鸣列车", move.TRACKER_DISTANCE, move.TRACKER_DISTANCE)):
            with self.subTest(changed=changed):
                walk, io = walker(World((0, 0)))
                io.regions = lambda n, changed=changed: TRACKER if n == 1 else changed
                result = walk.walk([Goal(0, -20)], (0, 0), 60)
                self.assertEqual((result.outcome, len(io.steps), io.observed), ("screen", 1, 2))
                self.assertEqual(io.saved[0][2].number, 2)
        walk, io = walker(World((0, 0)))
        io.regions = lambda n: TRACKER if n == 1 else ("登上鸣鸣列车", "任务开始")
        result = walk.walk([Goal(0, -20)], (0, 0), 60)
        self.assertEqual((result.outcome, len(io.steps), io.observed), ("screen", 1, 2))
        self.assertEqual(io.saved[0][2].number, 2)

    # 2: narration only from a known ambient speaker, one plain line
    def test_2_codexs_dialogue_and_instruction_cases_stop(self):
        cases = {
            "villager, line and continue marker": (("村民", [609, 506, 59, 31]), ("你也来了吗", [560, 557, 160, 30]),
                                                    ("▼", [740, 560, 20, 20])),
            "villager and a line": (("村民", [609, 506, 59, 31]), ("你也来了吗", [560, 557, 160, 30])),
            "an instruction without a label": (("去找大喵", [580, 557, 120, 30]),),
            "an instruction from a known speaker": (("大喵", [608, 502, 61, 38]), ("暖暖，去找大喵吧", [520, 557, 240, 30])),
            "two choices on one line": (("暖暖", [609, 506, 59, 31]), ("好的", [420, 560, 60, 28]),
                                        ("再想想", [760, 560, 90, 28])),
            "a long line": (("暖暖", [609, 506, 59, 31]),
                            ("这是一句非常非常长的话，长到不像一句随口说的环境旁白了吧呢", [300, 557, 700, 30])),
        }
        for name, rows in cases.items():
            with self.subTest(name):
                self.assertEqual(hud_with(*rows, *HUD_TEXT)[0], "other")
        self.assertEqual(hud_with(*WIND, *HUD_TEXT)[0], "narration")  # the saved remarks still pass
        self.assertEqual(hud_with(*BELL, *HUD_TEXT)[0], "narration")

    # 3: a captured frame missing a required HUD element stops; only a capture with no image is retried
    def test_3_a_frame_without_the_minimap_chevron_stops_on_that_frame(self):
        walk, io = walker(World((0, 0)))
        original = io.observe

        def observe():
            io.present = io.observed < 1  # frame 2 on: the chevron is covered
            return original()
        io.observe = observe
        result = walk.walk([Goal(0, -20)], (0, 0), 60)
        self.assertEqual((result.outcome, len(io.steps), io.observed), ("screen", 1, 2))
        self.assertEqual(io.saved[0][2].number, 2)

    def test_3_and_13_runtime_io_judges_every_returned_image(self):
        import numpy as np
        from types import SimpleNamespace
        from unittest.mock import patch
        from host.android.actions import Screen
        from host.android.target import TouchSnapshot
        dark = np.full((720, 1280, 3), 3, np.uint8)
        dark[300:320, 600:700] = 9  # a loading screen with a faint label (std ~1.3)
        cases = {"no image": (None, True, TouchSnapshot("none", 1, 1), "blank"),
                 "dark loading screen": (dark, True, TouchSnapshot("none", 1, 1), "other"),
                 "focus changed since": (dark, False, TouchSnapshot("none", 1, 1), "other"),
                 "contact held": (dark, True, TouchSnapshot("injected", 1, None), "other"),
                 "touch unreadable": (dark, True, None, "other")}
        for name, (frame, valid, touch, kind) in cases.items():
            with self.subTest(name):
                rt = SimpleNamespace(stopped=False, _fresh_device_frame=lambda: frame,
                                     _frame_still_valid=lambda f: valid, _recognizer_on=lambda f: (lambda *a: None))
                state = SimpleNamespace(touch_of=lambda f: touch)
                with patch("host.device_input", return_value=state), patch("host.android_user_stop",
                                                                           return_value=False), \
                        patch("host.android.actions.read_screen", return_value=Screen("unknown", reason="认不出")):
                    obs = move.RuntimeIo(rt).observe()
                self.assertEqual(obs.kind, kind)
        hud_rows = Ocr([Row(*row) for row in HUD_TEXT])
        with patch("host.device_input", return_value=SimpleNamespace(touch_of=lambda f: TouchSnapshot("none", 1, 1))), \
                patch("host.android.actions.read_screen", return_value=Screen("hud", {"duck": [30, 18, 54, 54]})):
            rt = SimpleNamespace(stopped=False, _fresh_device_frame=lambda: dark, _frame_still_valid=lambda f: True,
                                 _recognizer_on=lambda f: (lambda node, override=None: hud_rows))
            obs = move.RuntimeIo(rt).observe()
            self.assertEqual((obs.kind, obs.regions), ("hud", TRACKER))
            rt._recognizer_on = lambda f: (lambda node, override=None: Ocr([]))  # a full-frame OCR miss
            self.assertEqual(move.RuntimeIo(rt).observe().kind, "other")

    def test_13_a_held_contact_frame_never_declares_arrival(self):
        walk, io = walker(World((5, 5)), kinds=[("other", "画面（截图时设备上有按住的触点，或读不出触摸状态）")])
        result = walk.walk([Goal(5, 5)], (5, 5), 60)  # already there
        self.assertEqual(result.outcome, "refused")
        self.assertFalse(result.ok)

    # 4: the frame's expiry travels to contact-down; the bell takes the same rule
    def test_4_an_expired_dispatch_is_decided_again_on_a_new_frame(self):
        from host.android.gesture import DecisionExpired
        walk, io = walker(World((0, 0), yaw=0.0), step_results=[DecisionExpired("late"), True])
        result = walk.walk([Goal(0, -6)], (0, 0), 60)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(io.steps[0]["frame"].number, 2)  # the first call (frame 1) raised before anything was sent
        self.assertEqual(result.attempted, 1)

    def test_4_dispatches_that_keep_expiring_stop(self):
        from host.android.gesture import DecisionExpired
        walk, io = walker(World((0, 0)), step_results=[DecisionExpired("late")] * 5)
        result = walk.walk([Goal(0, -6)], (0, 0), 60)
        self.assertEqual((result.outcome, result.attempted), ("screen", 0))

    def test_4_the_bell_follows_the_age_rule(self):
        from host.android.gesture import DecisionExpired
        world = World((0, 0), yaw=90.0)
        clock = Clock()
        walk, io = walker(world, allow_bell=True, clock=clock, reader=FakeReader(world, slow={1}, clock=clock))
        io.bell_raises = DecisionExpired("late")
        result = walk.walk([Goal(0.5, 0, 1.2, action="bell")], (0, 0), 60)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(len(io.bells), 1)
        self.assertGreaterEqual(io.bells[0].number, 3)  # frame 1 aged out, frame 2's press expired

    # 5: the deadline bounds steps, waits, the bell and the report
    def test_5_a_step_is_cut_to_the_time_left_and_none_starts_without_enough(self):
        walk, io = walker(World((0, 0), yaw=0.0))
        result = walk.walk([Goal(0, -30)], (0, 0), 2.0)
        self.assertEqual(result.outcome, "timeout")
        self.assertEqual([round(step["seconds"], 2) for step in io.steps], [1.2])  # 2.0 - 0.3 capture - 0.5 margin
        self.assertLessEqual(io.clock.t - 100, 2.0 + FakeIo.CAPTURE_S + 1e-9)

    def test_5_a_long_wait_ends_at_the_deadline(self):
        walk, io = walker(World((0, 0), yaw=90.0))
        result = walk.walk([Goal(0.5, 0, 1.6, action="wait", params={"seconds": 10}), Goal(9, 0)], (0, 0), 3)
        self.assertEqual(result.outcome, "timeout")
        self.assertLessEqual(io.clock.t - 100, 3.0 + 1e-9)

    def test_5_the_bell_starts_only_when_its_whole_press_fits(self):
        walk, io = walker(World((0, 0), yaw=90.0), allow_bell=True)
        result = walk.walk([Goal(0.5, 0, 1.2, action="bell")], (0, 0), 4.0)  # 4.0 s press + margin > 3.7 s left
        self.assertEqual((result.outcome, io.bells), ("timeout", []))

    def test_5_a_slow_last_write_turns_arrival_into_a_timeout(self):
        walk, io = walker(World((0, 0)))
        original = io.record

        def record(kind, record, frame):
            io.clock.t += 5  # a slow diagnostic write
            return original(kind, record, frame)
        io.record = record
        result = walk.walk([Goal(0.3, 0)], (0, 0), 2)  # already there: only the first-fix record is written
        self.assertEqual(result.outcome, "timeout")

    # 8: one bound across the whole interval without input
    def test_8_chained_rechecks_cannot_add_up_past_the_lag(self):
        world = World((0, 0), yaw=0.0)
        reader = FakeReader(world, bad={3: Fix(0, -10.6, 0.78, None, 0.0, True), 4: Fix(0, -12.4, 0.78, 0.0, 1.0, True),
                                        5: Fix(0, -12.4, 0.78, 0.0, 1.0, True)})
        walk, io = walker(world, reader=reader)
        result = walk.walk([Goal(0, -13, 1.0)], (0, 0), 60)
        self.assertEqual((result.outcome, len(io.steps)), ("lost", 1))

    # 9: the start radius applies to the fix that authorises the first gesture
    def test_9_a_replacement_first_fix_outside_the_start_radius_sends_nothing(self):
        world = World((8.5, 0))
        clock = Clock()
        reader = FakeReader(world, slow={1}, clock=clock, bad={2: Fix(11, 0, 0.8, 0.0, 1.0, True)})
        walk, io = walker(world, clock=clock, reader=reader)
        result = walk.walk([Goal(0, 0), Goal(20, 0)], (8.5, 0), 60, start_radius=9)
        self.assertEqual((result.outcome, io.steps), ("refused", []))
        self.assertEqual(io.saved[0][2].number, 2)


class ReviewRound3Tests(unittest.TestCase):
    """Movement review round 3: each finding's scenario, replayed."""

    # 1: exact forms and places only; glyphs on the raw text; a positive full-frame pass
    def test_1_codexs_region_cases_stop(self):
        cases = {
            "换人 where 换装 is": (("换人", [1203, 62, 38, 22]),),
            "Lv. 40 in the UID corner": (("Lv. 40", [80, 700, 60, 18], 0.95),),
            "combat numbers on the 心誓 bar": (("1234", [600, 668, 60, 22]),),
            "combat numbers by the icon badges": (("999", [330, 40, 30, 16]),),
            "a marker after the quest name": (("登上鸣鸣列车▼", [110, 235, 140, 25]),),
        }
        for name, rows in cases.items():
            with self.subTest(name):
                rest = [row for row in HUD_TEXT if not (name.startswith("a marker") and row[0] == "登上鸣鸣列车")]
                self.assertIn(hud_with(*rows, *rest)[0], NO_MOVE)
        self.assertEqual(hud_with()[0], "other")  # a full-frame OCR miss authorises nothing
        self.assertEqual(hud_with(*HUD_TEXT[3:])[0], "other")  # only 3 of 9 labels read in place

    def test_1_a_first_frame_quest_without_its_distance_never_authorises_a_step(self):
        walk, io = walker(World((0, 0)))
        io.regions = lambda n: ("交谈",)
        result = walk.walk([Goal(0, -6)], (0, 0), 60)
        self.assertEqual((result.outcome, io.steps), ("refused", []))
        self.assertLessEqual(io.clock.t - 100, move.STARTUP_WAIT_S + 1.0)

    # 2: the line joined before the deny check
    def test_2_a_tap_prompt_split_across_boxes_stops(self):
        rows = (("暖暖", [609, 506, 59, 31]), ("现在点", [520, 557, 96, 30]), ("击屏幕", [614, 557, 96, 30]))
        self.assertEqual(hud_with(*rows, *HUD_TEXT)[0], "other")

    # 3: the chevron before the narration exception; a remark at the start is waited through
    def test_3_a_narration_frame_without_the_chevron_stops_on_that_frame(self):
        walk, io = walker(World((0, 0)), kinds=["hud", "narration", "hud"])
        original = io.observe

        def observe():
            io.present = io.observed != 1  # frame 2 (the narration frame) lacks the chevron
            return original()
        io.observe = observe
        result = walk.walk([Goal(0, -20)], (0, 0), 60)
        self.assertEqual((result.outcome, len(io.steps), io.observed), ("screen", 1, 2))
        self.assertEqual(io.saved[0][2].number, 2)

    def test_3_an_ambient_remark_at_the_start_is_waited_for_not_refused(self):
        walk, io = walker(World((0, 0), yaw=0.0), kinds=["narration", "narration", "hud"])
        result = walk.walk([Goal(0, -6)], (0, 0), 60)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(io.steps[0]["frame"].number, 3)
        walk, io = walker(World((0, 0)), kinds=["narration"] * 50)
        result = walk.walk([Goal(0, -6)], (0, 0), 60)
        self.assertEqual((result.outcome, io.steps), ("refused", []))

    def test_3_a_startup_remark_without_the_chevron_refuses(self):
        walk, io = walker(World((0, 0)), kinds=["narration"])
        io.present = False
        result = walk.walk([Goal(0, -6)], (0, 0), 60)
        self.assertEqual((result.outcome, io.observed, io.steps), ("refused", 1, []))

    # 4: the expiry counts from before the pixels were requested
    def test_4_a_slow_query_after_the_capture_ages_the_frame_from_its_request(self):
        world = World((0, 0), yaw=0.0)
        walk, io = walker(world)
        original = io.observe

        def observe():
            obs = original()
            if obs.frame.number == 1:
                io.clock.t += 2.0  # the post-capture focus query took 2 s: captured stamps late, at does not
                obs.captured = io.clock()
            return obs
        io.observe = observe
        result = walk.walk([Goal(0, -6)], (0, 0), 60)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(io.steps[0]["frame"].number, 2)
        self.assertTrue(all(n <= io.started[step["frame"].number] + move.MAX_DECISION_AGE_S + 1e-9
                            for n, step in zip(io.not_afters, io.steps)))

    # 5: a gesture that starts late cannot end past the deadline; waits are wall-clock bounded
    def test_5_not_after_leaves_room_for_the_hold_before_the_deadline(self):
        walk, io = walker(World((0, 0), yaw=0.0))
        walk.walk([Goal(0, -30)], (0, 0), 4.0)
        deadline = 100 + 4.0
        for not_after, step in zip(io.not_afters, io.steps):
            self.assertLessEqual(not_after + step["seconds"] + move.LIFT_MARGIN_S, deadline + 1e-9)

    def test_5_the_bell_cannot_end_past_the_deadline(self):
        walk, io = walker(World((0, 0), yaw=90.0), allow_bell=True)
        # 5 s budget: the frame's own expiry (capture request + 1.2 s) would let a 4 s press end at 105.4 > 105.0;
        # only the deadline cutoff (deadline - press - lift margin) keeps it inside: removing it fails this test
        walk.walk([Goal(0.5, 0, 1.2, action="bell")], (0, 0), 5.0)
        self.assertEqual(len(io.not_afters), 1)
        self.assertLess(io.not_afters[0], io.started[1] + move.MAX_DECISION_AGE_S)
        self.assertLessEqual(io.not_afters[0] + FakeIo.BELL_S + move.LIFT_MARGIN_S, 100 + 5.0 + 1e-9)

    def test_5_runtime_io_pause_is_bounded_wall_time_not_runtime_pause(self):
        import time as real_time
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        rt = SimpleNamespace(stopped=False, pause=Mock(side_effect=AssertionError("Runtime.pause may block")))
        with patch("host.android_user_stop", return_value=False):
            started = real_time.monotonic()
            self.assertTrue(move.RuntimeIo(rt).pause(0.1))
            self.assertLess(real_time.monotonic() - started, 0.5)
        rt.stopped = True
        with patch("host.android_user_stop", return_value=False):
            self.assertFalse(move.RuntimeIo(rt).pause(5))

    # 7: no-input rechecks bounded by the anchor and the preceding fix
    def test_7_false_peaks_either_side_of_the_anchor_cannot_add_up(self):
        world = World((0, 0), yaw=0.0)
        far = Fix(0, -10.84, 0.78, 0.0, 1.0, True)
        reader = FakeReader(world, bad={3: Fix(0, -4.04, 0.78, None, 0.0, True), 4: far, 5: far})
        walk, io = walker(world, reader=reader)
        result = walk.walk([Goal(0, -11.5, 1.0)], (0, 0), 60)
        self.assertEqual((result.outcome, len(io.steps)), ("lost", 1))

    # 8: a bell as the first gesture takes the start guard
    def test_8_a_first_bell_outside_the_start_radius_sends_nothing(self):
        world = World((8.5, 0))
        clock = Clock()
        reader = FakeReader(world, slow={1}, clock=clock, bad={2: Fix(11, 0, 0.8, 0.0, 1.0, True)})
        walk, io = walker(world, clock=clock, reader=reader, allow_bell=True)
        result = walk.walk([Goal(0, 0, 12, action="bell")], (8.5, 0), 60, start_radius=9)
        self.assertEqual((result.outcome, io.bells), ("refused", []))

    # 11: the jump is re-evaluated after the step is shortened
    def test_11_a_shortened_step_that_cannot_reach_drops_its_jump(self):
        walk, io = walker(World((0, 0), yaw=0.0), allow_jump=True)
        walk.walk([Goal(0, -6, 1.0, jump=True)], (0, 0), 1.5)  # 1.5 - 0.3 capture - 0.5 margin = 0.7 s: ~2.5 u
        self.assertEqual([(round(step["seconds"], 2), step["jump_at"]) for step in io.steps], [(0.7, None)])
        walk, io = walker(World((0, 0), yaw=0.0), allow_jump=True)
        walk.walk([Goal(0, -6, 1.0, jump=True)], (0, 0), 60)
        self.assertEqual(io.steps[0]["jump_at"], move.JUMP_AT_S)  # unshortened, it still jumps

    # 12: the probe's fix prints no OCR text
    def test_12_the_probe_fix_command_prints_no_ocr_text(self):
        import contextlib
        import io as text_io
        from types import SimpleNamespace
        from unittest.mock import patch
        sys.path.insert(0, str(ROOT / "scripts" / "android"))
        import probe
        verdict = move.Verdict("hud", "", ("登上鸣鸣列车", move.TRACKER_DISTANCE), ())
        recognizer = SimpleNamespace(on_one_frame=lambda function: verdict, last_frame=object(), release=lambda: None)
        reader = SimpleNamespace(fix=lambda *a: Fix(1, 2, 0.8, 10.0, 1.0, True), present=lambda frame: True)
        out = text_io.StringIO()
        with patch.object(probe, "Recognizer", return_value=recognizer), \
                patch.object(probe, "frame_controller", return_value=SimpleNamespace(show=lambda image: None)), \
                patch.object(probe, "read_frame", return_value=object()), patch.object(probe, "destroy_native"), \
                patch("host.android.minimap.MinimapReader", return_value=reader), contextlib.redirect_stdout(out):
            self.assertEqual(probe.main(["fix", "--map", "miraland", "--hint", "1", "2", "--frame", "f.png"]), 0)
        printed = out.getvalue()
        self.assertIn('"tracker"', printed)
        self.assertFalse(any("一" <= ch <= "鿿" for ch in printed), printed)  # no OCR-derived text


HUD_BLOCKS = (("hearts", 520.0, 720.0, 679.0, False), ("labels", 638.0, 676.0, 73.0, False),
              ("uid", 0.0, 90.0, 709.0, False))
PING = ("uid", 141.0, 168.0, 709.0, True)  # 46ms where c01--100-b read it


class MisreadRuleTests(unittest.TestCase):
    """A frame whose only failure is a permanent-HUD block's string (same blocks as the first frame): saved, one
    recapture with nothing sent, and the walk goes on only if that frame passes fully."""

    def walker(self, kinds, blocks=lambda n: HUD_BLOCKS):
        walk, io = walker(World((0, 0), yaw=0.0), kinds=kinds)
        io.blocks = blocks
        original = io.observe

        def observe():
            obs = original()
            obs.blocks = io.blocks(obs.frame.number)
            return obs
        io.observe = observe
        return walk, io

    def test_a_misread_then_a_clean_frame_goes_on_with_the_misread_frame_saved(self):
        walk, io = self.walker(["hud", ("misread", "读错"), "hud"])
        result = walk.walk([Goal(0, -12)], (0, 0), 60)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(io.saved[0][0], "android_move_misread")
        self.assertEqual(io.saved[0][2].number, 2)
        self.assertNotIn(2, [step["frame"].number for step in io.steps])  # never a step on the misread frame
        self.assertGreaterEqual(len(io.steps), 2)  # frame 3 passed fully and the walk went on

    def test_two_misreads_in_a_row_stop(self):
        walk, io = self.walker(["hud", ("misread", "读错"), ("misread", "读错")])
        result = walk.walk([Goal(0, -20)], (0, 0), 60)
        self.assertEqual((result.outcome, len(io.steps), io.observed), ("screen", 1, 3))
        self.assertEqual([frame.number for _stage, _reason, frame in io.saved], [2, 3])

    def test_a_misread_followed_by_narration_or_another_failure_stops(self):
        for after in ("narration", "blank", ("other", "另一个页面")):
            with self.subTest(after=after):
                walk, io = self.walker(["hud", ("misread", "读错"), after, "hud"])
                result = walk.walk([Goal(0, -20)], (0, 0), 60)
                self.assertEqual((result.outcome, len(io.steps), io.observed), ("screen", 1, 3))

    def test_an_added_or_moved_block_stops_at_once(self):
        for name, layout in (("added", HUD_BLOCKS + (("uid", 140.0, 180.0, 709.0, False),)),  # not a ping there
                             ("moved", (("hearts", 520.0, 720.0, 679.0, False), ("labels", 560.0, 600.0, 73.0, False),
                                        ("uid", 0.0, 90.0, 709.0, False)))):
            with self.subTest(name):
                walk, io = self.walker(["hud", ("misread", "读错"), "hud"],
                                       blocks=lambda n, layout=layout: HUD_BLOCKS if n == 1 else layout)
                result = walk.walk([Goal(0, -20)], (0, 0), 60)
                self.assertEqual((result.outcome, len(io.steps), io.observed), ("screen", 1, 2))
                self.assertEqual(io.saved[0][0], "android_move_screen")

    def test_classify_marks_only_in_place_misreads_and_still_counts_covered_labels(self):
        misread_row = (("奇迹之旅活动人厅奇想日历无限之心", [761, 62, 284, 22]),)
        rows = [row for row in HUD_TEXT if row[0] != "奇迹之旅活动大厅奇想日历无限之心"] + list(misread_row)
        self.assertEqual(hud_with(*rows)[0], "misread")
        self.assertEqual(hud_with(*HUD_TEXT, ("23Gms", [80, 700, 40, 18]))[0], "misread")
        self.assertEqual(hud_with(*rows, ("查看", [794, 329, 55, 31]))[0], "other")  # plus text elsewhere: other

    def test_same_blocks(self):
        self.assertTrue(move.same_blocks(HUD_BLOCKS, HUD_BLOCKS))
        self.assertTrue(move.same_blocks((("hearts", 520.0, 600.0, 679.0, False),) + HUD_BLOCKS[1:], HUD_BLOCKS))
        self.assertTrue(move.same_blocks(HUD_BLOCKS[:2], HUD_BLOCKS))  # a block not read this time is no new block
        self.assertTrue(move.same_blocks(HUD_BLOCKS + (PING,), HUD_BLOCKS))  # the ping may appear later
        self.assertFalse(move.same_blocks((("uid", 638.0, 676.0, 73.0, False),) + HUD_BLOCKS[::2], HUD_BLOCKS))
        self.assertFalse(move.same_blocks(HUD_BLOCKS + (("labels", 560.0, 600.0, 73.0, False),), HUD_BLOCKS[:1]))
        self.assertFalse(move.same_blocks((("hearts", 740.0, 775.0, 679.0, False),), HUD_BLOCKS))  # past the bar
        # one frame's incomplete read: a label or the bar's ×1 the first frame missed is in its slot, not new
        self.assertTrue(move.same_blocks((("labels", 1204.0, 1241.0, 73.0, False),
                                          ("hearts", 573.0, 688.0, 679.0, False)), HUD_BLOCKS[2:]))
        # real overlap: a block starting 10 px after the first frame's ended is a new block (review round 4, 4)
        self.assertFalse(move.same_blocks((("uid", 100.0, 110.0, 709.0, False),), HUD_BLOCKS))


class ReviewRound4Tests(unittest.TestCase):
    """Movement review round 4: each finding's scenario, replayed."""

    def test_the_hud_label_set_has_one_source(self):
        # The label row is account-specific: actions.HUD_LABEL_ROW is the one place it is kept and extended.
        from host.android import actions, pages
        self.assertIs(move.LABEL_CENTRES, actions.HUD_LABEL_ROW)
        self.assertEqual(actions.HUD_ROW_TEXT.split("|"), [label for label, _x in actions.HUD_LABEL_ROW])
        self.assertEqual(pages.HUD_LABELS, tuple(label for label, _x in actions.HUD_LABEL_ROW))

    def test_adjacent_blocks_share_no_pixel(self):
        # Route review round 2, finding 10: boxes are half-open [x, x + w): touching edges are no overlap.
        ping = ("uid", 141.0, 168.0, 708.0, True)
        self.assertFalse(move.same_blocks((ping, ("uid", 168.0, 197.0, 708.0, False)), (ping,)))
        self.assertTrue(move.same_blocks((("uid", 167.0, 197.0, 708.0, False),), (("uid", 141.0, 168.0, 708.0, False),)))
        # Codex's replay: the ping [141,168) on the first frame, a new UID123 block [168,197) on the second: the walk
        # stops on the second frame instead of stepping from it.
        walk, io = self._blocks_walker(lambda n: HUD_BLOCKS + (PING,) if n == 1
                                       else HUD_BLOCKS + (PING, ("uid", 168.0, 197.0, 709.0, False)))
        result = walk.walk([Goal(0, -20)], (0, 0), 60)
        self.assertEqual((result.outcome, len(io.steps), io.observed), ("screen", 1, 2))
        self.assertEqual(io.saved[0][2].number, 2)

    def test_1_a_misread_at_the_start_is_saved_and_refused(self):
        walk, io = walker(World((0, 0)), kinds=[("misread", "读错"), ("misread", "读错"), "hud"])  # c05 a, b, b-hud
        result = walk.walk([Goal(0, -6)], (0, 0), 60)
        self.assertEqual((result.outcome, io.steps, io.observed), ("refused", [], 1))
        self.assertEqual(io.saved[0][2].number, 1)

    def _blocks_walker(self, layout):
        walk, io = walker(World((0, 0), yaw=0.0))
        original = io.observe

        def observe():
            obs = original()
            obs.blocks = layout(obs.frame.number)
            return obs
        io.observe = observe
        return walk, io

    def test_2_the_ping_appearing_later_goes_on_but_another_new_block_stops_on_any_frame(self):
        walk, io = self._blocks_walker(lambda n: HUD_BLOCKS if n == 1 else HUD_BLOCKS + (PING,))  # c01-a, c01-b
        self.assertTrue(walk.walk([Goal(0, -12)], (0, 0), 60).ok)
        walk, io = self._blocks_walker(
            lambda n: HUD_BLOCKS if n == 1 else HUD_BLOCKS + (("uid", 100.0, 108.0, 709.0, False),))
        result = walk.walk([Goal(0, -20)], (0, 0), 60)  # a plain hud frame with a new block: stop on it
        self.assertEqual((result.outcome, len(io.steps), io.observed), ("screen", 1, 2))
        self.assertEqual(io.saved[0][2].number, 2)

    def test_3_the_recapture_after_a_misread_must_have_a_trusted_fix(self):
        world = World((0, 0), yaw=0.0)
        weak = Fix(0, -7.44, 0.22, 0.0, 1.0, False)
        walk, io = walker(world, kinds=["hud", ("misread", "读错"), "hud"], reader=FakeReader(world, bad={3: weak}))
        result = walk.walk([Goal(0, -20)], (0, 0), 60)
        self.assertEqual((result.outcome, len(io.steps), io.observed), ("lost", 1, 3))

    def test_5_a_misread_frame_that_cannot_be_saved_stops(self):
        walk, io = walker(World((0, 0)), kinds=["hud", ("misread", "读错"), "hud"])
        original = io.save
        io.save = lambda stage, reason, frame, details=None: (original(stage, reason, frame, details),
                                                              stage != "android_move_misread")[1]
        result = walk.walk([Goal(0, -20)], (0, 0), 60)
        self.assertEqual((result.outcome, len(io.steps), io.observed), ("screen", 1, 2))
        self.assertFalse(any("frame saved" in text for _detail, text in io.logs))

    def test_capture_age_counts_from_the_real_timestamp_source(self):
        import numpy as np
        from types import SimpleNamespace
        from unittest.mock import patch
        from host.android.actions import Screen
        from host.android.target import TouchSnapshot
        clock = Clock()
        frame = np.zeros((720, 1280, 3), np.uint8)

        def capture():  # the reading, the screencap and a slow post-capture focus query: 2 s inside the call
            clock.t += 2.0
            return frame
        rt = SimpleNamespace(stopped=False, _fresh_device_frame=capture, _frame_still_valid=lambda f: True,
                             _recognizer_on=lambda f: (lambda node, override=None: Ocr([Row(*r) for r in HUD_TEXT])))
        with patch("host.device_input", return_value=SimpleNamespace(touch_of=lambda f: TouchSnapshot("none", 1, 1))), \
                patch("host.android_user_stop", return_value=False), \
                patch("host.android.actions.read_screen", return_value=Screen("hud", {"duck": [30, 18, 54, 54]})):
            obs = move.RuntimeIo(rt, clock=clock).observe()
        self.assertEqual((obs.at, obs.captured), (100.0, 102.0))  # the expiry base is before the capture request
        walk = Walker(FakeIo(World((0, 0)), clock), FakeReader(World((0, 0))), clock=clock)
        walk.deadline = 1000
        self.assertEqual(walk._not_after(obs, 1.0), 100.0 + move.MAX_DECISION_AGE_S)  # already expired at 102


def _frame(name):
    import cv2
    import numpy as np
    crop = cv2.imread(str(FIXTURES / name))
    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[11:211, 60:260] = crop  # where it was cut from (minimap pivot (159.6, 110.8))
    return frame


TRACKER_CROP_AT = (100, 220)  # where the move_tracker_*.jpg crops were cut from their 1280x720 frames
STARSEA_NAME = ("·登上鸣鸣列车", [126, 231, 126, 23], 0.971)  # phase0/hud-1280x720.png (星海), as read
HUD_WITHOUT_TRACKER = tuple(row for row in HUD_TEXT if row[0] not in ("登上鸣鸣列车", "1049米"))


def _tracker_frame(name):
    """A 1280x720 frame (flat grey) with a saved tracker crop at its place."""
    import cv2
    import numpy as np
    crop = cv2.imread(str(FIXTURES / name))
    frame = np.full((720, 1280, 3), 90, np.uint8)
    x, y = TRACKER_CROP_AT
    frame[y:y + crop.shape[0], x:x + crop.shape[1]] = crop
    return frame


def tracker_verdict(frame, *rows):
    from host.android.actions import Screen
    verdict = move.classify(Screen("hud", {"duck": [30, 18, 54, 54]}),
                            lambda node, override=None: Ocr([Row(*row) for row in (*HUD_WITHOUT_TRACKER, *rows)]),
                            frame)
    return verdict.kind, verdict.regions, verdict.name_only


class NameOnlyTrackerTests(unittest.TestCase):
    """星海: the tracked quest is in another map and the tracker shows its name without a distance row (phase0 and
    the survey frames). Such a first frame walks only with the distance row positively empty; later frames keep the
    shape exactly. Crops: move_tracker_starsea.jpg (phase0), move_tracker_starsea_shore.jpg (survey/jump/j4, the
    shoreline behind the row), move_tracker_distance_unread.jpg (m2-rl10b t18: 「1040米」 drawn, OCR read no
    distance)."""

    @classmethod
    def setUpClass(cls):
        cls.starsea = _tracker_frame("move_tracker_starsea.jpg")

    def test_the_real_starsea_tracker_is_a_name_with_positively_no_distance(self):
        self.assertEqual(tracker_verdict(self.starsea, STARSEA_NAME), ("hud", ("登上鸣鸣列车",), True))
        shore = _tracker_frame("move_tracker_starsea_shore.jpg")
        self.assertEqual(tracker_verdict(shore, ("登上鸣鸣列车", [144, 231, 106, 23], 0.913))[2], True)

    def test_an_unread_distance_is_not_an_empty_row(self):
        frame = _tracker_frame("move_tracker_distance_unread.jpg")
        self.assertEqual(tracker_verdict(frame, ("·登上呜鸣列车", [134, 227, 120, 28], 0.872)),
                         ("hud", ("登上鸣鸣列车",), False))
        self.assertGreater(move.edge_fraction(frame, move.distance_area([134, 227, 120, 28])),
                           move.DISTANCE_EDGE_MAX)

    def test_the_name_row_must_be_a_plain_quest_name(self):
        for text in ("交谈", "鸣鸣列车"[:3], "前往鸣鸣列车", "点击继续游戏"):
            with self.subTest(text):
                self.assertFalse(tracker_verdict(self.starsea, (text, [126, 231, 126, 23]))[2])

    def test_anything_else_in_the_tracker_or_its_distance_row_is_no_empty_row(self):
        cases = {
            "a weak marker glyph over the tracker": (("▼", [256, 236, 14, 14], 0.4),),
            "a weak row in the distance row": (("1", [140, 258, 10, 18], 0.3),),
            "the distance read": (("1044米", [129, 255, 79, 24], 1.0),),
            "two name rows": (("登上鸣鸣列车", [126, 205, 126, 23]),),
        }
        for name, rows in cases.items():
            with self.subTest(name):
                self.assertFalse(tracker_verdict(self.starsea, STARSEA_NAME, *rows)[2])
        self.assertFalse(tracker_verdict(None, STARSEA_NAME)[2])  # no frame, no evidence
        flat = _tracker_frame("move_tracker_starsea.jpg")
        flat[258:278, 130:200:6] = 255  # stroke-like pixels in the distance row OCR did not read
        self.assertFalse(tracker_verdict(flat, STARSEA_NAME)[2])

    def test_a_short_distance_row_is_a_distance(self):
        # Route review round 2, finding 6: 1米 / 3米 (two characters, under counts_as_text's floor) count as the
        # distance row instead of being dropped as noise, so the tracker is no name-only tracker.
        for text in ("1米", "3米", "12米"):
            with self.subTest(text):
                self.assertEqual(tracker_verdict(self.starsea, STARSEA_NAME, (text, [129, 255, 30, 24], 0.9)),
                                 ("hud", ("登上鸣鸣列车", move.TRACKER_DISTANCE), False))
        # at any score: the row is recognised before the score and counts_as_text filters
        for text, score in (("3米", 0.3), ("◆1米", 0.5)):
            with self.subTest(text=text, score=score):
                self.assertEqual(tracker_verdict(self.starsea, STARSEA_NAME, (text, [129, 255, 30, 24], score)),
                                 ("hud", ("登上鸣鸣列车", move.TRACKER_DISTANCE), False))
        # a lone digit there is still noise, not a distance (and the row is no longer positively empty)
        self.assertEqual(tracker_verdict(self.starsea, STARSEA_NAME, ("3", [129, 255, 12, 24], 0.9))[:2],
                         ("hud", ("登上鸣鸣列车",)))

    def test_a_name_only_first_frame_walks_and_later_frames_keep_that_shape(self):
        walk, io = walker(World((0, 0), yaw=0.0), map_id="starsea")
        io.regions, io.name_only = (lambda n: ("登上鸣鸣列车",)), (lambda n: True)
        result = walk.walk([Goal(0, -12)], (0, 0), 60)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(any("没有距离" in text for _detail, text in io.logs))
        for changed in (("登上鸣鸣列车", move.TRACKER_DISTANCE), ("离开鸣鸣列车",), ("登上鸣鸣列车", "任务开始"), ()):
            with self.subTest(changed=changed):
                walk, io = walker(World((0, 0)), map_id="starsea")
                io.regions = lambda n, changed=changed: ("登上鸣鸣列车",) if n == 1 else changed
                io.name_only = lambda n: True
                result = walk.walk([Goal(0, -20)], (0, 0), 60)
                self.assertEqual((result.outcome, len(io.steps), io.observed), ("screen", 1, 2))
                self.assertEqual(io.saved[0][2].number, 2)

    def test_the_name_only_shape_is_only_for_starsea(self):
        # Route review round 2, finding 6: off 星海 (or with the map unknown) a name-only tracker never starts a walk.
        for map_id in (None, "miraland"):
            with self.subTest(map_id=map_id):
                walk, io = walker(World((0, 0)), map_id=map_id)
                io.regions, io.name_only = (lambda n: ("登上鸣鸣列车",)), (lambda n: True)
                result = walk.walk([Goal(0, -6)], (0, 0), 60)
                self.assertEqual((result.outcome, io.steps), ("refused", []))

    def test_a_quest_instruction_never_starts_a_walk(self):
        # Route review round 2, finding 6: native OCR read 「与大喵交谈」 in the tracker on constructed pixels and the
        # walker took it for a name-only tracker. 交谈 is an instruction word (NARRATION_DENY): refused on 星海 too.
        for map_id in (None, "starsea"):
            with self.subTest(map_id=map_id):
                walk, io = walker(World((0, 0)), map_id=map_id)
                io.regions, io.name_only = (lambda n: ("与大喵交谈",)), (lambda n: True)
                result = walk.walk([Goal(0, -6)], (0, 0), 60)
                self.assertEqual((result.outcome, io.steps, io.observed), ("refused", [], 1))
        from types import SimpleNamespace
        reader = SimpleNamespace(present=lambda frame: True)
        alone = move.Observation("f", "hud", 0.0, "", regions=("登上鸣鸣列车",), blocks=(), name_only=True)
        self.assertEqual(move.first_frame_rule(alone, reader, "starsea")[0], "ok")
        for map_id in (None, "miraland"):  # refused at once, not waited through: the evidence will not change
            self.assertEqual(move.first_frame_rule(alone, reader, map_id)[0], "stop")
        # route review round 3, finding 9: the map-opening tap (map not known, map_unknown_ok) may use it; a known
        # other map never
        self.assertEqual(move.first_frame_rule(alone, reader, None, map_unknown_ok=True)[0], "ok")
        self.assertEqual(move.first_frame_rule(alone, reader, "miraland", map_unknown_ok=True)[0], "stop")

    def test_a_distance_appearing_after_a_name_only_start_stops(self):
        # Route review round 2, finding 6: a new 3米 row read on a later frame (recognised at any score) is a change.
        walk, io = walker(World((0, 0)), map_id="starsea")
        io.regions = lambda n: ("登上鸣鸣列车",) if n == 1 else ("登上鸣鸣列车", move.TRACKER_DISTANCE)
        io.name_only = lambda n: n == 1
        result = walk.walk([Goal(0, -20)], (0, 0), 60)
        self.assertEqual((result.outcome, len(io.steps), io.observed), ("screen", 1, 2))
        self.assertEqual(io.saved[0][2].number, 2)

    def test_the_empty_distance_evidence_is_rechecked_on_every_later_frame(self):
        # Route review round 2, finding 6: the distance row must stay positively empty, not merely unread.
        walk, io = walker(World((0, 0)), map_id="starsea")
        io.regions, io.name_only = (lambda n: ("登上鸣鸣列车",)), (lambda n: n == 1)
        result = walk.walk([Goal(0, -20)], (0, 0), 60)
        self.assertEqual((result.outcome, len(io.steps), io.observed), ("screen", 1, 2))
        self.assertIn("距离那一行", result.message)

    def test_a_name_only_first_frame_without_the_evidence_or_too_short_never_steps(self):
        for name, regions, name_only in (("no positive evidence", ("登上鸣鸣列车",), False),
                                         ("交谈 even if flagged", ("交谈",), True),
                                         ("a deny word even if flagged", ("前往鸣鸣列车",), True)):
            with self.subTest(name):
                walk, io = walker(World((0, 0)), map_id="starsea")
                io.regions, io.name_only = (lambda n, r=regions: r), (lambda n, f=name_only: f)
                result = walk.walk([Goal(0, -6)], (0, 0), 60)
                self.assertEqual((result.outcome, io.steps), ("refused", []))


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

    def test_an_out_of_map_hint_is_refused_in_chinese_not_an_opencv_assertion(self):
        from navigation.models import NavigationError
        with self.assertRaisesRegex(NavigationError, "地图之外"):
            self.reader.fix(_frame("move_minimap_goto_start.jpg"), (-500.0, 99999.0), 40)
        self.assertFalse(self.reader.within(-1, 10))
        self.assertTrue(self.reader.within(9886, 10005))

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
            return Observation(frame, "hud", at, regions=TRACKER)
        io.observe = observe
        io.step = lambda frame, angle, seconds, jump_at, not_after=None: (io.steps.append((angle, seconds)),
                                                                           clock.__setattr__("t", clock.t + seconds
                                                                                             + 0.1),
                                                                           len(io.steps) < 2)[-1]
        result = Walker(io, self.reader, clock=clock).walk([Goal(9878.3, 10011.5)], (9886, 10006), 60)
        angle, seconds = io.steps[0]
        self.assertAlmostEqual(angle, 75.7, delta=1.5)  # what the device run sent toward the same point
        self.assertEqual(seconds, move.MAX_STEP_S)  # 10.4 u away: a full-length step
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

    def test_a_stop_before_the_final_report_reports_nothing_and_returns_false(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from host.android import walk_probe
        arrived = move.WalkResult("arrived", "已到达目标位置。", (3, 4), [{"confirmed": True}])
        rt = SimpleNamespace(log=Mock(), stopped=False)
        with tempfile.TemporaryDirectory() as folder, patch("app_paths.DATA_ROOT", Path(folder)), \
                patch("host.game_backend", return_value="android"), patch("host.android.minimap.MinimapReader"), \
                patch.object(move.Walker, "walk", return_value=arrived), \
                patch("host.android_user_stop", return_value=True):  # stop.request seen at the final checks
            self.assertFalse(walk_probe.run(rt, {"map": "miraland", "hint": [1, 2], "target": [3, 4]}))
            rt.log.assert_not_called()
            line = json.loads(next(Path(folder).rglob("walk.jsonl")).read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual((line["kind"], line["outcome"], line["attempted"], line["confirmed"]),
                             ("result", "stopped", 1, 1))

    def test_a_stop_published_during_the_result_write_reports_nothing(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from host.android import walk_probe
        arrived = move.WalkResult("arrived", "已到达目标位置。", (3, 4), [{"confirmed": True}])
        rt = SimpleNamespace(log=Mock(), stopped=False)
        stop = {"now": False}
        original = walk_probe.DiagnosticIo.record

        def record(io, kind, data, frame):
            path = original(io, kind, data, frame)
            if kind == "result":
                stop["now"] = True  # stop.request published inside the write, the tasker still running
            return path
        with tempfile.TemporaryDirectory() as folder, patch("app_paths.DATA_ROOT", Path(folder)), \
                patch("host.game_backend", return_value="android"), patch("host.android.minimap.MinimapReader"), \
                patch.object(move.Walker, "walk", return_value=arrived), \
                patch.object(walk_probe.DiagnosticIo, "record", record), \
                patch("host.android_user_stop", side_effect=lambda *a, **k: stop["now"]):
            self.assertFalse(walk_probe.run(rt, {"map": "miraland", "hint": [1, 2], "target": [3, 4]}))
        rt.log.assert_not_called()

    def _probe_run(self, *, record_hook=None, log_hook=None, deadline_in=None, stop=lambda: False, write_ok=True):
        """walk_probe.run with an arrived walk; returns (result, rt, the walk.jsonl lines)."""
        import time as real_time
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from host.android import walk_probe
        arrived = move.WalkResult("arrived", "已到达目标位置。", (3, 4), [{"confirmed": True}])

        def fake_walk(walker, *args, **kwargs):
            walker.deadline = None if deadline_in is None else real_time.monotonic() + deadline_in
            return arrived
        rt = SimpleNamespace(log=Mock(side_effect=log_hook), stopped=False,
                             context=SimpleNamespace(run_task=Mock()))
        original = walk_probe.DiagnosticIo.record

        def record(io, kind, data, frame):
            path = original(io, kind, data, frame)
            if record_hook and kind == "result":
                record_hook()
            return path
        with tempfile.TemporaryDirectory() as folder, patch("app_paths.DATA_ROOT", Path(folder)), \
                patch("host.game_backend", return_value="android"), patch("host.android.minimap.MinimapReader"), \
                patch.object(move.Walker, "walk", fake_walk), patch.object(walk_probe.DiagnosticIo, "record", record), \
                patch("host.android_user_stop", side_effect=lambda *a, **k: stop()), \
                patch("message_log.write_message", side_effect=lambda *a, **k: (log_hook and log_hook(), write_ok)[1]) \
                as written:
            result = walk_probe.run(rt, {"map": "miraland", "hint": [1, 2], "target": [3, 4]})
            rt.written = written
            lines = [json.loads(line) for line in next(Path(folder).rglob("walk.jsonl")).read_text(
                encoding="utf-8").splitlines()]
        return result, rt, lines

    def test_6_a_stop_inside_the_message_file_write_shows_nothing(self):
        flag = {"stop": False}
        result, rt, lines = self._probe_run(log_hook=lambda *a, **k: flag.update(stop=True), stop=lambda: flag["stop"])
        self.assertFalse(result)
        rt.context.run_task.assert_not_called()  # the arrival was never displayed
        self.assertEqual((lines[-1]["outcome"], lines[-1]["corrects"]), ("stopped", "arrived"))

    def test_6_a_deadline_passed_inside_the_result_write_is_no_arrival_anywhere(self):
        import time as real_time

        def slow():
            end = real_time.monotonic() + 0.1
            while real_time.monotonic() < end:
                pass
        result, rt, lines = self._probe_run(record_hook=slow, deadline_in=0.05)
        self.assertFalse(result)
        self.assertEqual((lines[-1]["outcome"], lines[-1]["corrects"]), ("timeout", "arrived"))
        rt.context.run_task.assert_called_once()
        self.assertNotIn("已到达", str(rt.context.run_task.call_args))  # the timeout text is shown instead
        self.assertIn("超时", str(rt.context.run_task.call_args))

    def test_6_an_arrival_with_both_rechecks_passing_is_shown_once(self):
        result, rt, lines = self._probe_run(deadline_in=60)
        self.assertTrue(result)
        self.assertEqual(lines[-1]["outcome"], "arrived")
        rt.context.run_task.assert_called_once()
        rt.log.assert_not_called()  # the file copy went straight to the message file: no Runtime.log fallback
        self.assertIsNone(rt.written.call_args.args[1])  # written as a file-only message
        # route review round 3, finding 13: marked as a report, so the dev runner prints it (fixed text, no OCR)
        self.assertEqual(rt.written.call_args.kwargs["report"], rt.written.call_args.args[0])

    def test_6_a_failed_message_file_append_with_a_stop_shows_nothing(self):
        flag = {"stop": False}
        result, rt, lines = self._probe_run(log_hook=lambda: flag.update(stop=True), stop=lambda: flag["stop"],
                                            write_ok=False)
        self.assertFalse(result)
        rt.context.run_task.assert_not_called()
        self.assertEqual(lines[-1]["outcome"], "stopped")

    def test_6_expiry_inside_the_post_write_stop_query_is_no_arrival(self):
        import time as real_time
        calls = {"n": 0}

        def slow_stop_query():  # the stop query asked after the result write outlasts the deadline
            calls["n"] += 1
            if calls["n"] == 2:
                end = real_time.monotonic() + 0.1
                while real_time.monotonic() < end:
                    pass
            return False
        result, rt, lines = self._probe_run(deadline_in=0.05, stop=slow_stop_query)
        self.assertFalse(result)
        self.assertEqual(lines[-1]["outcome"], "timeout")
        self.assertNotIn("已到达", str(rt.context.run_task.call_args))

    def test_bad_runner_options_are_reported_in_chinese(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        sys.path.insert(0, str(ROOT / "scripts" / "android"))
        import runner
        for bad in ({"target": "1,2"}, {"hint": "1,2", "target": "3"}, {"hint": "1,2", "target": "3,4", "x": "1"},
                    {"hint": "1,2", "target": "3,4", "timeout": "soon"}, {"hint": "nan,2", "target": "3,4"}):
            with self.subTest(options=bad), self.assertRaises(runner.RunnerError) as caught:
                runner.dev_override(self.ENTRY, bad)
            self.assertTrue(any("一" <= ch <= "鿿" for ch in str(caught.exception)), str(caught.exception))

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


# The label rows full-frame OCR read on the 11 saved prompt frames, in move_prompt_column.jpg's order (m2-rl10
# t00-b-hud, t00-b, t01-a, t04-b-hud, t04-b, t05-a; m2-rl10b t14-b-hud, t14-b; m4-jump4 t00-b-hud, t00-b;
# phase-e-walk2's stop frame), as scripts/android/move_frames.py --all-frames reads them through the real nodes.
PROMPT_ROWS = (("查看", [794, 329, 55, 31]),) * 6 + (("拾取", [793, 328, 57, 32]), ("拾取", [790, 326, 62, 35]),
                                                   ("查看", [794, 329, 56, 31]), ("查看", [794, 329, 56, 31]),
                                                   ("查看", [793, 329, 57, 31]))


def _prompt_frame(index):
    """A 1280x720 frame (flat grey) with the prompt column of saved prompt frame ``index`` at its place."""
    import cv2
    import numpy as np
    strip = cv2.imread(str(FIXTURES / "move_prompt_column.jpg"))
    x0, y0, x1, y1 = PROMPT_CROP
    frame = np.full((720, 1280, 3), 90, np.uint8)
    frame[y0:y1, x0:x1] = strip[index * (y1 - y0):(index + 1) * (y1 - y0)]
    return frame


def frame_verdict(frame, *rows):
    """classify() on a recognised HUD whose full-frame OCR reads the HUD's own text and ``rows``, on ``frame``."""
    from host.android.actions import Screen
    return move.classify(Screen("hud", {"duck": [30, 18, 54, 54]}),
                         lambda node, override=None: Ocr([Row(*row) for row in (*HUD_TEXT, *rows)]), frame)


class PromptColumnTests(unittest.TestCase):
    """The interaction-prompt column (coordinator's decision, 2026-11-26): the game's interaction prompt does not
    block movement; it is judged by position and shape (the label's box, the hexagon icon, the gold line), never by
    its words, and never tapped. Anything else in the column or beyond the HUD still stops. Pixels: the prompt column
    of the 11 saved frames with a prompt (move_prompt_column.jpg); the real nodes on them: FrameworkTests."""

    def test_the_saved_prompts_allow_movement(self):
        self.assertEqual(len(PROMPT_ROWS), 11)
        for index, row in enumerate(PROMPT_ROWS):
            with self.subTest(index):
                verdict = frame_verdict(_prompt_frame(index), row)
                self.assertEqual((verdict.kind, verdict.reason), ("hud_prompt", ""))
                self.assertEqual(len(verdict.prompts), 1)
                self.assertTrue(move.controls_clear(verdict.prompts[0], jump=True, bell=move.RuntimeIo.BELL_POINT))
                self.assertIn(verdict.kind, move.MOVE_KINDS)

    def test_its_words_are_not_read(self):
        frame = _prompt_frame(0)
        for text, width in (("交谈", 55), ("采集", 55), ("大喵", 55), ("旅行商人雅兰", 165)):  # NPC names, labels
            with self.subTest(text):
                self.assertEqual(frame_verdict(frame, (text, [794, 329, width, 31])).kind, "hud_prompt")
        # OCR boxing the icon and the bar with the label (as on the composites FrameworkTests reads)
        for box in ([742, 316, 118, 55], [737, 313, 247, 65], [741, 308, 215, 74]):
            with self.subTest(box=box):
                self.assertEqual(frame_verdict(frame, ("查看", box)).kind, "hud_prompt")

    def test_another_shape_in_the_column_stops(self):
        import numpy as np
        frame = _prompt_frame(0)
        cases = {
            "the label elsewhere in the column": (frame, (("查看", [900, 335, 55, 31]),)),
            "a block taller than the row": (frame, (("查看", [794, 300, 55, 90]),)),
            "a block below the row's middle": (frame, (("查看", [794, 350, 55, 31]),)),
            "a label running past the bar": (frame, (("查看", [794, 329, 240, 31]),)),
            "two rows (two prompts)": (frame, (PROMPT_ROWS[0], ("拾取", [880, 329, 57, 31]))),
            "a glyph in the column": (frame, (PROMPT_ROWS[0], ("▶", [1000, 335, 20, 20]))),
            "the label without the icon and line": (np.full((720, 1280, 3), 90, np.uint8), (PROMPT_ROWS[0],)),
            "no frame to check the shape on": (None, (PROMPT_ROWS[0],)),
        }
        no_icon, no_line = frame.copy(), frame.copy()
        no_icon[330:365, 755:792] = 90
        no_line[318:330, 740:1040] = 90
        cases["the label and line without the icon"] = (no_icon, (PROMPT_ROWS[0],))
        cases["the label and icon without the line"] = (no_line, (PROMPT_ROWS[0],))
        for name, (image, rows) in cases.items():
            with self.subTest(name):
                verdict = frame_verdict(image, *rows)
                self.assertEqual(verdict.kind, "other")
                self.assertTrue(verdict.reason)
                self.assertFalse(any(text in verdict.reason for text, _box in rows))  # no OCR text for the user

    def test_text_outside_the_column_still_stops(self):
        frame = _prompt_frame(0)
        cases = {
            "a banner": (("获得新的服装", [500, 140, 280, 40]),),
            "a tap prompt below": (("点击继续", [560, 560, 160, 28]),),
            "a prompt-like block on the jump control": (("查看", [938, 521, 55, 31]),),
            "a remark with the prompt": WIND,
            "a prompt in the 心誓 bar": (("心誓5 点击领取", [520, 668, 240, 22]),),
        }
        for name, rows in cases.items():
            with self.subTest(name):
                verdict = frame_verdict(frame, PROMPT_ROWS[0], *rows)
                self.assertIn(verdict.kind, NO_MOVE)
                self.assertTrue(verdict.reason)
        self.assertEqual(frame_verdict(frame, ("查看", [938, 521, 55, 31])).kind, "other")  # alone, off the column

    def test_dialogue_and_narration_rules_are_unchanged(self):
        frame = _prompt_frame(0)
        self.assertEqual(frame_verdict(frame, *WIND).kind, "narration")  # pixels in the column but no text there
        self.assertEqual(frame_verdict(frame, *BELL).kind, "narration")
        self.assertEqual(frame_verdict(frame).kind, "hud")
        for rows in ((("暖暖，你看那边", [480, 556, 300, 28]), ("好像有什么东西", [480, 578, 300, 28])),
                     (("我知道了", [900, 420, 160, 30]),)):
            self.assertEqual(frame_verdict(frame, *rows).kind, "other")

    def test_the_column_is_clear_of_every_movement_control(self):
        self.assertTrue(move.controls_clear(move.PROMPT_COLUMN, jump=True, bell=move.RuntimeIo.BELL_POINT))
        over_jump = (900, 500, 1010, 560)
        self.assertFalse(move.controls_clear(over_jump, jump=True))
        self.assertTrue(move.controls_clear(over_jump, jump=False))  # a step that does not jump never touches it
        self.assertFalse(move.controls_clear((250, 540, 300, 600)))  # on the joystick ring
        self.assertFalse(move.controls_clear((300, 560, 340, 580)))  # next to it (within CONTROL_CLEAR_PX)
        self.assertFalse(move.controls_clear((600, 600, 640, 640), bell=move.RuntimeIo.BELL_POINT))

    def test_the_saved_lines_are_thin_and_the_saved_icons_hexagons(self):
        for index in range(len(PROMPT_ROWS)):
            with self.subTest(index):
                frame = _prompt_frame(index)
                self.assertTrue(move.line_thin(frame))
                self.assertIsNotNone(move.prompt_icon(frame))

    def test_a_thick_header_or_a_white_l_is_no_prompt(self):
        # Route review round 3, finding 2: a 29 px thick red panel header and a white L passed the line and hexagon
        # checks; with a 点击继续 row in the column the classifier returned hud_prompt. Now neither passes.
        frame = _prompt_frame(0)
        header, letter = frame.copy(), frame.copy()
        header[300:329, 740:1040] = (40, 40, 210)  # red, warm enough for the gold test on every row
        letter[330:365, 755:792] = 90
        letter[337:357, 767:775] = 255  # an L: a 8 x 20 bar
        letter[349:357, 767:791] = 255  # and a 24 x 8 foot (288 px, fill 0.6 of its 24 x 20 box)
        self.assertIsNotNone(move.prompt_line(frame))
        self.assertIsNone(move.prompt_line(header))
        self.assertFalse(move.line_thin(header))
        self.assertIsNone(move.prompt_icon(letter))
        both = header.copy()
        both[330:365, 740:1040] = letter[330:365, 740:1040]
        row = ("点击继续", [794, 329, 110, 31])
        for name, image in (("header", header), ("letter", letter), ("both", both)):
            with self.subTest(name):
                self.assertEqual(frame_verdict(image, row).kind, "other")
        self.assertEqual(frame_verdict(frame, row).kind, "hud_prompt")  # the seen shape: words are not read

    def test_single_characters_near_a_contact_are_text_there(self):
        # Route review round 3, finding 2: 「确」 and 「认」 over the jump button, each a single high-score character,
        # were dropped one by one and the walker put the jump contact down inside 认. Now the dropped fragments are
        # kept (adjacent ones as one block) and any of them near a contact the gesture uses stops.
        frame = _prompt_frame(0)
        verdict = frame_verdict(frame, PROMPT_ROWS[0], ("确", [940, 525, 22, 24], 0.97), ("认", [964, 525, 22, 24], 0.97))
        self.assertEqual(verdict.kind, "hud_prompt")  # still too short to count as text on its own
        self.assertEqual(verdict.fragments, (((940, 525, 986, 549), "确认"),))  # judged together
        obs = move.Observation(frame, verdict.kind, 0.0, prompts=verdict.prompts, fragments=verdict.fragments)
        self.assertFalse(move.contacts_clear(obs, jump=True))
        self.assertTrue(move.contacts_clear(obs, jump=False))  # far from the joystick and the bell
        low = frame_verdict(frame, ("确认", [940, 525, 46, 24], 0.2))  # a low-score row is kept the same way
        self.assertFalse(move.contacts_clear(move.Observation(frame, low.kind, 0.0, fragments=low.fragments),
                                             jump=True))
        far = frame_verdict(frame, ("丶", [640, 140, 12, 12], 0.9))
        self.assertTrue(move.contacts_clear(move.Observation(frame, far.kind, 0.0, fragments=far.fragments),
                                            jump=True, bell=move.RuntimeIo.BELL_POINT))

    def test_the_controls_own_readings_are_not_text_by_them(self):
        # The saved frames (move_frames.py --all-frames, text_by_a_contact): OCR reads noise on the jump figure (2A1,
        # 以, ...), on the bell (Q, 口, ...) and drops pieces of the 心誓 bar next to the bell; 175 of 483 movable
        # frames would have stopped. Those are the HUD's own readings; two CJK characters on an icon, or anything
        # elsewhere in a clearance, still stop.
        def clear(rows, **contacts):
            verdict = frame_verdict(_prompt_frame(0), *rows)
            return move.contacts_clear(move.Observation(None, verdict.kind, 0.0, fragments=verdict.fragments),
                                       **contacts)
        bell = {"bell": move.RuntimeIo.BELL_POINT}
        for rows, contacts in (((("2A1", [941, 493, 53, 80], 0.3),), {"jump": True}),  # m2-rl10 t04-b
                               ((("以", [942, 497, 50, 65], 0.6),), {"jump": True}),
                               ((("Q", [598, 595, 30, 35], 0.5),), bell),  # m2-fb10 t00-b
                               ((("口", [592, 591, 46, 46], 0.4),), bell),
                               ((("3", [598, 672, 32, 14], 0.4),), bell),  # the 心誓 bar's dropped piece
                               ((("33", [570, 668, 40, 22], 0.4),), bell),
                               ((("口", [897, 485, 35, 35], 0.9),), {"jump": True}),  # the jump badge (m2-fb05 t16)
                               ((("K", [1056, 571, 62, 73], 0.88),), {"jump": True})):  # the sprint shoe (t01-a)
            with self.subTest(rows=rows):
                self.assertTrue(clear(rows, **contacts))
        for rows, contacts in (((("确", [940, 525, 22, 24], 0.97), ("认", [964, 525, 22, 24], 0.97)), {"jump": True}),
                               ((("确定", [596, 600, 40, 24], 0.3),), bell),  # two CJK on the bell icon
                               ((("口", [870, 560, 20, 20], 0.5),), {"jump": True}),  # in its ring, off every icon
                               ((("好", [560, 560, 20, 20], 0.9),), bell),  # by the bell, off its icon
                               ((("1", [300, 600, 10, 20], 0.9),), {})):  # by the joystick (never read there)
            with self.subTest(stop=rows):
                self.assertFalse(clear(rows, **contacts))

    def test_the_walker_stops_on_text_by_a_contact_it_would_touch(self):
        for name, fragment, goal, options in (
                ("确认 over the jump button, the step jumps", ((940, 525, 986, 549), "确认"), Goal(0, -5, jump=True),
                 {"allow_jump": True}),
                ("a character by the joystick", ((300, 600, 312, 620), "1"), Goal(0, -12), {})):
            with self.subTest(name):
                walk, io = walker(World((0, 0), yaw=0.0), **options)
                io.fragments = lambda n, fragment=fragment: (fragment,)
                result = walk.walk([goal], (0, 0), 60)
                self.assertEqual((result.outcome, io.steps), ("screen", []))
                self.assertEqual(io.saved[0][2].number, 1)
                self.assertIn("文字", result.message)
        walk, io = walker(World((0, 0), yaw=0.0))
        io.fragments = lambda n: (((940, 525, 986, 549), "确认"),)  # by the jump button, the step does not jump
        self.assertTrue(walk.walk([Goal(0, -5)], (0, 0), 60).ok)

    def test_nothing_in_the_walk_taps(self):
        source = Path(move.__file__).read_text(encoding="utf-8")
        self.assertNotRegex(source, r"\.(action|click|tap|swipe|camera_drag|post_click)\(")
        self.assertFalse({"tap", "click", "action", "swipe"} & set(dir(move.RuntimeIo)))


class PromptWalkTests(unittest.TestCase):
    """The walk on "hud_prompt" frames: starts and goes on, logs the class per step, sends nothing near a prompt."""

    def test_a_prompt_neither_stops_the_walk_nor_is_tapped_and_each_step_logs_it(self):
        world = World((0, 0), yaw=0.0)
        walk, io = walker(world, kinds=["hud_prompt", "hud_prompt", "hud", "hud_prompt"])
        result = walk.walk([Goal(0, -12)], (0, 0), 60)
        self.assertTrue(result.ok, result.message)
        steps = [record for kind, record in io.records if kind == "step"]
        self.assertEqual(steps[0]["screen"], "hud_prompt")
        self.assertEqual(steps[0]["prompts"], [list(PROMPT_EXTENT)])
        self.assertTrue(all("screen" in record for record in steps))
        self.assertEqual(io.saved, [])
        self.assertEqual(io.bells, [])
        self.assertTrue(any("hud_prompt" in text for detail, text in io.logs if detail))

    def test_after_a_misread_a_prompt_frame_passes(self):
        world = World((0, 0), yaw=0.0)
        walk, io = walker(world, kinds=["hud", "misread", "hud_prompt"])
        result = walk.walk([Goal(0, -12)], (0, 0), 60)
        self.assertTrue(result.ok, result.message)

    def test_a_prompt_next_to_a_control_the_step_uses_stops_with_the_frame_saved(self):
        for name, extent, goal, options in (
                ("over the jump button, the step jumps", (900, 500, 1010, 560), Goal(0, -5, jump=True),
                 {"allow_jump": True}),
                ("on the joystick ring", (250, 540, 300, 600), Goal(0, -12), {})):
            with self.subTest(name):
                walk, io = walker(World((0, 0), yaw=0.0), kinds=["hud_prompt"], **options)
                io.prompts = lambda n, extent=extent: (extent,)
                result = walk.walk([goal], (0, 0), 60)
                self.assertEqual((result.outcome, io.steps), ("screen", []))
                self.assertEqual(io.saved[0][2].number, 1)
                self.assertIn("按键", result.message)

    def test_a_prompt_over_the_jump_button_while_the_step_does_not_jump_moves(self):
        walk, io = walker(World((0, 0), yaw=0.0), kinds=["hud_prompt"])
        io.prompts = lambda n: ((900, 500, 1010, 560),)
        result = walk.walk([Goal(0, -5)], (0, 0), 60)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(all(step["jump_at"] is None for step in io.steps))


class FreshHeadingTests(unittest.TestCase):
    """A step is planned only with the camera yaw read from its own deciding frame (coordinator's decision B).
    Frames: the minimap of phase-e-walk2 run 2 (move_minimap_walk2_0.jpg, its first frame, on which step 0 was
    decided; move_minimap_walk2_1.jpg, step 1's), each reading yaw 73.2 on its own; ``withheld`` captures read with
    the view-cone reading failing."""

    @classmethod
    def setUpClass(cls):
        from host.android.minimap import MinimapReader
        cls.reader = MinimapReader("miraland", ROOT / "resource")
        cls.frames = [_frame("move_minimap_walk2_0.jpg"), _frame("move_minimap_walk2_1.jpg")]

    def _walk(self, captures, withheld=(), steps=2):
        from types import SimpleNamespace
        from unittest.mock import patch
        from host.android.minimap import MinimapReader
        clock = Clock()
        io = FakeIo(World((0, 0)), clock)
        queue, shots, sent = list(captures), [], []

        def observe():  # every capture a new image (a copy), as on the device
            frame = self.frames[queue.pop(0) if queue else captures[-1]].copy()
            shots.append(frame)
            io.frames.append(frame)
            at = clock()
            clock.t += 0.3
            return Observation(frame, "hud", at, regions=TRACKER)

        def step(frame, angle, seconds, jump_at, not_after=None):
            sent.append((frame, angle))
            clock.t += seconds + 0.1
            return len(sent) < steps

        def fix(frame, hint, radius):
            number = next(n for n, shot in enumerate(shots, 1) if shot is frame)
            if number in withheld:
                with patch.object(self.reader.locator, "camera_heading", lambda *a, **k: (None, 0.0)):
                    return MinimapReader.fix(self.reader, frame, hint, radius)
            return MinimapReader.fix(self.reader, frame, hint, radius)
        io.observe, io.step = observe, step
        reader = SimpleNamespace(present=self.reader.present, within=self.reader.within, fix=fix)
        result = Walker(io, reader, clock=clock).walk([Goal(9880.48, 10010.62)], (9883.52, 10015.73), 60)
        return result, io, shots, sent

    def test_each_step_uses_the_yaw_read_from_its_own_frame(self):
        result, io, shots, sent = self._walk([0, 1, 1])
        self.assertEqual(result.outcome, "stopped")  # the second gesture "returned False"
        self.assertEqual([frame for frame, _angle in sent], [shots[0], shots[2]])  # 2: before the settle wait
        records = [record for kind, record in io.records if kind == "step"]
        for (frame, angle), record, device in zip(sent, records, (166.2, 160.4)):  # what the device run sent
            own = self.reader.fix(frame, (record["fix"]["x"], record["fix"]["y"]), move.TRACK_SEARCH)
            self.assertAlmostEqual(own.heading, 73.2, delta=0.5)
            self.assertEqual(record["heading"], round(own.heading, 1))
            self.assertEqual(record["fix"]["heading"], round(own.heading, 1))
            self.assertAlmostEqual(angle, device, delta=0.3)
            self.assertGreater(record["fix"]["heading_strength"], 1000)
            runner_yaw, runner_strength = record["fix"]["heading_runner_up"]
            self.assertLess(runner_strength, record["fix"]["heading_strength"] / 5)
            self.assertEqual(record["fix"]["position_method"], "outside_camera_cone")
            self.assertNotIn("method", record["fix"])

    def test_a_frame_whose_cone_read_fails_yields_no_step(self):
        # After step 0: three captures without a yaw (the settle frame, then one recapture): no step, the walk stops.
        result, io, shots, sent = self._walk([0, 1, 1, 1], withheld={2, 3, 4})
        self.assertEqual([frame for frame, _angle in sent], [shots[0]])
        self.assertEqual(result.outcome, "lost")
        self.assertIn("镜头朝向", result.message)
        self.assertIs(io.saved[-1][2], shots[-1])
        self.assertEqual(len(shots), 4)
        # The first frame without a yaw: nothing is sent on it; the recapture reads one and the step uses that frame.
        result, io, shots, sent = self._walk([0, 0, 1], withheld={1}, steps=1)
        self.assertEqual([frame for frame, _angle in sent], [shots[1]])
        self.assertEqual(result.outcome, "stopped")

    def test_a_withheld_reading_carries_nothing_from_the_frame_before(self):
        from unittest.mock import patch
        self.reader.fix(self.frames[0], (9883.52, 10015.73), 12)  # a reading with a yaw first
        with patch.object(self.reader.locator, "camera_heading", lambda *a, **k: (None, 0.0)):
            fix = self.reader.fix(self.frames[1], (9884.27, 10015.77), 12)
        self.assertEqual((fix.heading, fix.heading_strength, fix.heading_runner_up), (None, None, None))

    def test_a_fix_from_another_frame_never_plans_a_step(self):
        walk, io = walker(World((0, 0)))
        first, second = io.observe(), io.observe()
        fix = walk._fix_on(first, (0, 0), move.TRACK_SEARCH)
        self.assertEqual(walk._step_heading(first, fix), 30.0)
        with self.assertRaises(move.Halt) as caught:
            walk._step_heading(second, fix)
        self.assertEqual(caught.exception.result.outcome, "lost")
        self.assertIs(io.saved[-1][2], second.frame)


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
row, settings = crop("hud_label_row.png"), crop("page_title.png")
wind, bell = crop("move_narration_wind.jpg"), crop("move_narration_bell.jpg")
strip = crop("move_prompt_column.jpg")  # (740, 310)-(1040, 380) of the 11 saved prompt frames, stacked
x0, y0, x1, y1 = 740, 310, 1040, 380

def hud():
    frame = np.full((720, 1280, 3), 90, np.uint8)
    frame[18:72, 30:84], frame[50:100, 600:1280] = duck, row
    return frame

frames = {"hud": hud(), "blank": np.full((720, 1280, 3), 90, np.uint8)}
for name, image, x, y in (("wind", wind, 240, 495), ("bell", bell, 240, 495)):
    frames[name] = hud()
    frames[name][y:y + image.shape[0], x:x + image.shape[1]] = image
for i in range(strip.shape[0] // (y1 - y0)):
    frames[f"prompt {i}"] = hud()
    frames[f"prompt {i}"][y0:y1, x0:x1] = strip[i * (y1 - y0):(i + 1) * (y1 - y0)]
frames["wind and prompt"] = frames["wind"].copy()
frames["wind and prompt"][y0:y1, x0:x1] = strip[:y1 - y0]
frames["prompt without its icon"] = frames["prompt 0"].copy()
frames["prompt without its icon"][330:365, 755:792] = 90  # the label alone: not the seen shape
frames["starsea tracker"] = hud()
frames["starsea tracker"][220:292, 100:270] = crop("move_tracker_starsea.jpg")
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
        verdict = move.classify(actions.read_screen(recognize), recognize, image)
        results[current["name"]] = [verdict.kind, verdict.reason]
        results[current["name"] + "/tracker"] = [list(verdict.regions), verdict.name_only]
        results[current["name"] + "/prompts"] = [list(extent) for extent in verdict.prompts]
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
    """The screen classes through the real nodes and full-frame OCR (both layers), on frames composed from device
    crops: the HUD label row and duck; the subtitle band of the two saved narration frames over the HUD (「暖暖」 /
    「起风了——大喵小心，别被吹跑啦。」, m2-goto1; 「大喵」 / 「暖暖，星之铃似乎在闪烁呢！摇动它试试吧？」, phase0); the
    interaction-prompt column of every saved frame with a prompt (move_prompt_column.jpg, top to bottom: m2-rl10
    t00-b-hud, t00-b, t01-a, t04-b-hud, t04-b, t05-a, m2-rl10b t14-b-hud, t14-b (拾取), m4-jump4 t00-b-hud, t00-b,
    phase-e-walk2's stop frame; 查看 on the others)."""

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
        self.assertEqual(outcome["starsea tracker"], ["hud", ""])  # 星海: the quest name alone, real OCR
        self.assertEqual(outcome["starsea tracker/tracker"], [["登上鸣鸣列车"], True])
        self.assertEqual(outcome["hud/tracker"], [[], False])
        self.assertEqual(outcome["wind"], ["narration", ""])
        self.assertEqual(outcome["bell"], ["narration", ""])
        for i in range(11):  # every real prompt: movement allowed, nothing tapped
            with self.subTest(prompt=i):
                self.assertEqual(outcome[f"prompt {i}"], ["hud_prompt", ""])
                (extent,) = outcome[f"prompt {i}/prompts"]
                self.assertTrue(move.controls_clear(extent, jump=True, bell=move.RuntimeIo.BELL_POINT))
        self.assertEqual(outcome["wind and prompt"][0], "other")  # narration with anything else: stop
        self.assertEqual(outcome["prompt without its icon"][0], "other")
        self.assertEqual(outcome["hud/prompts"], [])
        self.assertEqual(outcome["blank"][0], "other")
        self.assertEqual(outcome["page"][0], "other")
        self.assertTrue(outcome["page"][1].startswith("另一个页面"))

    def test_the_screen_text_node_reads_the_whole_frame(self):
        nodes = json.loads((ROOT / "resource_android/pipeline/android_move.json").read_text(encoding="utf-8"))
        self.assertEqual(list(nodes), [move.SCREEN_TEXT])
        node = nodes[move.SCREEN_TEXT]
        self.assertEqual((node["recognition"], node["action"], node["roi"]), ("OCR", "DoNothing", [0, 0, 1280, 720]))
        self.assertLessEqual(node["threshold"], min(move.TEXT_SCORE, move.ASCII_SCORE))  # Python applies the floors


if __name__ == "__main__":
    unittest.main()
