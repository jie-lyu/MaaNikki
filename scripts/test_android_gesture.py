"""Android multi-contact gestures (host.android.gesture, Runtime.gesture) with a scripted controller:
python/python.exe -B scripts/test_android_gesture.py.

Runs on any OS: no emulator, adb or MaaFramework controller. Time runs on a fake clock, so a 1.5 s gesture takes no
real time. The desktop backend is pinned to macOS, the only host that selects the Android backend for now.

The scripted controller runs posted jobs in order, as MaaFramework's does, and its device model sees a call's event
only once that job completed: a new injected UP (ACTION_UP) appears only when the last held contact is lifted, so a
gesture is confirmed by its own final lift and never by an earlier one.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))

import numpy as np  # noqa: E402

import host  # noqa: E402
from host import android  # noqa: E402
from host.android import gesture as g  # noqa: E402
from host.android.target import TouchSnapshot  # noqa: E402

ADB_CONTROLLER = {"name": "nikki_android", "type": "Adb", "display_short_side": 720}
BOOT_ID = "992f1bef-d353-4a1b-b401-b9867f884164"
SETTLED = TouchSnapshot("none", 900, 900, BOOT_ID)  # the reading kept with the decision's frame: nothing held
NEVER = float("inf")
OVERRAN = "unconfirmed: overran its time cap"


@contextmanager
def android_run():
    env = {k: v for k, v in os.environ.items() if k not in ("PI_CONTROLLER", "MAANIKKI_MACOS_EXTRA_TASKS")}
    env["PI_CONTROLLER"] = json.dumps(ADB_CONTROLLER)
    with patch.dict(os.environ, env, clear=True), patch.object(host, "platform_backend", return_value="macos"), \
            patch("daily.runtime.active_session", return_value=None):
        yield


def injected_owner_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(close=lambda: None)


def stop_requested(state):
    state.stop_request().parent.mkdir(parents=True, exist_ok=True)
    state.stop_request().write_text("stop")


class Clock:
    """Fake time for host.android.gesture: sleep advances it; ``at`` runs a hook once the clock reaches a time."""

    def __init__(self):
        self.now, self.hooks = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        for hook in list(self.hooks):
            if self.now >= hook[0] - 1e-9:
                self.hooks.remove(hook)
                hook[1]()

    def at(self, when, action):
        self.hooks.append((when, action))


class Status:
    """maa.define.Status as the gesture code reads it."""

    def __init__(self, name):
        self.name = name

    done = property(lambda self: self.name in ("succeeded", "failed"))
    succeeded = property(lambda self: self.name == "succeeded")
    failed = property(lambda self: self.name == "failed")
    pending = property(lambda self: self.name == "pending")
    running = property(lambda self: self.name == "running")


class Job:
    """A controller job on the fake clock. ``status`` is a round trip to the controller (ScriptedController.status:
    IPC latency, stalls, errors); job id 0 is MaaFramework's invalid id (a rejected post). A gesture never waits on
    a job blocking, so ``wait`` fails the test."""

    def __init__(self, controller, job_id, start=0.0, done_at=0.0, ok=True):
        self.controller, self.job_id, self.start, self.done_at, self.ok = controller, job_id, start, done_at, ok
        self.cleared = self.stopped_running = False

    @property
    def status(self):
        return self.controller.status(self)

    @property
    def done(self):
        return self.status.done

    @property
    def succeeded(self):
        return self.status.succeeded

    def wait(self):
        raise AssertionError("a gesture must not block on a controller job")


class ScriptedController:
    """MaaFramework's controller as far as the gesture relies on it (v5.16.1 ControllerAgent and AsyncRunner).

    Jobs run one at a time in the order posted: job i starts when posted or when the previous one is done and takes
    ``delay[i]`` (NEVER: wedged); ``block[i]`` blocks inside call i itself (the clock runs, hooks fire);
    ``raise_at[i]`` raises from call i; ``fail(kind, args)`` makes a job report failure; ``reject(kind, args)``
    returns the invalid id 0 (never queued). A status read takes ``status_latency`` (IPC), plus ``status_stall``
    once, and raises when ``status_raise(job)``. ``post_stop`` (Tasker::post_stop) drops every job that has not
    started (status invalid, never delivered); the running one completes (its status reads invalid meanwhile), and
    while it runs a new post is rejected (id 0). The device sees a job's event ``lag`` after the job completed
    (MaaTouch still has to handle it), in order; ``drop(kind, args)`` hides it (MaaTouch gone)."""

    def __init__(self, clock, state):
        self.clock, self.state, self.calls, self.jobs, self.screencaps, self.events = clock, state, [], [], [], []
        self.raise_at, self.block, self.delay, self.tap_delays = {}, {}, {}, []
        self.fail = self.drop = self.reject = lambda kind, args: False
        self.status_raise = lambda job: False
        self.status_latency, self.status_stall, self.lag = 0.001, 0.0, 0.01
        self.busy_until, self.next_id, self.need_to_stop = 0.0, 1, False
        self.lock = threading.Lock()

    def _job(self, kind, args):
        index = len(self.calls)
        log = self.state.root / "logs" / "android-input.jsonl"
        last = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["outcome"] if log.exists() else None
        self.calls.append((round(self.clock.now, 3), kind, args, self.state.marker().is_file(), last))
        self.events.append(("post", kind, args[-1], self.clock.now))
        if index in self.block:
            self.clock.sleep(self.block.pop(index))
        if index in self.raise_at:
            raise self.raise_at.pop(index)
        if self.need_to_stop:
            if self.clock.now < self.busy_until:
                return Job(self, 0)  # ControllerAgent::check_stop: "stopping, ignore new post"
            self.need_to_stop = False
        if self.reject(kind, args):
            return Job(self, 0)
        start = max(self.clock.now, self.busy_until)
        done_at = self.busy_until = start + self.delay.pop(index, 0.0)
        job = Job(self, self.next_id, start, done_at, not self.fail(kind, args))
        self.next_id += 1
        with self.lock:
            self.jobs.append((kind, args, job, self.drop(kind, args), done_at + self.lag))
        return job

    def status(self, job):
        self.events.append(("status", job.job_id, self.clock.now))
        stall, self.status_stall = self.status_stall, 0.0
        self.clock.sleep(self.status_latency + stall)
        if self.status_raise(job):
            raise RuntimeError("status RPC failed")
        now = self.clock.now
        if job.job_id == 0 or job.cleared:
            return Status("invalid")
        if now < job.start:
            return Status("pending")
        if now < job.done_at:
            return Status("invalid" if job.stopped_running else "running")
        return Status("succeeded" if job.ok else "failed")

    def post_stop(self):
        now, self.need_to_stop, running = self.clock.now, True, []
        for _kind, _args, job, _dropped, _deliver in self.jobs:
            if job.start > now:
                job.cleared = True
            elif job.done_at > now:
                job.stopped_running = True
                running.append(job.done_at)
        self.busy_until = max(running, default=now)

    def post_touch_down(self, x, y, contact, pressure):
        return self._job("down", (x, y, contact))

    def post_touch_move(self, x, y, contact, pressure):
        return self._job("move", (x, y, contact))

    def post_touch_up(self, contact):
        return self._job("up", (contact,))

    def tap(self, *_args, **_kwargs):
        """run_action_direct for a tap or swipe: one contact down and up, delivered after ``tap_delays.pop(0)``."""
        deliver = self.clock.now + (self.tap_delays.pop(0) if self.tap_delays else 0.0)
        with self.lock:
            for kind, args in (("down", (640, 360, 0)), ("up", (0,))):
                self.jobs.append((kind, args, Job(self, -1, self.clock.now, self.clock.now), False, deliver))
        return SimpleNamespace(success=True)

    def held(self):
        return self.device()[0]

    def device(self):
        """(contacts held, latest event time, latest injected ACTION_UP time) from the events delivered so far."""
        held, latest, action_up = set(), 900, 900
        with self.lock:
            jobs = list(self.jobs)
        for position, (kind, args, job, dropped, deliver_at) in enumerate(jobs):
            if job.cleared:
                continue
            if deliver_at > self.clock.now + 1e-9:
                break  # MaaTouch handles them in order
            if dropped or not job.ok:
                continue
            when, contact = 1000 + 10 * position, args[-1]
            if kind == "down" and contact not in held:
                held.add(contact)
            elif kind == "move" and contact in held:
                pass
            elif kind == "up" and contact in held:
                held.discard(contact)
                if not held:
                    action_up = when  # ACTION_UP: the last contact left; with another still down, POINTER_UP
            else:
                continue
            latest = when
        return held, latest, action_up

    def snapshot(self):
        held, latest, action_up = self.device()
        return TouchSnapshot("injected" if held else "none", latest, action_up, BOOT_ID)

    def post_screencap(self):
        self.screencaps.append((len(self.calls), set(self.held()), self.state.marker().is_file()))
        image = np.zeros((720, 1280, 3), np.uint8)
        return SimpleNamespace(wait=lambda: None, succeeded=True, get=lambda: image)

    def touches(self):
        return [(kind, args) for _t, kind, args, _m, _l in self.calls]


class GestureTestCase(unittest.TestCase):
    """Fresh process-wide Android input state per test, and a Runtime wired to a scripted controller and device."""

    def setUp(self):
        import daily.runtime  # noqa: F401  (patched by name below)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_root = Path(tmp.name)
        try:
            import fcntl  # noqa: F401
        except ImportError:  # Windows CI: ownership is injected; the real lock is tested on macOS
            owner = patch.object(host, "take_owner_lock", side_effect=injected_owner_lock)
            owner.start()
            self.addCleanup(owner.stop)
        self.state = state = host.DeviceInputState(self.data_root)
        self.addCleanup(lambda: state._owner.close() if state._owner is not None else None)
        patcher = patch.object(host, "_DEVICE_INPUT", state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()
        clock = patch.object(g, "time", self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        sleep = patch("daily.runtime.wall_time.sleep")  # between a tap's lift reads
        sleep.start()
        self.addCleanup(sleep.stop)
        self.controller = ScriptedController(self.clock, self.state)
        self.reads, self.read_timeouts, self.read_latency = [], [], 0.02
        self.frame = np.zeros((720, 1280, 3), np.uint8)
        self.state.mark(self.frame, 0, SETTLED)  # the decision's frame, captured with device focus confirmed

    def runtime(self, readings=None):
        from daily.runtime import Runtime
        rt = Runtime.__new__(Runtime)
        rt.context = SimpleNamespace(run_action=Mock(), run_action_direct=Mock(side_effect=self.controller.tap),
                                     tasker=SimpleNamespace(stopping=False,
                                                            post_stop=Mock(side_effect=self.controller.post_stop)))
        rt.check_focus, rt.ensure_frame, rt.invalidate_frame = Mock(), Mock(return_value=True), Mock()
        rt._evidence = SimpleNamespace(observe=Mock(), remember_frame=Mock())
        rt.navigation_halted, rt._focus_generation = False, 0
        rt._active_inputs = Mock(return_value=None)
        rt._use_window_input = Mock(return_value=False)
        rt._validate_pointer_targets = Mock(return_value=(1280, 720))
        rt.pause = Mock(return_value=True)
        rt.ui = SimpleNamespace(unhover=Mock(return_value=True))
        rt.controller = self.controller
        snapshots = iter(readings) if readings is not None else None

        def touch_snapshot(timeout_s=None):
            self.reads.append(len(self.controller.calls))  # how many touch calls had been made by this read
            self.read_timeouts.append(timeout_s)
            if timeout_s is not None:  # a gesture's confirmation: a shell read on the gesture clock, capped
                self.clock.sleep(min(self.read_latency, timeout_s))
            return next(snapshots) if snapshots is not None else self.controller.snapshot()
        rt._device_target = SimpleNamespace(foreground=Mock(return_value=True), exists=Mock(return_value=True),
                                            serial="127.0.0.1:5555", touch_snapshot=Mock(side_effect=touch_snapshot))
        # Runtime._capture on the Android backend
        rt._device_focus_ready = Mock(return_value=True)
        rt.observation_generation = Mock(return_value=0)
        rt.validate_frame = Mock()
        return rt

    def fresh_frame(self):
        """A frame captured now (focus confirmed, the latest capture), with the device's reading taken right before
        it."""
        frame = np.zeros((720, 1280, 3), np.uint8)
        self.state.mark(frame, 0, self.controller.snapshot())
        return frame

    def log(self):
        path = self.data_root / "logs" / "android-input.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def outcomes(self):
        return [(entry["action"], entry["outcome"]) for entry in self.log()]

    def assert_nothing_sent(self, calls=0, outcomes=()):
        self.assertEqual(len(self.controller.calls), calls)
        self.assertFalse(self.state.marker().exists())
        self.assertFalse(self.state.halted)
        self.assertEqual(self.outcomes(), list(outcomes))

    def assert_halted_with_marker(self, outcome):
        self.assertTrue(self.state.halted)
        self.assertTrue(self.state.marker().exists())  # left for the runner to read the device
        self.assertEqual(self.outcomes()[-1], ("Gesture", outcome))


class CapTests(GestureTestCase):
    def test_a_gesture_over_its_cap_is_refused_before_anything_is_reserved_or_sent(self):
        rt = self.runtime()
        over = [g.joystick(270, 1, 1.51), g.joystick(270, 1, 1.6, jump_at_s=1.0),
                g.Gesture("joystick", (g.Step(0, "down", 0, (212, 572)), g.Step(1.6, "up", 0))),
                g.bell_press((616, 612), 4.51),
                # the bell's exception is one contact pressed in place: a moving script under its name is refused
                g.Gesture(g.BELL_PRESS, (g.Step(0, "down", 0, (616, 612)), g.Step(.5, "move", 0, (620, 612)),
                                         g.Step(2, "up", 0)))]
        for gesture in over:
            with self.subTest(gesture=gesture.log_fields()), android_run(), self.assertRaises(host.UnsupportedInput):
                rt.gesture(gesture, source_frame=self.frame)
        with self.assertRaises(host.UnsupportedInput):
            g.joystick(270, 1, 1.0, jump_at_s=0.95)  # the jump tap must end before the joystick lifts
        self.assert_nothing_sent()
        rt._evidence.observe.assert_not_called()

    def test_malformed_scripts_are_refused(self):
        down, up = g.Step(0, "down", 0, (212, 572)), g.Step(1, "up", 0)
        for steps in ((down,), (down, g.Step(1, "up", 1)), (down, g.Step(.5, "down", 0, (1, 1)), up),
                      (down, g.Step(.5, "down", 2, (1, 1)), g.Step(.6, "up", 2), up),
                      (down, g.Step(.5, "down", True, (1, 1)), g.Step(.6, "up", True), up),
                      (g.Step(0, "down", 0, (1280, 10)), up), (g.Step(0, "down", 0, (10.0, 10)), up),
                      (g.Step(.1, "down", 0, (1, 1)), up), (down, g.Step(.5, "move", 0, (1, 1)), g.Step(.4, "up", 0)),
                      (down, g.Step(float("nan"), "up", 0)), (down, g.Step(0, "up", 0)),
                      (down, g.Step(.5, "down", 1, (1, 1)), up),  # contact 1 never lifted
                      (down, g.Step(1, "tap", 0, (1, 1)), up)):
            with self.subTest(steps=steps), self.assertRaises(host.UnsupportedInput):
                g.check_gesture(g.Gesture("joystick", steps))
        for bad in (dict(magnitude=0), dict(magnitude=1.2), dict(duration_s=0.04), dict(angle_deg=float("inf"))):
            with self.subTest(bad=bad), self.assertRaises(host.UnsupportedInput):
                g.joystick(**{"angle_deg": 0, "magnitude": 1, "duration_s": 1, **bad})

    def test_only_the_joystick_hold_with_one_enclosed_jump_and_the_bell_press_are_accepted(self):
        # Review finding 2: one reservation held two independent taps, and the first tap's lift confirmed both.
        rt = self.runtime()
        d0, u0 = g.Step(0, "down", 0, (212, 572)), g.Step(1.0, "up", 0)

        def d1(at):
            return g.Step(at, "down", 1, (965, 537))

        def u1(at):
            return g.Step(at, "up", 1)
        refused = {
            "two sequential taps": ("joystick", (d0, g.Step(.2, "up", 0), d1(.5), u1(.6))),
            "contact 1 outlives contact 0": ("joystick", (d0, d1(.3), g.Step(.5, "up", 0), u1(.8))),
            "contact 1 lifted with contact 0": ("joystick", (d0, d1(.3), u1(1.0), u0)),
            "contact 1 down with contact 0": ("joystick", (d0, d1(0), u1(.5), u0)),
            "contact 1 first": ("joystick", (d1(0), g.Step(.1, "down", 0, (212, 572)), u1(.5), u0)),
            "two jumps": ("joystick", (d0, d1(.2), u1(.3), d1(.5), u1(.6), u0)),
            "contact 1 moves": ("joystick", (d0, d1(.2), g.Step(.3, "move", 1, (970, 537)), u1(.4), u0)),
            "contact 0 pressed twice": ("joystick", (d0, g.Step(.3, "up", 0), g.Step(.4, "down", 0, (212, 572)), u0)),
            "bell on contact 1": (g.BELL_PRESS, (g.Step(0, "down", 1, (616, 612)), g.Step(2, "up", 1))),
            "unknown shape": ("swipe", (d0, g.Step(.3, "move", 0, (300, 572)), u0)),
        }
        for name, (kind, steps) in refused.items():
            with self.subTest(name=name), android_run(), self.assertRaises(host.UnsupportedInput):
                rt.gesture(g.Gesture(kind, steps), source_frame=self.frame)
        self.assert_nothing_sent()  # refused before the reservation, the intent log or any call
        g.check_gesture(g.joystick(270, 1, 1.0, jump_at_s=.5))
        g.check_gesture(g.joystick(270, 1, 1.0))
        g.check_gesture(g.bell_press((616, 612), 4))

    def test_the_caps_themselves_are_allowed(self):
        rt = self.runtime()
        with android_run():
            self.assertTrue(rt.gesture(g.joystick(270, 1, 1.5), source_frame=self.frame))
            self.assertTrue(rt.gesture(g.bell_press((616, 612), 4.5), source_frame=self.fresh_frame()))
        times = [t for t, kind, args, _m, _l in self.controller.calls if args[-1] == 0 and kind != "move"]
        self.assertEqual([round(up - down, 3) for down, up in zip(times[::2], times[1::2])], [1.5, 4.5])
        self.assertEqual((g.GESTURE_MAX_S, g.BELL_PRESS_MAX_S), (1.5, 4.5))
        self.assertEqual(self.outcomes(), [("Gesture", "sending"), ("Gesture", "confirmed")] * 2)


class JoystickTests(GestureTestCase):
    def test_the_joystick_script_follows_the_layout(self):
        rt = self.runtime()
        with android_run():
            self.assertTrue(g.joystick_step(rt, self.frame, 270, 1, 0.6, jump_at_s=0.3))
        timed = [(t, kind, args) for t, kind, args, _m, _l in self.controller.calls]
        self.assertEqual(timed[0], (0.0, "down", (212, 572, 0)))
        self.assertEqual([args for _t, kind, args in timed if kind == "move"],
                         [(212, 548, 0), (212, 523, 0), (212, 499, 0)])  # pushed up to the ring (radius 73)
        self.assertEqual(timed[4:], [(0.3, "down", (965, 537, 1)), (0.38, "up", (1,)), (0.6, "up", (0,))])
        self.assertEqual(g.joystick(0, .5, 1).steps[-2].point, (248, 572))
        custom = g.joystick(180, 1, 1, layout=g.Layout(joystick=(300, 500), ring=50, jump=(900, 500)))
        self.assertEqual((custom.steps[0].point, custom.steps[-2].point), ((300, 500), (250, 500)))
        self.assertEqual(g.DEFAULT_LAYOUT, g.Layout((212, 572), 73, (965, 537)))

    def test_camera_drag_is_the_confirmed_swipe_capped_and_needs_a_frame(self):
        rt = self.runtime()
        with android_run():
            self.assertTrue(g.camera_drag(rt, self.frame, (640, 300), (840, 300), 0.4))
        action, param = rt.context.run_action_direct.call_args.args[:2]
        self.assertEqual((action, param.begin, param.end, param.duration, param.contact),
                         ("Swipe", [640, 300], [[840, 300]], [400], 0))
        rt.action = Mock(side_effect=AssertionError("refused before Runtime.action"))
        for frame, duration in ((self.frame, 1.6), (None, 0.4)):  # review finding 6: no source frame
            with self.subTest(frame=frame is not None, duration=duration), android_run(), \
                    self.assertRaises(host.UnsupportedInput):
                g.camera_drag(rt, frame, (640, 300), (840, 300), duration)
        self.assertEqual(rt.context.run_action_direct.call_count, 1)
        self.assertEqual(self.controller.calls, [])


class FrameTests(GestureTestCase):
    """Review finding 1: a frame captured before an input authorised the next gesture, and the earlier gesture's UP
    confirmed it."""

    def test_a_second_gesture_or_any_input_from_the_frame_before_a_gesture_is_refused(self):
        rt = self.runtime()
        with android_run():
            self.assertTrue(g.joystick_step(rt, self.frame, 270, 1, .5))
            calls, log = len(self.controller.calls), self.outcomes()
            for name, send in (("gesture", lambda: rt.gesture(g.bell_press((616, 612), 1), source_frame=self.frame)),
                               ("tap", lambda: rt.action("Click", target=[640, 360], source_frame=self.frame)),
                               ("swipe", lambda: rt.action("Swipe", begin=[640, 300], end=[840, 300], duration=200,
                                                           source_frame=self.frame))):
                with self.subTest(name=name), self.assertRaises(host.UnsupportedInput):
                    send()
            self.assert_nothing_sent(calls, log)
            rt.context.run_action_direct.assert_not_called()
            # A frame captured after the gesture was confirmed authorises the next input.
            self.assertTrue(rt.action("Click", target=[640, 360], source_frame=self.fresh_frame()))
            self.assertTrue(rt.gesture(g.bell_press((616, 612), 1), source_frame=self.fresh_frame()))
        self.assertEqual(self.outcomes()[-2:], [("Gesture", "sending"), ("Gesture", "confirmed")])

    def test_a_gesture_needs_a_frame_captured_after_the_last_tap(self):
        rt = self.runtime()
        with android_run():
            self.assertTrue(rt.action("Click", target=[640, 360], source_frame=self.frame))
            log = self.outcomes()
            with self.assertRaises(host.UnsupportedInput):
                g.joystick_step(rt, self.frame, 270, 1, .5)
            self.assert_nothing_sent(0, log)
            self.assertTrue(rt.action("Click", target=[640, 360], source_frame=self.frame))  # taps: as before

    def test_an_earlier_lift_cannot_confirm_the_next_gesture(self):
        rt = self.runtime()
        with android_run():
            self.assertTrue(g.joystick_step(rt, self.frame, 270, 1, .5))
            frame = self.fresh_frame()  # its baseline is the first gesture's own UP
            self.controller.drop = lambda kind, args: True  # the second gesture never reaches the device
            with self.assertRaises(host.InputUnconfirmed):
                g.joystick_step(rt, frame, 0, 1, .5)
        self.assert_halted_with_marker("unconfirmed: device reports no new lift")


class RuleTests(GestureTestCase):
    def test_the_marker_is_held_for_the_whole_gesture_and_cleared_after_the_device_confirms(self):
        rt = self.runtime()
        with android_run():
            self.assertTrue(g.joystick_step(rt, self.frame, 90, .8, 1.0, jump_at_s=.5))
        self.assertTrue(all(marker for _t, _k, _a, marker, _l in self.controller.calls))
        self.assertFalse(self.state.marker().exists())
        self.assertFalse(self.state.halted)
        # No device read and no capture between the first down and the last up; the lift is read only after every
        # call was made.
        self.assertEqual(self.reads, [len(self.controller.calls)])
        self.assertEqual(self.controller.screencaps, [])

    def test_the_intent_is_logged_before_the_first_down_with_the_script(self):
        rt = self.runtime()
        with android_run():
            self.assertTrue(g.joystick_step(rt, self.frame, 0, 1, .5))
        self.assertEqual(self.controller.calls[0][4], "sending")
        self.assertEqual(self.outcomes(), [("Gesture", "sending"), ("Gesture", "confirmed")])
        entry = self.log()[0]
        self.assertEqual((entry["gesture"], entry["duration"]), ("joystick", 500))
        self.assertEqual(entry["steps"][0], [0, "down", 0, 212, 572])
        self.assertEqual(entry["steps"][-1], [500, "up", 0])
        rt._evidence.observe.assert_called_once()

    def test_an_exception_mid_gesture_lifts_every_contact_and_halts(self):
        cases = (
            ("second move raises", {2: RuntimeError("pipe broke")}, None, [("up", (0,))], "raised RuntimeError"),
            ("jump down raises", {4: RuntimeError("pipe broke")}, None, [("up", (1,)), ("up", (0,))],
             "raised RuntimeError"),
            ("interrupted in the hold", {}, KeyboardInterrupt, [("up", (0,))], "raised KeyboardInterrupt"))
        for name, raise_at, interrupt, lifts, outcome in cases:
            with self.subTest(name=name):
                self.setUp()
                rt = self.runtime()
                self.controller.raise_at = dict(raise_at)
                if interrupt:
                    self.clock.at(.3, lambda: (_ for _ in ()).throw(interrupt()))
                with android_run(), self.assertRaises(interrupt or host.InputUnconfirmed):
                    g.joystick_step(rt, self.frame, 270, 1, 1.0, jump_at_s=.4)
                failed_at = min(raise_at) if raise_at else len(self.controller.calls) - len(lifts)
                self.assertEqual(self.controller.touches()[failed_at + (1 if raise_at else 0):], lifts)
                self.assert_halted_with_marker(outcome)
                rt.context.tasker.post_stop.assert_called()

    def test_a_failed_call_lifts_and_halts(self):
        rt = self.runtime()
        self.controller.fail = lambda kind, args: kind == "move"
        with android_run(), self.assertRaises(host.InputUnconfirmed):
            g.joystick_step(rt, self.frame, 270, 1, 1.0)
        self.assertEqual(self.controller.touches()[-1], ("up", (0,)))
        self.assertEqual(sum(1 for kind, _a in self.controller.touches() if kind == "move"), 1)
        self.assertEqual(self.reads, [])  # nothing to confirm: the gesture itself failed
        self.assert_halted_with_marker("unconfirmed")

    def test_an_overrun_sends_only_lifts_and_is_never_confirmed(self):
        # Review finding 3: a blocked call let the jump go out past the cap, a late final UP was confirmed, and a
        # stop seen during an overlong call masked the overrun.
        cases = {
            "first move's job late": (dict(delay={1: 1.65}), g.joystick(270, 1, 1.5, jump_at_s=1.0)),
            "first move blocks": (dict(block={1: 1.65}), g.joystick(270, 1, 1.5, jump_at_s=1.0)),
            "bell's final up late": (dict(delay={1: 2.0}), g.bell_press((616, 612), 4.5)),
            "bell's final up blocks": (dict(block={1: 0.5}), g.bell_press((616, 612), 4.5)),
            "stop during a blocking move": (dict(block={1: 2.0}, stop_at=.5), g.joystick(270, 1, 1.5)),
        }
        for name, (script, gesture) in cases.items():
            with self.subTest(name=name):
                self.setUp()
                rt = self.runtime()
                self.controller.delay = dict(script.get("delay", {}))
                self.controller.block = dict(script.get("block", {}))
                if "stop_at" in script:
                    self.clock.at(script["stop_at"], lambda: stop_requested(self.state))
                with android_run(), self.assertRaises(host.InputUnconfirmed):
                    rt.gesture(gesture, source_frame=self.frame)
                touches = self.controller.touches()
                self.assertNotIn(("down", (965, 537, 1)), touches)  # no jump past the cap
                late = [kind for t, kind, _a, _m, _l in self.controller.calls
                        if kind != "up" and t > g.cap_for(gesture)]
                self.assertEqual(late, [])
                self.assertEqual(touches[-1], ("up", (0,)))
                self.assertEqual(self.reads, [])
                self.assert_halted_with_marker(OVERRAN)

    def test_a_lift_the_device_does_not_show_halts_and_keeps_the_marker(self):
        rt = self.runtime(readings=[TouchSnapshot("injected", 1000, None)] * 100)
        with android_run(), self.assertRaises(host.InputUnconfirmed):
            g.joystick_step(rt, self.frame, 270, 1, .5)
        self.assert_halted_with_marker("unconfirmed: device reports injected")
        self.assertGreaterEqual(len(self.reads), 2)  # read until the confirmation deadline, not a fixed count

    def test_a_stop_request_during_the_hold_lifts_early_and_is_confirmed(self):
        rt = self.runtime()
        self.clock.at(.5, lambda: stop_requested(self.state))
        with android_run():
            self.assertFalse(g.joystick_step(rt, self.frame, 270, 1, 1.5, jump_at_s=1.0))
        last_t, kind, args, _m, _l = self.controller.calls[-1]
        self.assertEqual((kind, args), ("up", (0,)))
        self.assertLess(last_t, .5 + 2 * g.POLL_S)  # lifted within a poll, not at 1.5 s
        self.assertNotIn(("down", (965, 537, 1)), self.controller.touches())
        self.assertFalse(self.state.marker().exists())
        self.assertFalse(self.state.halted)  # a stop is no unconfirmed touch
        self.assertEqual(self.outcomes(), [("Gesture", "sending"), ("Gesture", "stopped early: confirmed")])

    def test_a_stop_while_the_jump_is_held_lifts_contact_1_first_and_the_final_up_covers_both(self):
        # Review finding 2, cleanup path: contact 1's lift is posted before contact 0's, so the final ACTION_UP is
        # contact 0's; if contact 1 stayed down, contact 0's lift is a POINTER_UP and the device shows the contact.
        for name in ("both lifted", "contact 1's lift lost"):
            with self.subTest(name=name):
                self.setUp()
                rt = self.runtime()
                self.clock.at(.42, lambda: stop_requested(self.state))
                if name != "both lifted":
                    self.controller.drop = lambda kind, args: kind == "up" and args == (1,)
                with android_run():
                    if name == "both lifted":
                        self.assertFalse(g.joystick_step(rt, self.frame, 270, 1, 1.0, jump_at_s=.4))
                    else:
                        with self.assertRaises(host.InputUnconfirmed):
                            g.joystick_step(rt, self.frame, 270, 1, 1.0, jump_at_s=.4)
                self.assertEqual(self.controller.touches()[-3:], [("down", (965, 537, 1)), ("up", (1,)), ("up", (0,))])
                if name == "both lifted":
                    self.assertEqual(self.outcomes()[-1], ("Gesture", "stopped early: confirmed"))
                    self.assertFalse(self.state.marker().exists())
                else:
                    self.assertEqual(self.controller.held(), {1})
                    self.assert_halted_with_marker("unconfirmed: device reports injected")

    def test_a_stop_while_a_job_is_pending_posts_the_lifts_at_once_and_waits_a_bounded_time(self):
        # Review finding 4: a blocking job wait outlived the stopper's 5 s before any UP was posted.
        for name in ("down's job wedged", "down's job slow"):
            with self.subTest(name=name):
                self.setUp()
                rt = self.runtime()
                self.controller.delay = {0: NEVER if name == "down's job wedged" else 1.0}
                self.clock.at(.2, lambda: stop_requested(self.state))
                with android_run(), self.assertRaises(host.InputUnconfirmed):
                    g.joystick_step(rt, self.frame, 270, 1, 1.5)
                # The UP was posted behind the pending DOWN within a poll of the stop; nothing else was sent.
                self.assertEqual(self.controller.touches(), [("down", (212, 572, 0)), ("up", (0,))])
                self.assertLess(self.controller.calls[-1][0], .2 + 2 * g.POLL_S)
                # Gave up after CLEANUP_S at most, well inside the stopper's 5 s. The DOWN completed (or not) after
                # its own limit: an overrun, never an early stop (round 2 finding 4); the marker is kept.
                self.assertLess(self.clock.now, .2 + g.CLEANUP_S + 2 * g.POLL_S)
                self.assertEqual(self.reads, [])
                self.assert_halted_with_marker(OVERRAN)
                self.assertEqual(self.state.lifts_outstanding, name == "down's job wedged")

    def test_refused_on_stop_halt_held_contact_or_stale_frame(self):
        for name in ("stopping", "halted", "stop request", "held contact", "unreadable contact", "stale frame",
                     "no frame", "game not in front"):
            with self.subTest(name=name):
                self.setUp()
                rt = self.runtime()
                expected = None
                if name == "stopping":
                    rt.context.tasker.stopping = True
                elif name == "halted":
                    self.state.halt()
                elif name == "stop request":
                    stop_requested(self.state)
                elif name == "held contact":
                    self.state.mark(self.frame, 0, TouchSnapshot("injected", 950, None))
                    expected = host.InputUnconfirmed
                elif name == "unreadable contact":
                    self.state.mark(self.frame, 0, None)
                    expected = host.InputUnconfirmed
                elif name == "stale frame":
                    self.state.observed_loss()  # a focus loss after the frame was captured
                    expected = "ForegroundChanged"
                elif name == "no frame":
                    expected = host.UnsupportedInput
                else:
                    rt._device_target.foreground.return_value = False
                    expected = "ForegroundChanged"
                frame = None if name == "no frame" else self.frame
                with android_run():
                    if expected is None:
                        self.assertFalse(g.joystick_step(rt, frame, 270, 1, .5))
                    elif isinstance(expected, str):
                        with self.assertRaises(Exception) as caught:
                            g.joystick_step(rt, frame, 270, 1, .5)
                        self.assertEqual(type(caught.exception).__name__, expected)
                    else:
                        with self.assertRaises(expected):
                            g.joystick_step(rt, frame, 270, 1, .5)
                self.assertEqual(self.controller.calls, [])
                self.assertFalse(self.state.marker().exists())
                self.assertEqual(self.state.halted, name in ("halted", "held contact", "unreadable contact"))
                if name in ("stop request", "held contact", "unreadable contact", "game not in front"):
                    self.assertEqual(self.outcomes(), [("Gesture", "sending"), ("Gesture", "cancelled")])
                else:
                    self.assertEqual(self.log(), [])

    def test_a_marker_left_by_an_earlier_touch_refuses_the_gesture(self):
        rt = self.runtime()
        self.state.marker().parent.mkdir(parents=True, exist_ok=True)
        self.state.marker().write_text("{}")  # e.g. the agent died between a gesture's down and up
        with android_run(), self.assertRaises(host.InputUnconfirmed):
            g.joystick_step(rt, self.frame, 270, 1, .5)
        self.assertEqual(self.controller.calls, [])
        self.assertTrue(self.state.halted)


class CaptureTests(GestureTestCase):
    """Review finding 5: another Runtime's capture posted its screencap while a gesture's contact was down, and the
    frame was accepted. Android capture now holds the input lock from its touch reading until the frame is
    complete, and records the input count, so a frame from before a gesture authorises nothing after it."""

    def run_thread(self, target, results, key):
        def run():
            try:
                results[key] = target()
            except BaseException as error:  # noqa: BLE001 - reported by the test
                results[key] = error
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_a_gesture_waiting_behind_a_capture_is_refused_and_decides_on_the_newer_frame(self):
        gesturing, capturing = self.runtime(), self.runtime()
        read, go, results = threading.Event(), threading.Event(), {}

        def paused_reading():  # the capture pauses right after its idle touch reading
            snapshot = self.controller.snapshot()
            read.set()
            go.wait(5)
            return snapshot
        capturing._device_target = SimpleNamespace(**{**vars(capturing._device_target),
                                                      "touch_snapshot": Mock(side_effect=paused_reading)})
        with android_run():
            capture = self.run_thread(capturing.capture, results, "frame")
            self.assertTrue(read.wait(5))
            gesture = self.run_thread(lambda: g.joystick_step(gesturing, self.frame, 270, 1, .5), results, "gesture")
            gesture.join(.3)
            self.assertTrue(gesture.is_alive())  # waiting for the input lock: nothing reserved or sent yet
            self.assertEqual(self.controller.calls, [])
            self.assertFalse(self.state.marker().exists())
            go.set()
            capture.join(5)
            gesture.join(5)
            # Round 2 finding 7: a newer capture completed while the gesture waited, so its frame is no longer the
            # latest: refused, nothing reserved or sent; it must decide again on the newer pixels.
            self.assertIsInstance(results["gesture"], host.UnsupportedInput)
            self.assert_nothing_sent()
            frame = results["frame"]
            self.assertIsInstance(frame, np.ndarray)
            self.assertEqual(self.controller.screencaps, [(0, set(), False)])
            self.assertTrue(g.joystick_step(gesturing, frame, 0, 1, .5))  # the newer frame is the latest

    def test_a_capture_waits_until_the_gesture_is_lifted_and_confirmed(self):
        gesturing, capturing = self.runtime(), self.runtime()
        holding, proceed, results = threading.Event(), threading.Event(), {}
        self.clock.at(.3, lambda: (holding.set(), proceed.wait(5)))  # the gesture pauses mid-hold
        with android_run():
            gesture = self.run_thread(lambda: g.joystick_step(gesturing, self.frame, 270, 1, .6), results, "gesture")
            self.assertTrue(holding.wait(5))
            self.assertEqual(self.controller.held(), {0})
            capture = self.run_thread(capturing.capture, results, "frame")
            capture.join(.3)
            self.assertTrue(capture.is_alive())
            self.assertEqual(self.controller.screencaps, [])  # not posted while contact 0 is down
            proceed.set()
            gesture.join(5)
            capture.join(5)
            self.assertIs(results["gesture"], True)
            calls = len(self.controller.calls)
            self.assertEqual(self.controller.screencaps, [(calls, set(), False)])  # after the confirmed lift
            frame = results["frame"]
            self.assertFalse(self.state.predates_input(frame, gesture=True))
            self.assertTrue(g.joystick_step(capturing, frame, 0, 1, .5))


class ReviewRound2Tests(GestureTestCase):
    """One scenario per Codex round 2 finding (1-7), on the controller model above."""

    def test_1_a_halt_never_stops_the_tasker_while_a_lift_is_queued_behind_a_slow_down(self):
        # The model first: MaaFramework's stop drops the queued UP and refuses a new one while the DOWN runs.
        model = ScriptedController(self.clock, self.state)
        model.delay = {0: 1.7}
        model.post_touch_down(212, 572, 0, 1)
        queued = model.post_touch_up(0)
        model.post_stop()
        self.assertEqual((model.status(queued).name, model.post_touch_up(0).job_id), ("invalid", 0))
        self.clock.sleep(1.8)
        self.assertEqual(model.held(), {0})  # what the round 2 probe saw: the contact stays held
        # The gesture: DOWN takes 1.7 s, Stop at 0.2 s; cleanup posts the UP and gives up at 1.2 s.
        self.setUp()
        rt = self.runtime()
        self.controller.delay = {0: 1.7}
        self.clock.at(.2, lambda: stop_requested(self.state))
        with android_run(), self.assertRaises(host.InputUnconfirmed):
            g.joystick_step(rt, self.frame, 270, 1, 1.5)
        self.assert_halted_with_marker(OVERRAN)
        self.assertTrue(self.state.lifts_outstanding)
        rt.context.tasker.post_stop.assert_not_called()  # the queued UP survives
        self.clock.sleep(1.0)
        self.assertEqual(self.controller.held(), set())  # the DOWN completed, then its UP

    def test_2_lifts_go_out_before_any_status_read_and_the_stop_is_answered_inside_five_seconds(self):
        rt = self.runtime()
        self.controller.status_latency = 0.05  # a slow status RPC
        self.controller.delay = {4: 0.5}  # the jump DOWN is still pending at the stop
        self.clock.at(.45, lambda: stop_requested(self.state))
        with android_run(), self.assertRaises(host.InputUnconfirmed):
            g.joystick_step(rt, self.frame, 270, 1, 1.5, jump_at_s=.4)
        events = self.controller.events
        first = events.index(("post", "up", 1, events[[i for i, e in enumerate(events)
                                                      if e[:3] == ("post", "up", 1)][0]][3]))
        self.assertEqual([e[:3] for e in events[first:first + 2]], [("post", "up", 1), ("post", "up", 0)])
        reads_between = [e for e in events if e[0] == "status" and .45 < e[2] < events[first][3]]
        self.assertLessEqual(len(reads_between), 1)  # only the read in flight when the stop was published
        # Confirmation bounded by a deadline, device-read time included: the device never shows the lift and one
        # shell read would stall 5 s; the stop is still answered (marker kept) well inside the stopper's 5 s.
        self.setUp()
        rt = self.runtime()
        self.read_latency = 5.0
        self.controller.drop = lambda kind, args: kind == "up"
        self.clock.at(.5, lambda: stop_requested(self.state))
        with android_run(), self.assertRaises(host.InputUnconfirmed):
            g.joystick_step(rt, self.frame, 270, 1, 1.5)
        self.assertLess(self.clock.now, .5 + 3.0)
        self.assertTrue(all(timeout <= g.CONFIRM_S for timeout in self.read_timeouts))
        self.assert_halted_with_marker("unconfirmed: device reports injected")

    def test_3_a_rejected_lift_is_retried_and_a_status_error_skips_no_other_lift(self):
        for name in ("jump up rejected", "jump up status raises"):
            with self.subTest(name=name):
                self.setUp()
                rt = self.runtime()
                if name == "jump up rejected":
                    rejected = []

                    def reject(kind, args):  # the scripted jump UP only (MaaFramework returns id 0)
                        if kind == "up" and args == (1,) and not rejected:
                            rejected.append(True)
                            return True
                        return False
                    self.controller.reject = reject
                else:
                    first_up1 = []

                    def status_raise(job):
                        entry = next((e for e in self.controller.jobs if e[2] is job), None)
                        if entry and entry[:2] == ("up", (1,)) and (not first_up1 or first_up1[0] is job):
                            first_up1[:] = [job]
                            return True
                        return False
                    self.controller.status_raise = status_raise
                with android_run(), self.assertRaises(host.InputUnconfirmed):
                    g.joystick_step(rt, self.frame, 270, 1, 1.0, jump_at_s=.4)
                ups = [args for kind, args in self.controller.touches() if kind == "up"]
                self.assertEqual(ups, [(1,), (0,), (1,)])  # contact 0's up, then the jump's retried once
                self.clock.sleep(.1)
                self.assertEqual(self.controller.held(), set())
                self.assertTrue(self.state.halted and self.state.marker().exists())

    def test_4_a_late_jump_or_final_lift_is_an_overrun_never_a_confirmed_stop(self):
        cases = ("slow stop check before the jump", "jump down completes after the cap", "final up submitted late")
        for name in cases:
            with self.subTest(name=name):
                self.setUp()
                rt = self.runtime()
                gesture = g.joystick(270, 1, 1.5, jump_at_s=1.35)
                if name == cases[0]:
                    real, slow = self.state.stop_requested, []

                    def stop_check():  # one stop-file check takes 0.175 s, just before the jump is due
                        if self.clock.now >= 1.33 and not slow:
                            slow.append(True)
                            self.clock.now += .175
                        return real()
                    self.state.stop_requested = stop_check
                elif name == cases[1]:
                    self.controller.delay = {4: .25}  # posted at 1.35, completes at 1.60
                    self.clock.at(1.40, lambda: stop_requested(self.state))
                else:
                    gesture = g.joystick(270, 1, 1.5, jump_at_s=1.3)
                    self.controller.block = {5: .296}  # the jump UP returns at 1.676: the final UP is due at 1.5
                with android_run(), self.assertRaises(host.InputUnconfirmed):
                    rt.gesture(gesture, source_frame=self.frame)
                late = [(kind, args) for t, kind, args, _m, _l in self.controller.calls
                        if kind != "up" and t > 1.5 + g.TIMER_SLACK_S]
                self.assertEqual(late, [])  # after the cap, nothing but lifts
                if name == cases[0]:
                    self.assertNotIn(("down", (965, 537, 1)), self.controller.touches())
                self.assert_halted_with_marker(OVERRAN)

    def test_5_two_taps_from_one_frame_cannot_share_a_lift(self):
        rt = self.runtime()
        with android_run():
            self.assertTrue(rt.action("Click", target=[640, 360], source_frame=self.frame))
            self.controller.tap_delays = [NEVER]  # tap B has not reached the device: only A's UP is visible
            with self.assertRaises(host.InputUnconfirmed):
                rt.action("Click", target=[640, 360], source_frame=self.frame)
        self.assertTrue(self.state.halted and self.state.marker().exists())
        self.assertEqual(self.outcomes()[-1], ("Click", "unconfirmed: device reports no new lift"))
        # Same-frame taps stay legal, each confirmed by its own UP; and a frame captured before B reached the device
        # (baseline A's UP) cannot borrow B's UP for a gesture whose events never arrive.
        self.setUp()
        rt = self.runtime()
        with android_run():
            self.assertTrue(rt.action("Click", target=[640, 360], source_frame=self.frame))
            a_up = self.controller.device()[2]
            self.assertTrue(rt.action("Click", target=[640, 360], source_frame=self.frame))
            self.assertGreater(self.controller.device()[2], a_up)
            frame = np.zeros((720, 1280, 3), np.uint8)
            self.state.mark(frame, 0, TouchSnapshot("none", a_up, a_up, BOOT_ID))
            self.controller.drop = lambda kind, args: True
            with self.assertRaises(host.InputUnconfirmed):
                g.joystick_step(rt, frame, 0, 1, .5)
        self.assert_halted_with_marker("unconfirmed: device reports no new lift")

    def test_6_camera_drag_takes_the_gesture_freshness_rule_both_ways(self):
        rt = self.runtime()
        with android_run():
            self.assertTrue(g.camera_drag(rt, self.frame, (640, 300), (840, 300), .4))
            self.assertEqual(self.state.last_gesture, self.state.inputs)
            for send in (lambda: rt.action("Click", target=[640, 360], source_frame=self.frame),
                         lambda: g.camera_drag(rt, self.frame, (640, 300), (840, 300), .4)):
                with self.assertRaises(host.UnsupportedInput):
                    send()
            frame = self.fresh_frame()
            self.assertTrue(rt.action("Click", target=[640, 360], source_frame=frame))
            with self.assertRaises(host.UnsupportedInput):  # needs a frame captured after the last input
                g.camera_drag(rt, frame, (640, 300), (840, 300), .4)
            # Ordinary task swipes keep their rules: a tap may still follow one on the same frame.
            frame = self.fresh_frame()
            self.assertTrue(rt.action("Swipe", begin=[640, 300], end=[840, 300], duration=200, source_frame=frame))
            self.assertTrue(rt.action("Click", target=[640, 360], source_frame=frame))
        self.assertEqual(rt.context.run_action_direct.call_count, 4)

    def test_7_a_gesture_on_a_frame_older_than_the_latest_capture_is_refused(self):
        rt = self.runtime()
        self.fresh_frame()  # another Runtime's capture completed after self.frame (same focus, same input count)
        with android_run(), self.assertRaises(host.UnsupportedInput):
            g.joystick_step(rt, self.frame, 270, 1, .5)
        self.assert_nothing_sent()


DESKTOP_CHECK = r"""
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(sys.argv[1]) / "agent"))
os.environ.pop("PI_CONTROLLER", None)
import numpy
import host
import daily.runtime as runtime_module
from daily.runtime import Runtime


