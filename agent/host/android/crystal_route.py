"""Dev-only Android entry MaaNikki_AndroidCrystalRoute: the first 星海 crystal route on the phone client, end to end.

HUD -> big map on 星海 -> the card read: today's bell rung or not, positively (teleport.card_state; neither readable:
stop, nothing pressed) -> not rung: teleport to 无界枢纽, the minimap fix confirms the hub, the bell pressed once
(~4 s, gesture.bell_step, the one gesture allowed past 1.5 s) on a clean HUD with the bell button recognised, the map
opened again and the card must now read rung (still not rung: stop, never a second press) -> teleport to
星梦群屿·主岛 -> xinghai_crystal_3 walked with host.android.move (the teleport point as the first position hint, the
route's start radius, its jumps and its wait) -> after the walk, unless it ended on a screen that stops all input,
the map card read again for the 星光结晶 count, and the map left by its back arrow (the character stays where the walk
ended: on the route, on the ground).

This version always walks route 3 (PC picks one of eight routes from the crystal markers the bell reveals on the map;
reading those markers on the phone is not done yet), so on a day the crystals lie on another route it collects
little or nothing, which is expected.

Never a user task, gated like MaaNikki_AndroidWalkProbe: its pipeline node exists only in
resource_android/pipeline/android_crystal_route.json; it is not in agent/host/android/tasks.json, interface.json or any
overlay; the agent refuses it unless scripts/android/runner.py names it in MAANIKKI_ANDROID_DEV_ENTRIES; nikki.gameplay
runs it only when both the task entry and the parameter's kind say so. Every walk step's frame and numbers go to
DATA_ROOT/debug/android_route/<time>/ (walk.jsonl).
"""
from __future__ import annotations

from datetime import datetime
import math
import time

from host.android import actions, pages
from host.android.teleport import HUD_BELL, Stop, Teleport, clean_hud

ENTRY = "MaaNikki_AndroidCrystalRoute"
KIND = "android_crystal_route"
ROUTE = "xinghai_crystal_3"
MAP_ID = "starsea"
HUB = "无界枢纽"
NOTICE = ("星海星光结晶路线（安卓，开发用）：这个版本固定走 3 号路线（星梦群屿），当天结晶在其他路线上时收集得很少或没有收集，"
          "这是预期的。")
BELL_POINT = (616, 612)  # the bell button's centre on every saved HUD frame (template hit at (616,613))
BELL_S = 4.0
AT_POINT_U = 15.0  # the first minimap fix after a teleport lies this close to the point (the walker's own trust aside)
NARRATION_WAIT = 20.0
AFTER_BELL_WAIT = 15.0
WALK_READ = ("arrived", "blocked", "timeout", "lost")  # outcomes after which the map may still be opened to read


def checkpoint(name: str) -> dict:
    """The 星海 record of ``name`` in resource/navigation/checkpoints.json."""
    import json
    from app_paths import RESOURCE_ROOT
    from navigation.models import NavigationError
    data = json.loads((RESOURCE_ROOT / "resource" / "navigation" / "checkpoints.json").read_text(encoding="utf-8"))
    for record in data["maps"][MAP_ID]:
        if record.get("name") == name:
            return record
    raise NavigationError(f"传送点资料里没有「{name}」。")


