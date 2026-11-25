"""兑换码 (MaaNikki_RedeemCodes) on the Android client: redeem the public and manual codes in 设置 -> 其他 -> 兑换码.

HUD -> 美鸭梨 menu (a strict tap on the duck) -> gear -> 设置 (opens on 图像) -> 其他 tab -> one small list swipe while the
兑换码 row is cut by the list's fade -> the row's 使用 -> 兑换奖励 modal -> per code: wait until no result banner shows
and the field's state is known, clear a field that is not empty (its X), tap the field, type the code through the
framework's text input (pages.input_text_on_screen: the device's clipboard and a paste key; never the host clipboard,
never the in-modal paste icon), read the field back and require exactly the code, tap the modal's 使用 (gold) once,
read the result banner -> the modal's ✕ -> back to the HUD (strict back-arrow taps, 设置 only on its 其他 tab).

The codes come from the shared collection (daily/redeem_codes.collect_candidate_codes with the task's options; an
Android start submits empty options, so online sync and no manual codes). The PC class is not used: it reads the UID
the PC way (the HUD footer, then ui.main()) for the account record and its run() reads and writes the host clipboard.
The phone flow keeps its own record (Record below) in the data directory, not per game account: the phone client's
UID is not read yet.

What the frames show (1280x720): the gear on the menu's bottom bar ~(633,668); 设置 opens on 图像 (its 「基础设置」
header); the 其他 tab label ~[82,600,55,32]; on 其他 the 兑换码 row is cut by the list's bottom fade until the list is
swiped up (survey: (700,520)->(700,300)), after which its label ~[289,571,77,33] and 使用 ~[949,569,79,34] line up,
52 px above 全部恢复默认 (a forbidden word: the guard's 40 px margin holds by ~10 px). The modal: field
[395,318,385,50] with the placeholder 「点击此处输入兑换码」, its clear X ~[794,329,28,28] once filled, the in-modal
paste icon ~(878,341) (never tapped), 使用 [603,480,70,39] gold (0.86 of its face) once filled, ✕ ~(945,193). Tapping
the field opens the system's white input strip across the bottom (y >= ~665); that strip is how a focused field is
recognised.

Device run 1 (2026-11-26): every step up to the submit worked (the Chinese code 「拾光二载暖赴新章」 was pasted and read
back exactly). The game answered with a pink banner 「兑换码已使用」 across the modal's title bar (~x 310-970,
y 192-232), hiding the title and most of ✕ (so the frame is no recognised modal), gone ~20 s later with the code still
in the field. The result is read only from that banner, on a frame that also shows the modal's body (its 使用 and its
field); a banner text the PC's classification does not know stops the task with that frame. A 「恭喜获得」 layer after a
submit has never been seen for this modal: it is saved and the task stops, nothing recorded, nothing tapped. Anything
else, or nothing within RESULT_WAIT, stops the task with that frame saved. A submit is never repeated.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time

import cv2

from host.android import actions, pages

MENU, SETTINGS, MODAL = "美鸭梨", "设置", "兑换奖励"
MENU_SETTINGS = "MaaNikki_Android_MenuSettings"
OTHER_TAB = "MaaNikki_Android_SettingsOtherTab"
IMAGE_CONTENT = "MaaNikki_Android_SettingsImageContent"
ROW_LABEL = "MaaNikki_Android_RedeemRowLabel"
ROW_USE = "MaaNikki_Android_RedeemRowUse"
PLACEHOLDER = "MaaNikki_Android_RedeemPlaceholder"
FIELD = "MaaNikki_Android_RedeemField"
CLEAR = "MaaNikki_Android_RedeemClear"
USE = "MaaNikki_Android_RedeemUse"
BANNER = "MaaNikki_Android_RedeemBanner"
LAYER_TITLE = "MaaNikki_Android_RewardLayerTitle"  # android_monthly.json: 「恭喜获得」
LAYER_HINT = "MaaNikki_Android_RewardLayerHint"  # 「点击空白区域继续」
NODES = (MENU_SETTINGS, OTHER_TAB, IMAGE_CONTENT, ROW_LABEL, ROW_USE, PLACEHOLDER, FIELD, CLEAR, USE, BANNER)

# The 兑换码 row: its 使用 (the only 使用 on 其他) and a 「兑换码」 label whose centres are this close vertically (the
# section header 「兑换码」 sits ~58 px above the row; the faded row label of the unswiped list reads 「总换码」).
ROW_ALIGN = 14
LIST_SWIPE = ((700, 520), (700, 300))  # empty row space between labels and buttons (phase A's survey swipe)
SWIPE_MS = 500
USE_FACE = (545, 482, 190, 36)
USE_GOLD = .4  # saved frames: 0.862 with a code in the field, 0.000 empty
STRIP = (0, 672, 1280, 46)
STRIP_WHITE = .8  # saved frames: 1.000 and 0.975 with the field focused, at most 0.168 on any other frame
# The result banner's pink band left of its text (device run 1: 0.995 of this box; 0.000 on every modal frame).
BANNER_BAND = (330, 196, 230, 34)
BANNER_PINK = .6
USE_COOLDOWN_SECONDS = 3.2  # the PC spacing between two submits (daily/redeem_codes.py)
OPEN_WAIT = 5
TRANSITION_ALLOWANCE = 1.5
FIELD_WAIT = 4  # the field showing what was typed / cleared, or the strip opening
RESULT_WAIT = 6
BANNER_WAIT = 30  # device run 1: gone within ~20 s

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


def banner_pink(frame) -> float:
    return max(_fraction(frame, BANNER_BAND, (150, 25, 170), (180, 170, 255)),
               _fraction(frame, BANNER_BAND, (0, 25, 170), (8, 170, 255))) if frame is not None else 0.0


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


def _other(screen, frame, recognize) -> bool:
    return other_view(screen, recognize)


def image_content(recognize) -> bool:
    """The 图像 tab's content (its 「基础设置」 header), the tab 设置 opens on, and not the 其他 tab's."""
    return actions._hit(recognize(IMAGE_CONTENT)) and not actions._hit(recognize(ROW_LABEL))


def image_view(screen, recognize) -> bool:
    return pages.is_page(screen, SETTINGS) and image_content(recognize)


def row_use(recognize):
    """The 兑换码 row's 使用 box when the row is wholly in view (its 使用 and its 「兑换码」 label line up), else None."""
    uses = [box for text, box in _rows(recognize(ROW_USE)) if len(box) == 4]
    if len(uses) != 1:
        return None
    labels = [box for text, box in _rows(recognize(ROW_LABEL)) if len(box) == 4]
    use = uses[0]
    return use if any(abs(_centre_y(label) - _centre_y(use)) <= ROW_ALIGN for label in labels) else None


