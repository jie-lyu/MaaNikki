"""兑换码 (MaaNikki_RedeemCodes) on the Android client: redeem the public and manual codes in 设置 -> 其他 -> 兑换码.

HUD -> 美鸭梨 menu (duck) -> gear -> 设置 -> 其他 tab -> one small list swipe when the 兑换码 row is not wholly in view ->
the row's 使用 -> 兑换奖励 modal -> per code: clear a field that is not empty (its X), tap the field, type the code
through the framework's text input (pages.input_text_on_screen: the device's clipboard and a paste key; never the
host clipboard, never the in-modal paste icon), read the field back and require exactly the code, tap the modal's
使用 (gold) once, classify what follows -> the modal's ✕ -> back to the HUD.

The codes come from the shared collection (daily/redeem_codes.collect_candidate_codes with the task's options; an
Android start submits empty options, so online sync and no manual codes). The PC class is not used: it reads the UID
the PC way (the HUD footer, then ui.main()) for the account record and its run() reads and writes the host clipboard.
The phone flow keeps its own record (Record below) in the data directory, not per game account: the phone client's
UID is not read yet.

What the saved frames show (1280x720): the gear on the menu's bottom bar ~(633,668); 设置 opens on 图像; the 其他 tab
label ~[82,600,55,32]; on 其他 the 兑换码 row is cut by the list's bottom fade until the list is swiped up (survey:
(700,520)->(700,300)), after which its label ~[289,571,77,33] and 使用 ~[949,569,79,34] line up, 52 px above
全部恢复默认 (a forbidden word: the guard's 40 px margin holds by ~10 px). The modal: field [395,318,385,50] with the
placeholder 「点击此处输入兑换码」, its clear X ~[794,329,28,28] once filled, the in-modal paste icon ~(878,341) (never
tapped), 使用 [603,480,70,39] gold (0.86 of its face) once filled, ✕ ~(945,193). Tapping the field opens the
system's white input strip across the bottom (y >= ~665); that strip is how a focused field is recognised.

Never seen on the device: the text input inside this flow (phase A typed into the field with the probe), the error
hint after a submit, the success layer and how it closes. So the result is classified only from what is positively
read: a 「恭喜获得」 layer (success; one tap on blank space, as in the monthly pass), or a hint whose text the PC's
classification knows (cooldown, already used, expired/invalid) on the modal; anything else, or nothing within
RESULT_WAIT, stops the task with that frame saved and sends nothing more. A submit is never repeated.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

import cv2

from host.android import actions, pages

MENU, SETTINGS, MODAL = "美鸭梨", "设置", "兑换奖励"
MENU_SETTINGS = "MaaNikki_Android_MenuSettings"
OTHER_TAB = "MaaNikki_Android_SettingsOtherTab"
ROW_LABEL = "MaaNikki_Android_RedeemRowLabel"
ROW_USE = "MaaNikki_Android_RedeemRowUse"
PLACEHOLDER = "MaaNikki_Android_RedeemPlaceholder"
FIELD = "MaaNikki_Android_RedeemField"
CLEAR = "MaaNikki_Android_RedeemClear"
USE = "MaaNikki_Android_RedeemUse"
HINT = "MaaNikki_Android_RedeemHint"
TOAST = "MaaNikki_Android_RedeemToast"
LAYER_TITLE = "MaaNikki_Android_RewardLayerTitle"  # android_monthly.json: 「恭喜获得」
LAYER_HINT = "MaaNikki_Android_RewardLayerHint"  # 「点击空白区域继续」
NODES = (MENU_SETTINGS, OTHER_TAB, ROW_LABEL, ROW_USE, PLACEHOLDER, FIELD, CLEAR, USE, HINT, TOAST)

# The 兑换码 row: its 使用 (the only 使用 on 其他) and a 「兑换码」 label whose centres are this close vertically (the
# section header 「兑换码」 sits ~58 px above the row; the faded row label of the unswiped list reads 「总换码」).
ROW_ALIGN = 14
LIST_SWIPE = ((700, 520), (700, 300))  # empty row space between labels and buttons (phase A's survey swipe)
SWIPE_MS = 500
FIELD_BOX = (395, 318, 385, 50)
USE_FACE = (545, 482, 190, 36)
USE_GOLD = .4  # saved frames: 0.862 with a code in the field, 0.000 empty
STRIP = (0, 672, 1280, 46)
STRIP_WHITE = .8  # saved frames: 1.000 and 0.975 with the field focused, at most 0.168 on any other frame
# The reward layer's dismissal tap: blank on the 「恭喜获得」 layer (as in the monthly pass, in the bottom band left of
# its hint ~[569,640,140,19]) and on the modal over 设置 beneath it (saved modal and 其他 frames: >= 45 px from every
# label, the 兑换码 row's label the nearest; far from 全部恢复默认), above the input strip and left of the system's
# 「Shell pasted from your clipboard」 toast ~[484,632,311,56] that follows a text input.
DISMISS_POINT = (400, 650)
USE_COOLDOWN_SECONDS = 3.2  # the PC spacing between two submits (daily/redeem_codes.py)
OPEN_WAIT = 5
TRANSITION_ALLOWANCE = 1.5
FIELD_WAIT = 4  # the field showing what was typed / cleared, or the strip opening
RESULT_WAIT = 6
AFTER_DISMISS_WAIT = 5

RECORD_FILE = ("config", "android", "redeem_codes.json")
RECORD_OUTCOMES = frozenset({"redeemed", "already_used", "expired", "cooldown"})


class Record:
    """The codes this data directory has seen an outcome for on the Android client (the PC keeps them per game account
    in AccountState). Same skip rules as the PC: redeemed and already used codes are skipped, expired ones unless
    entered manually; a cooldown is kept as evidence only and tried again next run."""

    def __init__(self, root):
        self.path = Path(root).joinpath(*RECORD_FILE)

    def load(self) -> dict:
        if not self.path.is_file():
            return {"schema_version": 1, "scope": "android_data_directory", "items": {}}
        data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, dict) or any(not isinstance(v, dict) or v.get("outcome") not in RECORD_OUTCOMES
                                              for v in items.values()):
            raise ValueError("安卓兑换码记录无效")
        return data

    def outcomes(self) -> dict:
        return {code: entry["outcome"] for code, entry in self.load()["items"].items()}

    def record(self, code, outcome, detail=""):
        from file_io import exclusive_file_lock, write_json
        clean = str(code or "").strip()
        if not clean or len(clean) > 40 or any(ch.isspace() for ch in clean) or outcome not in RECORD_OUTCOMES:
            raise ValueError("兑换码或处理结果无效")
        with exclusive_file_lock(self.path.with_name("redeem_codes.lock")):
            data = self.load()
            entry = {"outcome": outcome, "confirmed_at": datetime.now(timezone.utc).isoformat()}
            if detail:
                entry["detail"] = str(detail)[:120]
            data["items"][clean] = entry
            write_json(self.path, data)


def pending_codes(candidates, done) -> list[str]:
    """The PC's filter (RedeemCodes.run) over ``(code, is_manual)`` candidates and the recorded outcomes."""
    pending = []
    for code, is_manual in candidates:
        previous = done.get(code)
        if previous in ("redeemed", "already_used") or (not is_manual and previous == "expired"):
            continue
        pending.append(code)
    return pending


