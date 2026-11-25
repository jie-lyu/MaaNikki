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
field); a banner text the PC's classification does not know stops the task with that frame. Device run 2: codes 1-7
got that banner; code 8 was redeemed and brought the 「恭喜获得」 layer over the blurred modal (title ~y 185, one item,
「点击空白区域继续」 ~(640,650)); a probe tap at (600,705) closed it and the modal came back with the code in the field.
So a layer after a submit is a candidate success: one guarded tap at DISMISS_POINT, and ``redeemed`` is recorded only
once the next settled frame is the modal with the field still reading exactly that code (confirm_success). Anything
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
# The result banner's pink band left of its text (device run 1: 0.995 of this box; 0.000 on every modal frame). A
# banner is read only at BANNER_PINK or more; it counts as gone only below BANNER_GONE (in between: still fading, so
# nothing is tapped on the modal and the frame is waited through), whatever OCR reads.
BANNER_BAND = (330, 196, 230, 34)
BANNER_PINK = .6
BANNER_GONE = .3
MODAL_CLOSE_BOX = (929, 177, 36, 36)  # where the 兑换奖励 modal's ✕ matches (saved modal frames)
PRICES = "[¥￥]|购买|充值|开通|兑换钻石"  # text of a paid popup: never under a result banner
USE_COOLDOWN_SECONDS = 3.2  # the PC spacing between two submits (daily/redeem_codes.py)
OPEN_WAIT = 5
TRANSITION_ALLOWANCE = 1.5
FIELD_WAIT = 4  # the field showing what was typed / cleared, or the strip opening
RESULT_WAIT = 6
BANNER_WAIT = 30  # device run 1: gone within ~20 s
# The success layer's dismissal (device run 2: a probe tap here closed the 「恭喜获得」 layer and the next frame was the
# 兑换奖励 modal with the code still in the field). Blank on the layer (46 px below its hint ~[569,640,140,19]) and on
# the modal over 设置 beneath it (saved modal and 其他 frames); the input strip must not be open across it.
DISMISS_POINT = (600, 705)
DISMISS_CLEAR = [480, 662, 240, 58]  # no text may be read here on either tap frame
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


def banner_pink(frame) -> float:
    """The share of the band that is pink: the union of the two hue ranges either side of red's wrap-around."""
    if frame is None:
        return 0.0
    x, y, w, h = BANNER_BAND
    hsv = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
    pink = cv2.inRange(hsv, (150, 25, 170), (180, 170, 255)) | cv2.inRange(hsv, (0, 25, 170), (8, 170, 255))
    return cv2.countNonZero(pink) / (w * h)


def _rows(result):
    if not actions._hit(result):
        return []
    rows = list(getattr(result, "filtered_results", None) or []) or [getattr(result, "best_result", None)]
    return [(str(getattr(row, "text", "") or ""), [int(v) for v in getattr(row, "box", None) or []])
            for row in rows if row is not None]


def _centre_y(box) -> float:
    return box[1] + box[3] / 2


def settings_page(screen) -> bool:
    """The 设置 page with exactly that title (every saved 设置 frame reads exactly 「设置」)."""
    return screen is not None and screen.kind == "page" and screen.title == SETTINGS


def other_view(screen, recognize) -> bool:
    """The 设置 page showing the 其他 tab's content (its 兑换码 section: no other tab has a 「兑换码」 label)."""
    return settings_page(screen) and actions._hit(recognize(ROW_LABEL))


def _other(screen, frame, recognize) -> bool:
    return other_view(screen, recognize)


def image_content(recognize) -> bool:
    """The 图像 tab's content (its 「基础设置」 header), the tab 设置 opens on, and not the 其他 tab's."""
    return actions._hit(recognize(IMAGE_CONTENT)) and not actions._hit(recognize(ROW_LABEL))


def image_view(screen, recognize) -> bool:
    return settings_page(screen) and image_content(recognize)


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


def quiet(frame) -> bool:
    """No result banner on the frame, by its colour alone (independent of OCR): the pink band clearly below a banner's."""
    return banner_pink(frame) < BANNER_GONE


def _not_quit(screen, recognize) -> bool:
    if screen is None or screen.kind not in ("unknown", "modal"):
        return False
    for node in (actions.QUIT_TITLE, actions.QUIT_CANCEL):
        result = recognize(node)
        if result is None or actions._hit(result):
            return False
    return True


def banner_fading(screen, frame, recognize) -> bool:
    """A frame of the modal (its body: 使用 and the field) with the pink band not yet gone (a banner appearing, showing
    or fading, read or not): waited through, never tapped on."""
    return (_not_quit(screen, recognize) and not quiet(frame) and actions._hit(recognize(USE))
            and (actions._hit(recognize(PLACEHOLDER)) or actions._hit(recognize(CLEAR))))


