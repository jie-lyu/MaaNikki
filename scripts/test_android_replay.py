"""Offline tests of scripts/android/replay.py, the one supported way to replay saved frames: one recognizer for a whole
replay, the memory, frame and wall-time limits (also on move_frames' measurement pass), and everything freed on the
way out: on an exception, on an abort and when setup fails half-way."""
from __future__ import annotations

import contextlib
import gc
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "android"))

import probe  # noqa: E402
import replay  # noqa: E402


class FakeController:
    built = 0

    def __init__(self):
        FakeController.built += 1
        self.image = None
        self.shown = []

    def show(self, image):
        self.image = image
        self.shown.append(image)


class FakeRecognizer:
    built = 0

    def __init__(self, controller, *, android_layer=False):
        FakeRecognizer.built += 1
        self.controller = controller
        self.android_layer = android_layer
        self.released = 0

    def on_one_frame(self, function):
        return function(self.controller.image)

    def release(self):
        self.released += 1


class Fakes:
    """probe's recognizer, frame controller, frame reader and native destroy replaced; events in order."""

    def __enter__(self):
        FakeController.built = FakeRecognizer.built = 0
        self.events = []
        self.controllers, self.recognizers = [], []
        fakes = self

        class Controller(FakeController):
            def __init__(self):
                super().__init__()
                fakes.controllers.append(self)

        class Recognizer(FakeRecognizer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                fakes.recognizers.append(self)

            def release(self):
                super().release()
                fakes.events.append("release recognizer")

        self.patches = [patch.object(probe, "Recognizer", Recognizer),
                        patch.object(probe, "frame_controller", Controller),
                        patch.object(probe, "read_frame", lambda path: f"pixels of {Path(path).name}"),
                        patch.object(probe, "destroy_native",
                                     lambda obj: fakes.events.append(f"destroy {type(obj).__name__}"))]
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *exc):
        for item in reversed(self.patches):
            item.stop()


class FakeJob:
    def __init__(self, succeeded):
        self.succeeded = succeeded

    def wait(self):
        return self


@contextlib.contextmanager
def fake_framework(log, *, bundle_ok=True, bind_ok=True, connect_ok=True):
    """Stand-in maa modules: the real probe.Recognizer, probe.frame_controller and probe.destroy_native run against
    them and one step fails half-way. `log` gets each creation and each native destroy call, in order."""

    class Native:
        _own = True

        def __init__(self):
            self._handle = object()
            log.append(f"create {type(self).__name__}")

    class Resource(Native):
        def __init__(self):
            super().__init__()
            self._custom_action_holder, self._custom_recognition_holder = {}, {}

        def post_bundle(self, path):
            return FakeJob(bundle_ok)

        def register_custom_recognition(self, name, recognition):
            self._custom_recognition_holder[name] = recognition
            return True

        def register_custom_action(self, name, action):
            self._custom_action_holder[name] = action
            return True

    class Tasker(Native):
        def bind(self, resource, controller):
            self._resource_holder, self._controller_holder = resource, controller
            return bind_ok

    class Controller(Native):
        pass

    class CustomController(Controller):
        def set_screenshot_target_short_side(self, side):
            pass

        def post_connection(self):
            return FakeJob(connect_ok)

    class CustomAction:
        pass

    def destroyer(name):
        return lambda handle: log.append(name)

    framework = SimpleNamespace(**{name: destroyer(name)
                                   for name in ("MaaTaskerDestroy", "MaaResourceDestroy", "MaaControllerDestroy")})
    modules = {"maa": ModuleType("maa")}
    for name, members in (("resource", {"Resource": Resource}), ("tasker", {"Tasker": Tasker}),
                          ("controller", {"Controller": Controller, "CustomController": CustomController}),
                          ("custom_action", {"CustomAction": CustomAction}),
                          ("library", {"Library": SimpleNamespace(framework=lambda: framework)})):
        module = ModuleType(f"maa.{name}")
        module.__dict__.update(members)
        modules[f"maa.{name}"] = module
    with patch.dict(sys.modules, modules), patch.object(probe, "agent_recognitions", lambda: {}), \
            patch.dict(probe.NATIVE_DESTROYED, clear=True):
        yield SimpleNamespace(Tasker=Tasker, Resource=Resource, Controller=Controller)


def frames(n):
    return [Path(f"frame-{i:03d}.png") for i in range(n)]


def no_rss():
    return 1


