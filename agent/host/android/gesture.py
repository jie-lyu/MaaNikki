"""Android: short scripted multi-contact gestures for movement (phase E), sent as one unit by Runtime.gesture.

A gesture is a fixed script of MaaFramework touch down/move/up calls on contacts 0 and 1 at offsets from its start.
Two shapes exist and nothing else is accepted (check_gesture):

- the joystick (JOYSTICK): one continuous contact-0 hold, its down first and its up last (down at the centre, a
  short ramp out to the chosen angle and magnitude, a hold, up), with at most one tap of the jump button on contact
  1 (one down, one up) strictly inside the hold;
- the bell long-press (BELL_PRESS): contact 0 pressed and released in place.

It is decided on one current, focus-checked frame, captured after the previous Android input was confirmed, and sent
whole through the same path as a tap or swipe (Runtime._dispatch_touch): the touch marker is held for all of it, its
intent is logged first, nothing is captured or decided while it runs (capture takes the same input lock), every
contact it put down is lifted in ``finally``, and the device must then show its final lift with nothing held before
anything else happens.

Limits, checked before anything is reserved or sent: at most GESTURE_MAX_S from the first down to the last up; the
bell press, at most BELL_PRESS_MAX_S. While it runs no controller job is waited on blocking: one status read per
poll, together with the local stop signals (the stop request on disk, a halt; no device read). On a stop, a failed
or rejected call or an overrun, an up is posted at once for every contact whose down was posted, before any further
status read, and the lifts are awaited at most CLEANUP_S; the device confirmation then reads until CONFIRM_S, so the
stopper's 5 s wait for the touch marker is answered with margin. Every step is submitted by its own limit (OVERRUN_S
after its scheduled time, never past the cap); a down or move must complete by then, a lift within OVERRUN_S past
the cap; a down or move still pending at a stop must be seen finished by its limit. Otherwise the gesture is an
overrun: from then on only lifts are sent and the result is unconfirmed (input halted, marker kept). A lift still
queued at the end (a slow or wedged controller) keeps the tasker from being stopped, as stopping it would clear that
lift from the controller's queue (DeviceInputState.lifts_outstanding). If the agent dies between down and up the
contact stays down on the device (MaaTouch keeps it); the next run reads it from the device before its first touch
and refuses (DeviceInputState, the runner's recovery).

The Windows package does not ship host/android, and Runtime.gesture raises before importing this module on any
other backend; camera_drag refuses off Android before it reaches Runtime.action.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

from host.android import FRAME_SIZE

GESTURE_MAX_S = 1.5  # first down to last up, every gesture but the bell press (docs/android-support-plan.md)
JOYSTICK = "joystick"
BELL_PRESS = "bell_press"
BELL_PRESS_MAX_S = 4.5  # the 星海 bell is held about 4 s (PC: rt.hold("bell", 4)); its own, single exception
# A down or move must complete within this of its scheduled time and before the cap; a lift within this past the
# cap. Later, the gesture's timing is no longer what was decided: it is an overrun (only lifts are sent after it).
OVERRUN_S = 0.3
# How long the lifts sent after a stop, a failure or an overrun are awaited. With a stop seen within POLL_S and the
# device read after it (Runtime.RELEASE_READS) this stays well inside the stopper's 5 s wait for the touch marker.
CLEANUP_S = 1.0
# The device confirmation after a gesture reads until this long after the gesture's last call (Runtime): with
# CLEANUP_S, a stop is answered in about 2.5 s plus one read's capped timeout, inside the stopper's 5 s.
CONFIRM_S = 1.5
# A lift submitted this little after its limit (timer and scheduler jitter of the poll's sleep) is still on time:
# without it a gesture exactly at its cap would overrun on every real clock. A down or move gets no slack.
TIMER_SLACK_S = 0.005
CONTACTS = (0, 1)
MAX_STEPS = 32
POLL_S = 0.02  # how often a held gesture looks for a stop while it waits for its next step
JOB_POLL_S = 0.002  # how often a posted job's status is polled (a touch job normally completes in a few ms)
KINDS = ("down", "move", "up")

DONE, ABORTED, FAILED, OVERRUN = "done", "aborted", "failed", "overrun"

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
    """Raise UnsupportedInput unless ``gesture`` is a well-formed script of one of the two shapes within its time cap:
    contacts 0 and 1 only, integer points inside the frame, steps in time order starting with a down at 0, each
    contact put down before it moves or lifts and lifted by the end; the joystick one continuous contact-0 hold (down
    first, up last) with at most one contact-1 down and up strictly inside it, the bell press contact 0 down then up
    in place. Why the shape matters: the device confirmation (Runtime._dispatch_touch) is one injected ACTION_UP after
    the frame's baseline with nothing held, which covers every contact only when contact 0 is lifted last (see
    lift)."""
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
    first, last = steps[0], steps[-1]
    if gesture.name == BELL_PRESS:
        if not (len(steps) == 2 and first.contact == 0 and last.kind == "up" and last.contact == 0):
            _refuse("「按铃」只能是触点 0 原地按下再抬起")
    elif gesture.name == JOYSTICK:
        # One continuous contact-0 hold: its only down is the first step and its only up the last.
        contact0 = [(index, step.kind) for index, step in enumerate(steps) if step.contact == 0 and step.kind != "move"]
        if contact0 != [(0, "down"), (len(steps) - 1, "up")]:
            _refuse("的摇杆必须是触点 0 一直按住：第一步按下、最后一步抬起，中间不抬起")
        # At most one jump tap: one contact-1 down and one up (no move), strictly inside the hold in time.
        contact1 = [step for step in steps if step.contact == 1]
        if contact1 and not (len(contact1) == 2 and [s.kind for s in contact1] == ["down", "up"]
                             and first.at < contact1[0].at and contact1[1].at < last.at):
            _refuse("里的触点 1 只能在摇杆按住期间按下、抬起一次（不移动，也不能比摇杆先按下或晚抬起）")
    else:
        _refuse(f"「{gesture.name}」不认识（只能是摇杆或按铃）")
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
    return Gesture(JOYSTICK, tuple(steps))


def bell_press(point, duration_s) -> Gesture:
    """The bell long-press: contact 0 pressed at ``point`` and released ``duration_s`` later (the only gesture
    allowed past GESTURE_MAX_S, up to BELL_PRESS_MAX_S)."""
    if type(duration_s) not in (int, float) or not math.isfinite(duration_s):
        _refuse("的参数无效")
    point = tuple(point) if isinstance(point, (list, tuple)) else point
    return Gesture(BELL_PRESS, (Step(0.0, "down", 0, point), Step(float(duration_s), "up", 0)))


def perform(controller, gesture: Gesture, abort, keep_queue=None) -> str:
    """Send a checked gesture's steps on schedule through ``controller``. Returns DONE; ABORTED (``abort()`` became
    true while a contact was held: it was lifted early); FAILED (a call reported failure or was rejected, or a lift
    was not reported done within CLEANUP_S); or OVERRUN (a step not submitted or completed by its limit, see
    OVERRUN_S, a job still pending at the stop seen done only after its limit, or the gesture ending past the cap's
    grace). Every contact whose down was posted and whose up was not reported done is lifted in ``finally``, also
    when a call or a wait raises; ``keep_queue()`` is called when a lift is still outstanding at the end (nothing
    may clear the controller's queue then, see DeviceInputState.lifts_outstanding). No job is waited on blocking:
    one status read per poll (_status). Nothing here captures, reads the device or decides."""
    start = time.monotonic()
    cap_end = start + cap_for(gesture)
    late_end = cap_end + OVERRUN_S
    held = {}  # contact -> the job of an up posted for it and not yet reported done (None: no up posted)
    pending = []  # (job, limit) of a down or move posted and not yet seen done: carried into the cleanup
    result, late, lifted, failed = FAILED, False, False, False
    try:
        result = _send_steps(controller, gesture, abort, held, pending, start, cap_end, late_end)
    finally:
        try:
            lifted, late, failed = lift(controller, held, pending)
        finally:
            if held and keep_queue is not None:
                keep_queue()
    if result == OVERRUN or late or time.monotonic() > late_end:
        return OVERRUN  # checked after the last call too: a lift that completed late is no confirmed gesture
    if failed or not lifted:
        return FAILED  # a job seen failed during the cleanup is never a clean stop
    return result


def _send_steps(controller, gesture, abort, held, pending, start, cap_end, late_end) -> str:
    for index, step in enumerate(gesture.steps):
        due = start + step.at
        lifting = step.kind == "up"
        # Every step is submitted by its own limit, never past the cap: a hard limit for a down or move, which must
        # also complete by then; TIMER_SLACK_S for a lift only. A lift whose post returned by then may complete
        # within the cap's grace (it may still be in flight).
        submit_by = min(due + OVERRUN_S, cap_end) + (TIMER_SLACK_S if lifting else 0.0)
        complete_by = late_end if lifting else submit_by
        # The first down follows the dispatch's own last checks at once (a stop seen here, before anything was
        # sent, would leave nothing for the device to confirm).
        stopped = _wait_until(due, submit_by, abort if index else None)
        if stopped:
            return stopped
        if time.monotonic() > submit_by:  # right before the post: the stop check above may itself have been slow
            return OVERRUN
        if step.kind == "down":
            held[step.contact] = None  # before the call: a down that raised may still have reached the device
            job = controller.post_touch_down(*step.point, step.contact, 1)
        elif step.kind == "move":
            job = controller.post_touch_move(*step.point, step.contact, 1)
        else:
            job = held[step.contact] = controller.post_touch_up(step.contact)
        if not lifting:
            pending.append((job, complete_by))
        if time.monotonic() > submit_by:  # the post itself returned late: not known to be on the controller in time
            return OVERRUN
        stopped = _await(job, complete_by, abort)
        if stopped:
            return stopped
        if lifting:
            del held[step.contact]
        else:
            pending.pop()
    return DONE


def _status(job) -> str:
    """One status read of a posted job: "done", "failed" or "pending". A job MaaFramework never queued (a rejected
    post returns the invalid id 0) or no longer knows (status invalid) counts as failed: it will never run."""
    if job is None or not getattr(job, "job_id", 0):
        return "failed"
    status = job.status
    if status.succeeded:
        return "done"
    if status.pending or status.running:
        return "pending"
    return "failed"


def _wait_until(due, limit, abort):
    """Poll (POLL_S) until ``due``: None then; OVERRUN once past ``limit``; ABORTED once ``abort()`` (when given) is
    true, checked before the step is sent."""
    while True:
        now = time.monotonic()
        if now > limit:
            return OVERRUN
        if abort is not None and abort():
            # The stop check itself may have been slow: a stop seen past the limit is an overrun, not an early stop.
            return OVERRUN if time.monotonic() > limit else ABORTED
        if time.monotonic() >= due:
            return None
        time.sleep(min(POLL_S, max(0.0, due - now)))


def _await(job, limit, abort):
    """Poll a posted job (one status read every JOB_POLL_S): None once it is done by ``limit``; FAILED; OVERRUN once
    past ``limit``, also when the read that saw it done returned after it (it proves nothing in time, and the
    overrun is checked before a stop, so a stop never masks it); ABORTED once ``abort()`` is true."""
    while True:
        if time.monotonic() > limit:
            return OVERRUN
        state = _status(job)
        if time.monotonic() > limit:
            return OVERRUN
        if state == "done":
            return None
        if state == "failed":
            return FAILED
        if abort():
            return OVERRUN if time.monotonic() > limit else ABORTED  # a slow stop check: as in _wait_until
        time.sleep(JOB_POLL_S)


def lift(controller, held, pending=()):
    """Lift every contact in ``held`` (contact -> its pending up job, or None) and return (lifted, late, failed).

    First an up is posted for every contact without one (contact 1 first, so contact 0's up comes last), before
    any status read: a stalled status read cannot delay them. Posting does not wait for jobs still pending (a down
    whose job has not completed): the controller runs posted jobs in order, so these ups follow it. Then the jobs
    are polled (one status read each per pass, never a blocking wait) until CLEANUP_S has passed: an up reported
    failed, rejected (invalid id) or whose status read raised is posted once more; a status-read exception is kept
    for that contact only, so every other lift is still attempted, and the first exception is raised at the end.
    ``lifted`` is True when every contact's up was reported done (``held`` keeps the others). ``pending`` holds the
    (job, limit) of a down or move still pending when the gesture stopped; ``late`` is True unless each was seen
    finished by its limit (a jump down that completes after the cap is an overrun, not an early stop). ``failed``
    is True when any job was seen failed or rejected here (a pending down or move, or an up before its retry): the
    gesture is then FAILED, never a clean stop.

    Why the device confirmation covers both contacts (Runtime._dispatch_touch: an injected UP after the frame's
    baseline and the last confirmed lift, then a reading with nothing held). Android reports a contact's lift as
    ACTION_POINTER_UP while another contact is still down, and as ACTION_UP only when the last contact leaves; only
    ACTION_UP is counted as the lift (host.android.target.touch_snapshot). In every shape check_gesture accepts,
    contact 0 is down from the first step to the last, so the enclosed jump's lift is a POINTER_UP and the first
    ACTION_UP after the baseline is the gesture's own final lift; MaaTouch handles commands in the order they were
    posted, so every down and move of the gesture was handled before it. Had contact 1 still been held, contact 0's
    lift would have been a POINTER_UP (no ACTION_UP), and the reading would show the held contact. The cleanup
    keeps the same order (contact 1's up is posted before contact 0's); if a retry reorders them, the last contact
    to leave still produces the only ACTION_UP and the reading after it still shows nothing held, so no
    confirmation can come from a contact that is down. (A partly delivered gesture, a lost jump down for one, is
    not detected this way: risk 113.)"""
    deadline = time.monotonic() + CLEANUP_S
    raised = None
    tries = {contact: 1 for contact, job in held.items() if job is not None}  # a scripted up is the first try

    def post(contact):
        nonlocal raised
        tries[contact] = tries.get(contact, 0) + 1
        try:
            held[contact] = controller.post_touch_up(contact)
        except BaseException as error:  # noqa: BLE001 - the other contacts are still lifted first
            raised = raised or error
            held[contact] = None

    for contact in sorted(held, reverse=True):
        if held[contact] is None:
            post(contact)
    waiting, late, failed, given_up = list(pending), False, False, set()
    while True:
        for contact in sorted(held, reverse=True):
            if contact in given_up:
                continue
            try:
                state = _status(held[contact])
            except BaseException as error:  # noqa: BLE001 - this contact only; the others are still lifted
                raised, state = raised or error, "failed"
            if state == "done":
                del held[contact]
            elif state == "failed":
                failed = True
                if tries.get(contact, 0) < 2:
                    post(contact)  # failed, rejected, or its status unreadable: once more
                else:
                    given_up.add(contact)
        for item in list(waiting):
            job, limit = item
            try:
                state = _status(job)
            except BaseException as error:  # noqa: BLE001 - unknown: it may still be running
                raised, state = raised or error, "pending"
            if time.monotonic() > limit:
                late = True  # not seen finished by its limit
                waiting.remove(item)
            elif state != "pending":
                failed = failed or state == "failed"  # a known controller failure is kept, never a clean stop
                waiting.remove(item)
        if (set(held) <= given_up and not waiting) or time.monotonic() >= deadline:
            break
        time.sleep(JOB_POLL_S)
    late = late or bool(waiting)
    if raised is not None:
        raise raised
    return not held, late, failed


def confirmation_clock():
    """For Runtime's device confirmation after a gesture: (now, sleep, deadline) on this module's clock, the reads
    to end by CONFIRM_S from now. With CLEANUP_S before it, a stop seen during a gesture is answered (marker
    released or kept) well inside the stopper's 5 s, device reads and status RPCs included (each read's own
    timeout is capped by the time left); a status RPC that itself stalls for seconds is risk 112."""
    return (lambda: time.monotonic()), (lambda seconds: time.sleep(seconds)), time.monotonic() + CONFIRM_S


def _refuse_off_android(frame):
    from host import UnsupportedInput, capabilities, game_backend
    if not capabilities(game_backend()).touch_actions_only:
        raise UnsupportedInput("移动和镜头手势只在安卓模拟器上使用，这里不发送。")
    if frame is None:
        raise UnsupportedInput("安卓模拟器上的手势必须说明它依据的是哪一张截图，这一步暂不支持。")


class DecisionExpired(Exception):
    """A gesture's ``not_after`` (time.monotonic) passed before its first contact went down: nothing was sent and
    nothing is halted; the caller captures a new frame and decides again."""


def joystick_step(rt, frame, angle_deg, magnitude, duration_s, jump_at_s=None, layout: Layout = DEFAULT_LAYOUT, *,
                  not_after=None):
    """One joystick movement step decided on ``frame`` (Runtime.gesture's rules); see joystick(). ``not_after``: the
    deciding frame's expiry (time.monotonic); past it, right before contact-down, DecisionExpired and nothing sent."""
    return rt.gesture(joystick(angle_deg, magnitude, duration_s, jump_at_s, layout), source_frame=frame,
                      not_after=not_after)


def bell_step(rt, frame, point, duration_s, *, not_after=None):
    """The bell long-press decided on ``frame`` (Runtime.gesture's rules, ``not_after`` as in joystick_step)."""
    return rt.gesture(bell_press(point, duration_s), source_frame=frame, not_after=not_after)


def camera_drag(rt, frame, begin, end, duration_s):
    """Turn the camera: a one-contact swipe from ``begin`` to ``end`` over ``duration_s`` (at most GESTURE_MAX_S),
    the ordinary confirmed swipe (Runtime.action) decided on ``frame``, with the gesture freshness rule. Android
    only, with a source frame: refused before Runtime.action otherwise (a desktop swipe must never come from
    here)."""
    _refuse_off_android(frame)
    if type(duration_s) not in (int, float) or not math.isfinite(duration_s) or not 0 < duration_s <= GESTURE_MAX_S:
        _refuse(f"的镜头滑动时长 {duration_s} 秒不在 0–{GESTURE_MAX_S} 秒之内")
    # _movement: the gesture freshness rule (Runtime._dispatch_touch): decided on a frame captured after the last
    # input, and counted as a gesture, so no later input uses a frame from before the camera turned.
    return rt.action("Swipe", begin=list(begin), end=list(end), duration=max(1, round(duration_s * 1000)),
                     source_frame=frame, _movement=True)
