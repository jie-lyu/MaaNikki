"""Android target: whether the game's activity can take input on the emulator.

There is no desktop window. "Exists" means the game's process runs on the device, "foreground" that its window has
input focus there (mCurrentFocus; mFocusedApp still names the game behind a system dialog or the launcher, so it
does not count). Both are read through the controller's own ADB shell, so they describe the device the controller
is connected to.

Desktop windows on top of the emulator do not matter for capture (2026-11-25 survey), but which Android app is in
front does: with the launcher or another app in front, the frame is not the game. So a frame counts only while the
game has focus (capture_requires_foreground).

Input arrives in a later step. Until then input() gives primitives that send nothing, so the session and
navigation code can hold a target, wait for focus and release (nothing) without any key reaching the device.
"""
from __future__ import annotations

import os
import re
import threading

from navigation.models import NavigationError

GAME_PACKAGE = "com.papegames.infinitynikki"
_FOCUS = re.compile(r"\su\d+\s+([\w.]+)/")
# The controller keeps one shell output: a second request before the first result is read would replace it.
# Every shell request of this process holds this lock from posting through reading the result.
_SHELL_LOCK = threading.Lock()


def focused_package(output: str | None) -> str | None:
    """The package of the window with input focus from `dumpsys window | grep mCurrentFocus`, or None."""
    packages = set()
    for line in (output or "").splitlines():
        if "mCurrentFocus" in line:
            match = _FOCUS.search(line)
            packages.add(match.group(1) if match else None)
    return packages.pop() if len(packages) == 1 else None


def process_state(output: str | None) -> bool | None:
    """From `pidof <package>; echo __exit=$?`: True when a numeric pid is reported, False when pidof confirmed
    there is none, None when the result cannot be read (it then says nothing about the game)."""
    if output is None:
        return None
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    status = next((line for line in lines if line.startswith("__exit=")), None)
    pids = [line for line in lines if not line.startswith("__exit=")]
    if status == "__exit=0" and pids and all(part.isdigit() for line in pids for part in line.split()):
        return True
    if status == "__exit=1" and not pids:
        return False
    return None


_TOUCH_ENTRY = re.compile(r"^\s*\d+: down=(true|false),.*\bdeviceId=(-?\d+)\b")
# ADB-injected events (MaaTouch uses InputManager.injectInputEvent) carry no input device: deviceId -1.
INJECTED_DEVICE = -1