def images(folder, sizes, names=None):
    import cv2
    import numpy as np
    paths = []
    for i, size in enumerate(sizes):
        paths.append(Path(folder, names[i] if names else f"{i}.png"))
        cv2.imwrite(str(paths[-1]), np.full((*size, 3), i, np.uint8))
    return paths


def hd(image):
    return image is not None and image.shape[:2] == (720, 1280)


class OneRecognizerTests(unittest.TestCase):
    def test_replaying_many_frames_builds_one_recognizer_and_shows_each_frame_to_it(self):
        seen = []

        def callback(recognizer, path, image):
            seen.append((id(recognizer), path, image))
            return recognizer.on_one_frame(lambda captured: f"judged {captured}")

        with Fakes() as fakes:
            results = replay.replay_frames(frames(40), callback, rss=no_rss)
        self.assertEqual((FakeRecognizer.built, FakeController.built), (1, 1))
        self.assertEqual(len({identity for identity, _, _ in seen}), 1)
        self.assertEqual([path for _, path, _ in seen], frames(40))
        self.assertEqual(fakes.controllers[0].shown, [f"pixels of frame-{i:03d}.png" for i in range(40)] + [None])
        self.assertEqual(results, [(p, f"judged pixels of {p.name}") for p in frames(40)])  # the frame shown, in turn
        self.assertTrue(fakes.recognizers[0].android_layer)
        self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])  # tasker side first

    def test_accept_skips_frames_without_a_callback(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = images(folder, [(720, 1280), (360, 640), (720, 1280)]) + [Path(folder, "missing.png")]
            with Fakes():
                with replay.Replay(rss=no_rss) as session:
                    done = [path.name for path, _ in session.run(paths, lambda r, path, image: None, accept=hd)]
                    self.assertEqual((session.frames, session.guard.attempts), (2, 4))  # skipped: attempted
        self.assertEqual(done, ["0.png", "2.png"])

    def test_an_open_replay_cannot_be_entered_again(self):
        with Fakes() as fakes:
            session = replay.Replay(rss=no_rss)
            with session:
                first = session.recognizer
                with self.assertRaises(RuntimeError):
                    with session:
                        self.fail("entered twice")
                self.assertIs(session.recognizer, first)  # still owned by the open block
                self.assertEqual((FakeRecognizer.built, FakeController.built), (1, 1))  # nothing allocated
                self.assertEqual(fakes.events, [])
            self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])
            self.assertEqual(first.released, 1)
            with session:  # closed: a new block builds afresh
                pass
            self.assertEqual((FakeRecognizer.built, FakeController.built), (2, 2))


