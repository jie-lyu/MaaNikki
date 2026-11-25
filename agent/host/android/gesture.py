"""Android: short scripted multi-contact gestures for movement (phase E), sent as one unit by Runtime.gesture.

A gesture is a fixed script of MaaFramework touch down/move/up calls on contacts 0 and 1 at offsets from its start
(the joystick: down at the centre, a short ramp out to the chosen angle and magnitude, a hold, up; optionally a tap
of the jump button on contact 1 during the hold). It is decided on one current, focus-checked frame and sent whole
through the same path as a tap or swipe (Runtime._dispatch_touch): the touch marker is held for all of it, its
intent is logged first, nothing is captured or decided while it runs, every contact it put down is lifted in
``finally``, and the device must then show its final lift with nothing held before anything else happens.

Limits, checked before anything is reserved or sent (check_gesture): at most GESTURE_MAX_S from the first down to
the last up; the only exception is the bell long-press (BELL_PRESS, one contact pressed and released in place, at
most BELL_PRESS_MAX_S). The stopper waits 5 s for the touch marker before it gives up and kills the agent, so while
a gesture holds a contact it polls for a stop (the stop request on disk, or a halt; local checks, no IPC or device
read) and lifts at once when it sees one. If the agent dies between down and up the contact stays down on the device
(MaaTouch keeps it); the next run reads it from the device before its first touch and refuses (DeviceInputState, the
runner's recovery).

The Windows package does not ship host/android, and Runtime.gesture raises before importing this module on any
other backend.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

from host.android import FRAME_SIZE

GESTURE_MAX_S = 1.5  # first down to last up, every gesture but the bell press (docs/android-support-plan.md)
BELL_PRESS = "bell_press"
BELL_PRESS_MAX_S = 4.5  # the 星海 bell is held about 4 s (PC: rt.hold("bell", 4)); its own, single exception
# Past its scheduled end by this much (a framework call that blocked) the gesture stops sending, lifts and counts as
# failed: its timing is no longer what was decided, and the stopper's 5 s wait must still cover the bell press.
OVERRUN_S = 0.3
CONTACTS = (0, 1)
MAX_STEPS = 32
POLL_S = 0.02  # how often a held gesture looks for a stop while it waits for its next step
KINDS = ("down", "move", "up")

DONE, ABORTED, FAILED = "done", "aborted", "failed"

# The joystick: down at the centre, then RAMP_MOVES moves RAMP_STEP_S apart out to the target (as measured in phase
# E, .build/android/phase-e-measure/measure.py), hold, up. The jump tap on contact 1 is held JUMP_TAP_S.
RAMP_MOVES = 3
RAMP_STEP_S = 0.015
JUMP_TAP_S = 0.08


@dataclass(frozen=True)
class Layout:
    """Where the movement controls are on the 1280x720 frame. The defaults were measured on BlueStacks Air with the
    default HUD (phase E research); the player can move them, so recognition is to replace these later."""
    joystick: tuple[int, int] = (212, 572)
    ring: int = 73  # the joystick ring's radius: a magnitude of 1 pushes the knob to it
    jump: tuple[int, int] = (965, 537)


DEFAULT_LAYOUT = Layout()


@dataclass(frozen=True)
class Step:
    at: float  # seconds after the gesture's first step
    kind: str  # "down", "move" or "up"
    contact: int
    point: tuple[int, int] | None = None  # down and move only


@dataclass(frozen=True)
class Gesture:
    name: str
    steps: tuple[Step, ...]

    @property
    def duration(self) -> float:
        return self.steps[-1].at

    def log_fields(self) -> dict:
        """What the input log and the failure evidence record for this gesture (times in ms)."""
        return {"gesture": self.name, "duration": round(self.duration * 1000),
                "steps": [[round(s.at * 1000), s.kind, s.contact, *(list(s.point) if s.point else [])]
                          for s in self.steps]}


def _refuse(reason: str):
    from host import UnsupportedInput
    raise UnsupportedInput(f"安卓模拟器上的手势{reason}，没有发送。")


def cap_for(gesture: Gesture) -> float:
    return BELL_PRESS_MAX_S if gesture.name == BELL_PRESS else GESTURE_MAX_S


def check_gesture(gesture) -> None:
    """Raise UnsupportedInput unless ``gesture`` is a well-formed script within its time cap: contacts 0 and 1 only,
    integer points inside the frame, steps in time order starting with a down at 0, each contact put down before it
    moves or lifts and lifted by the end, and the bell press exactly one contact down then up in place."""
    if not isinstance(gesture, Gesture) or not isinstance(gesture.name, str) or not isinstance(gesture.steps, tuple):
        _refuse("格式不对")
    steps = gesture.steps
    if not 2 <= len(steps) <= MAX_STEPS:
        _refuse(f"步数 {len(steps)} 不在 2–{MAX_STEPS} 之内")
    width, height = FRAME_SIZE
    down, previous = set(), 0.0
    for step in steps:
        if not isinstance(step, Step) or step.kind not in KINDS or type(step.contact) is not int \
                or step.contact not in CONTACTS:
            _refuse("里有不支持的一步（只能是触点 0 和 1 的按下、移动、抬起）")
        if type(step.at) not in (int, float) or not math.isfinite(step.at) or step.at < previous:
            _refuse("的时间顺序不对")
        previous = step.at
        if step.kind == "up":
            if step.point is not None:
                _refuse("的抬起不带位置")
        elif (not isinstance(step.point, tuple) or len(step.point) != 2 or any(type(v) is not int for v in step.point)
              or not (0 <= step.point[0] < width and 0 <= step.point[1] < height)):
            _refuse(f"的位置 {step.point!r} 无效或超出画面")
        if step.kind == "down":
            if step.contact in down:
                _refuse(f"在触点 {step.contact} 还按着时又按下")
            down.add(step.contact)
        elif step.contact not in down:
            _refuse(f"在触点 {step.contact} 没按下时移动或抬起")
        elif step.kind == "up":
            down.discard(step.contact)
    if steps[0].at != 0 or steps[0].kind != "down":
        _refuse("必须从第 0 秒的按下开始")
    if down:
        _refuse(f"结束时触点 {sorted(down)} 还没抬起")
    if gesture.name == BELL_PRESS and not (
            len(steps) == 2 and steps[1].kind == "up" and steps[0].contact == steps[1].contact):
        _refuse("「按铃」只能是一个触点原地按下再抬起")
    cap = cap_for(gesture)
    if not 0 < gesture.duration <= cap:
        _refuse(f"时长 {gesture.duration:.2f} 秒超过上限 {cap} 秒（或为零）")


def joystick(angle_deg, magnitude, duration_s, jump_at_s=None, layout: Layout = DEFAULT_LAYOUT) -> Gesture:
    """One movement step: the joystick (contact 0) down at its centre, pushed to ``magnitude`` (0–1 of the ring) at
    ``angle_deg`` in screen terms (0 right, 90 down, 270 up, the way measure.py measured it), held, and lifted
    ``duration_s`` after the down; with ``jump_at_s``, the jump button tapped on contact 1 that long after the down."""
    for value in (angle_deg, magnitude, duration_s) + (() if jump_at_s is None else (jump_at_s,)):
        if type(value) not in (int, float) or not math.isfinite(value):
            _refuse("的参数无效")
    if not 0 < magnitude <= 1:
        _refuse(f"的摇杆幅度 {magnitude} 不在 0–1 之内")
    ramp = RAMP_MOVES * RAMP_STEP_S
    if duration_s <= ramp:
        _refuse(f"的时长 {duration_s} 秒比推摇杆的 {ramp:.3f} 秒还短")
    cx, cy = layout.joystick
    angle = math.radians(angle_deg)
    tx, ty = cx + magnitude * layout.ring * math.cos(angle), cy + magnitude * layout.ring * math.sin(angle)
    steps = [Step(0.0, "down", 0, (cx, cy))]
    steps += [Step(k * RAMP_STEP_S, "move", 0, (round(cx + (tx - cx) * k / RAMP_MOVES),
                                                 round(cy + (ty - cy) * k / RAMP_MOVES)))
              for k in range(1, RAMP_MOVES + 1)]
    if jump_at_s is not None:
        if not (ramp <= jump_at_s and jump_at_s + JUMP_TAP_S < duration_s):
            _refuse(f"的跳跃时间 {jump_at_s} 秒要在推完摇杆之后、抬起摇杆 {JUMP_TAP_S} 秒之前")
        steps += [Step(float(jump_at_s), "down", 1, tuple(layout.jump)),
                  Step(float(jump_at_s) + JUMP_TAP_S, "up", 1)]
    steps.append(Step(float(duration_s), "up", 0))
    return Gesture("joystick", tuple(steps))


def bell_press(point, duration_s) -> Gesture:
    """The bell long-press: one contact pressed at ``point`` and released ``duration_s`` later (the only gesture
    allowed past GESTURE_MAX_S, up to BELL_PRESS_MAX_S)."""
    if type(duration_s) not in (int, float) or not math.isfinite(duration_s):
        _refuse("的参数无效")
    point = tuple(point) if isinstance(point, (list, tuple)) else point
    return Gesture(BELL_PRESS, (Step(0.0, "down", 0, point), Step(float(duration_s), "up", 0)))


def perform(controller, gesture: Gesture, abort) -> str:
    """Send a checked gesture's steps on schedule through ``controller``; DONE, ABORTED (``abort()`` became true
    while waiting for a later step) or FAILED (a call reported failure, or the gesture overran its cap). Every
    contact whose down was sent and whose up did not succeed is lifted in ``finally``, also when a call or the wait
    raises; a lift that fails makes the result FAILED. Nothing here captures, reads the device or decides."""
    held = set()
    try:
        result = _send_steps(controller, gesture, abort, held)
    finally:
        lifted = lift(controller, held)
    return result if lifted else FAILED


def _send_steps(controller, gesture, abort, held) -> str:
    start = time.monotonic()
    limit = start + cap_for(gesture) + OVERRUN_S
    for index, step in enumerate(gesture.steps):
        due = start + step.at
        while index:  # the first down follows the dispatch's own last checks at once
            if abort():
                return ABORTED
            now = time.monotonic()
            if now >= due:
                break
            time.sleep(min(POLL_S, due - now))
        if time.monotonic() > limit:
            return FAILED
        if step.kind == "down":
            held.add(step.contact)  # before the call: a down that raised may still have reached the device
            ok = _sent(controller.post_touch_down(*step.point, step.contact, 1))
        elif step.kind == "move":
            ok = _sent(controller.post_touch_move(*step.point, step.contact, 1))
        else:
            ok = _sent(controller.post_touch_up(step.contact))
            if ok:
                held.discard(step.contact)
        if not ok:
            return FAILED
    return DONE


def _sent(job) -> bool:
    return bool(job.wait().succeeded)


def lift(controller, contacts) -> bool:
    """Send an up for every contact in ``contacts`` (contact 1 first, so the last event is the final UP), one retry
    each; True when the framework reported every one done. An exception from one up does not skip the others; the
    first one is raised after all were tried."""
    ok, raised = True, None
    for contact in sorted(contacts, reverse=True):
        done = False
        for _attempt in range(2):
            try:
                done = _sent(controller.post_touch_up(contact))
            except BaseException as error:  # noqa: BLE001 - the other contacts are still lifted first
                raised = raised or error
                done = False
            if done:
                break
        if done:
            contacts.discard(contact)
        ok = ok and done
    if raised is not None:
        raise raised
    return ok


def joystick_step(rt, frame, angle_deg, magnitude, duration_s, jump_at_s=None, layout: Layout = DEFAULT_LAYOUT):
    """One joystick movement step decided on ``frame`` (Runtime.gesture's rules); see joystick()."""
    return rt.gesture(joystick(angle_deg, magnitude, duration_s, jump_at_s, layout), source_frame=frame)


def camera_drag(rt, frame, begin, end, duration_s):
    """Turn the camera: a one-contact swipe from ``begin`` to ``end`` over ``duration_s`` (at most GESTURE_MAX_S),
    the ordinary confirmed swipe (Runtime.action) decided on ``frame``."""
    if type(duration_s) not in (int, float) or not math.isfinite(duration_s) or not 0 < duration_s <= GESTURE_MAX_S:
        _refuse(f"的镜头滑动时长 {duration_s} 秒不在 0–{GESTURE_MAX_S} 秒之内")
    return rt.action("Swipe", begin=list(begin), end=list(end), duration=max(1, round(duration_s * 1000)),
                     source_frame=frame)
