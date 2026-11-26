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
are freed explicitly when the block ends, also on an exception or an abort (and inside their constructors when setup
fails half-way). An open Replay cannot be entered again.

Limits (ReplayLimit is raised; the block still frees everything). A pass over saved frames that builds no recognizer
(OpenCV only, e.g. move_frames' measurement pass) applies the same limits with a Guard:
  - frames: every attempted decode counts, skipped frames included; a replay over more than max_frames paths is
    refused before it starts, an iterator is stopped at that count;
  - wall time: max_seconds from before the recognizer is built, checked before each decode and after each callback;
  - memory: the process's own current resident set size (``ps -o rss=`` on macOS, /proc/self/status on Linux,
    GetProcessMemoryInfo on Windows), read after setup, every rss_every attempted decodes and once more before a
    replay ends successfully; above max_rss_bytes (default 3 GB) it stops. peak_rss reports the higher of those
    reads and the process's high-water mark (getrusage / PeakWorkingSetSize), so a spike between reads still shows.
All limits must be finite and above zero.

Scratch scripts over saved frames must use this module: one recognizer per frame costs about 100 MB each once OCR has
run, and a script that kept them exhausted 64 GB on the maintainer's Mac (docs/android-support-plan.md). Never run two
replays at once or one in the background. Tools take the limits from the command line with add_arguments/limits_from.
"""
from __future__ import annotations

import argparse
import dataclasses
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import weakref
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
    rss_every: int = 5  # attempted decodes between two memory reads

    def __post_init__(self):
        for name in ("max_frames", "rss_every"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a whole number above 0, not {value!r}")
        for name in ("max_seconds", "max_rss_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or value <= 0:
                raise ValueError(f"{name} must be a finite number above 0, not {value!r}")


def _windows_memory():
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
    return counters


def current_rss() -> int:
    """This process's resident set size now, in bytes (not the peak)."""
    if sys.platform.startswith("linux"):
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
        raise OSError("no VmRSS in /proc/self/status")
    if sys.platform == "win32":
        return _windows_memory().WorkingSetSize
    output = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True,
                            timeout=10, check=True).stdout
    return int(output.strip()) * 1024  # ps reports KiB


def process_peak_rss() -> int:
    """This process's resident-set high-water mark since it started, in bytes."""
    if sys.platform == "win32":
        return _windows_memory().PeakWorkingSetSize
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # bytes on macOS, KiB elsewhere


def _positive(kind):
    def parse(text):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
        if not math.isfinite(value) or value <= 0:
            raise argparse.ArgumentTypeError(f"must be finite and above 0, not {text!r}")
        return value
    return parse


def add_arguments(parser) -> None:
    """--max-frames, --max-seconds and --max-rss-mb for a tool that replays frames."""
    defaults = Limits()
    parser.add_argument("--max-frames", type=_positive(int), default=defaults.max_frames,
                        help=f"refuse a pass over more frames (default {defaults.max_frames})")
    parser.add_argument("--max-seconds", type=_positive(float), default=defaults.max_seconds,
                        help=f"stop a pass after this wall time (default {defaults.max_seconds:g})")
    parser.add_argument("--max-rss-mb", type=_positive(int), default=defaults.max_rss_bytes // 2 ** 20,
                        help=f"stop when this process uses more memory (default {defaults.max_rss_bytes // 2 ** 20})")


def limits_from(args) -> Limits:
    return Limits(max_frames=args.max_frames, max_seconds=args.max_seconds, max_rss_bytes=args.max_rss_mb * 2 ** 20)