class LimitTests(unittest.TestCase):
    def test_the_memory_guard_stops_the_replay_above_the_limit(self):
        readings = iter([500, 900, 4000, 5000])  # bytes: after the build, then every 2 frames
        calls = []
        limits = replay.Limits(max_rss_bytes=1000, rss_every=2)
        with Fakes() as fakes:
            with self.assertRaises(replay.ReplayLimit) as caught:
                replay.replay_frames(frames(10), lambda r, path, image: calls.append(path), limits=limits,
                                     rss=lambda: next(readings))
        self.assertEqual(len(calls), 4)  # stopped at the second read after the build, not at the end
        self.assertIn("after 4 frames", str(caught.exception))
        self.assertIn("limit", str(caught.exception))
        self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])

    def test_the_guard_reads_memory_right_after_the_recognizer_is_built(self):
        with Fakes() as fakes:
            with self.assertRaises(replay.ReplayLimit):
                replay.replay_frames(frames(3), lambda *a: self.fail("ran a frame"), rss=lambda: 4 * replay.GB)
        self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])

    def test_the_last_partial_interval_is_checked_before_success(self):
        for count, readings in ((6, [100, 2900, 3100]), (3, [100, 3100])):  # setup, every 5th frame, the end
            with self.subTest(frames=count):
                reads = iter(readings)
                with Fakes() as fakes:
                    with self.assertRaises(replay.ReplayLimit) as caught:
                        replay.replay_frames(frames(count), lambda *a: None, rss=lambda: next(reads),
                                             limits=replay.Limits(max_rss_bytes=3000))
                self.assertIn(f"after {count} frames", str(caught.exception))
                self.assertEqual(list(reads), [])
                self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])

    def test_skipped_frames_count_toward_the_memory_reads_and_the_frame_limit(self):
        reads = []

        def rss():
            reads.append(1)
            return 1

        with tempfile.TemporaryDirectory() as folder:
            paths = images(folder, [(360, 640)] * 6)  # all skipped by `accept`
            with Fakes():
                with replay.Replay(rss=rss, limits=replay.Limits(rss_every=2)) as session:
                    self.assertEqual(list(session.run(paths, lambda *a: self.fail("ran"), accept=hd)), [])
            self.assertEqual(len(reads), 1 + 3 + 1 + 1)  # setup, every 2 attempted decodes, the run's end, the exit
            with Fakes():
                with self.assertRaises(replay.ReplayLimit):  # an iterator of skipped frames is still stopped
                    replay.replay_frames(iter(paths), lambda *a: self.fail("ran"), accept=hd, rss=no_rss,
                                         limits=replay.Limits(max_frames=3))

    def test_the_reported_peak_includes_the_high_water_mark(self):
        with Fakes():
            with replay.Replay(rss=lambda: 100, peak=lambda: 5000, limits=replay.Limits(max_rss_bytes=1000)) as s:
                list(s.run(frames(2), lambda *a: None))
                self.assertEqual(s.peak_rss, 5000)  # a spike between reads still shows; the guard acts on now

    def test_a_replay_over_more_frames_than_the_limit_is_refused_before_any_frame(self):
        with Fakes() as fakes:
            with self.assertRaises(replay.ReplayLimit) as caught:
                replay.replay_frames(frames(11), lambda *a: self.fail("ran a frame"),
                                     limits=replay.Limits(max_frames=10), rss=no_rss)
            self.assertIn("11 frames", str(caught.exception))
            calls = []
            with self.assertRaises(replay.ReplayLimit):  # an iterator has no length: stopped at the count
                replay.replay_frames(iter(frames(11)), lambda r, p, i: calls.append(p),
                                     limits=replay.Limits(max_frames=10), rss=no_rss)
        self.assertEqual(len(calls), 10)
        self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"] * 2)

    def test_the_wall_time_is_checked_after_each_callback(self):
        clock = iter([0.0, 0.5, 1.0, 1.5, 2.0, 7.0])  # start, after setup, then before and after each frame
        results, calls = [], []
        with Fakes() as fakes, patch.object(replay, "time", SimpleNamespace(monotonic=lambda: next(clock))):
            with self.assertRaises(replay.ReplayLimit) as caught:
                with replay.Replay(rss=no_rss, limits=replay.Limits(max_seconds=5)) as session:
                    for item in session.run(frames(5), lambda r, p, i: calls.append(p)):
                        results.append(item)
        self.assertEqual((len(calls), len(results)), (2, 1))  # the slow second frame ran, its result is not given
        self.assertIn("wall-time", str(caught.exception))
        self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])

    def test_the_wall_time_starts_before_setup(self):
        clock = iter([0.0, 9.0])  # setup took 9 s
        with Fakes() as fakes, patch.object(replay, "time", SimpleNamespace(monotonic=lambda: next(clock))):
            with self.assertRaises(replay.ReplayLimit):
                with replay.Replay(rss=no_rss, limits=replay.Limits(max_seconds=5)):
                    self.fail("entered")
        self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])

    def test_limits_must_be_finite_and_above_zero(self):
        for field, value in (("max_seconds", math.nan), ("max_seconds", math.inf), ("max_seconds", 0),
                             ("max_seconds", -1), ("max_rss_bytes", math.inf), ("max_rss_bytes", 0),
                             ("max_frames", 0), ("max_frames", 1.5), ("max_frames", True), ("rss_every", 0)):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError) as caught:
                    replay.Limits(**{field: value})
                self.assertIn(field, str(caught.exception))
        import argparse
        parser = argparse.ArgumentParser()
        replay.add_arguments(parser)
        for argv in (["--max-seconds", "nan"], ["--max-seconds", "inf"], ["--max-seconds", "0"],
                     ["--max-frames", "0"], ["--max-rss-mb", "-5"], ["--max-frames", "x"]):
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
                    parser.parse_args(argv)
                self.assertIn(argv[0], err.getvalue())

    def test_the_command_line_limits(self):
        import argparse
        parser = argparse.ArgumentParser()
        replay.add_arguments(parser)
        self.assertEqual(replay.limits_from(parser.parse_args([])), replay.Limits())
        self.assertEqual(replay.Limits().max_rss_bytes, 3 * replay.GB)
        limits = replay.limits_from(parser.parse_args(["--max-frames", "7", "--max-seconds", "30",
                                                       "--max-rss-mb", "512"]))
        self.assertEqual((limits.max_frames, limits.max_seconds, limits.max_rss_bytes), (7, 30, 512 * 2 ** 20))

    def test_current_rss_reads_this_process(self):
        rss = replay.current_rss()
        self.assertGreater(rss, 1 << 20)  # an interpreter is at least a few MB
        self.assertLess(rss, 64 * replay.GB)
        self.assertGreaterEqual(replay.process_peak_rss(), rss // 2)


class MeasurePassTests(unittest.TestCase):
    """move_frames' measurement pass (OpenCV only, no recognizer) under the same limits."""

    @staticmethod
    def build(folder, records):
        run = Path(folder, "phase-e-measure", "m2-test")
        run.mkdir(parents=True)
        lines = []
        for i in range(records):
            tag = f"t{i:02d}"
            images(run, [(72, 128), (72, 128)], [f"{tag}-a.png", f"{tag}-b.png"])
            lines.append(json.dumps({"kind": "gesture", "tag": tag, "ms": 1000, "angle": 270, "mag": 1.0,
                                     "before": {"x": 0, "y": 0, "head": 0}, "after": {"x": 0, "y": -2},
                                     "settled": {"x": 0, "y": -3}}))
        (run / "results.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return Path(folder)

    @staticmethod
    def reader(*_args):
        fix = SimpleNamespace(x=0.0, y=-2.5, score=0.8, reliable=True)
        return SimpleNamespace(fix=lambda frame, hint, search: fix)

    def test_the_measurement_pass_runs_under_the_replay_limits(self):
        import move_frames
        reads = []

        def rss():
            reads.append(1)
            return 1

        with tempfile.TemporaryDirectory() as folder:
            build = self.build(folder, 3)  # 6 frames read
            with patch("host.android.minimap.MinimapReader", self.reader), patch.object(replay, "current_rss", rss):
                with self.assertRaises(replay.ReplayLimit):
                    move_frames.measure(build, replay.Limits(max_frames=4))
                reads.clear()
                report = move_frames.measure(build, replay.Limits(rss_every=2))
                self.assertEqual(report["fixes"]["n"], 6)
                self.assertEqual(len(reads), 1 + 3 + 1)  # after the reader's setup, every 2 frames read, the end
                with patch.object(sys, "argv", ["move_frames.py", "--build", str(build), "--no-screens",
                                                "--max-frames", "1"]), \
                        contextlib.redirect_stdout(io.StringIO()) as out, \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(move_frames.main(), 2)  # --no-screens is still under the limits
        self.assertEqual(out.getvalue(), "")
        self.assertIn("1-frame limit", err.getvalue())

    def test_an_over_budget_process_stops_after_the_reader_setup_before_any_fix(self):
        import move_frames
        fixes = []
        reader = SimpleNamespace(fix=lambda *a: fixes.append(1))
        with tempfile.TemporaryDirectory() as folder:
            build = self.build(folder, 3)
            with patch("host.android.minimap.MinimapReader", lambda *a: reader), \
                    patch.object(replay, "current_rss", lambda: 4 * replay.GB):
                with self.assertRaises(replay.ReplayLimit) as caught:
                    move_frames.measure(build)
        self.assertEqual(fixes, [])
        self.assertIn("after 0 frames", str(caught.exception))

    def test_the_log_is_streamed_under_the_guard(self):
        import move_frames
        with tempfile.TemporaryDirectory() as folder:
            build = self.build(folder, 3)
            log = build / "phase-e-measure" / "m2-test" / "results.jsonl"
            with log.open("a", encoding="utf-8") as handle:
                handle.write("not json: never parsed when the limit stops the pass first\n")
            with patch("host.android.minimap.MinimapReader", self.reader), patch.object(replay, "current_rss", no_rss):
                with self.assertRaises(replay.ReplayLimit):  # the 5th frame, before the bad line is read
                    move_frames.measure(build, replay.Limits(max_frames=4))
                with self.assertRaises(json.JSONDecodeError):  # without the limit the pass does reach it
                    move_frames.measure(build)


class GuardCoverageTests(unittest.TestCase):
    """The limits also hold for a caller that stops early, catches errors per frame or mixes iterators."""

    def test_a_caller_that_breaks_early_is_checked_on_exit(self):
        reads = iter([100, 5000])  # after setup, then on the successful exit
        with Fakes() as fakes:
            with self.assertRaises(replay.ReplayLimit) as caught:
                with replay.Replay(rss=lambda: next(reads), limits=replay.Limits(max_rss_bytes=3000)) as session:
                    for _ in session.run(frames(10), lambda *a: "found"):
                        break  # the first match is enough
        self.assertIn("after 1 frames", str(caught.exception))
        self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])  # freed after the check

    def test_failed_attempts_count_toward_the_limits(self):
        def fails(recognizer, path, image):
            raise ValueError("ocr post-processing failed")

        def unreadable(path):
            raise probe.ProbeError("unreadable")

        def bad_accept(image):
            raise ValueError("accept failed")

        for case in ("callback", "decode", "accept"):
            with self.subTest(case=case):
                reads, outcomes = [], []
                with Fakes(), contextlib.ExitStack() as stack:
                    if case == "decode":
                        stack.enter_context(patch.object(probe, "read_frame", unreadable))
                    with replay.Replay(rss=lambda: reads.append(1) or 1,
                                       limits=replay.Limits(max_frames=1, rss_every=1)) as session:
                        for path in frames(3):  # one session, an error caught per frame
                            try:
                                list(session.run([path], fails if case == "callback" else lambda *a: None,
                                                 accept=bad_accept if case == "accept" else None))
                            except replay.ReplayLimit:
                                outcomes.append("limit")
                            except (ValueError, probe.ProbeError):
                                outcomes.append("error")
                        self.assertEqual(session.guard.attempts, 1)
                self.assertEqual(outcomes, ["error", "limit", "limit"])
                self.assertEqual(len(reads), 1 + 1 + 1)  # setup, the failed attempt, the exit

    def test_a_run_inside_a_callback_is_refused(self):
        with Fakes() as fakes:
            with replay.Replay(rss=no_rss) as session:
                def nested(recognizer, path, image):
                    list(session.run(frames(1), lambda *a: None))  # would show another frame mid-judgement

                with self.assertRaises(RuntimeError) as caught:
                    list(session.run(frames(2), nested))
                self.assertIn("in progress", str(caught.exception))
                self.assertEqual(fakes.controllers[0].shown, ["pixels of frame-000.png"])  # never frame B
                self.assertEqual([p for p, _ in session.run(frames(1), lambda *a: None)], frames(1))  # freed again

    def test_a_second_run_while_one_is_suspended_is_refused(self):
        with Fakes():
            with replay.Replay(rss=no_rss) as session:
                first = session.run(frames(3), lambda *a: None)
                next(first)
                with self.assertRaises(RuntimeError):
                    next(session.run(frames(1), lambda *a: None))
                first.close()  # closing the first iterator ends its run
                self.assertEqual(len(list(session.run(frames(1), lambda *a: None))), 1)

    def test_an_iterator_is_refused_after_its_block_ends(self):
        with Fakes():
            session = replay.Replay(rss=no_rss, limits=replay.Limits(max_frames=2))
            with session:
                old = session.run(iter(frames(5)), lambda *a: None)
                next(old)
                unstarted = session.run(iter(frames(5)), lambda *a: None)
            with self.assertRaises(StopIteration):  # closed with its block
                next(old)
            with self.assertRaises(StopIteration):
                next(unstarted)
            with session:
                old2 = session.run(iter(frames(5)), lambda *a: None)
                next(old2)
                old2_guard = session.guard
            with session:  # reopened: the old iterator may not charge or use the new block
                with self.assertRaises(StopIteration):
                    next(old2)
                self.assertEqual(session.guard.attempts, 0)
                self.assertIsNot(session.guard, old2_guard)
                self.assertEqual(len(list(session.run(frames(2), lambda *a: None))), 2)


    def test_a_memory_breach_sticks_even_when_caught_and_memory_drops(self):
        reads = iter([100, 5000] + [100] * 10)  # after setup, frame 1 over the limit, then back under it
        decoded, ran = [], []

        def read(path):
            decoded.append(path.name)
            return f"pixels of {path.name}"

        with Fakes() as fakes, patch.object(probe, "read_frame", read):
            with self.assertRaises(replay.ReplayLimit) as at_exit:  # the block's end is refused as well
                with replay.Replay(rss=lambda: next(reads),
                                   limits=replay.Limits(rss_every=1, max_rss_bytes=1000)) as session:
                    try:
                        list(session.run(frames(3), lambda r, p, i: ran.append(p.name)))
                    except probe.ProbeError:  # a scratch script skipping "bad frames" catches the limit too
                        pass
                    for retry in (lambda: list(session.run(frames(1), lambda r, p, i: ran.append(p.name))),
                                  lambda: session.guard.before_decode()):
                        with self.assertRaises(replay.ReplayLimit) as caught:
                            retry()
                        self.assertIn("already hit", str(caught.exception))
        self.assertIn("already hit", str(at_exit.exception))
        self.assertEqual((decoded, ran), (["frame-000.png"], ["frame-000.png"]))  # nothing decoded after the breach
        self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])

    def test_an_explicit_close_after_a_break_is_checked_before_freeing(self):
        reads = iter([100, 5000])  # after setup, then at the close
        freed_at_check = []
        with Fakes() as fakes:
            def rss():
                freed_at_check.append(list(fakes.events))
                return next(reads)

            with self.assertRaises(replay.ReplayLimit):
                with replay.Replay(rss=rss, limits=replay.Limits(max_rss_bytes=3000)) as session:
                    try:
                        for _ in session.run(frames(10), lambda *a: "found"):
                            break
                    finally:
                        session.close()  # checks before freeing, and frees all even though the check raises
        self.assertEqual(freed_at_check, [[], []])  # nothing was freed when either read happened
        self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])  # once, not again at the exit

    def test_kept_iterators_recognizers_and_controllers_do_not_keep_the_pixels(self):
        class Pixels:
            pass

        made = []

        def read(path):
            pixels = Pixels()
            made.append(weakref.ref(pixels))
            return pixels

        class Controller:  # keeps no history, unlike FakeController
            def __init__(self):
                self.image = None

            def show(self, image):
                self.image = image

        with Fakes(), patch.object(probe, "frame_controller", Controller), patch.object(probe, "read_frame", read):
            session = replay.Replay(rss=no_rss)
            with session:
                iterator = session.run(frames(3), lambda r, p, image: r.on_one_frame(lambda captured: None))
                next(iterator)  # suspended with its frame's locals
                recognizer, controller = session.recognizer, session.controller
                self.assertIsNotNone(made[0]())
            gc.collect()
            self.assertEqual([ref() for ref in made], [None])  # freed although all three are still referenced
            self.assertIsNone(controller.image)
            self.assertIs(recognizer.controller, controller)
            with self.assertRaises(StopIteration):
                next(iterator)