def faded_row(recognize) -> bool:
    """The 兑换码 row cut by the list's fade, as on the saved unswiped frames: its one 使用 read, not lined up."""
    uses = [box for text, box in _rows(recognize(ROW_USE)) if len(box) == 4]
    return len(uses) == 1 and row_use(recognize) is None


def is_modal(screen) -> bool:
    return pages.is_modal(screen, MODAL)


def read_field(frame, recognize):
    """``(state, text)`` of the modal's field: "empty" (the placeholder, no clear X), "filled" (no placeholder, the
    clear X; ``text`` is what it reads, rows joined by a space, only the ends stripped) or "unknown"."""
    placeholder, clear = actions._hit(recognize(PLACEHOLDER)), actions._hit(recognize(CLEAR))
    if placeholder and not clear:
        return "empty", ""
    if clear and not placeholder:
        rows = sorted(_rows(recognize(FIELD)), key=lambda row: row[1][0] if row[1] else 0)
        return "filled", " ".join(text for text, _box in rows).strip()
    return "unknown", ""


def banner_text(frame, recognize):
    """The result banner's text over the 兑换奖励 modal (device run 1: 「兑换码已使用」), or None: the pink band, text
    read in the title bar, and the modal's body beneath (its 使用 and its field, placeholder or clear X). The banner
    hides the modal's title and most of its ✕, so such a frame is no recognised modal."""
    if banner_pink(frame) < BANNER_PINK or not actions._hit(recognize(USE)):
        return None
    if not (actions._hit(recognize(PLACEHOLDER)) or actions._hit(recognize(CLEAR))):
        return None
    text = " ".join(text for text, _box in _rows(recognize(BANNER))).strip()
    return text or None


def banner(screen, frame, recognize):
    """banner_text on a frame that is not the quit dialog (both quit checks ran and found nothing)."""
    if screen is None or screen.kind not in ("unknown", "modal"):
        return None
    for node in (actions.QUIT_TITLE, actions.QUIT_CANCEL):
        result = recognize(node)
        if result is None or actions._hit(result):
            return None
    return banner_text(frame, recognize)