def candidates(rt):
    """The shared collection with the task's options (defaults when an Android start submits none)."""
    from daily.redeem_codes import collect_candidate_codes
    from daily.runtime import parameters
    online = parameters(rt.context, "MaaNikki_RedeemCodes_Online").get("value", True) is not False
    extra_raw = str(parameters(rt.context, "MaaNikki_RedeemCodes_Extra").get("value", "") or "")
    return collect_candidate_codes(online=online, extra_raw=extra_raw, log=rt.log)


# ---- reading one frame ----

def _fraction(frame, box, lower, upper) -> float:
    x, y, w, h = box
    hsv = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
    return cv2.countNonZero(cv2.inRange(hsv, lower, upper)) / (w * h)


def strip_open(frame) -> bool:
    """The system's white input strip across the bottom: the field has the keyboard focus."""
    return _fraction(frame, STRIP, (0, 0, 235), (180, 25, 255)) >= STRIP_WHITE


def use_gold(frame) -> float:
    return _fraction(frame, USE_FACE, (10, 85, 130), (35, 255, 255))


def _rows(result):
    if not actions._hit(result):
        return []
    rows = list(getattr(result, "filtered_results", None) or []) or [getattr(result, "best_result", None)]
    return [(str(getattr(row, "text", "") or ""), [int(v) for v in getattr(row, "box", None) or []])
            for row in rows if row is not None]