class DiscoveryTests(unittest.TestCase):
    """The tools list their frames inside the replay's limits, never past the frame limit."""

    def test_collect_is_sorted_bounded_and_clocked(self):
        guard = replay.Guard(replay.Limits(max_frames=3)).start()
        self.assertEqual(guard.collect(iter([Path("c"), Path("a"), Path("b")])), [Path("a"), Path("b"), Path("c")])
        pulled = []

        def endless():
            while True:
                pulled.append(1)
                yield Path(f"{len(pulled):09d}.png")

        with self.assertRaises(replay.ReplayLimit):
            guard.collect(endless())
        self.assertEqual(len(pulled), 4)  # stopped one past the limit: a large tree is never listed whole
        self.assertEqual(len(guard.collect(iter([Path("x")] * 10), limit=12)), 10)
        clock = iter([0.0, 1.0, 9.0])
        with patch.object(replay, "time", SimpleNamespace(monotonic=lambda: next(clock))):
            slow = replay.Guard(replay.Limits(max_seconds=5)).start()
            with self.assertRaises(replay.ReplayLimit):
                slow.collect(iter([Path("a"), Path("b")]))

    def test_move_frames_lists_its_screens_inside_the_replay(self):
        import move_frames
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder, "phase-e-measure", "m2-test")
            run.mkdir(parents=True)
            images(run, [(72, 128)] * 5, [f"t{i}-hud.png" for i in range(5)])
            for all_frames in (False, True):
                with self.subTest(all_frames=all_frames), Fakes() as fakes:
                    with self.assertRaises(replay.ReplayLimit) as caught:
                        move_frames.screens(Path(folder), all_frames, replay.Limits(max_frames=3))
                    self.assertIn("more than 3 frames", str(caught.exception))
                    self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])  # the block was open
                    self.assertEqual(fakes.controllers[0].shown, [None])  # no frame shown

    def test_teleport_frames_loads_the_map_and_lists_frames_inside_the_replay(self):
        import teleport_frames
        order = []
        with tempfile.TemporaryDirectory() as folder:
            images(Path(folder), [], [])
            frames_dir = Path(folder, "map-survey", "frames")
            frames_dir.mkdir(parents=True)
            images(frames_dir, [(72, 128)] * 2, ["a.png", "b.png"])
            walk = Path(folder, "survey", "walk")
            walk.mkdir(parents=True)
            images(walk, [(72, 128)], ["w.png"])
            with Fakes() as fakes, patch("host.android.teleport.WorldMap", lambda **k: order.append("world")), \
                    patch.object(sys, "argv", ["teleport_frames.py", "--build", folder, "--max-frames", "2"]), \
                    contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(teleport_frames.main(), 2)
        self.assertEqual(order, ["world"])
        self.assertEqual(out.getvalue(), "")
        self.assertIn("more than 0 frames", err.getvalue())  # 2 map frames left no room for the walk frame
        self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])

    def test_the_measurement_pass_checks_after_building_its_report(self):
        import move_frames
        order = []
        median = move_frames.statistics.median
        with tempfile.TemporaryDirectory() as folder:
            build = MeasurePassTests.build(folder, 2)
            with patch("host.android.minimap.MinimapReader", MeasurePassTests.reader), \
                    patch.object(replay, "current_rss", no_rss), \
                    patch.object(replay.Guard, "finish", lambda self: order.append("finish")), \
                    patch.object(move_frames.statistics, "median", lambda *a: order.append("report") or median(*a)):
                move_frames.measure(build)
        self.assertIn("report", order)
        self.assertEqual(order[-1], "finish")