def forbidden(*args, **kwargs):
    raise AssertionError("Android input state reached off Android")


results = {}
for backend in ("win32", "macos"):
    with patch.object(host, "platform_backend", return_value=backend):
        rt = Runtime.__new__(Runtime)
        rt.controller = None  # any use of it would raise
        try:
            rt.gesture(object(), source_frame=object())
            results[backend] = "returned"
        except host.UnsupportedInput:
            results[backend] = "refused"
        results[backend + " backend"] = host.game_backend()
        # Capture is unchanged off Android: the input lock and count (device_input) are never touched.
        image = numpy.zeros((720, 1280, 3), numpy.uint8)
        rt = Runtime.__new__(Runtime)
        rt.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        rt.navigation_halted, rt._observations, rt._focus_generation, rt._pause_generation = False, [], 0, 0
        rt._seamless_background = lambda: False
        rt.check_focus = lambda: False
        rt._active_inputs = lambda: None
        rt.validate_frame = lambda frame: None
        rt._evidence = SimpleNamespace(remember_frame=lambda *args: None)
        rt.controller = SimpleNamespace(post_screencap=lambda: SimpleNamespace(
            wait=lambda: None, succeeded=True, get=lambda: image))
        with patch.object(runtime_module, "device_input", forbidden), patch.object(host, "device_input", forbidden):
            results[backend + " capture"] = "frame" if rt.capture() is image else "none"