def classify(text):
    from daily.redeem_codes import _classify_hint
    return _classify_hint(text)


def is_reward_layer(screen, recognize) -> bool:
    """A 「恭喜获得」 layer (title and its bottom hint read), not the quit dialog. Never seen after a code."""
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


def _modal_field(state=None, *, focused=None):
    """``done``/``source`` for wait_until: the modal with its field in ``state`` (and focused or not, when given);
    returns the field's ``(state, text)``."""
    def check(screen, frame, recognize):
        if not is_modal(screen):
            return None
        field = read_field(frame, recognize)
        if state is not None and field[0] != state:
            return None
        if focused is not None and strip_open(frame) != focused:
            return None
        return field
    return check


def _banner_shown(screen, frame, recognize) -> bool:
    return banner(screen, frame, recognize) is not None


# ---- the way in and out ----

def tap_duck(rt) -> bool:
    """The 美鸭梨 menu from the HUD by one strict tap on the duck (pages.tap_on_layer: the HUD with the duck in the same
    place on both tap frames, no forbidden label near it). Unlike the shared named action, a quit dialog or any other
    screen on either frame stops the task with that frame; there is no second attempt."""
    from host.android import touch_point
    screen, frame, _recognize = pages.screen_now(rt)
    if frame is None:
        return False
    if screen.kind != "hud":
        raise Stop("要打开美鸭梨菜单时画面不是大世界", [frame])
    point = touch_point(screen.targets["duck"])

    def accept(screen_, _recognize_):
        return screen_.kind == "hud" and actions._near(screen_.targets.get("duck"), point)
    return pages.tap_on_layer(rt, "鸭子图标在原处的大世界", accept, point)


def _left(title):
    """``done`` after a back-arrow tap on the page titled ``title``: the HUD or a page with another title."""
    return lambda s, f, r: s.kind == "hud" or (s.kind == "page" and not pages.is_page(s, title))


def to_hud(rt, what, *, any_page=False, attempts=6) -> bool:
    """Back to the HUD from where this task leaves the game, one strict step at a time, each step waited out before
    the next: a result banner fades (nothing is tapped while it shows), the 兑换奖励 modal closes by its ✕, 设置 (only on
    its 其他 tab, on both tap frames) and 美鸭梨 by their back arrows. With ``any_page`` (the start of a run), any
    other page is left by its back arrow too. Every tap is single (no second attempt); the quit dialog, a reward layer,
    another 设置 tab or anything else stops the task with that frame."""
    for _ in range(attempts):
        screen, frame, recognize = pages.screen_now(rt)
        if frame is None:
            return False
        if not rt._frame_still_valid(frame):
            rt.pause(.4)
            continue
        if screen.kind == "hud":
            return True
        if banner(screen, frame, recognize) is not None:
            found = wait_until(rt, f"{what}：等兑换结果的提示条消失", lambda s, f, r: is_modal(s), _banner_shown,
                               seconds=BANNER_WAIT)
        elif is_modal(screen):
            if not pages.tap_on_page(rt, MODAL, actions.MODAL_CLOSE, kind="modal", retry=False):
                return False
            found = wait_until(rt, f"{what}：关闭兑换奖励弹窗", _other, lambda s, f, r: is_modal(s))
        elif pages.is_page(screen, SETTINGS):
            if not other_view(screen, recognize):
                raise Stop(f"{what}时设置页面不在「其他」页签", [frame])
            if not pages.tap_on_page(rt, SETTINGS, actions.BACK_ARROW, retry=False,
                                     accept=lambda f, r: actions._hit(r(ROW_LABEL))):
                return False
            found = wait_until(rt, f"{what}：离开设置", _left(SETTINGS), _other)
        elif pages.is_page(screen, MENU) or (any_page and screen.kind == "page"):
            title = MENU if pages.is_page(screen, MENU) else screen.title
            if not pages.tap_on_page(rt, title, actions.BACK_ARROW, retry=False):
                return False
            found = wait_until(rt, f"{what}：离开「{title}」", _left(title), lambda s, f, r: pages.is_page(s, title))
        else:
            raise Stop(f"{what}时画面不是能安全退回的页面（{screen.reason or screen.kind}）", [frame])
        if found is None:
            return False
    raise Stop(f"{what}：退了 {attempts} 步还没回到大世界", [frame])


