"""Dev-only Android entry MaaNikki_AndroidCrystalRoute: the first 星海 crystal route on the phone client, end to end.

HUD (the game account told from the UID in its corner, host.android.account.identify, nothing sent; an account that
cannot be told stops the run here, whatever the card says) -> big map on 星海 -> the card read: today's bell rung or not,
positively (teleport.card_state: exactly the observed not-rung message, or exactly the PC card's three counters;
anything else: stop, nothing pressed) -> not rung: unless this account (confirmed again on a fresh HUD right before its
record decides, Run.confirm_account) already recorded a bell attempt for today's game day (then stop: the bell needs a
manual check), teleport to 无界枢纽, the minimap fix confirms the hub, the attempt recorded, the bell pressed once (~4 s,
gesture.bell_step, the one gesture allowed past 1.5 s) decided and confirmed on clean HUD frames with the bell button
recognised, the map opened again and the card must now read rung (still not rung: stop, never a second press) ->
teleport to 星梦群屿·主岛 -> xinghai_crystal_3 walked with host.android.move (the teleport point as the first position hint,
the route's start radius, its jumps and its wait) -> after the walk, unless it ended on a screen that stops all input,
the map card read again for the 星光结晶 count and the map left by its back arrow onto a confirmed HUD (the character stays
where the walk ended: on the route, on the ground). Success: the walk arrived, both counts read, the final HUD confirmed
and no stop requested.

The run starts from the open world: before the map opens the map is not known; the tap that opens it may be decided
on a name-only tracker (a restart on 星海), and the map must then show the 星海 card positively (Teleport.open_map);
walking and the bell accept a name-only tracker only on 星海 (move.first_frame_rule), which the teleports establish.
Nothing on the route is a prompt to tap: route 3 has only plain points and one wait, and its crystals are collected by walking through them (as on PC, navigation.daily_route: crystal routes complete by traversal).

This version always walks route 3 (PC picks one of eight routes from the crystal markers the bell reveals on the map;
reading those markers on the phone is not done yet), so on a day the crystals lie on another route it collects
little or nothing, which is expected.

The bell record (BellRecord): per game account, config/android/accounts/<key>/bell_attempts.json in the data directory
(host.android.account.record_path), keyed by the game day (account_state.game_day: 04:00 UTC+8). The record of the data
directory kept before that split (config/android/bell_attempts.json) belongs to an unknown account and is not read. It
is written right before the press is handed to the device path and removed only when the device input count
(DeviceInputState.inputs, counted right before a touch's first contact goes down) shows that nothing went down; a press
that may have gone down stays recorded, whatever it raised. The card, the record's key and the press belong to one game
day: the card's from its own captures' times (Teleport.card), no press within RESET_WINDOW_MIN of the 04:00 reset
(inclusive) or once the day changed, and the press's deadline capped at the next window's start. A record dated after
the current game day is refused, never pruned.

Never a user task, gated like MaaNikki_AndroidWalkProbe: its pipeline node exists only in
resource_android/pipeline/android_crystal_route.json; it is not in agent/host/android/tasks.json, interface.json or any
overlay; the agent refuses it unless scripts/android/runner.py names it in MAANIKKI_ANDROID_DEV_ENTRIES; nikki.gameplay
runs it only when both the task entry and the parameter's kind say so. Every walk step's frame and numbers go to
DATA_ROOT/debug/android_route/<time>/ (walk.jsonl).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import time

from host.android import actions, pages
from host.android.move import BELL_RADIUS_PX
from host.android.teleport import HUD_BELL, STARSEA, STARSEA_MAP, Stop, Teleport, announce, guard, say, stopping

ENTRY = "MaaNikki_AndroidCrystalRoute"
KIND = "android_crystal_route"
ROUTE = "xinghai_crystal_3"
MAP_ID = "starsea"
HUB = "无界枢纽"
NOTICE = ("星海星光结晶路线（安卓，开发用）：这个版本固定走 3 号路线（星梦群屿），当天结晶在其他路线上时收集得很少或没有收集，"
          "这是预期的。")
# A run stopped after the bell press but before its result was read: what the user can do (the coordinator's note).
BELL_NOTE = ("如果按铃之后、读到结果之前任务停了：当天的记录会阻止再按一次；可以手动打开星海地图看卡片，"
             "仍显示尚未召唤摇铃就手动摇一次铃；如果铃其实已经响过，不会有任何损失。"
             "按铃记录按游戏账号分开：开始时要在大世界左下角分开截的两张图上读出同样的 UID；"
             "两次读不出或读到的不一样时，任务在打开地图之前就停下，不传送、不按铃；"
             "按铃之前还会在大世界上再读一次 UID，不是同一个账号就不按。")
BELL_POINT = (616, 612)  # the bell button's centre on every saved HUD frame (template hit at (616,613))
BELL_S = 4.0
AT_POINT_U = 15.0  # the first minimap fix after a teleport lies this close to the point (the walker's own trust aside)
AFTER_BELL_WAIT = 15.0
WALK_READ = ("arrived", "blocked", "timeout", "lost")  # outcomes after which the map may still be opened to read
RECORD_NAME = "bell_attempts.json"  # per game account: account.record_path
LEGACY_RECORD_FILE = ("config", "android", "bell_attempts.json")  # before the per-account split: never read
RECORD_STATES = frozenset({"pressing", "pressed", "interrupted"})
RESET_MINUTE = 4 * 60  # the daily reset, 04:00 UTC+8 (account_state.game_day)
RESET_WINDOW_MIN = 10  # no press from 03:50 to 04:10
MANUAL = "今天已经尝试过按铃（有当天的记录），没有再按；请手动打开星海地图看卡片，仍显示尚未召唤摇铃就手动摇一次铃"
RECORD_BROKEN = "安卓按铃记录读不出或写不进去，没有按铃"
RECORD_FUTURE = ("安卓按铃记录里有比今天还晚的日期（系统时间可能被调回过，或记录文件是从别处恢复的），没有按铃；"
                 "请确认后删除数据目录里这个账号的 config/android/accounts/<账号>/bell_attempts.json")
RECORD_KEEP = 14  # days kept (the day being reserved always among them)
UNIDENTIFIED = "认不出现在登录的是哪个游戏账号"


def checkpoint(name: str) -> dict:
    """The 星海 record of ``name`` in resource/navigation/checkpoints.json."""
    from app_paths import RESOURCE_ROOT
    from navigation.models import NavigationError
    data = json.loads((RESOURCE_ROOT / "resource" / "navigation" / "checkpoints.json").read_text(encoding="utf-8"))
    for record in data["maps"][MAP_ID]:
        if record.get("name") == name:
            return record
    raise NavigationError(f"传送点资料里没有「{name}」。")


def record_key(day) -> str:
    """The key of a bell attempt in one account's record: the game day (the account is the record's folder). Keys
    sort in day order (ISO dates; BellRecord compares them for pruning and future-dated entries)."""
    return str(day)


class RecordError(Exception):
    """The bell record cannot be read or written: nothing is pressed."""


class BellRecord:
    """The bell attempts of one game account (``key``: host.android.account), by game day (see the module's
    docstring)."""

    def __init__(self, root, key):
        from host.android.account import record_path
        self.key = key
        try:
            self.path = record_path(root, key, RECORD_NAME)
        except ValueError as error:
            raise RecordError(RECORD_BROKEN) from error

    def load(self) -> dict:
        if not self.path.is_file():
            return {"schema_version": 1, "scope": "android_game_account", "account": self.key, "attempts": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise RecordError(RECORD_BROKEN) from error
        attempts = data.get("attempts") if isinstance(data, dict) else None
        if not isinstance(attempts, dict) or any(not isinstance(v, dict) or v.get("state") not in RECORD_STATES
                                                 for v in attempts.values()) or data.get("account") != self.key:
            raise RecordError(RECORD_BROKEN)
        return data

    @staticmethod
    def _current(attempts, day) -> dict:
        """``attempts`` when none is dated after game day ``day`` (the card's: the current game day); otherwise
        RecordError (RECORD_FUTURE): a clock set back or a journal restored from elsewhere is refused, never pruned
        (route review round 3, finding 8)."""
        if any(key > record_key(day) for key in attempts):
            raise RecordError(RECORD_FUTURE)
        return attempts

    def attempt(self, day) -> dict | None:
        return self._current(self.load()["attempts"], day).get(record_key(day))

    def _write(self, day, change):
        from file_io import exclusive_file_lock, write_json
        try:
            with exclusive_file_lock(self.path.with_name("bell_attempts.lock")):
                data = self.load()
                attempts = self._current(data["attempts"], day)
                change(attempts)
                # RECORD_KEEP days (two weeks are plenty); the day being written is never pruned
                keep = set(sorted(attempts)[-RECORD_KEEP:]) | ({record_key(day)} & set(attempts))
                data["attempts"] = {key: attempts[key] for key in sorted(keep)}
                write_json(self.path, data)
        except OSError as error:
            raise RecordError(RECORD_BROKEN) from error

    def mark(self, day, state):
        if state not in RECORD_STATES:
            raise ValueError(state)
        self._write(day, lambda attempts: attempts.__setitem__(record_key(day), {
            "state": state, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}))

    def clear(self, day):
        self._write(day, lambda attempts: attempts.pop(record_key(day), None))


class Run:
    def __init__(self, rt, *, reader=None, world=None, io=None, record=None, accounts=None):
        self.rt = rt
        self.map = Teleport(rt, world, io=io, reader=reader, now=lambda: now_utc())
        self._record = record
        if accounts is None:
            from host.android import account as accounts
        self.accounts = accounts  # identify, confirm, Unidentified, refusal (host.android.account; tests: a stand-in)
        self.binding = None  # the account told at the start, renewed by each confirmation

    @property
    def reader(self):
        return self.map.reader

    @property
    def io(self):
        return self.map.io

    @property
    def record(self) -> BellRecord:
        """The bell record of the account told at the start; RecordError without one (nothing is pressed)."""
        if self.binding is None:
            raise RecordError(UNIDENTIFIED)
        if self._record is None:
            from app_paths import DATA_ROOT
            self._record = BellRecord(DATA_ROOT, self.binding.key)
        if self._record.key != self.binding.key:
            raise RecordError(RECORD_BROKEN)
        return self._record

    def identify_account(self) -> bool:
        """The account from the HUD at the start (host.android.account.identify; nothing sent), before anything else
        is done: an account that cannot be told stops the run there, whatever the card would say (Stop). False when
        the run stopped."""
        try:
            binding = self.accounts.identify(self.rt)
        except self.accounts.Unidentified as error:
            raise Stop(f"{self.accounts.refusal(error)}，不知道是哪个账号的按铃记录，没有开始", error.frames) from None
        if binding is None:
            return False
        self.binding = binding
        return True

    def confirm_account(self) -> bool:
        """The account told at the start confirmed on a fresh HUD (one read equal to its key) right before its record
        decides whether to reserve and press (host.android.account.confirm); Stop when it is not that account or
        cannot be read, False when the run stopped."""
        try:
            binding = self.accounts.confirm(self.rt, self.binding)
        except self.accounts.Unidentified as error:
            raise Stop(f"{self.accounts.refusal(error)}，没有按铃", error.frames) from None
        if binding is None:
            return False
        self.binding = binding
        return True

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

    def ring(self, hub, day) -> bool:
        """One bell press for game day ``day`` (the day the not-rung card was read on), decided on a HUD frame at the
        hub by the walker's first-frame rule and confirmed on the next by its later-frame rule (hud_pair); the gesture
        carries the confirming frame, the latest capture, and its expiry. Refused within RESET_WINDOW_MIN of the
        04:00 reset (inclusive, at full precision) or once the game day is no longer ``day``, and the press must go
        down before the next exclusion window starts (its not_after is capped there; gesture.perform checks it right
        before contact-down). The decision pair starts again when either frame's focus mark is no longer valid. The
        attempt is recorded before the press is handed
        over and removed only when the device input count (DeviceInputState.inputs, counted right before a touch's
        first contact goes down) shows that nothing went down: an expired decision or a focus change then decides
        again, a stop seen before contact-down ends without a record. Anything that may have gone down keeps the
        record ("pressing" when it raised, "interrupted" when cut short). Never a second press."""
        from host import device_input
        from host.android import gesture, touch_point
        from host.android.move import MAX_DECISION_AGE_S
        from task_control import ForegroundChanged
        same_day(day)
        for _attempt in range(self.rt.TOUCH_KEY_ATTEMPTS):
            # The account confirmed on a fresh HUD before its record decides (again after any focus change below).
            if not self.confirm_account():
                return False
            if self.record.attempt(day) is not None:
                raise Stop(MANUAL, [])
            pair = self.map.hud_pair("按铃", self.bell_target(hub), STARSEA_MAP, (BELL_POINT, BELL_RADIUS_PX),
                                     contact="bell")
            if pair is None:
                return False
            first, second, box = pair
            if not (self.rt._frame_still_valid(first.frame) and self.rt._frame_still_valid(second.frame)
                    and self.binding.valid(self.rt)):
                continue  # a focus change voided the pair or the account's confirmation (route r3 finding 7)
            clock = time.monotonic()
            window = same_day(day, [second.frame])  # seconds until the next exclusion window starts
            not_after = min(second.at + MAX_DECISION_AGE_S, clock + window)
            before = device_input().inputs
            self.record.mark(day, "pressing")  # raises when it cannot be written: then nothing is pressed
            try:
                sent = gesture.bell_step(self.rt, second.frame, touch_point(box), BELL_S, not_after=not_after)
            except gesture.DecisionExpired:  # raised before anything was posted (the dispatch's last check, or
                self.record.clear(day)        # gesture.perform right before the first down): definitely unsent
                continue
            except ForegroundChanged:
                if device_input().inputs != before:  # not raised before contact-down after all: keep the record
                    raise
                self.record.clear(day)
                continue
            except BaseException:
                if device_input().inputs == before:  # refused before anything went down
                    self.record.clear(day)
                raise
            if device_input().inputs == before:  # stopped before contact-down: nothing pressed, nothing recorded
                self.record.clear(day)
                return False
            self.record.mark(day, "pressed" if sent else "interrupted")
            if sent:
                say(self.rt, "已长按铃铛约 4 秒。")
            return bool(sent)
        raise Stop(f"连续 {self.rt.TOUCH_KEY_ATTEMPTS} 次判断用时太长，没有按铃", [])

    # -- the run --

    def card(self):
        return self.map.card()

    def open_card(self, map_id):
        """The map opened from the open world on ``map_id`` (None at the start: the map is not known yet) with its
        first frame carried into the region step, and the 星海 card read."""
        entry = self.map.open_map(map_id)
        if entry is None or not self.map.region(STARSEA, entry):
            return None
        return self.card()

    def run(self) -> bool:
        from navigation.models import NavigationError, load_route
        from app_paths import RESOURCE_ROOT
        route = load_route(RESOURCE_ROOT / "resource", ROUTE)
        if route is None or route.map_id != MAP_ID:
            raise NavigationError("读不出 3 号星光结晶路线，没有开始。")
        island = checkpoint((route.document.get("teleport") or {}).get("name", ""))
        hub = checkpoint(HUB)
        if not self.identify_account():
            return False
        card = self.open_card(None)
        if card is None:
            return False
        day = self.map.card_day  # the card's own day (its captures): the record's key, the only day to press on
        if card.kind == "not_rung":
            same_day(day, [self.map.card_frame])
            if self.record.attempt(day) is not None:
                raise Stop(MANUAL, [self.map.card_frame])
            say(self.rt, "今天还没有召唤摇铃：先传送到无界枢纽按铃。")
            if self.map.transport(hub) is None or not self.ring(hub, day):
                return False
            if self.map.await_hud("按铃之后", AFTER_BELL_WAIT) is None:
                return False
            card = self.open_card(STARSEA_MAP)
            if card is None:
                return False
            if self.map.card_day != day:
                raise Stop("按铃之后已经过了每日 4 点重置，卡片读数不能说明这次按铃的结果", [self.map.card_frame])
            if card.kind != "rung":
                raise Stop("按铃之后地图卡片仍然显示今天尚未召唤摇铃，没有再按", [self.map.card_frame])
            say(self.rt, "地图卡片显示已召唤摇铃。")
        else:
            say(self.rt, "地图卡片显示今天已经召唤摇铃，不再按铃。")
        before = card.crystals
        hint = self.map.transport(island)
        if hint is None:
            return False
        result = self.walk(route, hint)
        if stopping(self.rt):
            return False
        after = self.read_after(result)
        if stopping(self.rt):  # checked again after the blocking reads and writes, before anything is reported
            return False
        self.report(before, after)
        if stopping(self.rt):
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
        walker = Walker(io, self.reader, allow_jump=True, map_id=route.map_id)
        result = walker.walk(route_goals(route), hint, route.timeout, start_radius=route.start_radius)
        io.record("result", {"outcome": result.outcome, "message": result.message, "position": result.position,
                             "attempted": result.attempted, "confirmed": result.confirmed}, None)
        # after the blocking write: a stop published meanwhile, or during the message's own write, shows nothing
        announce(self.rt, f"3 号路线：{result.message}（发出 {result.attempted} 步，完成 {result.confirmed} 步）")
        return result

    def read_after(self, result):
        """The card's crystal count after a walk that ended on the open world (WALK_READ), then the map left onto a
        HUD the walker would start on. None when not read: nothing is sent after a walk that stopped on another
        screen. A screen that is not what must be there stops with that frame (open_map, leave)."""
        if result.outcome not in WALK_READ:
            return None
        card = self.open_card(STARSEA_MAP)
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
            announce(self.rt, f"星光结晶：路线前 {before}/30，路线后 {after}/30（本趟 +{gained}）。" if gained >= 0
                     else f"星光结晶：路线前 {before}/30，路线后 {after}/30（读数变少，收集情况不确定）。")
        else:
            announce(self.rt, "没有读到路线前后的星光结晶数量，收集情况没有看到。")


def now_utc():
    return datetime.now(timezone.utc)


def game_day_now():
    from account_state import game_day
    return game_day(now_utc())


def reset_window(now):
    """``(inside, start)`` for the exclusion window RESET_WINDOW_MIN either side of the 04:00 reset (UTC+8), at full
    precision with both ends inclusive (03:50:00.000 and 04:10:00.000 are inside): whether ``now`` lies in one, and
    otherwise when the next one starts (an aware datetime)."""
    from account_state import GAME_TIMEZONE
    local = now.astimezone(GAME_TIMEZONE)
    reset = local.replace(hour=RESET_MINUTE // 60, minute=RESET_MINUTE % 60, second=0, microsecond=0)
    half = timedelta(minutes=RESET_WINDOW_MIN)
    resets = [reset + timedelta(days=offset) for offset in (-1, 0, 1)]
    if any(r - half <= local <= r + half for r in resets):
        return True, None
    return False, min(r - half for r in resets if r - half > local)


def same_day(day, frames=()) -> float:
    """Stop unless it is still game day ``day`` and not within the exclusion window around the 04:00 reset
    (reset_window): a press near the reset could land on the next day, after the card and the record spoke for this
    one. Returns the seconds until the next window starts (a press's not_after is capped there)."""
    from account_state import game_day
    now = now_utc()
    inside, start = reset_window(now)
    if inside:
        raise Stop("离每日 4 点重置太近，没有按铃", list(frames))
    if game_day(now) != day:
        raise Stop("已经过了每日 4 点重置，读到的地图卡片不能代表今天，没有按铃", list(frames))
    return (start - now).total_seconds()


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
    say(rt, NOTICE)
    say(rt, BELL_NOTE)
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
