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
        self.stop_after_observations = None
        self.bell_raises = None
        self.captured = {}
        self.present = True  # the minimap chevron on the next frames
        self.regions = lambda number: ()  # the quest tracker's rows on frame ``number``

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
        return Observation(frame, kind, at, reason, self.clock(), self.regions(self.observed))

    BELL_S = 4.0

    def step(self, frame, angle, seconds, jump_at, *, not_after=None):
        assert frame is self.frames[-1], "a step must be decided on the frame just captured"
        assert not_after is not None and not_after == frame_captured(self, frame) + move.MAX_DECISION_AGE_S
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
        assert not_after == frame_captured(self, frame) + move.MAX_DECISION_AGE_S
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
HUD_TEXT = (("商城", [630, 60, 40, 20]), ("登上鸣鸣列车", [110, 235, 120, 25]), ("1049米", [120, 262, 60, 20]),
            ("心誓5×1", [520, 668, 200, 22]), ("UID 123456789", [0, 700, 90, 18]))


class ClassifyTests(unittest.TestCase):
    """What may move: the HUD and nothing else; narration only as one ambient line (rows as OCR read them)."""

    def test_the_hud_alone_and_its_own_text_move(self):
        self.assertEqual(hud_with(), ("hud", ""))
        self.assertEqual(hud_with(*HUD_TEXT), ("hud", ""))
        self.assertEqual(hud_with(("口", [640, 610, 20, 20]), ("一飞", [240, 500, 800, 95], 0.52)), ("hud", ""))

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
                self.assertEqual(kind, "other")
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


TRACKER = ("登上鸣鸣列车", "#米")


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
                self.assertEqual(hud_with(*rows, *HUD_TEXT)[0], "other")

    def test_1_the_huds_own_text_as_ocr_misreads_it_still_moves(self):
        misreads = (("奇迹之旅活动人厅奇想日历无限之心", [700, 60, 360, 22]), ("共吗", [705, 60, 40, 22]),
                    ("LID123456789", [0, 700, 90, 18]), ("23Gms", [80, 700, 40, 18]), ("心誓5や♡", [520, 668, 120, 22]),
                    ("2A1", [966, 535, 30, 20], 0.79), ("ittit", [1017, 95, 30, 14], 0.69))
        self.assertEqual(hud_with(*misreads, *HUD_TEXT), ("hud", ""))

    def test_1_the_first_frame_must_be_a_clean_hud(self):
        for name, kinds, regions in (("quest start in the tracker", ["hud"], lambda n: ("任务开始",)),
                                     ("two rows in the tracker", ["hud"], lambda n: ("登上鸣鸣列车", "新的委托")),
                                     ("narration", ["narration"], lambda n: TRACKER)):
            with self.subTest(name):
                walk, io = walker(World((0, 0)), kinds=kinds)
                io.regions = regions
                result = walk.walk([Goal(0, -6)], (0, 0), 60)
                self.assertEqual((result.outcome, io.steps), ("refused", []))
                self.assertEqual(io.saved[0][2].number, 1)

    def test_1_a_tracker_change_stops_but_ocrs_misreads_do_not(self):
        walk, io = walker(World((0, 0), yaw=0.0))
        io.regions = lambda n: TRACKER if n == 1 else ("登上呜呜列车", "#米") if n == 2 else ("登上鸣鸣列车",)
        self.assertTrue(walk.walk([Goal(0, -12)], (0, 0), 60).ok)
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
        with patch("host.device_input", return_value=SimpleNamespace(touch_of=lambda f: TouchSnapshot("none", 1, 1))), \
                patch("host.android.actions.read_screen", return_value=Screen("hud", {"duck": [30, 18, 54, 54]})):
            rt = SimpleNamespace(stopped=False, _fresh_device_frame=lambda: dark, _frame_still_valid=lambda f: True,
                                 _recognizer_on=lambda f: (lambda node, override=None: Ocr([])))
            self.assertEqual(move.RuntimeIo(rt).observe().kind, "hud")

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
            return Observation(frame, "hud", at)
        io.observe = observe
        io.step = lambda frame, angle, seconds, jump_at, not_after=None: (io.steps.append((angle, seconds)),
                                                                           clock.__setattr__("t", clock.t + seconds
                                                                                             + 0.1),
                                                                           len(io.steps) < 2)[-1]
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
            self.assertEqual((line["kind"], line["stopped"], line["attempted"], line["confirmed"]),
                             ("result", True, 1, 1))

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
wind, bell, look = crop("move_narration_wind.jpg"), crop("move_narration_bell.jpg"), crop("move_prompt_look.jpg")

def hud():
    frame = np.full((720, 1280, 3), 90, np.uint8)
    frame[18:72, 30:84], frame[50:100, 600:1280] = duck, row
    return frame

frames = {"hud": hud(), "blank": np.full((720, 1280, 3), 90, np.uint8)}
for name, image, x, y in (("wind", wind, 240, 495), ("bell", bell, 240, 495), ("look prompt", look, 760, 315)):
    frames[name] = hud()
    frames[name][y:y + image.shape[0], x:x + image.shape[1]] = image
frames["wind and look prompt"] = frames["wind"].copy()
frames["wind and look prompt"][315:375, 760:900] = look
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
        verdict = move.classify(actions.read_screen(recognize), recognize)
        results[current["name"]] = [verdict.kind, verdict.reason]
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
    interaction prompt 「查看」 seen over the HUD in m2-rl10."""

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
        self.assertEqual(outcome["wind"], ["narration", ""])
        self.assertEqual(outcome["bell"], ["narration", ""])
        self.assertEqual(outcome["look prompt"][0], "other")
        self.assertEqual(outcome["wind and look prompt"][0], "other")  # narration with anything else: stop
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
