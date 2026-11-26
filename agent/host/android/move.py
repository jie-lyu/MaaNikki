"""Android movement backend (phase E): walk the character to a point, or along route points, with joystick steps.

Every step is decided on a frame captured just before it and sent as one gesture (host.android.gesture.joystick_step,
the only touch this module sends; the bell press, off by default, is the one other gesture):

1. Look: capture a frame and classify it (Io.observe, classify). Move only when the frame is the open-world HUD and
   nothing else: the HUD positively recognised (host.android.actions.read_screen), the frame's focus mark current and
   no contact held when it was captured, and full-frame OCR reading no text outside the HUD's own text regions, each
   region showing only what it may (HUD_REGIONS: the label row's labels, the 心誓 bar, the masked UID and ping, the
   icon badges' numbers, and the quest tracker as it was on the walk's first frame). The first frame must be such a
   clean HUD or nothing is sent. The one exception is ambient narration: a known companion's label (AMBIENT_SPEAKERS)
   over one short plain line in the subtitle band, no marker, no word of NARRATION_DENY: it pauses without input until
   it goes (logged), up to NARRATION_MAX_S. Anything else (a prompt, a banner, a quest or interaction button, an
   overlay glyph, a missing minimap chevron, another page, a dialog, the quit confirmation, an unrecognised or dark
   screen) ends the walk on that first frame, saved, with nothing tapped. Only a capture that returned no image is
   tried again, once.
2. Fix: the minimap position (host.android.minimap). The first fix needs a caller-supplied hint inside the map (later
   the teleport point's coordinates); no hint, or a hinted match under the score floor or far from the hint, refuses
   before any input, and so does a fix outside a route's start radius (the first fix and the one that authorises the
   first gesture). Later fixes match around the predicted position and must be plausible for the step just made
   (PLAUSIBLE_FACTOR x the expected distance + PLAUSIBLE_SLACK_U from the step's origin) and within REFIX_SHIFT_U of
   the first fix after the last input (the whole no-input interval); a bad fix gets one wider retry on a fresh, settled
   frame, then the walk stops.
3. Plan: world bearing to the goal (0 = map up, clockwise; map y grows down) and the camera yaw read from the minimap's
   view cone on the same frame give the joystick's screen angle: screen_angle = bearing - yaw + 270 (screen angles: 0
   right, 90 down; the camera looks "up" the screen, 270). The joystick is always at full deflection; the distance is
   set by the duration from the measured model expected_distance(T) = SPEED_U_S * (T - DEAD_S), clamped to
   [MIN_STEP_S, MAX_STEP_S] and to the time left before the deadline. The camera is never turned. A decision is sent
   with its frame's expiry (capture requested + MAX_DECISION_AGE_S, and early enough that its hold ends before the
   deadline: gesture ``not_after``): Runtime refuses it right before
   contact-down once expired (DecisionExpired) and the walk captures again and replans; the bell follows the same rule
   and starts only when its whole press fits before the deadline.
4. Settle: after a lift the minimap keeps easing for 0.6-0.9 s. A frame captured right after a step therefore shows
   the position up to LAG_U short along the step (2.8 u at most on the device frames). Instead of always waiting, the
   walk waits only when the lag could change the decision: near a goal that must be reached precisely (the last
   point, or one with an action), i.e. when remaining - LAG_U <= REACH_U, it waits until SETTLE_S after the lift and
   decides on a fresh frame. Far from such a goal the next step is a full-length step toward it with or without the
   lag, and an intermediate route point counts as passed when the segment between two fixes comes within its radius
   (navigation.geometry), which a lagging fix cannot hide. Arrival at a precise goal is judged on a settled frame.
5. Stop conditions: STALL_STEPS consecutive steps with almost no displacement (blocked), no progress toward the goal
   in GOAL_STALL_STEPS steps, the deadline (it bounds every step's duration and every wait, and is checked before
   every gesture and point action and after the last write before reporting arrival), leaving the leash, an
   unreadable camera yaw twice in a row, a stop (the gesture returned False, or the run stopped, also right before
   reporting arrival): each returns at once, with no further input; failures save the frame.

Step records (Io.record) are written only after the gesture's lift was confirmed (or when the walk ends), never
between a decision and its gesture.

Measured facts used here are in docs/android-support-plan.md (阶段 E, the 09:10 handover) and
.build/android/phase-e-measure (m2-* tables, m2-settle, m2-goto2: the screen-angle formula moved the character along
the computed bearing within a few degrees on the device).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
import re
import time

from host.android.gesture import DecisionExpired
from navigation.geometry import segment_distance

# ---- the measured model ----
# Displacement at full deflection (m2-fb10, m2-rl10b): 0.5 s -> ~1.2 u, 1.0 s -> 4.2-4.8 u, 1.5 s -> 6.6-7.9 u. A
# least-squares line through the three means is d = 6.2 * (T - 0.3): 0.3 s of every step is ramp and acceleration.
SPEED_U_S = 6.2
DEAD_S = 0.3
MAX_STEP_S = 1.5  # the gesture cap (docs: 每一步 ≤ 1.5 秒)
# The shortest step: with 0.3 s of dead time a 0.4 s step still moves predictably (~0.6 u, under the smallest route
# radius in use, 0.95 u, so a final approach can land inside); anything shorter pays the ~0.84 s fixed cost per step
# for almost no movement.
MIN_STEP_S = 0.4
REACH_U = SPEED_U_S * (MAX_STEP_S - DEAD_S)  # the farthest one step goes (~7.4 u)
# After the lift the minimap is still catching up: settled 0.6-0.9 s after it (m2-settle). A frame captured right
# after the lift confirmation (~0.3 s) was short of the settled position by 0.95 u (median) and 2.8 u (max) over 74
# full-deflection steps (scripts/android/move_frames.py).
SETTLE_S = 0.9
LAG_U = 3.0
ARRIVAL_U = 1.8  # the default arrival radius for a single point (the route points carry their own)

# ---- trust ----
FIRST_FIX_RADIUS = 40.0  # the first fix must lie this close to the hint (a teleport lands within a few units)
TRACK_SEARCH = 12  # around the predicted position (the Locator's window is never under ~45 u anyway)
WIDE_SEARCH = 75  # the one wider retry, around the last good fix
PLAUSIBLE_FACTOR = 1.3  # the farthest measured step was ~1.1x the model
PLAUSIBLE_SLACK_U = 3.0  # the previous step's lag plus the match spread
REFIX_SHIFT_U = LAG_U + 0.5  # without input in between, a new fix moves at most the lag plus the match spread
# From before a frame's capture was requested (Observation.at: earlier than the screencap's submission) to its
# gesture's contact-down: the capture (~0.25 s on the device), classification (read_screen, full-frame OCR) and the
# fix (~0.2 s offline) fit well inside; older, the screen may have changed unseen. Each step logs its decision age.
MAX_DECISION_AGE_S = 1.2
STALE_DECISIONS = 3  # consecutive decisions that aged out: the walk stops
DEADLINE_MARGIN_S = 0.5  # a step ends at least this long before the deadline (lift confirmation, the next capture)
LIFT_MARGIN_S = 0.2  # a gesture must start early enough that its hold plus this ends before the deadline (not_after)
STARTUP_WAIT_S = 5.0  # how long the start waits for a clean HUD frame (an allowed remark going, the distance read)

# ---- stops ----
STALL_STEPS = 3
STALL_MIN_U = 0.4
STALL_FRACTION = 0.15  # a step "barely moved" below max(STALL_MIN_U, this x its expected distance)
GOAL_STALL_STEPS = 8
GOAL_PROGRESS_U = 0.5
HEADING_MISSES = 2
BLANK_RETRIES = 1  # a capture that returned no image is tried again once; every returned image is judged
NARRATION_MAX_S = 20.0
POLL_S = 0.4
JUMP_AT_S = 0.35  # a jump tap after the ramp, when a route point asks for one (allow_jump)


class Halt(Exception):
    """Ends the walk with ``result``; raised inside the loop only."""

    def __init__(self, result):
        super().__init__(result.message)
        self.result = result


@dataclass(frozen=True)
class Goal:
    x: float
    y: float
    radius: float = ARRIVAL_U
    jump: bool = False
    action: str = ""  # "" (pass through), "wait" (params.seconds, no input) or "bell"
    params: dict = field(default_factory=dict)


@dataclass
class Observation:
    """One classified frame. ``kind``: "hud" (move), "narration" (pause), "blank" (the capture returned no image:
    tried again once) or "other" (stop; ``reason`` says what, a fixed Chinese text). ``at``: the clock when its capture
    started; ``captured``: when it ended (the decision's age counts from there; ``at`` when not given). ``regions``:
    Verdict.regions, compared with the walk's first frame."""
    frame: object
    kind: str
    at: float
    reason: str = ""
    captured: float | None = None
    regions: tuple = ()
    blocks: tuple = ()  # Verdict.blocks

    def __post_init__(self):
        if self.captured is None:
            self.captured = self.at


@dataclass
class WalkResult:
    outcome: str  # arrived, stopped, refused, blocked, timeout, lost, screen, leash
    message: str  # for the user (Chinese, no OCR text)
    position: tuple[float, float] | None = None
    steps: list = field(default_factory=list)  # every attempted step's record ("confirmed": the gesture completed)
    saved: object = None  # what Io.save returned for the frame kept with a failure

    @property
    def ok(self) -> bool:
        return self.outcome == "arrived"

    @property
    def attempted(self) -> int:
        return len(self.steps)

    @property
    def confirmed(self) -> int:
        return sum(1 for step in self.steps if step.get("confirmed"))


# ---- pure geometry ----

def bearing(dx: float, dy: float) -> float:
    """World bearing of a map displacement: 0 = map up (y decreasing), 90 = map right, clockwise, in [0, 360)."""
    return math.degrees(math.atan2(dx, -dy)) % 360


def screen_angle(world_bearing: float, camera_yaw: float) -> float:
    """The joystick's screen angle (0 right, 90 down) that walks along ``world_bearing`` when the camera looks along
    ``camera_yaw`` (same convention as bearing). Pushing the stick up (270) walks where the camera looks."""
    return (world_bearing - camera_yaw + 270) % 360


def step_seconds(distance: float) -> float:
    """The full-deflection duration expected to cover ``distance`` map units, clamped to the step limits."""
    return min(MAX_STEP_S, max(MIN_STEP_S, DEAD_S + max(0.0, distance) / SPEED_U_S))


def expected_distance(seconds: float) -> float:
    return SPEED_U_S * max(0.0, seconds - DEAD_S)


def plausible_distance(seconds: float) -> float:
    """The largest believable displacement between the fixes before and after a step of ``seconds``."""
    return PLAUSIBLE_FACTOR * expected_distance(seconds) + PLAUSIBLE_SLACK_U


def _distance(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class _P:  # what navigation.geometry expects: .x and .y
    __slots__ = ("x", "y")

    def __init__(self, xy):
        self.x, self.y = float(xy[0]), float(xy[1])


STOPPED = "已停止。"


# ---- the walk ----

class Walker:
    """One walk on one map. ``io`` is the device binding (RuntimeIo; walk_probe.DiagnosticIo for the dev walk);
    ``reader`` a minimap.MinimapReader (or anything with ``present(frame)``, ``within(x, y)`` and
    ``fix(frame, hint, search_radius)``)."""

    def __init__(self, io, reader, *, clock=time.monotonic, allow_bell: bool = False, allow_jump: bool = False,
                 leash=None):
        self.io, self.reader, self.clock = io, reader, clock
        self.allow_bell, self.allow_jump = allow_bell, allow_jump
        self.leash = leash  # (x, y, radius): every fix must stay inside, the dev walk's safety fence
        self.steps: list[dict] = []
        self.position = None
        self.deadline = None
        self._lift_at = None  # the clock when the last gesture returned (its lift confirmed)
        self._last_step = None  # (origin fix, seconds, bearing) of the last step
        self._pending = []  # (kind, record, frame) to write once no decision is waiting for its gesture
        self._anchor = None  # the first fix after the last input: no-input rechecks stay within REFIX_SHIFT_U of it
        self._gestured = False  # any gesture (step or bell) sent yet
        self._start = (None, None)  # (first goal, start radius) for the guard before the first gesture
        self._regions = None  # the first frame's quest name (Verdict.regions)
        self._blocks = None  # the first frame's permanent-HUD text block layout (Verdict.blocks)

    # -- entry --

    def walk(self, goals, hint, timeout: float, *, start_radius: float | None = None) -> WalkResult:
        """Walk through ``goals`` from the position near ``hint`` within ``timeout`` seconds. ``start_radius`` (a
        route's): the first trusted fix must lie this close to the first goal, else nothing is sent."""
        goals = [goal if isinstance(goal, Goal) else Goal(*goal) for goal in goals]
        try:
            if not goals:
                raise Halt(WalkResult("refused", "没有要去的位置，不开始移动。"))
            result = self._walk(goals, hint, timeout, start_radius)
        except Halt as halt:
            result = halt.result
        finally:
            self._flush()
        result.position = result.position or self.position
        result.steps = list(self.steps)
        return result

    def _walk(self, goals, hint, timeout, start_radius):
        if hint is None:
            raise Halt(WalkResult("refused", "没有起点位置的提示，不从猜测的位置开始移动。"))
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (*hint, *(c for g in goals
                                                                                       for c in (g.x, g.y)))):
            raise Halt(WalkResult("refused", "起点或目标的坐标无效，没有开始移动。"))
        if not self.reader.within(*hint) or not all(self.reader.within(g.x, g.y) for g in goals):
            raise Halt(WalkResult("refused", "起点或目标的坐标在地图之外，没有开始移动。"))
        if any(goal.action == "bell" for goal in goals) and not self.allow_bell:
            raise Halt(WalkResult("refused", "路线上有按铃的点，按铃在安卓模拟器上还没有开放，没有开始移动。"))
        if self.leash is not None and any(_distance((g.x, g.y), self.leash[:2]) > self.leash[2] for g in goals):
            raise Halt(WalkResult("refused", "目标超出了允许的范围，没有开始移动。"))
        self.deadline = self.clock() + timeout
        obs = self._first_look()
        obs, fix = self._first_fix(obs, hint)
        self._start = (goals[0], start_radius)
        self._check_start(obs, fix, goals[0], start_radius)
        index = 0
        stalls, best, since_best, heading_misses, stale = 0, math.inf, 0, 0, 0
        previous = None  # the fix before the last step, for passing intermediate points
        while True:
            self._check_stop()
            goal = goals[index]
            here = (fix.x, fix.y)
            last = index == len(goals) - 1
            precise = last or bool(goal.action)
            # An intermediate point is passed when this fix, or the way from the previous one, comes within its radius.
            if not precise and (_distance(here, (goal.x, goal.y)) <= goal.radius or (
                    previous is not None and segment_distance(_P((goal.x, goal.y)), _P(previous), _P(here))
                    <= goal.radius)):
                self._detail(f"passed point {index}")
                index, best, since_best = index + 1, math.inf, 0
                continue
            remaining = _distance(here, (goal.x, goal.y))
            if precise and remaining - LAG_U <= REACH_U and not self._settled(obs):
                obs, fix = self._settle(obs, fix)
                continue
            if precise and remaining <= goal.radius:
                self._detail(f"arrived at point {index} ({remaining:.2f} u)")
                if not goal.action:
                    return self._arrived(obs, here)
                self._check_deadline(obs)
                obs, fix, done = self._point_action(goal, obs, fix)
                if not done:  # a decision that aged out: judged again on the fresh frame
                    continue
                if last:  # judged again on the fresh frame the action ended with (the action is not repeated)
                    goals[index] = replace(goal, action="")
                else:
                    index, best, since_best, previous = index + 1, math.inf, 0, None
                continue
            self._check_deadline(obs)
            if remaining < best - GOAL_PROGRESS_U:
                best, since_best = remaining, 0
            elif since_best >= GOAL_STALL_STEPS:
                self._fail("blocked", "连续多步没有靠近目标（可能被挡住了），已停止", obs)
            if fix.heading is None:
                heading_misses += 1
                if heading_misses >= HEADING_MISSES:
                    self._fail("lost", "读不出镜头朝向，已停止", obs)
                self._pause(POLL_S)
                obs, fix = self._refix(self._look(), fix)
                continue
            heading_misses = 0
            # Plan on this frame; send only while the frame is recent enough to stand for the screen.
            world = bearing(goal.x - fix.x, goal.y - fix.y)
            seconds = step_seconds(remaining)
            angle = screen_angle(world, fix.heading)
            jump_at = None
            if goal.jump and self.allow_jump and expected_distance(seconds) >= remaining - goal.radius \
                    and seconds > JUMP_AT_S + 0.2:
                jump_at = JUMP_AT_S
            if not self._gestured:  # the fix that authorises the first gesture, whichever frame it came from
                self._check_start(obs, fix, goals[0], start_radius)
            # The deadline bounds the step itself: it ends DEADLINE_MARGIN_S before it, or is not made.
            self._check_deadline(obs)
            seconds = min(seconds, self.deadline - self.clock() - DEADLINE_MARGIN_S)
            if seconds < MIN_STEP_S:
                self._fail("timeout", "移动超时（剩下的时间不够走一步），没有到达目标位置，已停止", obs)
            if jump_at is not None and not (expected_distance(seconds) >= remaining - goal.radius
                                            and seconds > JUMP_AT_S + 0.2):
                jump_at = None  # the shortened step no longer reaches the jump point
            age = self.clock() - obs.at
            if age > MAX_DECISION_AGE_S:
                stale = self._stale(stale, age, obs)
                obs, fix = self._refix(self._look(), fix)
                continue
            self._check_stop()
            record = {"step": len(self.steps), "goal": index, "fix": fix.log(), "remaining": round(remaining, 2),
                      "bearing": round(world, 1), "angle": round(angle, 1), "seconds": round(seconds, 3),
                      "jump_at": jump_at, "expected": round(expected_distance(seconds), 2),
                      "decision_age": round(age, 3), "confirmed": False}
            try:  # the frame's expiry travels to the last check before contact-down (Runtime._dispatch_touch)
                sent = self.io.step(obs.frame, angle, seconds, jump_at, not_after=self._not_after(obs, seconds))
            except DecisionExpired:
                stale = self._stale(stale, self.clock() - obs.at, obs)
                obs, fix = self._refix(self._look(), fix)
                continue
            stale = 0
            self._lift_at = self.clock()
            self._anchor, self._gestured = None, True
            record["confirmed"] = bool(sent)
            self.steps.append(record)
            self._pending.append(("step", record, obs.frame))
            self._flush()
            if not sent:
                raise Halt(WalkResult("stopped", STOPPED))
            self._check_stop()
            origin = here
            self._last_step = (origin, seconds, world)
            obs, fix = self._track(self._look(), origin, seconds, world)
            moved = _distance(origin, (fix.x, fix.y))
            self._pending.append(("moved", {"step": record["step"], "moved": round(moved, 2), "after": fix.log(),
                                            "settled": self._settled(obs)}, None))
            record.update(moved=round(moved, 2), after=fix.log())
            stalls = stalls + 1 if moved < max(STALL_MIN_U, STALL_FRACTION * expected_distance(seconds)) else 0
            since_best += 1
            if stalls >= STALL_STEPS:
                if not self._settled(obs):  # judge "blocked" on a settled frame only
                    obs, fix = self._settle(obs, fix)
                    moved = _distance(origin, (fix.x, fix.y))
                    self._pending.append(("moved", {"step": record["step"], "moved": round(moved, 2),
                                                    "after": fix.log(), "settled": True}, None))
                    record.update(moved=round(moved, 2), after=fix.log())
                if moved < max(STALL_MIN_U, STALL_FRACTION * expected_distance(seconds)):
                    self._fail("blocked", f"连续 {STALL_STEPS} 步几乎没有移动（可能被挡住了），已停止", obs)
                stalls = 0
            previous = origin

    def _arrived(self, obs, here):
        self._flush()  # the last writes first: the deadline and the stop are checked after them
        self._check_deadline(obs)
        self._check_stop()
        return WalkResult("arrived", "已到达目标位置。", position=here)

    def _check_start(self, obs, fix, first, start_radius):
        if start_radius is not None and first is not None \
                and _distance((fix.x, fix.y), (first.x, first.y)) > start_radius:
            self._fail("refused", "起点离路线的第一个点太远（超出路线规定的起点范围），没有开始移动", obs,
                       details={"fix": fix.log(), "start_radius": start_radius})

    def _not_after(self, obs, hold) -> float:
        """The latest contact-down for a gesture of ``hold`` seconds decided on ``obs``: within the frame's age limit
        (from before its capture was requested) and early enough that the hold ends LIFT_MARGIN_S before the
        deadline."""
        return min(obs.at + MAX_DECISION_AGE_S, self.deadline - hold - LIFT_MARGIN_S)

    def _stale(self, stale, age, obs) -> int:
        stale += 1
        self._detail(f"decision {age:.2f} s after its capture: captured again ({stale})")
        if stale >= STALE_DECISIONS:
            self._fail("screen", "连续几次判断用的时间太长，截图已经不能代表当前画面，已停止", obs)
        return stale

    # -- looking --

    def _check_stop(self):
        if self.io.stopped():
            raise Halt(WalkResult("stopped", STOPPED))

    def _check_deadline(self, obs):
        if self.deadline is not None and self.clock() >= self.deadline:
            self._fail("timeout", "移动超时，没有到达目标位置，已停止", obs)

    def _pause(self, seconds, obs=None):
        """Wait without input, never past the deadline (then the walk times out with ``obs`` saved)."""
        if self.deadline is not None:
            seconds = min(seconds, max(0.0, self.deadline - self.clock()))
        if seconds > 0 and not self.io.pause(seconds):
            raise Halt(WalkResult("stopped", STOPPED))
        self._check_stop()
        if obs is not None:
            self._check_deadline(obs)

    def _first_look(self) -> Observation:
        """The walk's first frame: the open-world HUD and nothing else, with the minimap chevron and the quest tracker
        showing one quest name and its distance; that name is what later frames must keep showing. While an allowed
        ambient remark is on screen, or the tracker's distance is not read yet, the walk waits (STARTUP_WAIT_S,
        within the deadline) for such a frame; anything else, or no clean frame in time, refuses with nothing sent."""
        since, blanks, logged = self.clock(), 0, False
        while True:
            self._check_stop()
            obs = self.io.observe()
            if obs is None:
                raise Halt(WalkResult("stopped", STOPPED))
            if obs.kind == "blank":
                blanks += 1
                if blanks > BLANK_RETRIES:
                    self._fail("refused", "连续两次没有拿到可用的截图，没有开始移动", obs)
                self._pause(POLL_S, obs)
                continue
            if obs.kind not in ("hud", "narration", "misread"):
                self._fail("refused", f"开始时画面不是干净的大世界（{obs.reason or '认不出的画面'}），没有开始移动", obs)
            if not self.reader.present(obs.frame):
                self._fail("refused", "没有在默认位置认出小地图（界面布局可能改过，或被挡住了），没有开始移动", obs)
            names, distances = self._tracker_parts(obs)
            if len(names) > 1 or distances > 1 or any(word in name for name in names for word in NARRATION_DENY):
                self._fail("refused", "开始时任务栏的位置有多余的文字，不是干净的大世界，没有开始移动", obs)
            if obs.kind == "hud" and len(names) == 1 and distances == 1:
                self._regions, self._blocks = names[0], obs.blocks
                return obs
            if obs.kind == "narration" and not logged:
                logged = True
                self.io.log("开始时画面上有一行环境旁白，等它消失再走。")
            if self.clock() - since >= STARTUP_WAIT_S:
                self._fail("refused", "开始时一直没有等到干净的大世界画面（任务栏没有显示任务名和距离，或旁白没有消失），"
                                      "没有开始移动", obs)
            self._pause(POLL_S, obs)

    @staticmethod
    def _tracker_parts(obs):
        names = [entry for entry in obs.regions if entry != TRACKER_DISTANCE]
        return names, len(obs.regions) - len(names)

    def _tracker_kept(self, obs) -> bool:
        """The quest tracker still shows the first frame's quest name exactly (after tracker_name's 呜/鸣 and
        punctuation), at most its distance besides, nothing else."""
        names, distances = self._tracker_parts(obs)
        return names == [self._regions] and distances <= 1

    def _look(self) -> Observation:
        """A frame that is the open-world HUD and nothing else, or one with allowed narration waited through; any
        other screen ends the walk on its first frame, saved. A failed or blank capture is tried again once."""
        narration_since, blanks, misread = None, 0, False
        while True:
            self._check_stop()
            obs = self.io.observe()
            if obs is None:
                raise Halt(WalkResult("stopped", STOPPED))
            hud_like = obs.kind in ("hud", "narration", "misread")
            if hud_like and not self.reader.present(obs.frame):  # before any exception
                self._fail("screen", "小地图上的位置标记不见了（可能有东西挡住了大世界），已停止所有输入", obs)
            if hud_like and self._regions is not None and not self._tracker_kept(obs):
                self._fail("screen", "大世界界面上的任务栏文字变了（可能出现了新的提示或任务），已停止所有输入", obs)
            if misread and obs.kind != "hud":  # after a misread frame only a frame that passes fully goes on
                self._fail("screen", "大世界常驻界面的文字连续两次没有按原样读出，已停止所有输入", obs)
            if obs.kind == "misread":
                # Only a permanent-HUD block's string differs, its blocks exactly where the first frame had them:
                # an OCR misread. The frame is kept, nothing is decided on it, and one fresh frame must pass fully.
                if self._blocks is None or not same_blocks(obs.blocks, self._blocks):
                    self._fail("screen", "大世界常驻界面的文字和开始时不一样，已停止所有输入", obs)
                misread = True
                self.io.save("android_move_misread", "大世界常驻界面的文字没有按原样读出，不在这张图上走，重新截一张", obs.frame,
                             {"position": self.position, "steps": len(self.steps)})
                self._detail("a permanent-HUD text block misread: frame saved, captured once more")
                self._check_deadline(obs)
                self._pause(POLL_S, obs)
                continue
            if obs.kind == "hud":
                if narration_since is not None:
                    self.io.log("环境旁白已消失，继续移动。")
                return obs
            if obs.kind == "blank":
                blanks += 1
                if blanks > BLANK_RETRIES:
                    self._fail("screen", "连续两次没有拿到可用的截图，已停止所有输入", obs)
            elif obs.kind == "narration":
                if narration_since is None:
                    narration_since = self.clock()
                    self.io.log("画面上有一行环境旁白，暂停移动，等它消失。")
                elif self.clock() - narration_since >= NARRATION_MAX_S:
                    self._fail("screen", f"环境旁白 {NARRATION_MAX_S:.0f} 秒没有消失，已停止", obs)
            else:
                self._fail("screen", f"移动时出现了{obs.reason or '认不出的画面'}，已停止所有输入", obs)
            self._check_deadline(obs)
            self._pause(POLL_S, obs)

    def _settled(self, obs) -> bool:
        return self._lift_at is None or obs.at - self._lift_at >= SETTLE_S

    def _settle(self, obs, fix):
        """Wait until SETTLE_S after the last lift and fix on a fresh frame."""
        self._check_deadline(obs)
        wait = SETTLE_S - (self.clock() - self._lift_at)
        self._detail(f"settle wait {max(0.0, wait):.2f} s")
        self._pause(wait, obs)
        return self._refix(self._look(), fix)

    def _refix(self, obs, fix):
        """A fix on a new frame without input since the last one (a settle, a retry, a point action): plausible for
        the last step when there was one, and within REFIX_SHIFT_U of the first fix after the last input (the whole
        no-input interval, not only the fix before)."""
        if self._last_step is not None:
            return self._track(obs, *self._last_step)
        return self._track(obs, (fix.x, fix.y), 0.0, None)

    # -- fixing --

    def _first_fix(self, obs, hint):
        hint = (float(hint[0]), float(hint[1]))
        if not self.reader.present(obs.frame):
            self._fail("refused", "没有在默认位置认出小地图（界面布局可能改过），没有开始移动", obs)
        fix = self.reader.fix(obs.frame, hint, FIRST_FIX_RADIUS)
        if not fix.reliable or _distance((fix.x, fix.y), hint) > FIRST_FIX_RADIUS:
            self._fail("refused", "在提示的起点附近认不出小地图上的位置，不从猜测的位置开始移动", obs,
                       details={"hint": list(hint), "fix": fix.log()})
        self._pending.append(("first_fix", {"hint": list(hint), "fix": fix.log()}, obs.frame))
        self._anchor = (fix.x, fix.y)
        return self._accept(obs, fix)

    def _good(self, fix, origin, seconds, anchor) -> bool:
        """Trusted, plausible for the last step, and without input since the anchor: within REFIX_SHIFT_U of both the
        anchor and the preceding accepted fix (two false peaks on either side of the anchor cannot add up)."""
        here = (fix.x, fix.y)
        return fix.reliable and _distance(here, origin) <= plausible_distance(seconds) and (
            anchor is None or (_distance(here, anchor) <= REFIX_SHIFT_U
                               and _distance(here, self.position) <= REFIX_SHIFT_U))

    def _track(self, obs, origin, seconds, world):
        """Fix ``obs`` after a step of ``seconds`` along ``world`` from ``origin``, and within REFIX_SHIFT_U of the
        first fix after the last input (self._anchor; set by the first fix accepted after an input). A frame without
        the minimap's player chevron stops the walk (something covers the HUD: no position retry); a bad fix gets one
        wider retry on a fresh settled frame, then the walk stops. Returns (obs, fix): the frame it used."""
        anchor = self._anchor
        if world is None:
            predicted = origin
        else:
            reach = expected_distance(seconds)
            predicted = (origin[0] + reach * math.sin(math.radians(world)),
                         origin[1] - reach * math.cos(math.radians(world)))
        if anchor is not None:
            predicted = anchor
        self._require_minimap(obs)
        fix = self.reader.fix(obs.frame, predicted, TRACK_SEARCH)
        if self._good(fix, origin, seconds, anchor):
            return self._accept(obs, fix)
        self._detail(f"bad fix {fix.log()} near {predicted}; one wider retry")
        if self._lift_at is not None:
            self._pause(SETTLE_S - (self.clock() - self._lift_at), obs)
        obs = self._look()
        self._require_minimap(obs)
        fix = self.reader.fix(obs.frame, anchor or origin, WIDE_SEARCH)
        if self._good(fix, origin, seconds, anchor):
            return self._accept(obs, fix)
        self._fail("lost", "小地图定位不可信（匹配分数太低或位置跳变），已停止", obs,
                   details={"origin": list(origin), "anchor": list(anchor) if anchor else None,
                            "fix": fix.log() if fix else None})

    def _require_minimap(self, obs):
        if not self.reader.present(obs.frame):
            self._fail("screen", "小地图上的位置标记不见了（可能有东西挡住了大世界），已停止所有输入", obs)

    def _accept(self, obs, fix):
        if self.leash is not None and _distance((fix.x, fix.y), self.leash[:2]) > self.leash[2]:
            self._fail("leash", "位置超出了允许的范围，已停止", obs, details={"fix": fix.log()})
        self.position = (fix.x, fix.y)
        if self._anchor is None:
            self._anchor = (fix.x, fix.y)
        return obs, fix

    # -- point actions --

    def _point_action(self, goal, obs, fix):
        if goal.action == "wait":
            seconds = float((goal.params or {}).get("seconds", 0) or 0)
            self._pause(min(max(seconds, 0.0), 10.0), obs)
            return (*self._refix(self._look(), fix), True)
        if goal.action == "bell":
            if not self.allow_bell:
                self._fail("refused", "这一步要按铃，按铃在安卓模拟器上还没有开放，已停止", obs)
            press = float(getattr(self.io, "BELL_S", 4.0))
            if self.deadline - self.clock() < press + DEADLINE_MARGIN_S:  # the whole press must fit
                self._fail("timeout", "移动超时（剩下的时间不够按完铃），已停止", obs)
            if not self._gestured:  # a bell as the first gesture takes the route's start guard too
                self._check_start(obs, fix, *self._start)
            if self.clock() - obs.at > MAX_DECISION_AGE_S:  # the same age rule as a step: decide again
                return (*self._refix(self._look(), fix), False)
            self._check_stop()
            try:
                sent = self.io.bell(obs.frame, not_after=self._not_after(obs, press))
            except DecisionExpired:
                return (*self._refix(self._look(), fix), False)
            self._lift_at = self.clock()
            self._anchor, self._gestured = None, True
            self._pending.append(("bell", {"fix": fix.log(), "confirmed": bool(sent)}, obs.frame))
            self._flush()
            if not sent:
                raise Halt(WalkResult("stopped", STOPPED))
            self._last_step = ((fix.x, fix.y), 0.0, None)
            return (*self._refix(self._look(), fix), True)
        self._detail(f"point action {goal.action!r} not supported on Android; skipped")
        return obs, fix, True

    # -- reporting --

    def _flush(self):
        """Write the queued records (frames included): called right after a lift is confirmed and when the walk ends,
        so no write ever sits between a decision and its gesture. A failing write is logged, never raised."""
        pending, self._pending = self._pending, []
        for kind, record, frame in pending:
            try:
                path = self.io.record(kind, record, frame)
            except Exception as error:  # noqa: BLE001 - the record is diagnostics only
                self._detail(f"record {kind} not written: {type(error).__name__}: {error}")
                continue
            if frame is not None and path is not None:
                record["frame"] = path

    def _detail(self, text):
        self.io.log(text, detail=True)

    def _fail(self, outcome, message, obs, details=None):
        saved = self.io.save(f"android_move_{outcome}", message, obs.frame if obs is not None else None,
                             {"position": self.position, "steps": len(self.steps), **(details or {})})
        raise Halt(WalkResult(outcome, f"{message}，画面已保存。" if saved else f"{message}。",
                              saved=saved))


# ---- what may move: the screen classes ----

SCREEN_TEXT = "MaaNikki_Android_ScreenText"  # full-frame OCR (android_move.json)
# OCR rows read below these are ignored. On the 492 saved open-world frames (threshold 0.5): rows with CJK or a glyph
# outside the HUD's regions under 0.6 were single characters; ASCII-only rows outside them all read under 0.8 (the
# jump button as 2A1 at 0.79), real text at 0.9-1.0.
TEXT_SCORE = 0.6
ASCII_SCORE = 0.8
# A tap or continue marker, an arrow: never part of the plain HUD. Checked on the raw OCR text, before anything is
# normalised. (◆ is not one: the quest tracker's marker icon reads as ◆ before its distance, 「◆1048米」.)
OVERLAY_GLYPHS = "▼▽▶▷►▲△◀◁★☆→←↑↓…"


def _cjk(text: str) -> int:
    return sum("一" <= ch <= "鿿" for ch in text)


def _alnum(text: str) -> int:
    return sum(ch.isascii() and ch.isalnum() for ch in text)


def _glyph(text: str) -> bool:
    return any(ch in OVERLAY_GLYPHS for ch in text)


def counts_as_text(text: str) -> bool:
    """A row worth judging: an overlay glyph, two CJK characters, or three CJK and ASCII letters/digits together
    (``Lv. 40``); single characters (口, G, K on icons; 2A, 41 on textures) are icon or texture noise."""
    return _glyph(text) or _cjk(text) >= 2 or _cjk(text) + _alnum(text) >= 3


# The label row's labels and their x centres on the 1280x720 frame (single-label OCR boxes over the saved frames:
# 商城 656.5, 共鸣 727, 任务 1078, 设计图 1152, 换装 1222; the middle four, always read as one row, at the same spacing).
LABEL_CENTRES = (("商城", 656.5), ("共鸣", 727.0), ("奇迹之旅", 797.0), ("活动大厅", 868.0), ("奇想日历", 938.0),
                 ("无限之心", 1008.0), ("任务", 1078.0), ("设计图", 1152.0), ("换装", 1222.0))
LABEL_HALF_CHAR_PX = 9  # half a label character's width: a run spans its labels' centres +- this x their length
LABEL_EDGE_PX = 40  # box edges vs. that span (saved rows: within -33..+30 px)
MIN_LABELS = 7  # labels the full-frame pass must read in place (saved frames: 7-9 of 9, 466 of 492 all 9)


def label_run(text: str, box) -> int:
    """How many labels ``text`` is when it is exactly a contiguous run of the fixed labels (spaces aside) whose box
    lies where those labels are; 0 otherwise (a misread label or a new word is no part of the label row)."""
    row = re.sub(r"\s", "", text)
    for i in range(len(LABEL_CENTRES)):
        for j in range(i, len(LABEL_CENTRES)):
            if "".join(label for label, _x in LABEL_CENTRES[i:j + 1]) == row:
                first, last = LABEL_CENTRES[i], LABEL_CENTRES[j]
                lo, hi = first[1] - len(first[0]) * LABEL_HALF_CHAR_PX, last[1] + len(last[0]) * LABEL_HALF_CHAR_PX
                if abs(box[0] - lo) <= LABEL_EDGE_PX and abs(box[0] + box[2] - hi) <= LABEL_EDGE_PX:
                    return j - i + 1
    return 0


# The other HUD text regions' exact observed forms (every form read there on the saved frames, threshold 0.5):
# 心誓5 / 心誓5×1 and its hearts (❤ ♡ や ゃ ～, or 3s with an occasional 0 or 8: 33333×1, 心誓530333, 38333);
# the masked UID (UID 123456789, UID-123456789, LID123456789, D123456789, cut short) and the ping (46ms, 46m, 45mm).
HEARTS_FORM = re.compile(r"(?:心?誓5?)?[308❤♡やゃ～]*(?:[×x]1?)?")
UID_FORM = re.compile(r"(?:UID|LID|D)[\s\-]?\d{3,9}|\d{1,4}m{1,2}s?")
TRACKER_DISTANCE_FORM = re.compile(r"◆?\d{1,5}米?")  # 1049米, ◆1048米, 1046 (without 米)


def tracker_name(text: str) -> str:
    """A quest name row as compared between frames: its CJK characters and letters/digits (the leading · and spaces
    dropped), with OCR's one observed flip 呜 → 鸣."""
    return "".join(ch for ch in text if _cjk(ch) or (ch.isascii() and ch.isalnum())).replace("呜", "鸣")


TRACKER_DISTANCE = "#"  # how a distance row appears in Verdict.regions


# The HUD's own text regions (box centres; x0, y0, x1, y1), from full-frame OCR over the saved open-world frames, each
# with what it may show there; text anywhere else (the icon badges' corner included: nothing was ever read there) is
# judged as overlay text.
HUD_REGIONS = (
    ("labels", (540, 0, 1280, 100)),
    ("tracker", (0, 180, 340, 300)),
    ("hearts", (420, 630, 780, 720)),
    ("uid", (0, 660, 260, 720)),
)
# Ambient narration on the saved frames (「暖暖」 over 「起风了——大喵小心，别被吹跑啦。」, m2-goto1; 「大喵」 over
# 「暖暖，星之铃似乎在闪烁呢！摇动它试试吧？」, phase0/hud-1280x720.png): a known companion's label centred around
# (638, 521) and one line centred around y 570, split by OCR at most into adjacent pieces.
AMBIENT_SPEAKERS = frozenset({"暖暖", "大喵"})
SUBTITLE_BAND = (240, 495, 1040, 600)
LINE_Y = (545, 600)
LABEL_ZONE = (540, 495, 740, 548)
LINE_MERGE_PX = 10
LINE_GAP_PX = 24  # pieces of one line touch or overlap (the saved split: -8 px); choices stand apart
LINE_MAX_CHARS = 26
# A line with any of these is no ambient remark: a tap prompt, dialogue, a quest, an interaction, a reward, combat,
# or an instruction to go somewhere or do something.
NARRATION_DENY = ("点击", "点按", "继续", "跳过", "任务", "对话", "选择", "前往", "交互", "领取", "战斗", "开始", "完成",
                  "获得", "奖励", "解锁", "挑战", "拾取", "查看", "采集", "确认", "取消", "按住", "长按", "滑动",
                  "触摸", "提交", "传送", "进入", "打开", "失败", "胜利", "击败", "教程", "提示", "去", "找", "跟上",
                  "跟着", "过来", "快来", "帮我", "帮忙", "请")


@dataclass(frozen=True)
class Verdict:
    kind: str  # "hud", "narration", "misread" (only a permanent-HUD block's text differs: see Walker) or "other"
    reason: str = ""  # for "other": a fixed Chinese text (never OCR text)
    regions: tuple = ()  # the quest tracker's rows top to bottom: tracker_name() for a name, TRACKER_DISTANCE
    blocks: tuple = ()  # Rows.blocks: the permanent-HUD text block layout


def _inside(box, region) -> bool:
    x, y, w, h = box
    cx, cy = x + w / 2, y + h / 2
    return region[0] <= cx < region[2] and region[1] <= cy < region[3]


# A permanent-HUD text block of a misread frame is no new block when it overlaps one of the first frame's blocks in the
# same region (x ranges overlapping within BLOCK_PX, centres within BLOCK_PX vertically). Overlap, not equal count
# and centre: OCR splits and merges the label row differently from frame to frame (共鸣 apart or joined to the next
# labels), and the 心誓 block's width changes with ×1 read or not; a block where the first frame had none stops.
BLOCK_PX = 12
BLOCK_REGIONS = ("labels", "hearts", "uid")  # the regions whose block layout a misread frame must keep


@dataclass(frozen=True)
class Rows:
    outside: list  # [(text, box)] judged as overlay text (glyph rows among them, on their raw text)
    regions: tuple  # the quest tracker's rows (Verdict.regions)
    labels: int  # labels read in place, plus those a misread label-row block covers (see misread)
    blocks: tuple  # ((region, x0, x1, cy), ...) of every counted block in BLOCK_REGIONS, matching or misread
    misread: int  # blocks in BLOCK_REGIONS whose text is not that region's exact form


def read_rows(result) -> Rows:
    """The full-frame OCR result sorted out: rows that are not the HUD's own text (outside), the quest tracker, the
    labels read in place, the permanent-HUD block layout, and how many of those blocks were misread (in a HUD region,
    not in its exact form; Walker gives such a frame one recapture when nothing else differs from the first frame)."""
    from host.android import actions, pages
    outside, tracker, blocks, labels, covered, misread = [], [], [], 0, 0, 0
    if result is not None and actions._hit(result):
        for row in pages._rows(result):
            if row is None:
                continue
            text, box = str(getattr(row, "text", "") or ""), getattr(row, "box", None)
            score = float(getattr(row, "score", 0) or 0)
            if box is None:
                continue
            box = [int(v) for v in box]
            if _glyph(text):  # the raw text, before anything else
                if score >= TEXT_SCORE:
                    outside.append((text, box))
                continue
            if score < (TEXT_SCORE if _cjk(text) else ASCII_SCORE) or not counts_as_text(text):
                continue
            region = next((name for name, area in HUD_REGIONS if _inside(box, area)), None)
            squashed = re.sub(r"\s", "", text)
            if region is None:
                outside.append((text, box))
                continue
            if region == "tracker":
                distance = TRACKER_DISTANCE_FORM.fullmatch(squashed)
                tracker.append((box[1], box[0], TRACKER_DISTANCE if distance else tracker_name(text)))
                continue
            blocks.append((region, box[0], box[0] + box[2], box[1] + box[3] / 2))
            if region == "labels" and label_run(text, box):
                labels += label_run(text, box)
            elif (region == "hearts" and HEARTS_FORM.fullmatch(squashed)) or (
                    region == "uid" and UID_FORM.fullmatch(text.strip())):
                pass
            else:
                misread += 1
                if region == "labels":  # the labels whose places the misread block covers
                    covered += sum(1 for _label, x in LABEL_CENTRES if box[0] <= x <= box[0] + box[2])
    return Rows(outside, tuple(entry for _y, _x, entry in sorted(tracker)), labels + covered,
                tuple(sorted(blocks)), misread)


def same_blocks(blocks, first) -> bool:
    """No block of ``blocks`` is new against ``first`` (the walk's first frame): each overlaps one of first's blocks
    in the same region and row (see BLOCK_PX)."""
    return all(any(region == r and x0 <= fx1 + BLOCK_PX and fx0 <= x1 + BLOCK_PX and abs(y - fy) <= BLOCK_PX
                   for r, fx0, fx1, fy in first) for region, x0, x1, y in blocks)


def narration_verdict(rows) -> str:
    """"" when ``rows`` (text outside the HUD regions, at least one) are one ambient remark; otherwise why not (a fixed
    Chinese text, no OCR text)."""
    if not all(_inside(box, SUBTITLE_BAND) for _text, box in rows):
        return "大世界以外的文字（可能是提示、横幅、任务或交互按钮）"
    if any(_glyph(text) for text, _box in rows):
        return "画面上有继续或点击的标记"
    labels = [(text, box) for text, box in rows if _inside(box, LABEL_ZONE) and box[3] <= 45]
    line = sorted(((box[0], box[1] + box[3] / 2, text, box) for text, box in rows if (text, box) not in labels))
    joined = "".join(re.sub(r"\s", "", text) for _x, _y, text, _b in line)  # the line as one text: 现在点 + 击屏幕
    if any(word in text for text in (joined, *(text for text, _box in labels)) for word in NARRATION_DENY):
        return "画面下方像是要点击的提示、对话、任务或指引的文字"
    if len(labels) != 1 or labels[0][0].strip() not in AMBIENT_SPEAKERS:
        return "画面下方的文字没有已知的旁白说话人（可能是对话）"
    if not line:
        return "画面下方的文字不像一行环境旁白"
    if max(y for _x, y, _t, _b in line) - min(y for _x, y, _t, _b in line) > LINE_MERGE_PX:
        return "画面下方有不止一行文字（可能是对话或选项）"
    for (_x0, _y0, _t0, first), (_x1, _y1, _t1, second) in zip(line, line[1:]):
        if second[0] - (first[0] + first[2]) > LINE_GAP_PX:
            return "画面下方的文字分成了几块（可能是选项）"
    centre = sum(y for _x, y, _t, _b in line) / len(line)
    if not LINE_Y[0] <= centre < LINE_Y[1] or labels[0][1][1] + labels[0][1][3] / 2 >= centre:
        return "画面下方的文字不在环境旁白的位置"
    if len(joined) > LINE_MAX_CHARS:
        return "画面下方的文字太长，不像一句环境旁白"
    return ""


def classify(screen, recognize) -> Verdict:
    """The Verdict for one frame, from host.android.actions.read_screen's ``screen`` and ``recognize`` on it: "hud"
    when the HUD is positively recognised and no text is read outside the HUD's own text regions (each region matching
    its pattern); "narration" when the only such text is one ambient remark (narration_verdict); otherwise "other" with
    a fixed description (what OCR read is never part of it). The quest tracker's text is returned for the walk to
    compare with its first frame."""
    from host.android import pages
    if screen.kind != "hud":
        what = pages.SCREEN_KINDS.get(screen.kind, "认不出的画面")
        return Verdict("other", f"{what}（{screen.reason}）" if screen.reason else what)
    result = recognize(SCREEN_TEXT)
    if result is None:
        return Verdict("other", "画面（画面文字的检查没能完成）")
    rows = read_rows(result)
    regions, blocks = rows.regions, rows.blocks
    if rows.labels < MIN_LABELS:  # positive evidence from this pass itself, not only read_screen's label check
        return Verdict("other", "画面（全画面文字里没有在原位读到大世界的快捷栏）", regions, blocks)
    if rows.outside:
        why = narration_verdict(rows.outside) if not rows.misread else "大世界的界面文字读错了，同时还有别的文字"
        return Verdict("narration", "", regions, blocks) if not why else Verdict("other", why, regions, blocks)
    if rows.misread:
        return Verdict("misread", "大世界常驻界面的文字没有按原样读出", regions, blocks)
    return Verdict("hud", "", regions, blocks)


class RuntimeIo:
    """Walker I/O on the agent's Runtime: focus-checked frames classified with the Android action table
    (host.android.actions.read_screen) plus full-frame OCR (classify), joystick steps through Runtime.gesture,
    diagnostics through Runtime.diagnostic. Android only (Runtime.gesture refuses elsewhere)."""

    BELL_POINT = (616, 612)  # the bottom-centre bell icon on the saved HUD frames (inference; bell is off by default)
    BELL_S = 4.0

    def __init__(self, rt, *, clock=time.monotonic):
        self.rt, self.clock = rt, clock

    def stopped(self) -> bool:
        from host import android_user_stop
        return bool(self.rt.stopped or android_user_stop())

    def observe(self):
        """The next frame and its class. Only a capture that returned no image is "blank" (tried again once); a frame
        whose focus mark is no longer current, or whose touch reading shows a contact held (or none readable), is
        "other": it cannot stand for the screen a gesture would act on, so the walk stops on it."""
        from host import device_input
        from host.android import actions
        at = self.clock()
        frame = self.rt._fresh_device_frame()
        captured = self.clock()
        if frame is None:
            return None if self.stopped() else Observation(None, "blank", at, captured=captured)
        if not self.rt._frame_still_valid(frame):
            return Observation(frame, "other", at, "画面（截图之后游戏焦点变过）", captured)
        touch = device_input().touch_of(frame)
        if touch is None or getattr(touch, "state", None) != "none":
            return Observation(frame, "other", at, "画面（截图时设备上有按住的触点，或读不出触摸状态）", captured)
        recognize = self.rt._recognizer_on(frame)
        verdict = classify(actions.read_screen(recognize), recognize)
        return Observation(frame, verdict.kind, at, verdict.reason, captured, verdict.regions, verdict.blocks)

    def step(self, frame, angle, seconds, jump_at, *, not_after=None) -> bool:
        from host.android import gesture
        return bool(gesture.joystick_step(self.rt, frame, angle, 1.0, seconds, jump_at, not_after=not_after))

    def bell(self, frame, *, not_after=None) -> bool:
        from host.android import gesture
        return bool(gesture.bell_step(self.rt, frame, self.BELL_POINT, self.BELL_S, not_after=not_after))

    def pause(self, seconds) -> bool:
        """Wait ``seconds`` of wall time (the walker passes at most the time left before its deadline), checking the
        stop signals; no input is sent and none decided. Not Runtime.pause: its focus gate may block past the
        deadline, and the next capture waits for focus anyway."""
        end = self.clock() + max(0.0, seconds)
        while not self.stopped():
            left = end - self.clock()
            if left <= 0:
                return True
            time.sleep(min(0.05, left))
        return False

    def save(self, stage, reason, frame, details=None):
        reference = self.rt.diagnostic(stage, reason, image=frame, details=details)
        return isinstance(reference, dict) and reference.get("image_status") == "saved"

    def log(self, text, *, detail=False):
        self.rt.log(text, detail=detail)

    def record(self, kind, record, frame):
        import json
        self.rt.log(f"android move {kind}: {json.dumps(record, ensure_ascii=False, default=str)}", detail=True)
        return None


def walk_to(rt, map_id: str, target, hint, *, radius: float = ARRIVAL_U, timeout: float = 60.0, **options):
    """Walk to ``target`` (map x, y) on ``map_id``, starting from the position near ``hint``."""
    from host.android.minimap import MinimapReader
    walker = Walker(RuntimeIo(rt), MinimapReader(map_id), **options)
    return walker.walk([Goal(float(target[0]), float(target[1]), float(radius))], hint, timeout)


def route_goals(route) -> list[Goal]:
    """navigation.models.Route points as goals (radius, jump, action, params kept)."""
    return [Goal(p.x, p.y, p.radius, p.jump, p.action or "", dict(p.params or {})) for p in route.points]


def walk_route(rt, route, hint, **options):
    """Walk all of ``route`` (navigation.models.Route) from the position near ``hint`` within the route's timeout;
    the first trusted fix must lie within the route's start radius of its first point (else nothing is sent)."""
    from host.android.minimap import MinimapReader
    walker = Walker(RuntimeIo(rt), MinimapReader(route.map_id), **options)
    return walker.walk(route_goals(route), hint, route.timeout, start_radius=route.start_radius)