def foreign(screen, recognize):
    """What else a result frame shows that is not the 兑换奖励 modal under its banner, as ``(what, text read or
    None)``, or None: a close mark away from the modal's ✕ (another popup), or prices (a paid popup)."""
    close = recognize(actions.MODAL_CLOSE)
    if close is None:
        return "弹窗的检查没能完成", None
    if actions._hit(close) and not actions._near(actions._box(close), list(MODAL_CLOSE_BOX)):
        return "画面上还有别的弹窗的关闭标记", None
    prices = recognize(pages.FORBIDDEN, {"expected": PRICES})
    if prices is None:
        return "价格的检查没能完成", None
    if actions._hit(prices):
        return "画面上有价格或购买的字样", _rows(prices)[0][0]
    return None


def banner(screen, frame, recognize):
    """banner_text on a frame that is not the quit dialog (both quit checks ran and found nothing)."""
    return banner_text(frame, recognize) if _not_quit(screen, recognize) else None


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
    """Something unexpected: report it and send nothing more (not even the way back). ``frames`` are saved. The
    message reaches the user and never quotes OCR'd text; ``detail`` (what was read) goes to the saved scene only."""

    def __init__(self, reason, frames=(), detail=None):
        super().__init__(reason)
        self.frames = [frame for frame in frames if frame is not None]
        self.detail = f"{reason}（读到「{detail}」）" if detail is not None else reason


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
        if not is_modal(screen) or not quiet(frame):
            return None
        field = read_field(frame, recognize)
        if state is not None and field[0] != state:
            return None
        if focused is not None and strip_open(frame) != focused:
            return None
        return field
    return check


def _banner_shown(screen, frame, recognize) -> bool:
    return banner(screen, frame, recognize) is not None or banner_fading(screen, frame, recognize)


# ---- the way in and out ----

# The 美鸭梨 menu by one strict duck tap (shared with the mail task): any other screen stops with that frame.
tap_duck = pages.tap_duck


def _left(title):
    """``done`` after a back-arrow tap on the page titled exactly ``title``: the HUD or a page with another title
    (whitespace aside: OCR spacing alone is the same page)."""
    return lambda s, f, r: s.kind == "hud" or (s.kind == "page" and not pages.same_title(s.title, title))


def to_hud(rt, what, *, any_page=False, attempts=6) -> bool:
    """Back to the HUD from where this task leaves the game, one strict step at a time, each step waited out before
    the next: a result banner fades (nothing is tapped while it shows), the 兑换奖励 modal closes by its ✕, 设置 (only on
    its 其他 tab, on both tap frames) and 美鸭梨 by their back arrows. With ``any_page`` (the start of a run), any
    other page is left by its back arrow too. Every tap is single (no second attempt; back arrows only on the page
    with exactly the decided title, tap_on_page exact=True); the quit dialog, a reward layer, another 设置 tab or
    anything else stops the task with that frame. Reaching the HUD after the last allowed step succeeds.

    Not pages.to_hud(single=True): that one stops on the 兑换奖励 modal (a leftover modal must be closed here) and
    taps the back arrow of any 设置 tab (here only 其他)."""
    for step in range(attempts + 1):
        screen, frame, recognize = pages.screen_now(rt)
        if frame is None:
            return False
        if not rt._frame_still_valid(frame):
            rt.pause(.4)
            continue
        if screen.kind == "hud":
            return True
        if step == attempts:
            break
        if _banner_shown(screen, frame, recognize):
            found = wait_until(rt, f"{what}：等兑换结果的提示条消失", lambda s, f, r: is_modal(s) and quiet(f),
                               _banner_shown, seconds=BANNER_WAIT)
        elif is_modal(screen):
            if not pages.tap_on_page(rt, MODAL, actions.MODAL_CLOSE, kind="modal", retry=False,
                                     accept=lambda f, r: quiet(f)):
                return False
            found = wait_until(rt, f"{what}：关闭兑换奖励弹窗", _other, lambda s, f, r: is_modal(s))
        elif pages.is_page(screen, SETTINGS):
            if screen.title != SETTINGS:  # e.g. 「设置 控制」: which tab this is cannot be told from the title
                raise Stop(f"{what}时设置页面的标题认不准是哪个页签", [frame], screen.title)
            if not other_view(screen, recognize):
                raise Stop(f"{what}时设置页面不在「其他」页签", [frame])
            if not pages.tap_on_page(rt, SETTINGS, actions.BACK_ARROW, retry=False, exact=True,
                                     accept=lambda f, r: actions._hit(r(ROW_LABEL))):
                return False
            found = wait_until(rt, f"{what}：离开设置", _left(SETTINGS), _other)
        elif screen.kind == "page" and (pages.is_exactly(screen, MENU) or any_page):
            # Left by its whole observed title (whitespace aside), named in messages without it.
            title = screen.title
            if not pages.tap_on_page(rt, title, actions.BACK_ARROW, retry=False, exact=True, place=pages.LEAVING):
                return False
            found = wait_until(rt, f"{what}：离开当前页面", _left(title), lambda s, f, r: pages.is_exactly(s, title))
        else:
            raise Stop(f"{what}时画面不是能安全退回的页面（{screen.reason or screen.kind}）", [frame])
        if found is None:
            return False
    raise Stop(f"{what}：退了 {attempts} 步还没回到大世界", [frame])