class ReleaseTests(unittest.TestCase):
    def test_everything_is_released_when_the_callback_raises(self):
        def callback(recognizer, path, image):
            if path.name == "frame-002.png":
                raise ValueError("boom")

        with Fakes() as fakes:
            with self.assertRaises(ValueError):
                replay.replay_frames(frames(5), callback, rss=no_rss)
        self.assertEqual(fakes.recognizers[0].released, 1)
        self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])

    def test_an_unreadable_frame_raises_and_releases(self):
        def unreadable(path):
            raise probe.ProbeError(f"could not read {path}")

        with Fakes() as fakes, patch.object(probe, "read_frame", unreadable):
            with self.assertRaises(probe.ProbeError):
                replay.replay_frames(frames(2), lambda *a: None, rss=no_rss)
        self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])

    def test_a_bind_failure_frees_the_tasker_the_resource_and_the_controller(self):
        log = []
        with fake_framework(log, bind_ok=False):
            with self.assertRaises(probe.ProbeError) as caught:
                with replay.Replay(rss=no_rss):
                    self.fail("entered")
            destroyed = dict(probe.NATIVE_DESTROYED)
        self.assertIn("bind", str(caught.exception))
        self.assertEqual(log, ["create FrameController", "create Resource", "create Tasker",
                               "MaaTaskerDestroy", "MaaResourceDestroy", "MaaControllerDestroy"])
        self.assertEqual(destroyed, {"MaaTaskerDestroy": 1, "MaaResourceDestroy": 1, "MaaControllerDestroy": 1})

    def test_a_resource_that_does_not_load_is_freed_inside_the_constructor(self):
        log = []
        with fake_framework(log, bundle_ok=False):
            with self.assertRaises(probe.ProbeError):
                probe.Recognizer(SimpleNamespace(), android_layer=True)
        self.assertEqual(log, ["create Resource", "MaaResourceDestroy"])  # no tasker was made

    def test_a_controller_that_does_not_connect_is_freed_inside_the_factory(self):
        log = []
        with fake_framework(log, connect_ok=False):
            with self.assertRaises(probe.ProbeError):
                probe.frame_controller()
            with patch.object(probe, "read_frame", side_effect=probe.ProbeError("unreadable")):
                with self.assertRaises(probe.ProbeError):
                    probe.frame_controller(Path("missing.png"))
        self.assertEqual(log, ["create FrameController", "MaaControllerDestroy"])  # unreadable frame: nothing made

    def test_destroy_runs_once_and_only_for_an_owned_live_handle(self):
        log = []
        with fake_framework(log) as kinds:
            tasker, borrowed = kinds.Tasker(), kinds.Tasker()
            borrowed._own = False
            handle = borrowed._handle
            self.assertTrue(probe.destroy_native(tasker))
            self.assertIsNone(tasker._handle)
            self.assertFalse(probe.destroy_native(tasker))  # already freed: no second call
            self.assertFalse(probe.destroy_native(borrowed))
            self.assertIs(borrowed._handle, handle)  # not freed, so not cleared
            self.assertEqual(dict(probe.NATIVE_DESTROYED), {"MaaTaskerDestroy": 1})
            with self.assertRaises(TypeError):
                probe.destroy_native(object())
        self.assertEqual([entry for entry in log if entry.startswith("Maa")], ["MaaTaskerDestroy"])

    def test_run_outside_the_block_is_refused(self):
        with Fakes():
            session = replay.Replay(rss=no_rss)
            with session:
                pass
            with self.assertRaises(RuntimeError):
                list(session.run(frames(1), lambda *a: None))


