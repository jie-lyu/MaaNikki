"""Offline tests of scripts/android/replay.py, the one supported way to replay saved frames: one recognizer for a whole
replay, the memory, frame and wall-time limits, and everything freed on the way out (also on an exception)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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


def frames(n):
    return [Path(f"frame-{i:03d}.png") for i in range(n)]


class OneRecognizerTests(unittest.TestCase):
    def test_replaying_many_frames_builds_one_recognizer_and_shows_each_frame_to_it(self):
        seen = []

        def callback(recognizer, path, image):
            seen.append((id(recognizer), path, image))
            return recognizer.on_one_frame(lambda captured: f"judged {captured}")

        with Fakes() as fakes:
            results = replay.replay_frames(frames(40), callback, rss=lambda: 1)
        self.assertEqual((FakeRecognizer.built, FakeController.built), (1, 1))
        self.assertEqual(len({identity for identity, _, _ in seen}), 1)
        self.assertEqual([path for _, path, _ in seen], frames(40))
        self.assertEqual(fakes.controllers[0].shown, [f"pixels of frame-{i:03d}.png" for i in range(40)])
        self.assertEqual(results, [(p, f"judged pixels of {p.name}") for p in frames(40)])  # the frame shown, in turn
        self.assertTrue(fakes.recognizers[0].android_layer)
        self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])  # tasker side first

    def test_accept_skips_frames_without_a_callback_or_a_count(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as folder:
            import cv2
            paths = []
            for i, size in enumerate(((720, 1280), (360, 640), (720, 1280))):
                paths.append(Path(folder, f"{i}.png"))
                cv2.imwrite(str(paths[-1]), np.full((*size, 3), i, np.uint8))
            paths.append(Path(folder, "missing.png"))
            with Fakes():
                with replay.Replay(rss=lambda: 1) as session:
                    done = [path.name for path, _ in session.run(
                        paths, lambda r, path, image: None,
                        accept=lambda image: image is not None and image.shape[:2] == (720, 1280))]
                    self.assertEqual(session.frames, 2)
        self.assertEqual(done, ["0.png", "2.png"])


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

    def test_a_replay_over_more_frames_than_the_limit_is_refused_before_any_frame(self):
        with Fakes() as fakes:
            with self.assertRaises(replay.ReplayLimit) as caught:
                replay.replay_frames(frames(11), lambda *a: self.fail("ran a frame"),
                                     limits=replay.Limits(max_frames=10), rss=lambda: 1)
            self.assertIn("11 frames", str(caught.exception))
            calls = []
            with self.assertRaises(replay.ReplayLimit):  # an iterator has no length: stopped at the count
                replay.replay_frames(iter(frames(11)), lambda r, p, i: calls.append(p),
                                     limits=replay.Limits(max_frames=10), rss=lambda: 1)
        self.assertEqual(len(calls), 10)
        self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"] * 2)

    def test_the_wall_time_limit_stops_the_replay(self):
        clock = iter([0.0, 1.0, 2.0, 7.0, 8.0])  # the start, then one read before each frame
        calls = []
        with Fakes(), patch.object(replay, "time", SimpleNamespace(monotonic=lambda: next(clock))):
            with self.assertRaises(replay.ReplayLimit) as caught:
                replay.replay_frames(frames(5), lambda r, p, i: calls.append(p),
                                     limits=replay.Limits(max_seconds=5), rss=lambda: 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("wall-time", str(caught.exception))

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


class ReleaseTests(unittest.TestCase):
    def test_everything_is_released_when_the_callback_raises(self):
        def callback(recognizer, path, image):
            if path.name == "frame-002.png":
                raise ValueError("boom")

        with Fakes() as fakes:
            with self.assertRaises(ValueError):
                replay.replay_frames(frames(5), callback, rss=lambda: 1)
        self.assertEqual(fakes.recognizers[0].released, 1)
        self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])

    def test_an_unreadable_frame_raises_and_releases(self):
        def unreadable(path):
            raise probe.ProbeError(f"could not read {path}")

        with Fakes() as fakes, patch.object(probe, "read_frame", unreadable):
            with self.assertRaises(probe.ProbeError):
                replay.replay_frames(frames(2), lambda *a: None, rss=lambda: 1)
        self.assertEqual(fakes.events, ["release recognizer", "destroy Controller"])

    def test_the_controller_is_freed_when_the_recognizer_cannot_be_built(self):
        with Fakes() as fakes, patch.object(probe, "Recognizer", side_effect=probe.ProbeError("no bundle")):
            with self.assertRaises(probe.ProbeError):
                with replay.Replay(rss=lambda: 1):
                    self.fail("entered")
        self.assertEqual(fakes.events, ["destroy Controller"])

    def test_run_outside_the_block_is_refused(self):
        with Fakes():
            session = replay.Replay(rss=lambda: 1)
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
print(json.dumps({"results": results, "built": len(built), "peak_rss": peak,
                  "freed": [tasker._handle is None, resource._handle is None, controller._handle is None,
                            recognizer.tasker is None, session.recognizer is None]}))
"""


@unittest.skipUnless((ROOT / "maafw").exists(), "needs the MaaFramework runtime in ./maafw")
class FrameworkTests(unittest.TestCase):
    """The real framework on three composed frames (hud, page, hud) in a fresh interpreter: one recognizer, each
    capture is the frame just shown, and the tasker, resource and controller are freed when the block ends."""

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
        self.assertLess(outcome["peak_rss"], replay.Limits().max_rss_bytes)


if __name__ == "__main__":
    unittest.main()
