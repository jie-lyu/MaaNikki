"""The one supported way to run recognition over saved frames offline (dev tooling; no device, no input).

    import replay
    with replay.Replay(limits=replay.Limits(max_frames=300)) as session:
        for path, result in session.run(paths, callback):     # callback(recognizer, frame_path, image) -> result
            ...
    results = replay.replay_frames(paths, callback)            # the same, collected: [(path, result), ...]

A Replay owns exactly one probe.Recognizer (one maafw Tasker and Resource, both resource layers by default) and one
frame controller for the whole replay. Each frame is read from disk and shown to that controller
(probe.frame_controller's show), so a capture inside the callback is that frame; the callback usually calls
``recognizer.on_one_frame(function)`` or ``recognizer.run_many(nodes)``. The tasker, the resource and the controller
are freed explicitly when the block ends, also on an exception or an abort.

Limits, checked in the loop (ReplayLimit is raised; the block still frees everything):
  - frames: a replay over more than max_frames frames is refused before it starts (and stopped at that count when
    the paths are an iterator);
  - wall time: max_seconds from the start of the replay, checked before each frame;
  - memory: the process's own current resident set size (``ps -o rss=`` on macOS, /proc/self/status on Linux,
    GetProcessMemoryInfo on Windows), read after the recognizer is built and then every rss_every frames; above
    max_rss_bytes (default 3 GB) the replay stops. A healthy replay stays well under 1 GB.

Scratch scripts over saved frames must use this module: one recognizer per frame costs about 100 MB each once OCR has
run, and a script that kept them exhausted 64 GB on the maintainer's Mac (docs/android-support-plan.md). Never run two
replays at once or one in the background. Tools take the limits from the command line with add_arguments/limits_from.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe  # noqa: E402

GB = 1024 ** 3


class ReplayLimit(probe.ProbeError):
    """A replay stopped at one of its limits (frames, wall time, memory)."""


@dataclasses.dataclass(frozen=True)
class Limits:
    max_frames: int = 1000
    max_seconds: float = 600.0
    max_rss_bytes: int = 3 * GB
    rss_every: int = 5  # frames between two memory reads


def current_rss() -> int:
    """This process's resident set size now, in bytes (not the peak)."""
    if sys.platform.startswith("linux"):
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
        raise OSError("no VmRSS in /proc/self/status")
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + \
                [(name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return counters.WorkingSetSize
    output = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True,
                            timeout=10, check=True).stdout
    return int(output.strip()) * 1024  # ps reports KiB


def add_arguments(parser) -> None:
    """--max-frames, --max-seconds and --max-rss-mb for a tool that replays frames."""
    defaults = Limits()
    parser.add_argument("--max-frames", type=int, default=defaults.max_frames,
                        help=f"refuse a replay over more frames (default {defaults.max_frames})")
    parser.add_argument("--max-seconds", type=float, default=defaults.max_seconds,
                        help=f"stop the replay after this wall time (default {defaults.max_seconds:g})")
    parser.add_argument("--max-rss-mb", type=int, default=defaults.max_rss_bytes // 2 ** 20,
                        help=f"stop when this process uses more memory (default {defaults.max_rss_bytes // 2 ** 20})")


def limits_from(args) -> Limits:
    return Limits(max_frames=args.max_frames, max_seconds=args.max_seconds, max_rss_bytes=args.max_rss_mb * 2 ** 20)


class Replay:
    """One recognizer and one frame controller for a whole replay; see the module docstring."""

    def __init__(self, *, android_layer: bool = True, limits: Limits = Limits(),
                 rss: Callable[[], int] = current_rss):
        self.android_layer = android_layer
        self.limits = limits
        self._rss = rss
        self.controller = None
        self.recognizer = None
        self.frames = 0          # frames shown so far
        self.peak_rss = 0        # the highest RSS read, bytes
        self._started = None

    def __enter__(self) -> "Replay":
        try:
            self.controller = probe.frame_controller()
            self.recognizer = probe.Recognizer(self.controller, android_layer=self.android_layer)
            self._started = time.monotonic()
            self._check_memory()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        """Free the tasker, the resource and the controller (in that order). Safe to call twice."""
        recognizer, controller = self.recognizer, self.controller
        self.recognizer = self.controller = None
        try:
            if recognizer is not None:
                recognizer.release()
        finally:
            if controller is not None:
                probe.destroy_native(controller)

    def _check_memory(self) -> None:
        rss = self._rss()
        self.peak_rss = max(self.peak_rss, rss)
        if rss > self.limits.max_rss_bytes:
            raise ReplayLimit(f"replay stopped after {self.frames} frames: this process uses {rss / GB:.2f} GB, "
                              f"above the {self.limits.max_rss_bytes / GB:.2f} GB limit (frames are not being freed?)")

    def run(self, paths: Iterable[Path], callback: Callable[[Any, Path, Any], Any], *,
            accept: Callable[[Any], bool] | None = None) -> Iterator[tuple[Path, Any]]:
        """Yield (path, callback(recognizer, path, image)) for each frame, in order, on the one recognizer.

        Without `accept`, a frame that cannot be read or is not 16:9 raises probe.ProbeError. With it, each frame is
        read with cv2.imread and skipped (no callback, not counted) unless accept(image) is true; image may be None.
        """
        if self.recognizer is None:
            raise RuntimeError("Replay.run outside its with block")
        limits = self.limits
        if hasattr(paths, "__len__") and len(paths) > limits.max_frames:
            raise ReplayLimit(f"replay refused: {len(paths)} frames, above the {limits.max_frames}-frame limit "
                              f"(raise it explicitly, e.g. --max-frames, if this is intended)")
        for path in paths:
            if time.monotonic() - self._started > limits.max_seconds:
                raise ReplayLimit(f"replay stopped after {self.frames} frames: {limits.max_seconds:g} s wall-time "
                                  f"limit reached")
            if accept is None:
                image = probe.read_frame(path)
            else:
                import cv2
                image = cv2.imread(str(path))
                if not accept(image):
                    continue
            if self.frames >= limits.max_frames:
                raise ReplayLimit(f"replay stopped at the {limits.max_frames}-frame limit")
            self.controller.show(image)
            result = callback(self.recognizer, path, image)
            self.frames += 1
            if self.frames % limits.rss_every == 0:
                self._check_memory()
            yield path, result


def replay_frames(paths: Iterable[Path], callback: Callable[[Any, Path, Any], Any], *, android_layer: bool = True,
                  limits: Limits = Limits(), accept: Callable[[Any], bool] | None = None,
                  rss: Callable[[], int] = current_rss) -> list[tuple[Path, Any]]:
    """Replay.run collected into a list, everything freed before it returns."""
    with Replay(android_layer=android_layer, limits=limits, rss=rss) as session:
        return list(session.run(paths, callback, accept=accept))
