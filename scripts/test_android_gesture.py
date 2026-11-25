"""Android multi-contact gestures (host.android.gesture, Runtime.gesture) with a scripted controller:
python/python.exe -B scripts/test_android_gesture.py.

Runs on any OS: no emulator, adb or MaaFramework controller. Time runs on a fake clock, so a 1.5 s gesture takes no
real time. The desktop backend is pinned to macOS, the only host that selects the Android backend for now.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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


class ScriptedController:
    """Records every touch call with the fake time, whether the touch marker was on disk and the input log's last
    line then; ``raise_at``/``fail`` script exceptions and failed results, ``block`` advances the clock in a call."""

    def __init__(self, clock, state):
        self.clock, self.state, self.calls = clock, state, []
        self.raise_at, self.fail, self.block = {}, lambda kind, args: False, {}

    def _job(self, kind, args):
        index = len(self.calls)
        log = self.state.root / "logs" / "android-input.jsonl"
        last = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["outcome"] if log.exists() else None
        self.calls.append((round(self.clock.now, 3), kind, args, self.state.marker().is_file(), last))
        self.clock.now += self.block.pop(index, 0.0)
        if index in self.raise_at:
            raise self.raise_at.pop(index)
        ok = not self.fail(kind, args)
        return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=ok))

    def post_touch_down(self, x, y, contact, pressure):
        return self._job("down", (x, y, contact))

    def post_touch_move(self, x, y, contact, pressure):
        return self._job("move", (x, y, contact))

    def post_touch_up(self, contact):
        return self._job("up", (contact,))

    def post_screencap(self):
        raise AssertionError("no capture during a gesture")

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
        sleep = patch("daily.runtime.wall_time.sleep")  # between lift reads
        sleep.start()
        self.addCleanup(sleep.stop)

    def runtime(self, readings=None):
        from daily.runtime import Runtime
        rt = Runtime.__new__(Runtime)
        rt.context = SimpleNamespace(run_action=Mock(), tasker=SimpleNamespace(stopping=False, post_stop=Mock()),
                                     run_action_direct=Mock(return_value=SimpleNamespace(success=True)))
        rt.check_focus, rt.ensure_frame, rt.invalidate_frame = Mock(), Mock(return_value=True), Mock()
        rt._evidence = SimpleNamespace(observe=Mock(), remember_frame=Mock())
        rt.navigation_halted, rt._focus_generation = False, 0
        rt._active_inputs = Mock(return_value=None)
        rt._use_window_input = Mock(return_value=False)
        rt._validate_pointer_targets = Mock(return_value=(1280, 720))
        rt.pause = Mock(return_value=True)
        rt.controller = self.controller = ScriptedController(self.clock, self.state)
        self.reads = []
        snapshots = iter(readings) if readings is not None else (
            TouchSnapshot("none", t, t) for t in range(1000, 100000, 10))

        def touch_snapshot():
            self.reads.append(len(self.controller.calls))  # how many touch calls had been made by this read
            return next(snapshots)
        rt._device_target = SimpleNamespace(foreground=Mock(return_value=True), exists=Mock(return_value=True),
                                            serial="127.0.0.1:5555", touch_snapshot=Mock(side_effect=touch_snapshot))
        self.frame = np.zeros((720, 1280, 3), np.uint8)
        self.state.mark(self.frame, 0, SETTLED)  # the decision's frame, captured with device focus at generation 0
        return rt

    def log(self):
        path = self.data_root / "logs" / "android-input.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def outcomes(self):
        return [(entry["action"], entry["outcome"]) for entry in self.log()]


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
        self.assertEqual(self.controller.calls, [])
        self.assertFalse(self.state.marker().exists())
        self.assertEqual(self.log(), [])
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

    def test_the_caps_themselves_are_allowed(self):
        rt = self.runtime()
        with android_run():
            self.assertTrue(rt.gesture(g.joystick(270, 1, 1.5), source_frame=self.frame))
            self.assertTrue(rt.gesture(g.bell_press((616, 612), 4.5), source_frame=self.frame))
        ups = [t for t, kind, args, _m, _l in self.controller.calls if kind == "up"]
        self.assertEqual(len(ups), 2)
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

    def test_camera_drag_is_the_confirmed_swipe_and_capped(self):
        rt = self.runtime()
        with android_run():
            self.assertTrue(g.camera_drag(rt, self.frame, (640, 300), (840, 300), 0.4))
        action, param = rt.context.run_action_direct.call_args.args[:2]
        self.assertEqual((action, param.begin, param.end, param.duration, param.contact),
                         ("Swipe", [640, 300], [[840, 300]], [400], 0))
        with android_run(), self.assertRaises(host.UnsupportedInput):
            g.camera_drag(rt, self.frame, (640, 300), (840, 300), 1.6)
        self.assertEqual(rt.context.run_action_direct.call_count, 1)
        self.assertEqual(self.controller.calls, [])


class RuleTests(GestureTestCase):
    def test_the_marker_is_held_for_the_whole_gesture_and_cleared_after_the_device_confirms(self):
        rt = self.runtime()
        with android_run():
            self.assertTrue(g.joystick_step(rt, self.frame, 90, .8, 1.0, jump_at_s=.5))
        self.assertTrue(all(marker for _t, _k, _a, marker, _l in self.controller.calls))
        self.assertFalse(self.state.marker().exists())
        self.assertFalse(self.state.halted)
        # No device read (and no capture: post_screencap would raise) between the first down and the last up; the
        # lift is read only after every call was made.
        self.assertEqual(self.reads, [len(self.controller.calls)])

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
                self.assertTrue(self.state.halted)
                self.assertTrue(self.state.marker().exists())  # left for the runner to read the device
                self.assertEqual(self.outcomes()[-1], ("Gesture", outcome))
                rt.context.tasker.post_stop.assert_called()

    def test_a_failed_or_overrunning_call_lifts_and_halts(self):
        for name in ("failed move", "blocked move"):
            with self.subTest(name=name):
                self.setUp()
                rt = self.runtime()
                if name == "failed move":
                    self.controller.fail = lambda kind, args: kind == "move"
                else:
                    self.controller.block = {1: 2.0}  # the first move takes 2 s: past the cap, nothing more is sent
                with android_run(), self.assertRaises(host.InputUnconfirmed):
                    g.joystick_step(rt, self.frame, 270, 1, 1.0)
                self.assertEqual(self.controller.touches()[-1], ("up", (0,)))
                self.assertEqual(sum(1 for kind, _a in self.controller.touches() if kind == "move"), 1)
                self.assertTrue(self.state.halted and self.state.marker().exists())
                self.assertEqual(self.reads, [])  # nothing to confirm: the gesture itself failed
                self.assertEqual(self.outcomes()[-1], ("Gesture", "unconfirmed"))

    def test_a_lift_the_device_does_not_show_halts_and_keeps_the_marker(self):
        rt = self.runtime(readings=[TouchSnapshot("injected", 1000, None)] * 6)
        with android_run(), self.assertRaises(host.InputUnconfirmed):
            g.joystick_step(rt, self.frame, 270, 1, .5)
        self.assertTrue(self.state.halted and self.state.marker().exists())
        self.assertEqual(self.outcomes()[-1], ("Gesture", "unconfirmed: device reports injected"))
        self.assertEqual(len(self.reads), rt.RELEASE_READS)

    def test_a_stop_request_during_the_hold_lifts_early_and_is_confirmed(self):
        rt = self.runtime()
        self.clock.at(.5, lambda: (self.state.stop_request().parent.mkdir(parents=True, exist_ok=True),
                                   self.state.stop_request().write_text("stop")))
        with android_run():
            self.assertFalse(g.joystick_step(rt, self.frame, 270, 1, 1.5, jump_at_s=1.0))
        last_t, kind, args, _m, _l = self.controller.calls[-1]
        self.assertEqual((kind, args), ("up", (0,)))
        self.assertLess(last_t, .5 + 2 * g.POLL_S)  # lifted within a poll, not at 1.5 s
        self.assertNotIn(("down", (965, 537, 1)), self.controller.touches())
        self.assertFalse(self.state.marker().exists())
        self.assertFalse(self.state.halted)  # a stop is no unconfirmed touch
        self.assertEqual(self.outcomes(), [("Gesture", "sending"), ("Gesture", "stopped early: confirmed")])

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
                    self.state.stop_request().parent.mkdir(parents=True, exist_ok=True)
                    self.state.stop_request().write_text("stop")
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


DESKTOP_CHECK = r"""
import json, os, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(sys.argv[1]) / "agent"))
os.environ.pop("PI_CONTROLLER", None)
import host
from daily.runtime import Runtime
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
results["android modules"] = sorted(name for name in sys.modules if name.startswith("host.android"))
print(json.dumps(results))
"""


class DesktopTests(unittest.TestCase):
    def test_the_gesture_api_is_unreachable_on_windows_and_native_mac(self):
        # A fresh interpreter: Runtime.gesture refuses on the desktop backends before host.android is imported.
        env = {**os.environ, "MAAFW_BINARY_PATH": str(ROOT / "maafw")}
        env.pop("PI_CONTROLLER", None)
        # -X utf8: -I ignores PYTHONUTF8, and Windows consoles default to cp1252.
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", "-c", DESKTOP_CHECK, str(ROOT)],
                                capture_output=True, text=True, encoding="utf-8", timeout=120, env=env,
                                cwd=tempfile.gettempdir())
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(outcome, {"win32": "refused", "win32 backend": "win32", "macos": "refused",
                                   "macos backend": "macos", "android modules": []})

    def test_dispatch_still_takes_only_taps_and_swipes(self):
        self.assertEqual(set(android.DISPATCHABLE_ACTIONS), {"Click", "Swipe"})
        with self.assertRaises(host.UnsupportedInput):
            android.check_touch_fields("Gesture", {"steps": []})


if __name__ == "__main__":
    unittest.main()