FRAMEWORK_CHECK = r"""
import json, sys
from pathlib import Path
import cv2
import numpy as np

repo, folder = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(repo / "agent"))
sys.path.insert(0, str(repo / "scripts" / "android"))
import probe, replay
from host.android import actions
from maa.tasker import Tasker

# a real half-built recognizer: the bind fails after the resource and the tasker exist
bind = Tasker.bind
Tasker.bind = lambda self, resource, controller: bind(self, resource, controller) and False
controller = probe.frame_controller()
try:
    probe.Recognizer(controller)
    partial_error = None
except probe.ProbeError as error:
    partial_error = str(error)
Tasker.bind = bind
partial = dict(probe.NATIVE_DESTROYED)
probe.destroy_native(controller)
probe.NATIVE_DESTROYED.clear()

built = []
original = probe.Recognizer.__init__
def counting(self, *args, **kwargs):
    built.append(1)
    original(self, *args, **kwargs)
probe.Recognizer.__init__ = counting

duck = cv2.imread(str(repo / "resource_android/image/android/menu_duck.png"))
row = cv2.imread(str(repo / "scripts/fixtures/android/hud_label_row.png"))
title = cv2.imread(str(repo / "scripts/fixtures/android/page_title.png"))
paths = {}
for name, marks in (("hud", [(duck, 30, 18), (row, 600, 50)]), ("page", [(title, 0, 10)])):
    frame = np.full((720, 1280, 3), 90, np.uint8)
    for image, x, y in marks:
        frame[y:y + image.shape[0], x:x + image.shape[1]] = image
    paths[name] = folder / (name + ".png")
    cv2.imwrite(str(paths[name]), frame)

def callback(recognizer, path, image):
    kind = recognizer.on_one_frame(lambda recognize: actions.read_screen(recognize).kind)
    return [kind, bool(np.array_equal(recognizer.last_frame, image))]

with replay.Replay() as session:
    recognizer, controller = session.recognizer, session.controller
    tasker, resource = recognizer.tasker, recognizer.resource
    results = [[path.name] + result for path, result in session.run(
        [paths["hud"], paths["page"], paths["hud"]], callback)]
peak = session.peak_rss
print(json.dumps({"results": results, "built": len(built), "peak_rss": peak, "destroyed": probe.NATIVE_DESTROYED,
                  "partial": partial, "partial_error": partial_error,
                  "freed": [tasker._handle is None, resource._handle is None, controller._handle is None,
                            recognizer.tasker is None, session.recognizer is None]}))
"""


