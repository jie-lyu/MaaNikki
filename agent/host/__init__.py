"""Host backends: everything that depends on the desktop platform the game runs on.

The package is named ``host`` rather than ``platform`` because agent/main.py
puts agent/ first on sys.path, where a ``platform`` package would shadow the
standard library.

Four interfaces, implemented once per backend and chosen from the connected
controller's info:

- ``Target``: identity and focus of the game window (exists, minimized,
  foreground, hung, bring to front, what currently has focus).
- ``Input`` (``InputBackend``): game input with guaranteed release. Task code uses
  navigation.controller.ForegroundInput, which holds the keys and buttons
  it pressed and releases them through the backend on stop, focus loss or
  error. Win32 virtual-key integers stay the key id everywhere; a backend
  translates them if its platform needs other codes.
- ``Geometry``: capture geometry of the target (client size, client rect on
  screen, monitor work area).
- ``Capabilities``: what the backend supports, checked before work starts.

Optional adapters (overlay windows, launching and closing the game,
credential storage) are reached through ``overlay_backend()`` and similar
accessors only where a capability says they exist.

Backends live in subpackages (``host.win32``, ``host.macos``) and are imported
lazily, so importing task code never loads another platform's backend.

Which backend: the game target and the desktop are separate questions. ``game_backend()`` answers
for the game (input, focus, geometry, capabilities, task tiers, overlays and window handling that
follow the game window) from the controller the client submitted for this run (``PI_CONTROLLER``);
``platform_backend()`` answers for the desktop this process runs on (worker threads, clipboard,
credential store). On Windows a submitted controller is always Win32 and on macOS always MacOS, so
the two agree there; they differ only for a controller that does not drive this desktop (an
Android emulator over ADB).

Keys: Win32 virtual-key integers are the key id everywhere. ``native_key()`` is
the one place that turns one into what the connected controller expects
(unchanged on Win32, host/macos/keys.py on macOS); call it exactly where a key
is handed to the controller or to a pipeline action.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import os
import sys
from typing import Protocol


@dataclass(frozen=True)
class Capabilities:
    backend: str
    foreground_input: bool = False
    background_input: bool = False
    overlay: bool = False
    tall_window: bool = False  # 16:9 healing and the temporary tall scan window
    work_area_bounds: bool = False  # click targets clipped to the monitor work area (taskbar, off-screen)
    pointer_hover: bool = False  # the cursor may rest on a UI control before and after a click
    # Navigation presses Alt once before camera turns to leave the game's cursor mode (Windows). On macOS
    # the key shows the pointer while held, leaves no lasting mode, and makes the game swallow the next
    # relative move (device investigation, 2026-11-24).
    requires_alt_before_camera: bool = False
    launch_game: bool = False
    close_game: bool = False
    clipboard: bool = False
    # Typing a string into a game text field (InputText). macOS posts one character per request (patch 4) but is not
    # yet device-verified there, so text-dependent sub-options refuse before any committing input.
    text_input: bool = False
    credential_store: bool = False
    # The session's first wait sends an Alt key-up when the game window is not in front (Windows, and the
    # Option key on macOS), the existing way of nudging focus back to the game. Never on a device target,
    # where the key would go to whatever app has focus there.
    startup_focus_key: bool = False
    # The "return to the game" choice for focus loss is offered (Win32 brings the window back; macOS keeps its
    # existing behaviour). Without it a saved "return" behaves like "pause".
    refocus_game: bool = False
    # Frames show the game only while the game has focus on the device (Android): Runtime confirms that focus
    # right before and right after each capture and discards a frame taken without it.
    capture_needs_device_focus: bool = False
    # Only UI taps and swipes may be dispatched (Android): Runtime refuses every other action type, in any
    # spelling and in child-task overrides, before anything is sent; named game actions there are touch
    # implementations of their own (host/android/actions.py).
    touch_actions_only: bool = False


UNSUPPORTED = Capabilities("unsupported")


class UnsupportedInput(Exception):
    """An input this desktop cannot send yet. Deliberately not an OSError/RuntimeError/ValueError: callers that turn
    those into a fallback (for example the realm queue spending the remaining energy on the default realm) must not
    catch it; the task stops instead."""


class DeviceUnreachable(Exception):
    """The ADB device stopped answering: several shell reads in a row failed (each has its own timeout). The run
    stops with advice instead of waiting on an unreadable focus forever."""


class InputUnconfirmed(Exception):
    """A touch the device may not have completed (its release was not confirmed). Like UnsupportedInput it is no
    OSError/RuntimeError/ValueError, so no fallback catches it: the run stops before any further input."""


class DeviceInputState:
    """Process-wide state of input to Android devices; only Android code paths create or change it.

    Android input on this host has one owner and is scoped to the host, not to an ADB serial (one emulator is
    reachable under several serials, e.g. 127.0.0.1:5555 and emulator-5554):

    - owner: the first Android focus read, capture or touch takes an exclusive lock
      (config/android-input/owner.lock, held until the agent process exits). Another agent process gets nothing,
      so capture, decision and dispatch, and the focus epoch below, all belong to one process.
    - touch marker: before every touch config/android-input/touch.json is created exclusively (outside the cache
      the client can clear) and removed only after the touch is confirmed released: the framework reported it
      done and the device then showed this gesture's injected UP and no contact held (`dumpsys input`). A marker
      already present means an earlier touch was never confirmed: nothing is sent. The marker names the device
      the touch was meant for (serial and boot id), so the headless runner can clear it by reading that device
      before it connects (scripts/android/runner.py). When MaaNikki starts
      (phase B5) it takes the same owner lock and only then removes touch.json, never the lock file: the
      recovery boundary. Any storage error refuses the touch.
    - epoch: advanced by every observed loss of device focus (AndroidTarget records its own negative readings,
      under this lock); a frame captured before it is stale.
    - marks: (focus generation, epoch) per captured frame, recorded only if the epoch the capture started with is
      still current.
    - halted: this process saw an unconfirmed touch; nothing more is sent by it at all.
    - inputs: Android inputs this process sent (taps, swipes, gestures, text), counted under the lock as each is
      sent; every frame's mark records the count read when its capture began, under the lock, so every input it
      counts had been confirmed (or input halted) before that frame. A gesture is sent only on a frame that counts
      every input sent so far, and any input only on a frame that counts the latest gesture: a frame captured before
      an input that may have changed the screen authorises nothing after it, and the touch reading kept with the
      frame (the lift baseline) postdates the previous input's lift.
    - captures: Android screencaps completed (counted under the lock as each completes); a gesture is sent only on
      the latest one, so a gesture delayed behind another Runtime's capture decides again on the newer pixels.
    - last_lift: the injected UP that confirmed the latest touch (boot id, event time). A touch is confirmed only by
      an UP newer than both its frame's baseline and this one, so two taps decided on one frame cannot share a lift.
    - lifts_outstanding: a gesture ended with a lift posted but not reported done (a slow or wedged controller).
      Then nothing may stop the tasker: MaaFramework's Tasker::post_stop clears the controller's queued jobs, the
      queued UP included, while the running DOWN (posted without auto-up) still completes (Runtime._halt_unconfirmed).
    - lock: one touch at a time; reservation, halt, focus and marks are checked inside it right before dispatch.
      Android capture holds it from the touch reading until the frame is complete, so no frame is captured while
      a touch, a gesture's contact in particular, is down.
    """

    def __init__(self, root=None):
        import threading
        self.lock = threading.RLock()
        self.root = root
        self.halted = False
        self.epoch = 0
        self.inputs = 0
        self.last_gesture = 0  # ``inputs`` right after the latest gesture was sent
        self.captures = 0
        self.last_lift = None
        self.lifts_outstanding = False
        self._marks = {}
        self._owner = None

    def directory(self):
        return None if self.root is None else self.root / "config" / "android-input"

    def marker(self):
        directory = self.directory()
        return None if directory is None else directory / "touch.json"

    def stop_request(self):
        """config/android-input/stop.request: written by whoever stops a run (the client's Android finishing, the
        headless runner) before it waits for touch.json to go and then stops the tasker. Whoever starts a run
        removes a stale one before it spawns the agent."""
        directory = self.directory()
        return None if directory is None else directory / "stop.request"

    def stop_requested(self) -> bool:
        """True when a stop was requested, or when that cannot be checked (nothing is sent then). Checked after the
        touch is reserved: a stopper that wrote the request before the reservation is seen here, and one that wrote
        it after finds touch.json and waits for this touch to finish before it stops the tasker."""
        path = self.stop_request()
        if path is None:
            return True
        try:
            os.stat(path)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    def claim(self):
        """Make this process the only one using Android input on this host; raises UnsupportedInput otherwise."""
        with self.lock:
            if self._owner is not None:
                return
            directory = self.directory()
            if directory is None:
                raise UnsupportedInput("没有 MaaNikki 的数据目录，无法记录安卓模拟器上的输入，暂不操作模拟器。")
            self._owner = take_owner_lock(directory / "owner.lock")  # kept open: held as long as this process

    def begin_touch(self, device=None):
        """Reserve the next touch on disk, naming the device (serial and boot id) it is meant for; raises unless the
        reservation is there and this process owns it. A marker already present halts this process."""
        with self.lock:
            self.claim()
            path = self.marker()
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                self.halted = True
                raise InputUnconfirmed("安卓模拟器上一次的触摸没有确认抬起，设备也没有报告触点已经全部抬起，已停止所有输入；"
                                       "请检查模拟器后重新启动 MaaNikki。")
            except OSError:
                raise UnsupportedInput("无法记录安卓模拟器上的触摸（数据目录不可写），这一步暂不发送。") from None
            try:
                import datetime as _dt
                import uuid as _uuid
                # A nonce per reservation: no two markers are byte-identical, so a fingerprint taken from one can
                # never clear another. The whole payload must be on disk before the touch is sent.
                payload = json.dumps({"pid": os.getpid(), "since": _dt.datetime.now().astimezone().isoformat(),
                                      "nonce": _uuid.uuid4().hex, "device": device}).encode()
                written = 0
                while written < len(payload):
                    count = os.write(fd, payload[written:])
                    if count <= 0:
                        raise OSError("short write")
                    written += count
                os.fsync(fd)
                os.close(fd)
            except OSError:
                try:
                    os.close(fd)
                except OSError:
                    pass
                self.release_touch()  # nothing was sent: the half-written reservation is ours to remove
                raise UnsupportedInput("无法记录安卓模拟器上的触摸（数据目录不可写），这一步暂不发送。") from None

    def record_touch(self, action, fields, outcome) -> bool:
        """Append one touch to logs/android-input.jsonl under the data root (the rule: every input is logged): its
        intent ("sending") before it is dispatched, which must succeed or the touch is not sent, then its outcome.
        False when the line could not be written."""
        if self.root is None:
            return False
        import datetime as _dt
        logged = ("target", "begin", "end", "duration", "text", "gesture", "steps")  # gesture, steps: Runtime.gesture
        entry = {"time": _dt.datetime.now().astimezone().isoformat(timespec="milliseconds"), "pid": os.getpid(),
                 "action": action, **{key: fields[key] for key in logged if key in fields},
                 "outcome": outcome}
        try:
            path = self.root / "logs" / "android-input.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return True
        except OSError:
            return False

    def release_touch(self):
        """Remove this process's reservation: the touch was confirmed, or never sent. A reservation that cannot be
        removed halts this process (the next one will see it and refuse as well)."""
        with self.lock:
            try:
                self.marker().unlink()
            except FileNotFoundError:
                pass
            except OSError:
                self.halted = True
                raise UnsupportedInput("无法清除安卓模拟器的触摸记录，已停止所有输入；请重新启动 MaaNikki。") from None

    def halt(self):
        with self.lock:
            self.halted = True

    def observed_loss(self):
        with self.lock:
            self.epoch += 1

    def mark_if_unchanged(self, frame, generation, epoch, touch=None, inputs=None, capture=None) -> bool:
        """Record the frame's (focus generation, epoch), the device's touch reading taken right before it was
        captured (``touch``: a TouchSnapshot, or None), the input count read then (``inputs``; None: the count now)
        and its capture number (``capture``, from completed_capture; None: counted as completed now), if ``epoch``
        is still current."""
        import weakref
        with self.lock:
            if self.epoch != epoch:
                return False
            self._marks = {key: value for key, value in self._marks.items() if value[0]() is not None}
            self._marks[id(frame)] = (weakref.ref(frame), (generation, epoch), touch,
                                      self.inputs if inputs is None else inputs,
                                      self.completed_capture() if capture is None else capture)
            return True

    def completed_capture(self) -> int:
        """Count a completed Android screencap (inside the capture's locked section); its number."""
        with self.lock:
            self.captures += 1
            return self.captures

    def superseded(self, frame) -> bool:
        """True when another screencap completed after ``frame`` (in any Runtime), or ``frame`` has no mark."""
        entry = self._marks.get(id(frame)) if frame is not None else None
        return entry is None or entry[0]() is not frame or entry[4] < self.captures

    def lift_floor(self, boot_id):
        """The event time of the UP that confirmed the latest touch on this guest (None: none, or another boot)."""
        if self.last_lift is None:
            return None
        seen, when = self.last_lift
        return None if seen is not None and boot_id is not None and seen != boot_id else when

    def consumed_lift(self, snapshot):
        """Record the UP that just confirmed a touch: a later touch needs a newer one."""
        with self.lock:
            self.last_lift = (snapshot.boot_id, snapshot.injected_up)

    def mark(self, frame, generation, touch=None):
        with self.lock:
            return self.mark_if_unchanged(frame, generation, self.epoch, touch)

    def sent_input(self, *, gesture=False):
        """Count an Android input as it is sent (under the lock, inside the dispatch)."""
        with self.lock:
            self.inputs += 1
            if gesture:
                self.last_gesture = self.inputs

    def predates_input(self, frame, *, gesture) -> bool:
        """True when ``frame`` was captured before an input it must follow: for a gesture, any input sent so far;
        for any other input, the latest gesture. Also True for a frame without a mark."""
        entry = self._marks.get(id(frame)) if frame is not None else None
        if entry is None or entry[0]() is not frame:
            return True
        return entry[3] < (self.inputs if gesture else self.last_gesture)

    def marks_of(self, frame):
        entry = self._marks.get(id(frame)) if frame is not None else None
        return entry[1] if entry is not None and entry[0]() is frame else None

    def touch_of(self, frame):
        """The device's touch reading taken right before ``frame`` was captured, or None."""
        entry = self._marks.get(id(frame)) if frame is not None else None
        return entry[2] if entry is not None and entry[0]() is frame else None


def take_owner_lock(path):
    """An open handle holding an exclusive, non-blocking flock on ``path``; UnsupportedInput if another process
    holds it or it cannot be taken. Released when the handle is closed or the process exits. The lock file itself
    is never removed (MaaNikki's startup cleanup takes this same lock before clearing a touch marker)."""
    try:
        import fcntl
    except ImportError:
        raise UnsupportedInput("这台电脑上无法独占安卓模拟器的输入，暂不操作模拟器。") from None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+b")
    except OSError:
        raise UnsupportedInput("无法在数据目录里独占安卓模拟器的输入，暂不操作模拟器。") from None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise UnsupportedInput("另一个 MaaNikki 任务正在操作安卓模拟器，这里不再操作。") from None
    except OSError:
        handle.close()
        raise UnsupportedInput("无法在数据目录里独占安卓模拟器的输入，暂不操作模拟器。") from None
    return handle


_DEVICE_INPUT = None
_DEVICE_INPUT_INIT = __import__("threading").Lock()


def android_user_stop(*, ask_tasker: bool = True) -> bool:
    """Whether the user stopped this Android run (host.android.target.user_stop): shared code consults it to end such
    a run quietly as stopped, without a failure scene, an error message or a pause notice. Always False off the
    Android backend, where nothing is read, so Windows and native Mac never see it."""
    if game_backend() != "android":
        return False  # no backend module is imported or state read here
    from host.android.target import user_stop
    return user_stop(ask_tasker=ask_tasker)


def device_input() -> DeviceInputState:
    global _DEVICE_INPUT
    with _DEVICE_INPUT_INIT:
        if _DEVICE_INPUT is None:
            # Only a data directory MaaNikki chose: never the install directory a bare run would fall back to.
            from app_paths import DATA_ROOT, DATA_ROOT_ENV
            _DEVICE_INPUT = DeviceInputState(DATA_ROOT if os.environ.get(DATA_ROOT_ENV, "").strip() else None)
        return _DEVICE_INPUT


class Target(Protocol):
    """The connected game window.

    Backends may add platform handles (Win32Target.user32); neutral code must
    not rely on them.
    """

    handle: int
    capture_requires_foreground: bool
    # True where releasing through the controller never refocuses the game, so held input is
    # released through it even after focus loss (macOS). Win32 releases directly instead.
    release_ignores_focus: bool

    def exists(self) -> bool: ...
    def minimized(self) -> bool: ...
    def foreground(self) -> bool: ...
    def hung(self) -> bool: ...
    def bring_to_foreground(self, controller) -> bool:
        """Restore and activate the window; True when it is in the foreground afterwards."""
        ...
    def focus_details(self) -> dict: ...
    def input(self) -> "InputBackend":
        """Input primitives bound to this window."""
        ...
    def geometry(self) -> "Geometry":
        """Capture geometry of this window."""
        ...


class InputBackend(Protocol):
    """Platform input primitives behind navigation.controller.ForegroundInput.

    The neutral input class tracks held keys and buttons and guarantees their
    release; these primitives release without the agent channel or refocusing.
    Keys are Win32 virtual-key integers. The Win32 implementation is
    host/win32/input.py; window-targeted background input is
    host/win32/background_input.py.
    """

    def force_key_up(self, key: int) -> None: ...
    def force_mouse_up(self, button: int) -> None: ...
    def press_mouse(self, button: int) -> bool: ...
    def post_window_key(self, key: int, *, down: bool) -> bool: ...
    def relative_move(self, dx: int, dy: int) -> bool: ...
    def last_error(self) -> int: ...
    def physical_keys(self):
        """Object whose down(key) reports whether the user holds a physical key (route recording)."""
        ...


class Geometry(Protocol):
    """Where the target's client area is, in screen pixels.

    The Win32 implementation is host/win32/geometry.py over window_layout.py,
    which also owns 16:9 healing and the temporary tall window behind the
    neutral tall_window.py entry points.
    """

    def client_size(self) -> tuple[int, int] | None: ...
    def client_screen_rect(self) -> tuple[int, int, int, int] | None: ...
    def monitor_work_area(self) -> tuple[int, int, int, int] | None: ...


def platform_backend() -> str | None:
    """Backend for this process's desktop, or None when there is none."""
    if os.name == "nt":
        return "win32"
    if sys.platform == "darwin":
        return "macos"
    return None


# interface.json controller types that decide the game target, and the backend that drives each. Adb (Android
# emulators) is driven by host.android, on a macOS host only for now (the Windows package does not ship it until
# phase F). Other types (Gamepad, PlayCover, …) keep the desktop's rules, as before; the client applies the same
# rule (client/src-tauri/src/commands/utils.rs, GameTarget).
TARGET_TYPES = {"Win32": "win32", "MacOS": "macos", "Adb": "android"}
ANDROID_HOSTS = frozenset({"macos"})
NO_SUBMITTED_CONTROLLER = object()


@lru_cache(maxsize=8)
def _controller_type(raw: str):
    """The "type" of a PI_CONTROLLER value, or None when it is not a controller definition."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    kind = value.get("type") if isinstance(value, dict) else None
    return kind if isinstance(kind, str) and kind else None


def submitted_controller_type():
    """The type of the controller the client submitted for this run (PI_CONTROLLER), None when that value is
    malformed, or NO_SUBMITTED_CONTROLLER when there is none (agent startup checks, scripts, offline tests)."""
    raw = os.environ.get("PI_CONTROLLER")
    if raw is None:
        return NO_SUBMITTED_CONTROLLER
    return _controller_type(raw)


def game_backend() -> str | None:
    """Backend for the game target of this run, or None when nothing on this desktop can drive it.

    Follows the submitted controller: Win32 and MacOS controllers select their backend only on their own
    desktop; an Adb controller selects the Android backend on the desktops that ship it (macOS for now); a
    malformed value selects none, so target-dependent work is refused rather than run with the desktop's
    rules. Without a submitted controller, or with one of another type, it is the desktop's backend, as before.
    """
    kind = submitted_controller_type()
    if kind is NO_SUBMITTED_CONTROLLER:
        return platform_backend()
    if kind is None:
        return None  # malformed
    target = TARGET_TYPES.get(kind)
    if target is None:
        return platform_backend()
    if target in ("win32", "macos"):
        return target if target == platform_backend() else None
    if target == "android":
        return target if platform_backend() in ANDROID_HOSTS else None
    return None


def controller_backend(info) -> str | None:
    """Backend that can drive a controller described by Controller.info, or None."""
    backend = platform_backend()
    kind = info.get("type") if isinstance(info, dict) else None
    if (backend, kind) in {("win32", "win32"), ("macos", "macos")}:
        return backend
    return None


def native_key(key: int) -> int:
    """The connected controller's code for one of the agent's Win32 virtual keys.

    Unchanged on Win32. On macOS the single table in host/macos/keys.py; a key without a macOS
    counterpart raises NavigationError instead of becoming 0 (which is the A key there). The Android backend
    has no keyboard: every key raises UnsupportedInput, so no key code ever reaches the device (named game
    actions there are touch, host/android/actions.py).
    """
    backend = game_backend()
    if backend == "android":
        raise UnsupportedInput(f"安卓模拟器上没有电脑按键（键码 {key}），这一步暂不支持。")
    if backend != "macos":
        return key
    from host.macos.keys import UnmappedKey, translate
    try:
        return translate(key)
    except UnmappedKey as error:
        from navigation.models import NavigationError
        raise NavigationError(str(error)) from None


def connect_target(controller) -> Target:
    """Target for the window the client connected, never an independently found one.

    Controller.info is forwarded by MaaAgentServer; Toolkit enumeration is not.
    Raises NavigationError when this host cannot drive that controller.
    """
    from navigation.models import NavigationError
    backend = game_backend()
    if backend not in ("win32", "macos", "android"):
        raise NavigationError("前台导航仅支持 Windows 游戏窗口。")
    try:
        info = controller.info
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as error:
        raise NavigationError("无法读取当前连接的游戏窗口，请重新连接后再试。") from error
    if backend == "macos":
        from host.macos.target import MacOSTarget
        return MacOSTarget(info, controller)
    if backend == "android":
        from host.android.target import AndroidTarget
        return AndroidTarget(info, controller)
    from host.win32.target import Win32Target
    return Win32Target(info)


def controller_supports_foreground_input(controller) -> bool:
    """Whether the interface.json controller definition lets the agent send foreground input itself.

    The rule belongs to the backend for the game target; without one, never.
    """
    backend = game_backend()
    if backend == "win32":
        from host.win32 import supports_foreground_inputs
        return supports_foreground_inputs(controller)
    if backend == "macos":
        from host.macos import supports_foreground_inputs
        return supports_foreground_inputs(controller)
    return False


def connection_geometry_problem(get_controller) -> str | None:
    """Why input must pause because of the connection's geometry, or None when it is fine.

    macOS: the patched unit's geometry_changed latch, read live from Controller.info. True means
    the window moved, changed display or scaling since connect and clicks are refused until
    reconnect; a value that cannot be read is treated the same way, never as unchanged.
    Always None on Win32, where the controller is not even looked up (``get_controller`` is called
    only on macOS).
    """
    if game_backend() != "macos":
        return None
    try:
        info = get_controller().info
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        info = None
    latch = info.get("geometry_changed") if isinstance(info, dict) else None
    if latch is False:
        return None
    if latch is True:
        return "游戏窗口的位置、所在显示器或缩放在运行中发生了变化，已暂停输入；请重新连接游戏后再运行"
    return "无法读取 Mac 控制模块的窗口几何状态，已暂停输入；请重新连接游戏后再运行"


def task_entry_supported(entry) -> bool:
    """Whether this desktop's backend may run the task whose pipeline entry is ``entry``.

    Win32 runs every task, as before. macOS runs the preview's verified and experimental entries. A submitted
    controller that nothing on this desktop can drive (an Adb controller on Windows, a malformed value) runs none.
    The Android backend runs the entries on its own list (host/android/tasks.json).
    """
    backend = game_backend()
    if backend == "macos":
        from host.macos import dev_extra_task_entries, task_tier
        if entry in dev_extra_task_entries():
            return True  # development switch for supervised device tests; never set by a release client
        try:
            return task_tier(entry) is not None
        except (OSError, ValueError, KeyError, TypeError):
            return False  # no readable list: refuse, like the client
    if backend == "android":
        from host.android import task_allowed
        return task_allowed(entry)
    if backend is None and _target_refused():
        return False
    return True


def _target_refused() -> bool:
    """Whether the submitted controller itself rules out running tasks here (Adb on Windows for now, a Win32 or MacOS
    controller on the other desktop, a malformed value); False without one or for other types."""
    kind = submitted_controller_type()
    if kind is NO_SUBMITTED_CONTROLLER:
        return False
    return kind is None or kind in TARGET_TYPES


def task_refusal_reason(entry) -> str:
    """The user-facing reason this desktop refuses ``entry`` (macOS: the list's excluded reason when there is one)."""
    backend = game_backend()
    if backend == "macos":
        from host.macos import refusal_reason
        return refusal_reason(entry)
    if backend == "android":
        from host.android import refusal_reason as android_refusal
        return android_refusal(entry)
    kind = submitted_controller_type()
    if backend is None and kind == "Adb":
        return "当前系统的 MaaNikki 还不支持安卓模拟器，已跳过。"
    if backend is None and _target_refused():
        return f"所选的控制器（{kind or '无效'}）不能在当前系统上运行任务，已跳过。"
    return "当前系统不支持这个任务，已跳过。"


def task_start_notice(entry) -> str | None:
    """A warning for the task's own log when ``entry`` starts: macOS experimental tasks only, None elsewhere."""
    backend = game_backend()
    if backend == "macos":
        from host.macos import start_notice
        return start_notice(entry)
    if backend == "android":
        from host.android import start_notice as android_notice
        return android_notice(entry)
    return None


def background_input_class():
    """Window-targeted input class for the game target; raises NavigationError where there is none."""
    if game_backend() != "win32":
        from navigation.models import NavigationError
        raise NavigationError("前台导航仅支持 Windows 游戏窗口。")
    from host.win32.background_input import BackgroundInput
    return BackgroundInput


def launch_window_probes():
    """Window liveness and responsiveness probes for the start-game pretask, or None where this
    desktop cannot launch the game."""
    if not capabilities(game_backend()).launch_game:
        return None
    from host.win32 import launch
    return launch


def game_close_api():
    """Binding that closes the game and launcher windows/processes on this desktop."""
    if not capabilities(game_backend()).close_game:
        raise RuntimeError("当前系统不支持关闭游戏。")
    from host.win32.close import Win32Api
    return Win32Api()


def clipboard():
    """Module with get_clipboard_text() and set_clipboard_text() for this desktop."""
    backend = platform_backend()
    if not capabilities(backend).clipboard:
        raise RuntimeError("当前系统不支持读写剪贴板。")
    if backend == "macos":
        from host.macos import clipboard as module
        return module
    from host.win32 import clipboard as module
    return module


def credential_store():
    """Module whose credentials(path) reads the PearPal login protected for the current user."""
    if not capabilities(platform_backend()).credential_store:
        raise RuntimeError("当前系统不支持读取已保存的登录信息。")
    from host.win32 import credentials as module
    return module


def capabilities(backend: str | None) -> Capabilities:
    if backend == "win32":
        from host.win32 import CAPABILITIES
        return CAPABILITIES
    if backend == "macos":
        from host.macos import CAPABILITIES
        return CAPABILITIES
    if backend == "android":
        from host.android import CAPABILITIES
        return CAPABILITIES
    return UNSUPPORTED


def overlay_backend():
    """Module providing the overlay windows over the game window, or None when overlays are unavailable.

    Overlays are desktop windows that follow the game window, so they need a game target with a window on
    this desktop; the only overlay backend is Win32.
    """
    if capabilities(game_backend()).overlay:
        from host.win32 import overlays
        return overlays
    return None
