"""Android movement backend (phase E): walk the character to a point, or along route points, with joystick steps.

Every step is decided on a frame captured just before it and sent as one gesture (host.android.gesture.joystick_step,
the only touch this module sends; the bell press, off by default, is the one other gesture):

1. Look: capture a frame and classify it (Io.observe). Move only on a positively recognised open-world HUD. A HUD
   with a line of ambient narration (a remark in the subtitle band, nothing to tap) pauses without input until it goes
   (logged), up to NARRATION_MAX_S. A frame on which nothing is recognised yet is waited through for HUD_GRACE_S.
   Anything else (another page, a dialog, the quit confirmation, a popup, an unrecognised screen) stops: the frame is
   saved, nothing is tapped.
2. Fix: the minimap position (host.android.minimap). The first fix needs a caller-supplied hint (later the teleport
   point's coordinates); no hint, or a hinted match under the score floor or far from the hint, refuses before any
   input. Later fixes match around the predicted position and must be plausible for the step just made (a jump larger
   than PLAUSIBLE_FACTOR x the expected distance + PLAUSIBLE_SLACK_U is a bad fix, never movement); a bad fix gets one
   wider retry on a fresh, settled frame, then the walk stops.
3. Plan: world bearing to the goal (0 = map up, clockwise; map y grows down) and the camera yaw read from the minimap's
   view cone on the same frame give the joystick's screen angle: screen_angle = bearing - yaw + 270 (screen angles: 0
   right, 90 down; the camera looks "up" the screen, 270). The joystick is always at full deflection; the distance is
   set by the duration from the measured model expected_distance(T) = SPEED_U_S * (T - DEAD_S), clamped to
   [MIN_STEP_S, MAX_STEP_S]. The camera is never turned.
4. Settle: after a lift the minimap keeps easing for 0.6-0.9 s. A frame captured right after a step therefore shows
   the position up to LAG_U short along the step (2.8 u at most on the device frames). Instead of always waiting, the
   walk waits only when the lag could change the decision: near a goal that must be reached precisely (the last
   point, or one with an action), i.e. when remaining - LAG_U <= REACH_U, it waits until SETTLE_S after the lift and
   decides on a fresh frame. Far from
   such a goal the next step is a full-length step toward it with or without the lag, and an intermediate route point
   counts as passed when the segment between two fixes comes within its radius (navigation.geometry), which a lagging
   fix cannot hide. Arrival at a precise goal is only ever judged on a settled frame.
5. Stop conditions: STALL_STEPS consecutive steps with almost no displacement (blocked), no progress toward the goal
   in GOAL_STALL_STEPS steps, the timeout, leaving the leash, an unreadable camera yaw twice in a row, a stop (the
   gesture returned False, or the run stopped): each returns at once, with no further input; failures save the frame.

Measured facts used here are in docs/android-support-plan.md (阶段 E, the 09:10 handover) and
.build/android/phase-e-measure (m2-* tables, m2-settle, m2-goto2: the screen-angle formula moved the character along
the computed bearing within a few degrees on the device).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import time

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

# ---- stops ----
STALL_STEPS = 3
STALL_MIN_U = 0.4
STALL_FRACTION = 0.15  # a step "barely moved" below max(STALL_MIN_U, this x its expected distance)
GOAL_STALL_STEPS = 8
GOAL_PROGRESS_U = 0.5
HEADING_MISSES = 2
HUD_GRACE_S = 2.0  # "not recognised yet" (nothing at all recognised) is tolerated this long
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
    """One classified frame. ``kind``: "hud" (move), "narration" (pause), "transition" (nothing recognised yet) or
    "other" (stop; ``reason`` says what, a fixed Chinese text). ``at``: the clock when its capture started."""
    frame: object
    kind: str
    at: float
    reason: str = ""


@dataclass
class WalkResult:
    outcome: str  # arrived, stopped, refused, blocked, timeout, lost, screen, leash
    message: str  # for the user (Chinese, no OCR text)
    position: tuple[float, float] | None = None
    steps: list = field(default_factory=list)
    saved: object = None  # what Io.save returned for the frame kept with a failure

    @property
    def ok(self) -> bool:
        return self.outcome == "arrived"


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


# ---- the walk ----

class Walker:
    """One walk on one map. ``io`` is the device binding (RuntimeIo; walk_probe.DiagnosticIo for the dev walk);
    ``reader`` a minimap.MinimapReader (or anything with ``present(frame)`` and ``fix(frame, hint, search_radius)``)."""

    def __init__(self, io, reader, *, clock=time.monotonic, allow_bell: bool = False, allow_jump: bool = False,
                 leash=None):
        self.io, self.reader, self.clock = io, reader, clock
        self.allow_bell, self.allow_jump = allow_bell, allow_jump
        self.leash = leash  # (x, y, radius): every fix must stay inside, the dev walk's safety fence
        self.steps: list[dict] = []
        self.position = None
        self._lift_at = None  # the clock when the last gesture returned (its lift confirmed)
        self._last_step = None  # (origin fix, seconds, bearing) of the last step

    # -- entry --

    def walk(self, goals, hint, timeout: float) -> WalkResult:
        goals = [goal if isinstance(goal, Goal) else Goal(*goal) for goal in goals]
        try:
            if not goals:
                raise Halt(WalkResult("refused", "没有要去的位置，不开始移动。"))
            return self._walk(goals, hint, timeout)
        except Halt as halt:
            result = halt.result
            result.position = result.position or self.position
            result.steps = self.steps
            return result

    def _walk(self, goals, hint, timeout):
        if hint is None:
            raise Halt(WalkResult("refused", "没有起点位置的提示，不从猜测的位置开始移动。"))
        if any(goal.action == "bell" for goal in goals) and not self.allow_bell:
            raise Halt(WalkResult("refused", "路线上有按铃的点，按铃在安卓模拟器上还没有开放，没有开始移动。"))
        if self.leash is not None and any(_distance((g.x, g.y), self.leash[:2]) > self.leash[2] for g in goals):
            raise Halt(WalkResult("refused", "目标超出了允许的范围，没有开始移动。"))
        deadline = self.clock() + timeout
        obs = self._look()
        obs, fix = self._first_fix(obs, hint)
        index = 0
        stalls, best, since_best, heading_misses = 0, math.inf, 0, 0
        previous = None  # the fix before the last step, for passing intermediate points
        while True:
            self._check_stop()
            goal = goals[index]
            here = (fix.x, fix.y)
            precise = index == len(goals) - 1 or bool(goal.action)
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
                if index == len(goals) - 1 and not goal.action:
                    return WalkResult("arrived", "已到达目标位置。", position=here)
                obs, fix = self._point_action(goal, obs, fix)
                if index == len(goals) - 1:
                    return WalkResult("arrived", "已到达目标位置。", position=(fix.x, fix.y))
                index, best, since_best, previous = index + 1, math.inf, 0, None
                continue
            if self.clock() >= deadline:
                self._fail("timeout", "移动超时，没有到达目标位置，已停止", obs)
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
            # Plan on this frame and send.
            world = bearing(goal.x - fix.x, goal.y - fix.y)
            seconds = step_seconds(remaining)
            angle = screen_angle(world, fix.heading)
            jump_at = None
            if goal.jump and self.allow_jump and expected_distance(seconds) >= remaining - goal.radius \
                    and seconds > JUMP_AT_S + 0.2:
                jump_at = JUMP_AT_S
            record = {"step": len(self.steps), "goal": index, "fix": fix.log(), "remaining": round(remaining, 2),
                      "bearing": round(world, 1), "angle": round(angle, 1), "seconds": round(seconds, 3),
                      "jump_at": jump_at, "expected": round(expected_distance(seconds), 2)}
            record["frame"] = self.io.record("step", record, obs.frame)
            self.steps.append(record)
            sent = self.io.step(obs.frame, angle, seconds, jump_at)
            self._lift_at = self.clock()
            if not sent:
                raise Halt(WalkResult("stopped", "已停止。"))
            self._check_stop()
            origin = here
            self._last_step = (origin, seconds, world)
            obs, fix = self._track(self._look(), origin, seconds, world)
            moved = _distance(origin, (fix.x, fix.y))
            self.io.record("moved", {"step": record["step"], "moved": round(moved, 2), "after": fix.log(),
                                     "settled": self._settled(obs)}, None)
            record.update(moved=round(moved, 2), after=fix.log())
            stalls = stalls + 1 if moved < max(STALL_MIN_U, STALL_FRACTION * expected_distance(seconds)) else 0
            since_best += 1
            if stalls >= STALL_STEPS:
                if not self._settled(obs):  # judge "blocked" on a settled frame only
                    obs, fix = self._settle(obs, fix)
                    moved = _distance(origin, (fix.x, fix.y))
                    self.io.record("moved", {"step": record["step"], "moved": round(moved, 2), "after": fix.log(),
                                             "settled": True}, None)
                    record.update(moved=round(moved, 2), after=fix.log())
                if moved < max(STALL_MIN_U, STALL_FRACTION * expected_distance(seconds)):
                    self._fail("blocked", f"连续 {STALL_STEPS} 步几乎没有移动（可能被挡住了），已停止", obs)
                stalls = 0
            previous = origin

    # -- looking --

    def _check_stop(self):
        if self.io.stopped():
            raise Halt(WalkResult("stopped", "已停止。"))

    def _pause(self, seconds):
        if seconds > 0 and not self.io.pause(seconds):
            raise Halt(WalkResult("stopped", "已停止。"))
        self._check_stop()

    def _look(self) -> Observation:
        """A frame on which the open-world HUD is positively recognised (waiting through narration and frames on which
        nothing is recognised yet); any other screen stops the walk with that frame saved."""
        narration_since = grace_until = None
        while True:
            self._check_stop()
            obs = self.io.observe()
            if obs is None:
                raise Halt(WalkResult("stopped", "已停止。"))
            if obs.kind == "hud":
                if narration_since is not None:
                    self.io.log("环境旁白已消失，继续移动。")
                return obs
            if obs.kind == "narration":
                grace_until = None
                if narration_since is None:
                    narration_since = self.clock()
                    self.io.log("画面上有一行环境旁白，暂停移动，等它消失。")
                elif self.clock() - narration_since >= NARRATION_MAX_S:
                    self._fail("screen", f"环境旁白 {NARRATION_MAX_S:.0f} 秒没有消失，已停止", obs)
            elif obs.kind == "transition":
                if grace_until is None:
                    grace_until = self.clock() + HUD_GRACE_S
                elif self.clock() >= grace_until:
                    self._fail("screen", "移动时认不出大世界的画面，已停止所有输入", obs)
            else:
                self._fail("screen", f"移动时出现了{obs.reason or '认不出的画面'}，已停止所有输入", obs)
            self._pause(POLL_S)

    def _settled(self, obs) -> bool:
        return self._lift_at is None or obs.at - self._lift_at >= SETTLE_S

    def _settle(self, obs, fix):
        """Wait until SETTLE_S after the last lift and fix on a fresh frame."""
        wait = SETTLE_S - (self.clock() - self._lift_at)
        self._detail(f"settle wait {max(0.0, wait):.2f} s")
        self._pause(wait)
        return self._refix(self._look(), fix)

    def _refix(self, obs, fix):
        """A fix on a new frame without a step since ``fix`` (a settle, a heading retry): judged against the last
        step when there was one, else against ``fix`` itself."""
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
        self.io.record("first_fix", {"hint": list(hint), "fix": fix.log()}, obs.frame)
        return self._accept(obs, fix)

    def _good(self, fix, origin, seconds) -> bool:
        return fix.reliable and _distance((fix.x, fix.y), origin) <= plausible_distance(seconds)

    def _track(self, obs, origin, seconds, world):
        """Fix ``obs`` after a step of ``seconds`` along ``world`` from ``origin``; a bad fix (score, plausibility)
        gets one wider retry on a fresh settled frame, then the walk stops. Returns (obs, fix): the frame it used."""
        if world is None:
            predicted = origin
        else:
            reach = expected_distance(seconds)
            predicted = (origin[0] + reach * math.sin(math.radians(world)),
                         origin[1] - reach * math.cos(math.radians(world)))
        fix = self.reader.fix(obs.frame, predicted, TRACK_SEARCH) if self.reader.present(obs.frame) else None
        if fix is not None and self._good(fix, origin, seconds):
            return self._accept(obs, fix)
        self._detail(f"bad fix {fix.log() if fix else 'no chevron'} near {predicted}; one wider retry")
        if self._lift_at is not None:
            self._pause(SETTLE_S - (self.clock() - self._lift_at))
        obs = self._look()
        fix = self.reader.fix(obs.frame, origin, WIDE_SEARCH) if self.reader.present(obs.frame) else None
        if fix is not None and self._good(fix, origin, seconds):
            return self._accept(obs, fix)
        self._fail("lost", "小地图定位不可信（匹配分数太低或位置跳变），已停止", obs,
                   details={"origin": list(origin), "fix": fix.log() if fix else None})

    def _accept(self, obs, fix):
        if self.leash is not None and _distance((fix.x, fix.y), self.leash[:2]) > self.leash[2]:
            self._fail("leash", "位置超出了允许的范围，已停止", obs, details={"fix": fix.log()})
        self.position = (fix.x, fix.y)
        return obs, fix

    # -- point actions --

    def _point_action(self, goal, obs, fix):
        if goal.action == "wait":
            seconds = float((goal.params or {}).get("seconds", 0) or 0)
            self._pause(min(max(seconds, 0.0), 10.0))
            return self._refix(self._look(), fix)
        if goal.action == "bell":
            if not self.allow_bell:
                self._fail("refused", "这一步要按铃，按铃在安卓模拟器上还没有开放，已停止", obs)
            self.io.record("bell", {"fix": fix.log()}, obs.frame)
            if not self.io.bell(obs.frame):
                raise Halt(WalkResult("stopped", "已停止。"))
            self._lift_at = self.clock()
            self._last_step = ((fix.x, fix.y), 0.0, None)
            return self._refix(self._look(), fix)
        self._detail(f"point action {goal.action!r} not supported on Android; skipped")
        return obs, fix

    # -- reporting --

    def _detail(self, text):
        self.io.log(text, detail=True)

    def _fail(self, outcome, message, obs, details=None):
        saved = self.io.save(f"android_move_{outcome}", message, obs.frame if obs is not None else None,
                             {"position": self.position, "steps": len(self.steps), **(details or {})})
        raise Halt(WalkResult(outcome, f"{message}，画面已保存。" if saved else f"{message}。",
                              saved=saved))


# ---- the agent's binding ----

NARRATION = "MaaNikki_Android_Narration"
# The narration band (android_move.json) reads a line of at least two CJK characters at this OCR confidence or more.
# On the saved device frames the real remark (「暖暖」 / 「起风了——大喵小心，别被吹跑啦。」, m2-goto1) read at 0.97-1.0 with
# tight boxes; stone-plaza textures read as one or two junk characters at 0.38-0.52 over the whole band.
NARRATION_SCORE = 0.8


def classify(screen, recognize):
    """``(kind, reason)`` for an Observation, from host.android.actions.read_screen's ``screen`` and ``recognize`` on
    the same frame: "hud" on the positively recognised open-world HUD with no text in the narration band, "narration"
    when that band holds a confidently read line of at least two CJK characters (what OCR read is not kept),
    "transition" when nothing at all is recognised (pages.nothing_recognised), else "other" with a fixed description
    of the screen."""
    from host.android import actions, pages
    if screen.kind == "hud":
        result = recognize(NARRATION)
        if result is None:
            return "other", "画面（环境旁白的检查没能完成）"
        if actions._hit(result) and any(actions._cjk(str(getattr(row, "text", "") or "")) >= 2
                                        and float(getattr(row, "score", 0) or 0) >= NARRATION_SCORE
                                        for row in pages._rows(result) if row is not None):
            return "narration", ""
        return "hud", ""
    if pages.nothing_recognised(screen, recognize):
        return "transition", ""
    what = pages.SCREEN_KINDS.get(screen.kind, "认不出的画面")
    return "other", (f"{what}（{screen.reason}）" if screen.reason else what)


class RuntimeIo:
    """Walker I/O on the agent's Runtime: focus-checked frames classified with the Android action table
    (host.android.actions.read_screen) plus the narration band, joystick steps through Runtime.gesture, diagnostics
    through Runtime.diagnostic. Android only (Runtime.gesture refuses elsewhere)."""

    BELL_POINT = (616, 612)  # the bottom-centre bell icon on the saved HUD frames (inference; bell is off by default)
    BELL_S = 4.0

    def __init__(self, rt, *, clock=time.monotonic):
        self.rt, self.clock = rt, clock

    def stopped(self) -> bool:
        from host import android_user_stop
        return bool(self.rt.stopped or android_user_stop())

    def observe(self):
        from host.android import pages
        at = self.clock()
        screen, frame, recognize = pages.screen_now(self.rt)
        if frame is None:
            return None
        if not self.rt._frame_still_valid(frame):
            return Observation(frame, "transition", at)
        kind, reason = classify(screen, recognize)
        return Observation(frame, kind, at, reason)

    def step(self, frame, angle, seconds, jump_at) -> bool:
        from host.android import gesture
        return bool(gesture.joystick_step(self.rt, frame, angle, 1.0, seconds, jump_at))

    def bell(self, frame) -> bool:
        from host.android import gesture
        return bool(self.rt.gesture(gesture.bell_press(self.BELL_POINT, self.BELL_S), source_frame=frame))

    def pause(self, seconds) -> bool:
        return bool(self.rt.pause(seconds))

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
    """Walk all of ``route`` (navigation.models.Route) from the position near ``hint`` within the route's timeout."""
    from host.android.minimap import MinimapReader
    walker = Walker(RuntimeIo(rt), MinimapReader(route.map_id), **options)
    return walker.walk(route_goals(route), hint, route.timeout)