class Run:
    def __init__(self, rt, *, reader=None, world=None, io=None):
        from host.android.move import RuntimeIo
        self.rt = rt
        self.map = Teleport(rt, world)
        self.io = io or RuntimeIo(rt)
        self._reader = reader

    @property
    def reader(self):
        if self._reader is None:
            from host.android.minimap import MinimapReader
            self._reader = MinimapReader(MAP_ID)
        return self._reader

    # -- the bell --

    def at_point(self, obs, record) -> bool:
        """The minimap fix on ``obs`` (hinted with the point) is trusted and within AT_POINT_U of it."""
        point = (float(record["x"]), float(record["y"]))
        if not self.reader.present(obs.frame):
            return False
        fix = self.reader.fix(obs.frame, point, 40.0)
        return bool(fix.reliable) and math.dist((fix.x, fix.y), point) <= AT_POINT_U

    def bell_frame(self, hub):
        """A clean HUD frame at the hub with the bell button where it belongs: ``(obs, point)``. Ambient narration is
        waited through (no input) up to NARRATION_WAIT; anything else stops with the frame."""
        from host.android import touch_point
        start = time.monotonic()
        while True:
            obs = self.io.observe()
            if obs is None:
                return None
            if obs.kind == "hud":
                break
            if obs.kind not in ("narration", "blank") or time.monotonic() - start >= NARRATION_WAIT:
                raise Stop(f"要按铃时画面不是干净的大世界（{obs.reason or '有旁白'}），没有按铃", [obs.frame])
            if not self.rt.pause(.4):
                return None
        if not self.at_point(obs, hub):
            raise Stop("小地图上的位置不在无界枢纽，没有按铃", [obs.frame])
        recognize = self.rt._recognizer_on(obs.frame)
        result = recognize(HUD_BELL)
        if not actions._hit(result):
            raise Stop("大世界上没有认出铃铛按钮，没有按铃", [obs.frame])
        point = touch_point(actions._box(result))
        if not actions._near(point, BELL_POINT):  # actions.CONFIRM_TOLERANCE (8 px) on each axis
            raise Stop("铃铛按钮不在平常的位置，没有按铃", [obs.frame])
        word = pages._forbidden_near(recognize, actions._box(result))
        if word:
            raise pages.GuardRefusal(pages.GUARD_TAP, obs.frame, word)
        return obs, point

    def ring(self, hub) -> bool:
        """One bell press, decided on a clean HUD frame at the hub and confirmed on a second (the gesture carries
        the second, the latest capture, and its expiry). A decision that aged out before contact-down was not sent
        and is decided again; a press is never repeated."""
        from host.android import gesture
        from host.android.move import MAX_DECISION_AGE_S
        for _ in range(self.rt.TOUCH_KEY_ATTEMPTS):
            first = self.bell_frame(hub)
            if first is None:
                return False
            second = self.bell_frame(hub)
            if second is None:
                return False
            obs, point = second
            if not actions._near(point, first[1]):
                raise Stop("确认时铃铛按钮的位置变了，没有按铃", [obs.frame])
            try:
                sent = gesture.bell_step(self.rt, obs.frame, list(point), BELL_S,
                                         not_after=obs.captured + MAX_DECISION_AGE_S)
            except gesture.DecisionExpired:
                continue
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
        route = load_route(RESOURCE_ROOT / "resource", ROUTE)
        if route is None or route.map_id != MAP_ID:
            raise NavigationError("读不出 3 号星光结晶路线，没有开始。")
        start = route.document.get("teleport") or {}
        island = checkpoint(start.get("name", ""))
        hub = checkpoint(HUB)
        screen, frame, _recognize = pages.screen_now(self.rt)
        if frame is None:
            return False
        if screen is None or screen.kind != "hud" or not self.rt._frame_still_valid(frame):
            raise Stop("开始时画面不是大世界，没有开始", [frame])
        if not (self.map.open_map() and self.map.region()):
            return False
        card = self.card()
        if card is None:
            return False
        if card.kind == "not_rung":
            self.rt.log("今天还没有召唤摇铃：先传送到无界枢纽按铃。")
            if self.map.transport(hub) is None:
                return False
            if not self.ring(hub):
                return False
            if clean_hud(self.rt, self.io, "按铃之后", AFTER_BELL_WAIT) is None:
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
        self.report(result, before, after)
        return result.ok and not self.rt.stopped

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
        """The card's crystal count after a walk that ended on the open world (WALK_READ), the map then left; None
        when not read (nothing sent after a walk that stopped on another screen)."""
        if result.outcome not in WALK_READ:
            return None
        screen, frame, _recognize = pages.screen_now(self.rt)
        if frame is None or screen is None or screen.kind != "hud":
            return None
        if not (self.map.open_map() and self.map.region()):
            return None
        card = self.card()
        if card is None or not self.map.leave():
            return None
        return card.crystals

    def report(self, result, before, after):
        if before is not None and after is not None:
            gained = after - before
            self.rt.log(f"星光结晶：路线前 {before}/30，路线后 {after}/30（本趟 +{gained}）。" if gained >= 0 else
                        f"星光结晶：路线前 {before}/30，路线后 {after}/30（读数变少，收集情况不确定）。")
        else:
            self.rt.log("没有读到路线前后的星光结晶数量，收集情况没有看到。")


def run(rt, options: dict) -> bool:
    """The route; True when the walk arrived at the route's end. Android only (refused before anything else)."""
    from host import capabilities, game_backend
    if not capabilities(game_backend()).touch_actions_only:
        rt.log("星海结晶路线只在安卓模拟器上运行，没有执行。")
        return False
    if set(options) - {"kind"}:
        rt.log("星海结晶路线没有可选参数，没有执行。")
        return False
    rt.log(NOTICE)
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
    except (pages.GuardRefusal, actions.ScreenUnrecognised, NavigationError) as error:
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