def _centre_y(box) -> float:
    return box[1] + box[3] / 2


def other_view(screen, recognize) -> bool:
    """The 设置 page showing the 其他 tab's content (its 兑换码 section: no other tab has a 「兑换码」 label)."""
    return pages.is_page(screen, SETTINGS) and actions._hit(recognize(ROW_LABEL))


def row_use(recognize):
    """The 兑换码 row's 使用 box when the row is wholly in view (its 使用 and its 「兑换码」 label line up), else None."""
    uses = [box for text, box in _rows(recognize(ROW_USE)) if len(box) == 4]
    if len(uses) != 1:
        return None
    labels = [box for text, box in _rows(recognize(ROW_LABEL)) if len(box) == 4]
    use = uses[0]
    return use if any(abs(_centre_y(label) - _centre_y(use)) <= ROW_ALIGN for label in labels) else None


def _other(screen, frame, recognize) -> bool:
    return other_view(screen, recognize)


def is_modal(screen) -> bool:
    return pages.is_modal(screen, MODAL)


def read_field(frame, recognize):
    """``(state, text)`` of the modal's field: "empty" (the placeholder, no clear X), "filled" (no placeholder, the
    clear X; ``text`` is what it reads, without spaces) or "unknown"."""
    placeholder, clear = actions._hit(recognize(PLACEHOLDER)), actions._hit(recognize(CLEAR))
    if placeholder and not clear:
        return "empty", ""
    if clear and not placeholder:
        rows = sorted(_rows(recognize(FIELD)), key=lambda row: row[1][0] if row[1] else 0)
        return "filled", re.sub(r"\s+", "", "".join(text for text, _box in rows))
    return "unknown", ""


def _inside(box, region) -> bool:
    if len(box) != 4:
        return False
    cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
    x, y, w, h = region
    return x <= cx <= x + w and y <= cy <= y + h


def read_hint(recognize):
    """``(kind, text)`` of a result hint on the modal whose text the PC's classification knows ("cooldown",
    "already_used", "expired"), else ``(None, "")``; ``("unreadable", "")`` when a check could not run. The field
    itself (it holds the code) is not read as a hint."""
    from daily.redeem_codes import _classify_hint
    for node in (HINT, TOAST):
        result = recognize(node)
        if result is None:
            return "unreadable", ""
        for text, box in _rows(result):
            if _inside(box, FIELD_BOX):
                continue
            kind = _classify_hint(text)
            if kind is not None:
                return kind, text
    return None, ""


def is_reward_layer(screen, recognize) -> bool:
    """A 「恭喜获得」 layer (title and its bottom hint read), not the quit dialog. Not seen after a code: the modal
    beneath may or may not still show its close mark through the dim, so the screen may be "unknown" or "modal"."""
    if screen is None or screen.kind not in ("unknown", "modal"):
        return False
    for node in (actions.QUIT_TITLE, actions.QUIT_CANCEL):
        result = recognize(node)
        if result is None or actions._hit(result):
            return False
    return actions._hit(recognize(LAYER_TITLE)) and actions._hit(recognize(LAYER_HINT))


# ---- waiting and stopping ----

class Stop(Exception):
    """Something unexpected: report it and send nothing more (not even the way back). ``frames`` are saved."""

    def __init__(self, reason, frames=()):
        super().__init__(reason)
        self.frames = [frame for frame in frames if frame is not None]