def open_settings(rt) -> bool:
    if not tap_duck(rt):
        return False
    if wait_until(rt, "打开美鸭梨菜单", lambda s, f, r: pages.is_page(s, MENU),
                  lambda s, f, r: s.kind == "hud") is None:
        return False
    if not pages.tap_on_page(rt, MENU, MENU_SETTINGS, retry=False):
        return False
    return wait_until(rt, "打开设置", lambda s, f, r: pages.is_page(s, SETTINGS),
                      lambda s, f, r: pages.is_page(s, MENU)) is not None


def open_other(rt) -> bool:
    """The 其他 tab: already showing, or switched to from 图像 (the tab 设置 opens on), identified on both tap frames
    and on every frame waited through; any other tab stops on its first frame."""
    found = wait_until(rt, "确认设置页面", lambda s, f, r: pages.is_page(s, SETTINGS))
    if found is None:
        return False
    _value, screen, frame, recognize = found
    if other_view(screen, recognize):
        return True
    if not image_view(screen, recognize):
        raise Stop("设置页面打开在认不出的页签上", [frame])
    if not pages.tap_on_page(rt, SETTINGS, OTHER_TAB, retry=False, accept=lambda f, r: image_content(r)):
        return False
    return wait_until(rt, "切换到「其他」", _other, lambda s, f, r: image_view(s, r)) is not None


def open_modal(rt) -> bool:
    """From the 其他 tab to the 兑换奖励 modal: one guarded list swipe while the row is cut by the list's fade (both swipe
    frames must show that), then the row's 使用 (both tap frames must show it lined up with its label), then wait for
    the modal. A swipe frame on which the row has already settled leads to the row tap, decided afresh."""
    found = wait_until(rt, "确认「其他」页签", _other)
    if found is None:
        return False
    _value, _screen, frame, recognize = found
    if row_use(recognize) is None:
        if not faded_row(recognize):
            raise Stop("「其他」页签上认不出兑换码这一行", [frame])
        try:
            if not pages.swipe_on_page(rt, SETTINGS, *LIST_SWIPE, duration=SWIPE_MS,
                                       accept=lambda f, r: actions._hit(r(ROW_LABEL)) and faded_row(r)):
                return False
            swiped = True
        except pages.SwipeMismatch as mismatch:
            if not (other_view(mismatch.screen, mismatch.recognize) and row_use(mismatch.recognize) is not None):
                raise
            swiped = False
        if swiped and wait_until(rt, "滑动设置列表", lambda s, f, r: other_view(s, r) and row_use(r) is not None,
                                 lambda s, f, r: other_view(s, r) and faded_row(r), seconds=3) is None:
            return False
    if not pages.tap_on_page(rt, SETTINGS, ROW_USE, retry=False,
                             accept=lambda f, r: row_use(r) is not None):
        return False
    return wait_until(rt, "打开兑换奖励弹窗", lambda s, f, r: is_modal(s), _other) is not None


# ---- one code ----

def _modal_accept(state, focused=None):
    def accept(frame, recognize):
        field = read_field(frame, recognize)
        return field[0] == state and (focused is None or strip_open(frame) == focused)
    return accept


def fill(rt, code) -> bool:
    """The modal (after the previous result's banner has gone) with its field empty, focused, then holding exactly
    ``code`` (read back by OCR, logged). Stop on anything else: a field that reads other text is never submitted."""
    def settled(state):
        def check(screen, frame, recognize):
            field = _modal_field(state)(screen, frame, recognize)
            return None if field is None or field[0] == "unknown" else (field[0], strip_open(frame))
        return check
    found = wait_until(rt, "等兑换奖励弹窗停稳", settled(None), _banner_shown, seconds=BANNER_WAIT)
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
        if field is not None and field[0] == "filled":
            if field[1] != code:
                raise Stop(f"输入框读回「{field[1]}」，不是兑换码「{code}」，没有提交", [frame])
            return True
        return None
    if wait_until(rt, "输入兑换码", typed, _modal_field("empty"), seconds=FIELD_WAIT) is None:
        return False
    rt.log(f"兑换码输入框读回「{code}」，与兑换码一致。")
    return True