class Guard:
    """The frame, wall-time and memory limits of one pass over saved frames, without a recognizer.

        guard = Guard(limits).start(len(paths))
        ...                                       # setup, then guard.check_memory()
        for path in paths:
            guard.before_decode()
            image = cv2.imread(str(path))
            ...                                   # the work on that frame
            guard.after_frame()                   # also when that work raised
        guard.finish()
    """

    def __init__(self, limits: Limits = Limits(), *, rss: Callable[[], int] | None = None,
                 peak: Callable[[], int] | None = None):
        self.limits = limits
        self._rss = rss if rss is not None else current_rss
        self._peak = peak if peak is not None else process_peak_rss
        self.attempts = 0        # decodes attempted, skipped frames included
        self.peak_rss = 0        # bytes: the highest RSS read or high-water mark seen
        self.breached = None     # the memory limit's message once it was hit: everything after is refused
        self._started = None

    def start(self, count: int | None = None) -> "Guard":
        if count is not None and count > self.limits.max_frames:
            raise ReplayLimit(f"refused: {count} frames, above the {self.limits.max_frames}-frame limit "
                              f"(raise it explicitly, e.g. --max-frames, if this is intended)")
        self._started = time.monotonic()
        return self

    def check_time(self) -> None:
        if time.monotonic() - self._started > self.limits.max_seconds:
            raise ReplayLimit(f"stopped after {self.attempts} frames: {self.limits.max_seconds:g} s wall-time "
                              f"limit reached")

    def check_breach(self) -> None:
        """A memory breach sticks: caught or not, and even if memory has dropped since, nothing more runs."""
        if self.breached:
            raise ReplayLimit(f"refused: the memory limit was already hit ({self.breached})")

    def check_memory(self) -> None:
        self.check_breach()
        rss = self._rss()
        self.peak_rss = max(self.peak_rss, rss, self._peak())
        if rss > self.limits.max_rss_bytes:
            self.breached = (f"this process used {rss / GB:.2f} GB after {self.attempts} frames, above the "
                             f"{self.limits.max_rss_bytes / GB:.2f} GB limit")
            raise ReplayLimit(f"stopped after {self.attempts} frames: this process uses {rss / GB:.2f} GB, "
                              f"above the {self.limits.max_rss_bytes / GB:.2f} GB limit (frames are not being freed?)")

    def collect(self, paths: Iterable[Path], limit: int | None = None) -> list[Path]:
        """Discovered paths, sorted, refused as soon as there are more than `limit` (default max_frames) so a large
        tree is never listed whole; the clock is checked while walking."""
        limit = self.limits.max_frames if limit is None else limit
        found = []
        for path in paths:
            self.check_time()
            found.append(path)
            if len(found) > limit:
                raise ReplayLimit(f"refused: more than {limit} frames found, above the frame limit "
                                  f"(raise it explicitly, e.g. --max-frames, if this is intended)")
        return sorted(found)

    def before_decode(self) -> None:
        """Reserve one attempt (it counts even if the decode or the work on the frame then fails)."""
        self.check_breach()
        self.check_time()
        if self.attempts >= self.limits.max_frames:
            raise ReplayLimit(f"stopped at the {self.limits.max_frames}-frame limit")
        self.attempts += 1

    def after_frame(self) -> None:
        """After a decode and whatever was done with it (nothing for a skipped frame), also when that failed."""
        self.check_time()
        if self.attempts % self.limits.rss_every == 0:
            self.check_memory()

    def finish(self) -> None:
        """The last partial interval: called before a pass reports success."""
        self.check_time()
        self.check_memory()