def wait_until(rt, what, done, source=lambda screen, frame, recognize: False, seconds=OPEN_WAIT):
    """Poll fresh frames until ``done(screen, frame, recognize)`` returns something other than None/False and return
    it with the frame: ``(value, screen, frame, recognize)``. Frames for which ``source`` holds (the state being left)
    are waited through up to ``seconds``; a frame on which nothing at all is recognised (pages.nothing_recognised)
    only within TRANSITION_ALLOWANCE; any other frame, or the time running out, saves that frame and stops. None when
    the run stopped."""
    start = time.monotonic()
    while not rt.stopped:
        screen, frame, recognize = pages.screen_now(rt)
        if frame is None:
            return None
        if rt._frame_still_valid(frame):
            value = done(screen, frame, recognize)
            if value is not None and value is not False:
                return value, screen, frame, recognize
            elapsed = time.monotonic() - start
            transition = pages.nothing_recognised(screen, recognize) and elapsed <= TRANSITION_ALLOWANCE
            if not (transition or source(screen, frame, recognize)):
                raise Stop(f"{what}时出现了不该出现的画面", [frame])
            if elapsed >= seconds:
                raise Stop(f"{what}超时", [frame])
        rt.pause(.4)
    return None


def _modal_field(state=None, *, focused=None, text=None):
    """``done``/``source`` for wait_until: the modal with its field in ``state`` (and focused or not, and reading
    ``text``, when given); returns the field's ``(state, text)``."""
    def check(screen, frame, recognize):
        if not is_modal(screen):
            return None
        field = read_field(frame, recognize)
        if state is not None and field[0] != state:
            return None
        if focused is not None and strip_open(frame) != focused:
            return None
        if text is not None and field[1] != text:
            return None
        return field
    return check


# ---- the steps ----

def open_settings(rt) -> bool:
    if not rt.key("menu"):
        return False
    if wait_until(rt, "打开美鸭梨菜单", lambda s, f, r: pages.is_page(s, MENU),
                  lambda s, f, r: s.kind == "hud") is None:
        return False
    if not pages.tap_on_page(rt, MENU, MENU_SETTINGS, retry=False):
        return False
    return wait_until(rt, "打开设置", lambda s, f, r: pages.is_page(s, SETTINGS),
                      lambda s, f, r: pages.is_page(s, MENU)) is not None


def open_other(rt) -> bool:
    found = wait_until(rt, "确认设置页面", lambda s, f, r: pages.is_page(s, SETTINGS))
    if found is None:
        return False
    _value, screen, _frame, recognize = found
    if other_view(screen, recognize):
        return True
    # The tab label, on the 设置 page showing another tab (both tap frames).
    if not pages.tap_on_page(rt, SETTINGS, OTHER_TAB, retry=False,
                             accept=lambda frame, recognize_: not actions._hit(recognize_(ROW_LABEL))):
        return False
    return wait_until(rt, "切换到「其他」", _other, lambda s, f, r: pages.is_page(s, SETTINGS)) is not None


def open_modal(rt) -> bool:
    """From the 其他 tab to the 兑换奖励 modal: one guarded list swipe when the row is not wholly in view, then the
    row's 使用 (both tap frames must show it lined up with its label), then wait for the modal."""
    found = wait_until(rt, "确认「其他」页签", _other)
    if found is None:
        return False
    if row_use(found[3]) is None:
        if not pages.swipe_on_page(rt, SETTINGS, *LIST_SWIPE, duration=SWIPE_MS,
                                   accept=lambda frame, recognize: actions._hit(recognize(ROW_LABEL))):
            return False
        if wait_until(rt, "滑动设置列表", lambda s, f, r: other_view(s, r) and row_use(r) is not None,
                      lambda s, f, r: other_view(s, r), seconds=3) is None:
            return False
    if not pages.tap_on_page(rt, SETTINGS, ROW_USE, retry=False,
                             accept=lambda frame, recognize: row_use(recognize) is not None):
        return False
    return wait_until(rt, "打开兑换奖励弹窗", lambda s, f, r: is_modal(s), _other) is not None


def _modal_accept(state, focused=None):
    def accept(frame, recognize):
        field = read_field(frame, recognize)
        return field[0] == state and (focused is None or strip_open(frame) == focused)
    return accept