def submit(rt, code):
    """One tap on the modal's 使用 (both tap frames: the recognised modal, no banner, the field reads exactly ``code``,
    使用 gold), then ``(outcome, banner text, frame)`` from the first banner whose text the PC's classification knows.
    Never repeated; anything else stops with its frame."""
    def accept(frame, recognize):
        return (read_field(frame, recognize) == ("filled", code) and use_gold(frame) >= USE_GOLD
                and banner_text(frame, recognize) is None)
    if not pages.tap_on_page(rt, MODAL, USE, kind="modal", retry=False, accept=accept):
        return None
    start, frame = time.monotonic(), None
    while not rt.stopped:
        screen, frame, recognize = pages.screen_now(rt)
        if frame is None:
            return None
        if rt._frame_still_valid(frame):
            text = banner(screen, frame, recognize)
            if text is not None:
                kind = classify(text)
                if kind is None:
                    raise Stop(f"提交兑换码「{code}」之后的提示条认不出：「{text}」", [frame])
                return kind, text, frame
            if is_reward_layer(screen, recognize):
                raise Stop(f"提交兑换码「{code}」之后出现了奖励层；兑换成功的画面还没在设备上见过，没有记录，也没有点",
                           [frame])
            if not (is_modal(screen) or pages.nothing_recognised(screen, recognize)):
                raise Stop(f"提交兑换码「{code}」之后出现了认不出的画面", [frame])
            if time.monotonic() - start >= RESULT_WAIT:
                break
        rt.pause(.3)
    if rt.stopped:
        return None
    raise Stop(f"提交兑换码「{code}」之后没有看到认得的提示条", [frame])


OUTCOME_LOGS = {
    "already_used": "兑换码「{code}」已使用过，已记录跳过。",
    "expired": "兑换码「{code}」已过期或无效，已记录跳过。",
    "cooldown": "兑换码「{code}」提示操作太频繁，本次不再提交它，下次运行再试。",
}


def redeem_all(rt, record, pending) -> bool:
    last_use = None
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
        if outcome == "cooldown":
            last_use += USE_COOLDOWN_SECONDS  # the next code waits twice the spacing
    rt.log("兑换码领取：本批兑换码均已使用、失效或暂时不能提交。")
    return True


def guarded(rt, step) -> bool:
    """``step()`` with this task's stops: a Stop or a tap frame that did not agree saves its frame; a guard refusal or
    an unrecognised screen is reported; the record failing to write stops too. Nothing more is sent after any."""
    from navigation.models import NavigationError
    try:
        return bool(step())
    except Stop as error:
        for index, frame in enumerate(error.frames):
            rt.diagnostic(f"android_redeem_stop_{index}", str(error), image=frame)
        fail(rt, str(error))
    except pages.FrameMismatch as error:  # a tap, swipe or text frame that was not the expected state: no retry
        rt.diagnostic("android_redeem_stop_0", str(error), image=error.frame)
        fail(rt, str(error))
    except (pages.GuardRefusal, actions.ScreenUnrecognised, NavigationError) as error:
        fail(rt, str(error))
    except (OSError, ValueError) as error:  # the record could not be written
        fail(rt, f"写不了安卓兑换码记录（{error}）")
    return False


def run(rt, collected=None) -> bool:
    from app_paths import DATA_ROOT
    record = Record(DATA_ROOT)
    try:
        pending = pending_codes(candidates(rt) if collected is None else collected, record.outcomes())
    except (OSError, ValueError) as error:
        return fail(rt, f"读不了安卓兑换码记录（{error}）")
    if not pending:
        rt.log("兑换码领取：暂无待兑换的新兑换码，跳过。")
        # Still checked: a stop after the last code may have left the modal open.
        return guarded(rt, lambda: to_hud(rt, "确认已回到大世界", any_page=True))
    rt.log(f"兑换码领取：待尝试 {len(pending)} 个兑换码。")
    done = guarded(rt, lambda: to_hud(rt, "开始前回到大世界", any_page=True) and open_settings(rt)
                   and open_other(rt) and open_modal(rt) and redeem_all(rt, record, pending))
    # The way back only after a successful result: after a stop, a guard refusal or a fatal error nothing more is sent.
    if not done or rt.stopped:
        return False
    return guarded(rt, lambda: to_hud(rt, "返回大世界"))


def fail(rt, reason) -> bool:
    if not rt.stopped:
        rt.log(f"兑换码领取：{reason}，本项未完成。")
    return False