def open_settings(rt) -> bool:
    if not tap_duck(rt):
        return False
    if wait_until(rt, "打开美鸭梨菜单", lambda s, f, r: pages.is_exactly(s, MENU),
                  lambda s, f, r: s.kind == "hud") is None:
        return False
    if not pages.tap_on_page(rt, MENU, MENU_SETTINGS, retry=False, exact=True):
        return False
    found = wait_until(rt, "打开设置", lambda s, f, r: pages.is_page(s, SETTINGS),
                      lambda s, f, r: pages.is_exactly(s, MENU))
    return found is not None and open_other(rt, found)


def open_other(rt, found) -> bool:
    """The 其他 tab: already showing, or switched to from 图像 (the tab 设置 opens on), judged on ``found``, the first
    设置 frame after the gear, then on both tap frames and on every frame waited through; any other tab, or a title
    that is not exactly 「设置」, stops on its first frame."""
    _value, screen, frame, recognize = found
    if not settings_page(screen):
        raise Stop("设置页面的标题认不准是哪个页签", [frame], screen.title)
    if other_view(screen, recognize):
        return True
    if not image_view(screen, recognize):
        raise Stop("设置页面打开在认不出的页签上", [frame])
    if not pages.tap_on_page(rt, SETTINGS, OTHER_TAB, retry=False, exact=True, accept=lambda f, r: image_content(r)):
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
    if not pages.tap_on_page(rt, SETTINGS, ROW_USE, retry=False, exact=True,
                             accept=lambda f, r: row_use(r) is not None):
        return False
    return wait_until(rt, "打开兑换奖励弹窗", lambda s, f, r: is_modal(s), _other) is not None


# ---- one code ----

def _modal_accept(state, focused=None):
    def accept(frame, recognize):
        field = read_field(frame, recognize)
        return quiet(frame) and field[0] == state and (focused is None or strip_open(frame) == focused)
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
                raise Stop(f"输入框读回的不是兑换码「{code}」，没有提交", [frame], field[1])
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
        return (quiet(frame) and read_field(frame, recognize) == ("filled", code) and use_gold(frame) >= USE_GOLD
                and banner_text(frame, recognize) is None)
    if not pages.tap_on_page(rt, MODAL, USE, kind="modal", retry=False, accept=accept):
        return None
    start, frame = time.monotonic(), None
    while not rt.stopped:
        screen, frame, recognize = pages.screen_now(rt)
        if frame is None:
            return None
        if rt._frame_still_valid(frame):
            if is_reward_layer(screen, recognize):  # also under a banner: the layer wins
                if banner(screen, frame, recognize) is not None:
                    raise Stop(f"提交兑换码「{code}」之后奖励层和提示条同时出现，没有记录，也没有点", [frame])
                return "layer", "", frame  # a candidate success: recorded only once the modal is back with the code
            text = banner(screen, frame, recognize)
            if text is not None:
                other = foreign(screen, recognize)
                if other is not None:
                    raise Stop(f"提交兑换码「{code}」之后的提示条旁边{other[0]}，没有记录", [frame], other[1])
                field = read_field(frame, recognize)
                if field != ("filled", code):
                    raise Stop(f"提示条出现时输入框里不是刚提交的「{code}」，没有记录", [frame], field[1])
                kind = classify(text)
                if kind is None:
                    raise Stop(f"提交兑换码「{code}」之后的提示条认不出", [frame], text)
                return kind, text, frame
            if not (is_modal(screen) or banner_fading(screen, frame, recognize)
                    or pages.nothing_recognised(screen, recognize)):
                raise Stop(f"提交兑换码「{code}」之后出现了认不出的画面", [frame])
            if time.monotonic() - start >= RESULT_WAIT:
                break
        rt.pause(.3)
    if rt.stopped:
        return None
    raise Stop(f"提交兑换码「{code}」之后没有看到认得的提示条", [frame])