def fill(rt, code) -> bool:
    """The modal's field empty, focused, then holding exactly ``code`` (read back by OCR). Stop on anything else:
    a field that reads other text after the input is never submitted."""
    def settled(state):
        def check(screen, frame, recognize):
            field = _modal_field(state)(screen, frame, recognize)
            return None if field is None or field[0] == "unknown" else (field[0], strip_open(frame))
        return check
    found = wait_until(rt, "确认兑换奖励弹窗", settled(None))
    if found is None:
        return False
    state, focused = found[0]
    if state == "filled":
        if not pages.tap_on_page(rt, MODAL, CLEAR, kind="modal", retry=False, accept=_modal_accept("filled")):
            return False
        found = wait_until(rt, "清空兑换码输入框", settled("empty"), _modal_field("filled"), seconds=FIELD_WAIT)
        if found is None:
            return False
        focused = found[0][1]
    if not focused:
        if not pages.tap_on_page(rt, MODAL, PLACEHOLDER, kind="modal", retry=False,
                                 accept=_modal_accept("empty", focused=False)):
            return False
        if wait_until(rt, "点开兑换码输入框", _modal_field("empty", focused=True),
                      _modal_field("empty", focused=False), seconds=FIELD_WAIT) is None:
            return False
    if not pages.input_text_on_screen(rt, code, "兑换奖励弹窗上已点开的空输入框",
                                      lambda s, f, r: _modal_field("empty", focused=True)(s, f, r) is not None):
        return False

    def typed(screen, frame, recognize):
        field = _modal_field()(screen, frame, recognize)
        if field is not None and field[0] == "filled" and field[1] != code:
            raise Stop(f"输入框里读到的是「{field[1]}」，不是兑换码「{code}」，没有提交", [frame])
        return field is not None and field == ("filled", code)
    return wait_until(rt, "输入兑换码", typed, _modal_field("empty"), seconds=FIELD_WAIT) is not None


def submit(rt, code):
    """One tap on the modal's 使用 (both tap frames: the field reads exactly ``code``, 使用 is gold, no result hint
    is showing yet), then ``(outcome, detail, frame)`` from the first frame that shows a result. Never repeated."""
    def accept(frame, recognize):
        return (read_field(frame, recognize) == ("filled", code) and use_gold(frame) >= USE_GOLD
                and read_hint(recognize)[0] is None)
    if not pages.tap_on_page(rt, MODAL, USE, kind="modal", retry=False, accept=accept):
        return None
    start, frame = time.monotonic(), None
    while not rt.stopped:
        screen, frame, recognize = pages.screen_now(rt)
        if frame is None:
            return None
        if rt._frame_still_valid(frame):
            if is_reward_layer(screen, recognize):
                return "redeemed", "", frame
            if is_modal(screen):
                kind, text = read_hint(recognize)
                if kind == "unreadable":
                    raise Stop("提交兑换码后检查不了提示文字", [frame])
                if kind is not None:
                    return kind, text, frame
            elif not pages.nothing_recognised(screen, recognize):
                raise Stop(f"提交兑换码「{code}」之后出现了认不出的画面", [frame])
            if time.monotonic() - start >= RESULT_WAIT:
                break
        rt.pause(.3)
    if rt.stopped:
        return None
    raise Stop(f"提交兑换码「{code}」之后没有看到成功的奖励层，也没有认得的提示", [frame])


def dismiss_layer(rt) -> bool:
    """One guarded tap on blank space of the reward layer; then the modal (or the 其他 tab, the modal closed with the
    layer) must follow. No second tap."""
    try:
        if not pages.tap_on_layer(rt, "兑换成功后的「恭喜获得」奖励层", is_reward_layer, DISMISS_POINT):
            return False
    except pages.LayerMismatch as mismatch:
        if rt.stopped:
            return False
        if not (is_modal(mismatch.screen) or other_view(mismatch.screen, mismatch.recognize)):
            raise Stop("「恭喜获得」奖励层在点之前变了，但画面没有回到兑换奖励弹窗", [mismatch.frame]) from None
    found = wait_until(rt, "关闭「恭喜获得」奖励层",
                       lambda s, f, r: "modal" if is_modal(s) else "other" if other_view(s, r) else None,
                       lambda s, f, r: is_reward_layer(s, r), seconds=AFTER_DISMISS_WAIT)
    if found is None:
        return False
    return True if found[0] == "modal" else open_modal(rt)