class Replay:
    """One recognizer and one frame controller for a whole replay; see the module docstring."""

    def __init__(self, *, android_layer: bool = True, limits: Limits = Limits(),
                 rss: Callable[[], int] | None = None, peak: Callable[[], int] | None = None):
        self.android_layer = android_layer
        self.limits = limits
        self._rss, self._peak = rss, peak
        self.guard = Guard(limits, rss=rss, peak=peak)
        self.controller = None
        self.recognizer = None
        self.frames = 0          # frames shown so far
        self._open = False
        self._generation = 0     # one per with block: an iterator of an earlier block is refused
        self._running = None     # the token of the run() iterator in progress
        self._iterators = weakref.WeakSet()  # this block's run() iterators, closed when it ends

    @property
    def peak_rss(self) -> int:
        return self.guard.peak_rss

    def __enter__(self) -> "Replay":
        if self._open:
            raise RuntimeError("this Replay is already open: one recognizer per replay, never a second one")
        self._open = True
        self._generation += 1
        self._running = None
        self._iterators = weakref.WeakSet()
        self.guard, self.frames = Guard(self.limits, rss=self._rss, peak=self._peak), 0
        try:
            self.guard.start()  # setup counts toward the wall time
            self.controller = probe.frame_controller()
            self.recognizer = probe.Recognizer(self.controller, android_layer=self.android_layer)
            self.guard.check_time()
            self.guard.check_memory()
        except BaseException:
            self._close(check=False)
            raise
        return self

    def __exit__(self, exc_type, *exc) -> None:
        self._close(check=exc_type is None)  # an exception already on its way is not replaced by a limit

    def close(self) -> None:
        """End the block: the guard's last check first (a caller that stopped early is checked too), then, also when
        that check raises, close this block's iterators and free the tasker, the resource and the controller (in that
        order). Only the first call does anything."""
        self._close(check=True)

    def _close(self, check: bool) -> None:
        if not self._open:
            return
        try:
            if check:
                self.guard.finish()
        finally:
            self._teardown()

    def _teardown(self) -> None:
        recognizer, controller = self.recognizer, self.controller
        self.recognizer = self.controller = None
        self._open, self._running = False, None
        iterators, self._iterators = list(self._iterators), weakref.WeakSet()
        try:
            for iterator in iterators:  # a suspended iterator holds the last image and result in its frame
                try:
                    iterator.close()
                except ValueError:
                    pass  # the one running right now (close() from its callback): it stops at its next frame
            if controller is not None:
                controller.show(None)  # a recognizer kept by the caller still points at the controller
        finally:
            try:
                if recognizer is not None:
                    recognizer.release()
            finally:
                if controller is not None:
                    probe.destroy_native(controller)

    def run(self, paths: Iterable[Path], callback: Callable[[Any, Path, Any], Any], *,
            accept: Callable[[Any], bool] | None = None) -> Iterator[tuple[Path, Any]]:
        """Yield (path, callback(recognizer, path, image)) for each frame, in order, on the one recognizer.

        Without `accept`, a frame that cannot be read or is not 16:9 raises probe.ProbeError. With it, each frame is
        read with cv2.imread and skipped (no callback) unless accept(image) is true; image may be None. Every attempt
        counts toward the frame limit and the memory reads: skipped frames, and frames whose decode, accept or
        callback raised. One run at a time: a second run() while one is in progress (started and neither finished
        nor closed), or an iterator resumed after its with block ended, raises RuntimeError.
        """
        if self.recognizer is None:
            raise RuntimeError("Replay.run outside its with block")
        iterator = self._run(self._generation, paths, callback, accept)
        self._iterators.add(iterator)
        return iterator

    def _check_current(self, generation: int, token) -> None:
        if not self._open or self._generation != generation:
            raise RuntimeError("this run() belongs to a Replay block that has ended")
        if self._running is not token:
            raise RuntimeError("another run() of this Replay is in progress")

    def _run(self, generation, paths, callback, accept):
        token = object()
        if self._running is not None:
            raise RuntimeError("another run() of this Replay is in progress: finish or close it first")
        self._check_current(generation, None)
        self.guard.check_breach()
        self._running = token
        try:
            guard = self.guard
            if hasattr(paths, "__len__") and len(paths) > self.limits.max_frames:
                raise ReplayLimit(f"replay refused: {len(paths)} frames, above the {self.limits.max_frames}-frame "
                                  f"limit (raise it explicitly, e.g. --max-frames, if this is intended)")
            for path in paths:
                self._check_current(generation, token)
                guard.before_decode()
                skipped = False
                try:
                    if accept is None:
                        image = probe.read_frame(path)
                    else:
                        import cv2
                        image = cv2.imread(str(path))
                        skipped = not accept(image)
                    if not skipped:
                        self.controller.show(image)
                        result = callback(self.recognizer, path, image)
                        self.frames += 1
                except BaseException:
                    guard.after_frame()  # the failed attempt is counted and checked (a limit hit replaces the error)
                    raise
                guard.after_frame()
                image = None  # not kept while the iterator waits
                if skipped:
                    continue
                yield path, result
                result = None
            self._check_current(generation, token)
            guard.finish()
        finally:
            if self._running is token:
                self._running = None


def replay_frames(paths: Iterable[Path], callback: Callable[[Any, Path, Any], Any], *, android_layer: bool = True,
                  limits: Limits = Limits(), accept: Callable[[Any], bool] | None = None,
                  rss: Callable[[], int] | None = None,
                  peak: Callable[[], int] | None = None) -> list[tuple[Path, Any]]:
    """Replay.run collected into a list, everything freed before it returns."""
    with Replay(android_layer=android_layer, limits=limits, rss=rss, peak=peak) as session:
        return list(session.run(paths, callback, accept=accept))