def touch_state(output: str | None) -> str | None:
    """Whether a contact is held on the device, from the InputDispatcher's touch state in `dumpsys input` (read-only).

    "none": the dispatcher reports no display touched; "injected": an injected contact (deviceId -1) is down on some
    display; "other": a contact from a real input device is down and no injected one; None: the answer could not be
    read or has a form not seen on the device (it then says nothing). Measured on BlueStacks Air (Android 13,
    2026-11-26): a contact held through MaaTouch shows `down=true ... deviceId=-1` and disappears once lifted; with
    the MaaTouch process killed while the contact was held, the entry stays until a new touch on the same spot."""
    if output is None:
        return None
    lines = output.splitlines()
    headers = [index for index, line in enumerate(lines) if line.strip().startswith("TouchStates")]
    if len(headers) != 1:
        return None
    header = lines[headers[0]]
    if header.strip() == "TouchStates: <no displays touched>":
        return "none"
    if header.strip() != "TouchStatesByDisplay:":
        return None
    depth = len(header) - len(header.lstrip())
    downs = []
    for line in lines[headers[0] + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= depth:
            break
        match = _TOUCH_ENTRY.match(line)
        if match:
            downs.append((match.group(1) == "true", int(match.group(2))))
    if any(down and device == INJECTED_DEVICE for down, device in downs):
        return "injected"
    if any(down for down, _device in downs):
        return "other"
    return None  # a touched display with nothing down: not a form seen on the device


_EVENT = re.compile(r"^\s*MotionEvent\(deviceId=(-?\d+), eventTime=(\d+),.*\baction=([A-Z_]+)\b")
_ENTRY = re.compile(r"^\s+\w+Event\(.*\bage=\d+ms\s*$")  # a whole entry line, as on the device
_BOOT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class TouchSnapshot:
    """One `dumpsys input` reading: the touch state (see touch_state()), the latest event time in the dispatcher's
    RecentQueue (-1 when it is empty) and the latest injected UP's event time (None when there is none)."""

    def __init__(self, state, latest, injected_up, boot_id=None):
        self.state, self.latest, self.injected_up = state, latest, injected_up
        self.boot_id = boot_id  # the guest's boot id, read in the same shell call (None when not read)

    def released_after(self, baseline) -> bool:
        """True when nothing is held and an injected UP was dispatched after ``baseline`` (an earlier reading's
        ``latest``): evidence tied to this gesture's own lift, not just an idle dispatcher before its DOWN."""
        return self.state == "none" and self.injected_up is not None and self.injected_up > baseline


def recent_events(output: str | None):
    """(deviceId, eventTime, action) of the motion events in the dispatcher's RecentQueue, or None when the queue
    cannot be found (an empty queue gives [])."""
    if output is None:
        return None
    lines = output.splitlines()
    headers = [index for index, line in enumerate(lines) if line.strip().startswith("RecentQueue:")]
    if len(headers) != 1:
        return None
    header = lines[headers[0]]
    if header.strip() == "RecentQueue: <empty>":
        return []
    if not header.strip().startswith("RecentQueue: length="):
        return None
    try:
        length = int(header.strip()[len("RecentQueue: length="):])
    except ValueError:
        return None
    depth = len(header) - len(header.lstrip())
    entries, events = 0, []
    for line in lines[headers[0] + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= depth:
            break
        if _ENTRY.match(line):
            entries += 1
        match = _EVENT.match(line)
        if match:
            events.append((int(match.group(1)), int(match.group(2)), match.group(3)))
    return events if entries == length else None  # a cut-off queue could hide the newest events


# The guest's boot id and the input dispatcher in one shell call (the separator is quoted apart in the command).
TOUCH_READING = "cat /proc/sys/kernel/random/boot_id; echo __in''put__; dumpsys input"


def touch_snapshot(output: str | None) -> TouchSnapshot | None:
    """Touch state and recent events from one `dumpsys input` output, optionally preceded by the boot id and the
    TOUCH_READING separator; None when the state or the events cannot be read."""
    booted = None
    if output is not None and output.count("__input__") == 1:
        head, output = output.split("__input__")
        booted = boot_id(head)
    state, events = touch_state(output), recent_events(output)
    if state is None or events is None:
        return None
    ups = [time for device, time, action in events if device == INJECTED_DEVICE and action == "UP"]
    return TouchSnapshot(state, max((time for _d, time, _a in events), default=-1), max(ups, default=None), booted)


def boot_id(output: str | None) -> str | None:
    value = (output or "").strip()
    return value if _BOOT_ID.match(value) else None


# One shell invocation for the runner's recovery decision: the guest's boot id, the input dispatcher and the
# processes are read together, so they describe the same guest at (nearly) the same moment. The separators are
# quoted apart in the command, so the command's own line in the process list does not contain them.
RECOVERY_READING = ("cat /proc/sys/kernel/random/boot_id; echo __p''s__; ps -A -o ARGS; echo __r''c=$?; "
                    "echo __in''put__; dumpsys input; echo __p''s__; ps -A -o ARGS; echo __r''c=$?")


def recovery_reading(output: str | None) -> dict | None:
    """From RECOVERY_READING's output: the boot id, the touch state, whether any input is still queued in the
    dispatcher (a pending event or a non-empty inbound queue) and whether a MaaTouch helper was running before or
    after the dispatcher was read (a helper that delivers a buffered DOWN and exits in between is caught by the
    first listing). None when any part cannot be read, including a process listing that does not look whole."""
    if not output or output.count("__input__") != 1 or output.count("__ps__") != 2:
        return None
    booted, rest = output.split("__ps__", 1)
    before, rest = rest.split("__input__")
    dump, after = rest.split("__ps__")
    state = touch_state(dump)
    pending = [line.strip() for line in dump.splitlines() if line.strip().startswith("PendingEvent:")]
    inbound = [line.strip() for line in dump.splitlines() if line.strip().startswith("InboundQueue:")]
    if boot_id(booted) is None or state is None or len(pending) != 1 or len(inbound) != 1 \
            or not (process_listing(before) and process_listing(after)) \
            or abs(_listing_size(before) - _listing_size(after)) > max(10, _listing_size(after) // 10):
        return None  # two listings a moment apart differ by a few processes, not by most of the list
    return {"boot_id": boot_id(booted), "state": state,
            "queued": pending != ["PendingEvent: <none>"] or inbound != ["InboundQueue: <empty>"],
            "maatouch": any(MAATOUCH_CLASS in line for line in (before + after).splitlines())}


MAATOUCH_CLASS = "com.shxyke.MaaTouch"


def process_listing(output: str) -> bool:
    """`ps -A -o ARGS` output that finished (its exit status 0 follows it) and looks whole: the header, init, and a
    plausible number of processes."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines or lines[-1] != "__rc=0":
        return False
    lines = lines[:-1]
    return len(lines) >= 20 and lines[0] == "ARGS" and any(line.split()[0].endswith("init") for line in lines[1:3])


def _listing_size(output: str) -> int:
    return len([line for line in output.splitlines() if line.strip()])


def check_controller_info(info) -> str:
    """The ADB serial of a connection the Android backend can drive; raises NavigationError otherwise."""
    serial = info.get("adb_serial") if isinstance(info, dict) else None
    if not isinstance(info, dict) or info.get("type") != "adb" or not isinstance(serial, str) or not serial:
        raise NavigationError("当前连接不是安卓模拟器，导航已停止。")
    return serial


class NoInput:
    """Input primitives for a target without a keyboard, mouse or relative pointer: presses are refused before
    anything counts as held (UnsupportedInput), releases send nothing."""

    # ForegroundInput.mouse_down asks this before it marks a button held, so no touch-up is ever sent for it.
    supports_scene_buttons = False

    def force_key_up(self, key: int) -> None:
        return None

    def force_mouse_up(self, button: int) -> None:
        return None

    def press_mouse(self, button: int) -> bool:
        from host import UnsupportedInput
        raise UnsupportedInput("安卓模拟器上没有电脑鼠标按键，这一步暂不支持。")

    def post_window_key(self, key: int, *, down: bool) -> bool:
        return False

    def relative_move(self, dx: int, dy: int) -> bool:
        from host import UnsupportedInput
        raise UnsupportedInput("安卓模拟器上没有相对鼠标移动，镜头需要用滑动来转，这一步暂不支持。")

    def last_error(self) -> int:
        return 0

    def physical_keys(self):
        raise NavigationError("安卓模拟器没有电脑键盘输入。")


class AndroidTarget:
    """The game on the emulator the controller is connected to."""

    capture_requires_foreground = True
    # Nothing is pressed yet; releases never wait for focus.
    release_ignores_focus = True
    handle = 0

    # Shell reads in a row that may fail (each times out after 5 s) before the device counts as unreachable.
    SHELL_FAILURE_LIMIT = 3

    def __init__(self, info: dict, controller):
        self.serial = check_controller_info(info)
        self.controller = controller
        self._shell_failures = 0

    def _shell(self, command: str) -> str | None:
        # The device-input lock first, as everywhere (a tap's last checks hold it): a read that finds the device
        # unreachable makes earlier frames stale before anyone else can check a frame against the epoch.
        from host import DeviceUnreachable, device_input
        state = device_input()
        with state.lock, _SHELL_LOCK:
            try:
                job = self.controller.post_shell(command, 5000).wait()
                output = job.get() if getattr(job, "succeeded", False) else None
            except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
                output = None
            if output is not None:
                self._shell_failures = 0
                return output
            if self._stop_in_progress():
                # The framework's stop also stops controller operations, so reads fail although ADB is fine: such a
                # failure is not counted. The caller gets an unreadable answer, as for any single failure (focus not
                # confirmed, no touch confirmed), and the run ends through its own stop checks.
                return None
            self._shell_failures += 1
            if self._shell_failures < self.SHELL_FAILURE_LIMIT:
                return None
            self._shell_failures = 0
            state.observed_loss()
            raise DeviceUnreachable(f"安卓模拟器 {self.serial} 连续 {self.SHELL_FAILURE_LIMIT} 次没有回应 ADB 命令，任务已停止："
                                    "请重启 ADB server（adb kill-server; adb start-server），确认模拟器还在，再重试。")

    @staticmethod
    def _stop_in_progress() -> bool:
        """True when this run is being stopped: the stopper's request is on disk (written before the framework is
        told to stop; DeviceInputState.stop_request), or the running task's tasker reports stopping. Unlike
        DeviceInputState.stop_requested an unknown answer counts as no: this only decides whether failed reads are
        reported as an unreachable device, never whether anything is sent."""
        from host import device_input
        path = device_input().stop_request()
        try:
            if path is not None and os.stat(path):
                return True
        except OSError:
            pass
        try:
            from task_control import active_session
            session = active_session()
            tasker = getattr(getattr(session, "context", None), "tasker", None)
            return tasker is not None and bool(tasker.stopping)
        except Exception:  # the tasker query crosses into the client; a failed query is no stop
            return False

    def exists(self) -> bool:
        # Only a confirmed absence ends the task as "game closed"; an unreadable answer is not one. Whether the
        # task then runs or pauses is the focus check's decision: positive game focus is enough to continue.
        return process_state(self._shell(f"pidof {GAME_PACKAGE}; echo __exit=$?")) is not False

    def minimized(self) -> bool:
        return False  # the emulator window's own state does not stop ADB capture or input (minimised: unverified)

    def foreground(self) -> bool:
        return self._read_focus() == GAME_PACKAGE

    def _read_focus(self) -> str | None:
        """The focused package, with a negative reading recorded in the same locked step: no tap can slip in
        between seeing another app in front and making earlier frames stale."""
        from host import device_input
        state = device_input()
        with state.lock:
            # Only the process that owns Android input reads focus: a reading elsewhere would advance an epoch
            # that the owner never sees. Raises UnsupportedInput when another agent owns it.
            state.claim()
            package = focused_package(self._shell("dumpsys window | grep mCurrentFocus"))
            if package != GAME_PACKAGE:
                state.observed_loss()
            return package

    def touch_snapshot(self) -> TouchSnapshot | None:
        """Whether a contact is held on the device now, with the dispatcher's recent events and the guest's boot
        id; None when unreadable."""
        return touch_snapshot(self._shell(TOUCH_READING))

    def hung(self) -> bool:
        return False

    def bring_to_foreground(self, controller) -> bool:
        return False  # never start, resume or switch apps on the device

    def focus_details(self) -> dict:
        from host import UnsupportedInput
        try:
            package = self._read_focus()
        except UnsupportedInput as error:
            return {"backend": "android", "serial": self.serial, "focused_package": None, "unavailable": str(error)}
        return {"backend": "android", "serial": self.serial, "focused_package": package}

    def input(self) -> NoInput:
        return NoInput()

    def geometry(self):
        raise NavigationError("安卓模拟器没有电脑上的游戏窗口。")