def blank_at_dismiss(recognize) -> bool:
    """No text read around DISMISS_POINT (the check ran). A single character spanning the whole region is the OCR's
    reading of empty background (seen on the saved modal and 其他 frames as 「1」), not a label."""
    result = recognize(actions.PAGE_TITLE, {"roi": DISMISS_CLEAR, "expected": ".+"})
    return result is not None and not any(len(text.strip()) >= 2 for text, _box in _rows(result))


def confirm_success(rt, code) -> bool:
    """After a submit whose read-back matched on both of its tap frames, a 「恭喜获得」 layer appeared: one tap at
    DISMISS_POINT (pages.tap_on_layer: the layer recognised, no text around the point, no input strip, on both tap
    frames; no second attempt), then the next settled frame must be the 兑换奖励 modal with the field reading exactly
    ``code`` (the layer belonged to this submit). Anything else stops with that frame; the caller records nothing
    then."""
    def accept(screen, recognize):
        return is_reward_layer(screen, recognize) and blank_at_dismiss(recognize)
    try:
        if not pages.tap_on_layer(rt, "兑换成功后的「恭喜获得」奖励层", accept, DISMISS_POINT,
                                  verify=lambda frame: not strip_open(frame)):
            return False
    except pages.LayerMismatch as mismatch:
        raise Stop(f"兑换码「{code}」的奖励层在点空白处之前变了，没有记录", [mismatch.frame]) from None

    def settled(screen, frame, recognize):
        if is_reward_layer(screen, recognize):  # the layer still showing, even with the modal readable under it
            return None
        field = _modal_field()(screen, frame, recognize)
        return None if field is None or field[0] == "unknown" else field

    def leaving(screen, frame, recognize):
        return is_reward_layer(screen, recognize) or banner_fading(screen, frame, recognize) or (
            is_modal(screen) and settled(screen, frame, recognize) is None)
    found = wait_until(rt, "关闭「恭喜获得」奖励层", settled, leaving, seconds=AFTER_DISMISS_WAIT)
    if found is None:
        return False
    if found[0] != ("filled", code):
        raise Stop(f"关掉奖励层后输入框里不是刚提交的「{code}」，没有记录", [found[2]], found[0][1])
    return True


OUTCOME_LOGS = {
    "redeemed": "兑换码「{code}」兑换成功。",
    "already_used": "兑换码「{code}」已使用过，已记录跳过。",
    "expired": "兑换码「{code}」已过期或无效，已记录跳过。",
    "cooldown": "兑换码「{code}」提示操作太频繁，本次不再提交它，下次运行再试。",
}


def redeem_all(rt, record, pending) -> bool:
    last_use, redeemed = None, 0
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
        if outcome == "layer":
            if not confirm_success(rt, code):
                return False
            outcome, redeemed = "redeemed", redeemed + 1
        record.record(code, outcome, detail)
        rt.log(OUTCOME_LOGS[outcome].format(code=code))
        if outcome == "cooldown":
            last_use += USE_COOLDOWN_SECONDS  # the next code waits twice the spacing
    rt.log(f"兑换码领取：成功兑换 {redeemed} 个兑换码。" if redeemed else "兑换码领取：本批兑换码均已使用、失效或暂时不能提交。")
    return True


def guarded(rt, step) -> bool:
    """``step()`` with this task's stops: a Stop or a tap frame that did not agree saves its frame; a guard refusal or
    an unrecognised screen is reported; the record failing to write stops too. Nothing more is sent after any."""
    from navigation.models import NavigationError
    try:
        return bool(step())
    except Stop as error:
        for index, frame in enumerate(error.frames):
            rt.diagnostic(f"android_redeem_stop_{index}", error.detail, image=frame)
        fail(rt, str(error))
    except pages.FrameMismatch as error:  # a tap, swipe or text frame that was not the expected state: no retry
        rt.diagnostic("android_redeem_stop_0", str(error), image=error.frame)
        fail(rt, str(error))
    except (pages.GuardRefusal, actions.ScreenUnrecognised, NavigationError) as error:
        frame = getattr(error, "frame", None)  # the refusing frame, when the refusal carries it
        if frame is not None:
            rt.diagnostic("android_redeem_stop_0", pages.diagnostic_reason(error), image=frame)
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
                   and open_modal(rt) and redeem_all(rt, record, pending))
    # The way back only after a successful result: after a stop, a guard refusal or a fatal error nothing more is sent.
    if not done or rt.stopped:
        return False
    return guarded(rt, lambda: to_hud(rt, "返回大世界"))


def fail(rt, reason) -> bool:
    from host import android_user_stop
    if not rt.stopped and not android_user_stop():  # a user stop is no failure
        rt.log(f"兑换码领取：{reason}，本项未完成。")
    return False