def close_modal(rt) -> bool:
    if not pages.tap_on_page(rt, MODAL, actions.MODAL_CLOSE, kind="modal", retry=False):
        return False
    return wait_until(rt, "关闭兑换奖励弹窗", lambda s, f, r: pages.is_page(s, SETTINGS),
                      lambda s, f, r: is_modal(s)) is not None


OUTCOME_LOGS = {
    "redeemed": "兑换码「{code}」兑换成功。",
    "already_used": "兑换码「{code}」已使用过，已记录跳过。",
    "expired": "兑换码「{code}」已过期或无效，已记录跳过。",
    "cooldown": "兑换码「{code}」提示操作太频繁，本次不再提交它，下次运行再试。",
}


def redeem_all(rt, record, pending) -> bool:
    last_use = None
    redeemed = 0
    for code in pending:
        if rt.stopped:
            return False
        if not fill(rt, code):
            return False
        if last_use is not None:
            remaining = USE_COOLDOWN_SECONDS - (time.monotonic() - last_use)
            if remaining > 0 and not rt.pause(remaining):
                return False
        result = submit(rt, code)
        last_use = time.monotonic()
        if result is None:
            return False
        outcome, detail, _frame = result
        record.record(code, outcome, detail)
        rt.log(OUTCOME_LOGS[outcome].format(code=code))
        if outcome == "redeemed":
            redeemed += 1
            if not dismiss_layer(rt):
                return False
        elif outcome == "cooldown":
            last_use += USE_COOLDOWN_SECONDS  # the next code waits twice the spacing
    if redeemed:
        rt.log(f"兑换码领取：成功兑换 {redeemed} 个兑换码。")
    else:
        rt.log("兑换码领取：本批兑换码均已使用、失效或暂时不能提交。")
    return True


def run(rt, collected=None) -> bool:
    from app_paths import DATA_ROOT
    from navigation.models import NavigationError
    record = Record(DATA_ROOT)
    try:
        pending = pending_codes(candidates(rt) if collected is None else collected, record.outcomes())
    except (OSError, ValueError) as error:
        return fail(rt, f"读不了安卓兑换码记录（{error}）")
    if not pending:
        rt.log("兑换码领取：暂无待兑换的新兑换码，跳过。")
        return True
    rt.log(f"兑换码领取：待尝试 {len(pending)} 个兑换码。")
    done = None
    try:
        start = []
        if not pages.to_hud(rt, rejected=lambda frame, reason: start.append((frame, reason))):
            for frame, reason in start:
                rt.diagnostic("android_redeem_start", f"开始时不在大世界，也没法安全地回去：{reason}", image=frame)
            return fail(rt, "没能回到大世界")
        done = (open_settings(rt) and open_other(rt) and open_modal(rt) and redeem_all(rt, record, pending)
                and close_modal(rt))
    except Stop as error:
        for index, frame in enumerate(error.frames):
            rt.diagnostic(f"android_redeem_stop_{index}", str(error), image=frame)
        fail(rt, str(error))
    except pages.FrameMismatch as error:  # a tap or text frame that was not the expected state: no retry
        rt.diagnostic("android_redeem_stop_0", str(error), image=error.frame)
        fail(rt, str(error))
    except (pages.GuardRefusal, actions.ScreenUnrecognised, NavigationError) as error:
        fail(rt, str(error))
    except (OSError, ValueError) as error:  # the record could not be written: nothing more is sent
        fail(rt, f"写不了安卓兑换码记录（{error}）")
    # The way back only after a successful result: after a stop, a guard refusal or a fatal error nothing more is sent.
    if not done:
        return False
    if rt.stopped or not pages.back_through(rt, (SETTINGS, MENU)):
        return fail(rt, "没能回到大世界")
    return True


def fail(rt, reason) -> bool:
    if not rt.stopped:
        rt.log(f"兑换码领取：{reason}，本项未完成。")
    return False