@unittest.skipUnless((ROOT / "maafw").exists(), "needs the MaaFramework runtime in ./maafw")
class FrameworkTests(unittest.TestCase):
    """The real framework in a fresh interpreter: a recognizer whose bind fails frees its tasker and resource; then
    three composed frames (hud, page, hud) on one recognizer, each capture the frame just shown, and one native
    destroy each for the tasker, the resource and the controller when the block ends."""

    def test_one_recognizer_sees_each_frame_and_is_freed(self):
        if not (ROOT / "resource_android").is_dir():
            self.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
            return
        env = {**os.environ, "MAAFW_BINARY_PATH": str(ROOT / "maafw")}
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", "-c", FRAMEWORK_CHECK, str(ROOT),
                                     folder], capture_output=True, text=True, encoding="utf-8", timeout=300, env=env,
                                    cwd=folder)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(outcome["results"], [["hud.png", "hud", True], ["page.png", "page", True],
                                              ["hud.png", "hud", True]])
        self.assertEqual(outcome["built"], 1)
        self.assertEqual(outcome["freed"], [True] * 5)
        self.assertEqual(outcome["destroyed"],
                         {"MaaTaskerDestroy": 1, "MaaResourceDestroy": 1, "MaaControllerDestroy": 1})
        self.assertIn("bind", outcome["partial_error"])
        self.assertEqual(outcome["partial"], {"MaaTaskerDestroy": 1, "MaaResourceDestroy": 1})
        self.assertLess(outcome["peak_rss"], replay.Limits().max_rss_bytes)


if __name__ == "__main__":
    unittest.main()
