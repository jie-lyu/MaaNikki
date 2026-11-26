"""Dev-only Android entry MaaNikki_AndroidCrystalRoute: the first 星海 crystal route on the phone client, end to end.

HUD -> big map on 星海 -> the card read: today's bell rung or not, positively (teleport.card_state: exactly the observed
not-rung message, or exactly the PC card's three counters; anything else: stop, nothing pressed) -> not rung: unless
this data directory already recorded a bell attempt for today's game day (then stop: the bell needs a manual
check), teleport to 无界枢纽, the minimap fix confirms the hub, the attempt recorded, the bell pressed once (~4 s,
gesture.bell_step, the one gesture allowed past 1.5 s) decided and confirmed on clean HUD frames with the bell button
recognised, the map opened again and the card must now read rung (still not rung: stop, never a second press) ->
teleport to 星梦群屿·主岛 -> xinghai_crystal_3 walked with host.android.move (the teleport point as the first position
hint, the route's start radius, its jumps and its wait) -> after the walk, unless it ended on a screen that stops all
input, the map card read again for the 星光结晶 count and the map left by its back arrow onto a confirmed HUD (the
character stays where the walk ended: on the route, on the ground). Success: the walk arrived, both counts read, the
final HUD confirmed and no stop requested.

This version always walks route 3 (PC picks one of eight routes from the crystal markers the bell reveals on the map;
reading those markers on the phone is not done yet), so on a day the crystals lie on another route it collects
little or nothing, which is expected.

The bell record (BellRecord): config/android/bell_attempts.json in the data directory, keyed by the game day
(account_state.game_day: 04:00 UTC+8) only, not by game account: the Android tasks do not read the phone client's UID
yet (as the 兑换码 record). It is written right before the press is handed to the device path and removed only when
that path refused it with nothing sent (DecisionExpired, UnsupportedInput, ForegroundChanged: all raised before
contact-down); a press that ran, was cut short or ended unconfirmed stays recorded.

Never a user task, gated like MaaNikki_AndroidWalkProbe: its pipeline node exists only in
resource_android/pipeline/android_crystal_route.json; it is not in agent/host/android/tasks.json, interface.json or any
overlay; the agent refuses it unless scripts/android/runner.py names it in MAANIKKI_ANDROID_DEV_ENTRIES; nikki.gameplay
runs it only when both the task entry and the parameter's kind say so. Every walk step's frame and numbers go to
DATA_ROOT/debug/android_route/<time>/ (walk.jsonl).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path

from host.android import actions, pages
from host.android.teleport import HUD_BELL, Stop, Teleport, guard

ENTRY = "MaaNikki_AndroidCrystalRoute"
KIND = "android_crystal_route"
ROUTE = "xinghai_crystal_3"
MAP_ID = "starsea"
HUB = "无界枢纽"
NOTICE = ("星海星光结晶路线（安卓，开发用）：这个版本固定走 3 号路线（星梦群屿），当天结晶在其他路线上时收集得很少或没有收集，"
          "这是预期的。")
# A run stopped after the bell press but before its result was read: what the user can do (the coordinator's note).
BELL_NOTE = ("如果按铃之后、读到结果之前任务停了：当天的记录会阻止再按一次；可以手动打开星海地图看卡片，"
             "仍显示尚未召唤摇铃就手动摇一次铃；如果铃其实已经响过，不会有任何损失。")
BELL_POINT = (616, 612)  # the bell button's centre on every saved HUD frame (template hit at (616,613))
BELL_S = 4.0
AT_POINT_U = 15.0  # the first minimap fix after a teleport lies this close to the point (the walker's own trust aside)
AFTER_BELL_WAIT = 15.0
WALK_READ = ("arrived", "blocked", "timeout", "lost")  # outcomes after which the map may still be opened to read
RECORD_FILE = ("config", "android", "bell_attempts.json")
RECORD_STATES = frozenset({"pressing", "pressed", "interrupted"})
MANUAL = "今天已经尝试过按铃（有当天的记录），没有再按；请手动打开星海地图看卡片，仍显示尚未召唤摇铃就手动摇一次铃"
RECORD_BROKEN = "安卓按铃记录读不出或写不进去，没有按铃"


def checkpoint(name: str) -> dict:
    """The 星海 record of ``name`` in resource/navigation/checkpoints.json."""
    from app_paths import RESOURCE_ROOT
    from navigation.models import NavigationError
    data = json.loads((RESOURCE_ROOT / "resource" / "navigation" / "checkpoints.json").read_text(encoding="utf-8"))
    for record in data["maps"][MAP_ID]:
        if record.get("name") == name:
            return record
    raise NavigationError(f"传送点资料里没有「{name}」。")


class RecordError(Exception):
    """The bell record cannot be read or written: nothing is pressed."""


class BellRecord:
    """The bell attempts of this data directory, by game day (see the module's docstring)."""

    def __init__(self, root):
        self.path = Path(root).joinpath(*RECORD_FILE)

    def load(self) -> dict:
        if not self.path.is_file():
            return {"schema_version": 1, "scope": "android_data_directory", "attempts": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise RecordError(RECORD_BROKEN) from error
        attempts = data.get("attempts") if isinstance(data, dict) else None
        if not isinstance(attempts, dict) or any(not isinstance(v, dict) or v.get("state") not in RECORD_STATES
                                                 for v in attempts.values()):
            raise RecordError(RECORD_BROKEN)
        return data

    def attempt(self, day) -> dict | None:
        return self.load()["attempts"].get(str(day))

    def _write(self, change):
        from file_io import exclusive_file_lock, write_json
        try:
            with exclusive_file_lock(self.path.with_name("bell_attempts.lock")):
                data = self.load()
                change(data["attempts"])
                keep = sorted(data["attempts"])[-14:]  # two weeks are plenty
                data["attempts"] = {day: data["attempts"][day] for day in keep}
                write_json(self.path, data)
        except OSError as error:
            raise RecordError(RECORD_BROKEN) from error

    def mark(self, day, state):
        if state not in RECORD_STATES:
            raise ValueError(state)
        self._write(lambda attempts: attempts.__setitem__(str(day), {
            "state": state, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}))

    def clear(self, day):
        self._write(lambda attempts: attempts.pop(str(day), None))


class Run:
    def __init__(self, rt, *, reader=None, world=None, io=None, record=None):
        self.rt = rt
        self.map = Teleport(rt, world, io=io, reader=reader)
        self._record = record

    @property
    def reader(self):
        return self.map.reader

    @property
    def io(self):
        return self.map.io

    @property
    def record(self) -> BellRecord:
        if self._record is None:
            from app_paths import DATA_ROOT
            self._record = BellRecord(DATA_ROOT)
        return self._record

    # -- the bell --

    def at_point(self, obs, record) -> bool:
        """The minimap fix on ``obs`` (hinted with the point) is trusted and within AT_POINT_U of it."""
        point = (float(record["x"]), float(record["y"]))
        if not self.reader.present(obs.frame):
            return False
        fix = self.reader.fix(obs.frame, point, 40.0)
        return bool(fix.reliable) and math.dist((fix.x, fix.y), point) <= AT_POINT_U

    def bell_target(self, hub):
        """locate for hud_pair: at the hub (minimap fix), the bell button recognised where it belongs, guarded."""
        def locate(obs):
            if not self.at_point(obs, hub):
                raise Stop("小地图上的位置不在无界枢纽，没有按铃", [obs.frame])
            recognize = self.rt._recognizer_on(obs.frame)
            result = recognize(HUD_BELL)
            if not actions._hit(result):
                raise Stop("大世界上没有认出铃铛按钮，没有按铃", [obs.frame])
            box = actions._box(result)
            if not actions._near(box, BELL_POINT):  # actions.CONFIRM_TOLERANCE (8 px) on each axis
                raise Stop("铃铛按钮不在平常的位置，没有按铃", [obs.frame])
            guard(recognize, box, obs.frame)
            return box
        return locate

    def ring(self, hub) -> bool:
        """One bell press, decided on a clean HUD frame at the hub and confirmed on the next (hud_pair: the walker's
        clean-HUD checks on both); the gesture carries the confirming frame, the latest capture, and its expiry. The
        attempt is recorded before the press is handed over; a refusal raised before contact-down (nothing sent)
        removes it and, for an expired decision or a focus change, decides again. Never a second press."""
        from account_state import game_day
        from host import UnsupportedInput
        from host.android import gesture, touch_point
        from host.android.move import MAX_DECISION_AGE_S
        from task_control import ForegroundChanged
        day = game_day()
        if self.record.attempt(day) is not None:
            raise Stop(MANUAL, [])
        for _attempt in range(self.rt.TOUCH_KEY_ATTEMPTS):
            pair = self.map.hud_pair("按铃", self.bell_target(hub))
            if pair is None:
                return False
            _first, second, box = pair
            self.record.mark(day, "pressing")  # raises when it cannot be written: then nothing is pressed
            try:
                sent = gesture.bell_step(self.rt, second.frame, touch_point(box), BELL_S,
                                         not_after=second.at + MAX_DECISION_AGE_S)
            except (gesture.DecisionExpired, ForegroundChanged):
                self.record.clear(day)  # refused before contact-down: nothing sent
                continue
            except UnsupportedInput:
                self.record.clear(day)
                raise
            self.record.mark(day, "pressed" if sent else "interrupted")
            if sent:
                self.rt.log("已长按铃铛约 4 秒。")
            return bool(sent)
        raise Stop(f"连续 {self.rt.TOUCH_KEY_ATTEMPTS} 次判断用时太长，没有按铃", [])

    # -- the run --

    def card(self):
        return self.map.card()

    def run(self) -> bool:
        from navigation.models import NavigationError, load_route
        from app_paths import RESOURCE_ROOT
        from account_state import game_day
        route = load_route(RESOURCE_ROOT / "resource", ROUTE)
        if route is None or route.map_id != MAP_ID:
            raise NavigationError("读不出 3 号星光结晶路线，没有开始。")
        island = checkpoint((route.document.get("teleport") or {}).get("name", ""))
        hub = checkpoint(HUB)
        if not (self.map.open_map() and self.map.region()):
            return False
        card = self.card()
        if card is None:
            return False
        if card.kind == "not_rung":
            if self.record.attempt(game_day()) is not None:
                raise Stop(MANUAL, [self.map.card_frame])
            self.rt.log("今天还没有召唤摇铃：先传送到无界枢纽按铃。")
            if self.map.transport(hub) is None or not self.ring(hub):
                return False
            if self.map.await_hud("按铃之后", AFTER_BELL_WAIT) is None:
                return False
            if not (self.map.open_map() and self.map.region()):
                return False
            card = self.card()
            if card is None:
                return False
            if card.kind != "rung":
                raise Stop("按铃之后地图卡片仍然显示今天尚未召唤摇铃，没有再按", [self.map.card_frame])
            self.rt.log("地图卡片显示已召唤摇铃。")
        else:
            self.rt.log("地图卡片显示今天已经召唤摇铃，不再按铃。")
        before = card.crystals
        hint = self.map.transport(island)
        if hint is None:
            return False
        result = self.walk(route, hint)
        if self.rt.stopped:
            return False
        after = self.read_after(result)
        self.report(before, after)
        from host import android_user_stop
        if self.rt.stopped or android_user_stop():
            return False
        return result.ok and before is not None and after is not None

    def walk(self, route, hint):
        from app_paths import DATA_ROOT
        from host.android.move import Walker, route_goals
        from host.android.walk_probe import DiagnosticIo
        folder = DATA_ROOT / "debug" / "android_route" / datetime.now().strftime("%Y%m%d-%H%M%S")
        io = DiagnosticIo(self.rt, folder)
        io.record("start", {"route": route.id, "hint": list(hint), "start_radius": route.start_radius,
                            "timeout": route.timeout}, None)
        walker = Walker(io, self.reader, allow_jump=True)
        result = walker.walk(route_goals(route), hint, route.timeout, start_radius=route.start_radius)
        io.record("result", {"outcome": result.outcome, "message": result.message, "position": result.position,
                             "attempted": result.attempted, "confirmed": result.confirmed}, None)
        self.rt.log(f"3 号路线：{result.message}（发出 {result.attempted} 步，完成 {result.confirmed} 步）")
        return result

    def read_after(self, result):
        """The card's crystal count after a walk that ended on the open world (WALK_READ), then the map left onto a
        confirmed HUD. None when not read: nothing is sent after a walk that stopped on another screen. A screen that
        is not the clean HUD where one must be stops with that frame (open_map, leave)."""
        if result.outcome not in WALK_READ:
            return None
        if not (self.map.open_map() and self.map.region()):
            return None
        card = self.card()
        if card is None:
            return None
        if card.kind != "rung":
            raise Stop("路线结束后地图卡片读不出星光结晶的数量", [self.map.card_frame])
        if self.map.leave() is None:
            return None
        return card.crystals

    def report(self, before, after):
        if before is not None and after is not None:
            gained = after - before
            self.rt.log(f"星光结晶：路线前 {before}/30，路线后 {after}/30（本趟 +{gained}）。" if gained >= 0 else
                        f"星光结晶：路线前 {before}/30，路线后 {after}/30（读数变少，收集情况不确定）。")
        else:
            self.rt.log("没有读到路线前后的星光结晶数量，收集情况没有看到。")


def run(rt, options: dict) -> bool:
    """The route; True when the walk arrived at the route's end, both counts were read and the final HUD confirmed.
    Android only (refused before anything else)."""
    from host import capabilities, game_backend
    if not capabilities(game_backend()).touch_actions_only:
        rt.log("星海结晶路线只在安卓模拟器上运行，没有执行。")
        return False
    if set(options) - {"kind"}:
        rt.log("星海结晶路线没有可选参数，没有执行。")
        return False
    rt.log(NOTICE)
    rt.log(BELL_NOTE)
    from navigation.models import NavigationError
    try:
        return bool(Run(rt).run()) and not rt.stopped
    except Stop as error:
        for index, frame in enumerate(error.frames):
            rt.diagnostic(f"android_route_stop_{index}", str(error), image=frame)
        fail(rt, str(error))
    except pages.FrameMismatch as error:  # a tap frame that was not the expected state: no retry
        rt.diagnostic("android_route_stop_0", pages.diagnostic_reason(error), image=error.frame)
        fail(rt, str(error))
    except (pages.GuardRefusal, actions.ScreenUnrecognised, NavigationError, RecordError) as error:
        frame = getattr(error, "frame", None)
        if frame is not None:
            rt.diagnostic("android_route_stop_0", pages.diagnostic_reason(error), image=frame)
        fail(rt, str(error))
    return False


def fail(rt, reason) -> bool:
    from host import android_user_stop
    if not rt.stopped and not android_user_stop():  # a user stop is no failure
        rt.log(f"星海结晶路线：{reason}，本次未完成。")
    return False