results["android modules"] = sorted(name for name in sys.modules if name.startswith("host.android"))
# camera_drag (a source checkout can import it on the desktop) refuses before Runtime.action.
from host.android import gesture
for backend in ("win32", "macos"):
    with patch.object(host, "platform_backend", return_value=backend):
        rt = Runtime.__new__(Runtime)
        rt.controller = None
        rt.action = forbidden
        outcome = []
        for frame in (numpy.zeros((720, 1280, 3), numpy.uint8), None):
            try:
                gesture.camera_drag(rt, frame, (640, 300), (840, 300), 0.4)
                outcome.append("returned")
            except host.UnsupportedInput:
                outcome.append("refused")
        results[backend + " camera_drag"] = outcome
print(json.dumps(results))
"""


class DesktopTests(unittest.TestCase):
    def test_the_gesture_api_and_capture_changes_are_unreachable_on_windows_and_native_mac(self):
        # A fresh interpreter: Runtime.gesture refuses on the desktop backends before host.android is imported,
        # capture there never touches the Android input state, and camera_drag refuses before Runtime.action.
        env = {**os.environ, "MAAFW_BINARY_PATH": str(ROOT / "maafw")}
        env.pop("PI_CONTROLLER", None)
        # -X utf8: -I ignores PYTHONUTF8, and Windows consoles default to cp1252.
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", "-c", DESKTOP_CHECK, str(ROOT)],
                                capture_output=True, text=True, encoding="utf-8", timeout=120, env=env,
                                cwd=tempfile.gettempdir())
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(outcome, {"win32": "refused", "win32 backend": "win32", "win32 capture": "frame",
                                   "macos": "refused", "macos backend": "macos", "macos capture": "frame",
                                   "android modules": [],
                                   "win32 camera_drag": ["refused", "refused"],
                                   "macos camera_drag": ["refused", "refused"]})

    def test_dispatch_still_takes_only_taps_and_swipes(self):
        self.assertEqual(set(android.DISPATCHABLE_ACTIONS), {"Click", "Swipe"})
        with self.assertRaises(host.UnsupportedInput):
            android.check_touch_fields("Gesture", {"steps": []})


if __name__ == "__main__":
    unittest.main()
