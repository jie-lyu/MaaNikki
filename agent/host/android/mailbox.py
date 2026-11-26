"""邮件 (MaaNikki_Mailbox) on the Android client: claim all attachments when the claim-all button is enabled.

HUD -> 美鸭梨 menu (duck) -> 邮件 (envelope) -> 领取全部 if enabled -> back to the HUD. Every tap goes through the
named actions or pages.tap_on_page (decided and confirmed on fresh focus-confirmed frames, never next to 删除 or other
forbidden words). The two delete buttons on the mail page are never tapped. What is verified on the device and what
is inferred: see docs/android-support-plan.md (phase C).
"""
from __future__ import annotations

import cv2

from host.android import actions, pages

MENU = "美鸭梨"
MAIL = "邮件"
MENU_MAIL = "MaaNikki_Android_MenuMail"
CLAIM_ALL = "MaaNikki_Android_MailClaimAll"
# The claim-all button's face in the 1280x720 frame. Disabled (observed): grey, no gold. Enabled: inferred from the
# two active buttons beside it (brown-gold, ~0.58 gold); not yet seen on the device.
BUTTON_FACE = (285, 652, 190, 40)
ENABLED_GOLD = .4
DISABLED_GOLD, DISABLED_GRAY = .05, .8


def button_state(frame) -> tuple[str, float, float]:
    x, y, w, h = BUTTON_FACE
    hsv = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
    gold = cv2.countNonZero(cv2.inRange(hsv, (10, 85, 130), (35, 255, 255))) / (w * h)
    gray = cv2.countNonZero(cv2.inRange(hsv, (0, 0, 85), (180, 65, 235))) / (w * h)
    if gold >= ENABLED_GOLD:
        return "enabled", gold, gray
    if gold <= DISABLED_GOLD and gray >= DISABLED_GRAY:
        return "disabled", gold, gray
    return "unknown", gold, gray


# The way to the HUD before the task and back after it are shared with the other Android tasks (pages.py).
to_hud = pages.to_hud


def back_from_mail(rt, attempts: int = 3) -> bool:
    """The way back after the task: the back arrow, tapped only on the 邮件 and 美鸭梨 pages."""
    return pages.back_through(rt, (MAIL, MENU), attempts)


class Stop(Exception):
    """Something unexpected: report it and send nothing more (not even the way back). ``frames`` are saved."""

    def __init__(self, reason, frames=()):
        super().__init__(reason)
        self.frames = [frame for frame in frames if frame is not None]


def mail_state(rt):
    """``(state, frame)`` from one fresh frame that is the mail page with the claim-all label on it, else
    ``(None, frame)``: title, label and colour always come from the same frame."""
    screen, frame, recognize = pages.screen_now(rt)
    if frame is None:
        return None, None
    if not pages.is_page(screen, MAIL) or not actions._hit(recognize(CLAIM_ALL)) or not rt._frame_still_valid(frame):
        return None, frame
    return button_state(frame)[0], frame


def _menu(screen, _frame=None, _recognize=None) -> bool:
    return pages.is_exactly(screen, MENU)  # not 美鸭梨挖掘


def _mail(screen, _frame=None, _recognize=None) -> bool:
    return pages.is_page(screen, MAIL)  # 「邮件 7/300 i」


def run(rt) -> bool:
    from navigation.models import NavigationError
    claimed = None
    try:
        start = []
        if not to_hud(rt, rejected=lambda frame, reason: start.append((frame, reason))):
            for frame, reason in start:  # the screen the task would not start from (e.g. a popup)
                rt.diagnostic("android_mailbox_start", f"开始时不在大世界，也没法安全地回去：{reason}", image=frame)
            return fail(rt, "没能回到大世界")
        # The strict duck tap, not the named action: that one cancels a quit dialog and goes on (decision 2), and a
        # quit dialog between the start and this tap must stop the task with its frame instead.
        if not pages.tap_duck(rt):
            return False
        # Each wait stops at once on a recognised screen that is neither the one being left nor the one expected.
        if pages.wait_screen(rt, "打开美鸭梨菜单", _menu, lambda s, f, r: s.kind == "hud", 5) is None:
            return False
        if not pages.tap_on_page(rt, MENU, MENU_MAIL, exact=True):
            return False
        if pages.wait_screen(rt, "打开邮件页面", _mail, _menu, 5) is None:
            return False
        claimed = claim(rt)
    except (Stop, actions.ScreenUnrecognised, NavigationError) as error:  # PageMismatch, FrameMismatch, guard too
        stopped(rt, error)
    # The way back only after a successful result: after a stop, a guard refusal or a fatal error (raised past this
    # point, e.g. the device stopped answering) nothing more is sent.
    if not claimed:
        return False
    try:
        if rt.stopped or not back_from_mail(rt):
            return fail(rt, "没能回到大世界")
    except (actions.ScreenUnrecognised, NavigationError) as error:
        return stopped(rt, error, None if isinstance(error, pages.GuardRefusal) else "没能回到大世界")
    return True


def stopped(rt, error, reason=None) -> bool:
    """Report one of this task's stops with its frames saved (a Stop's frames, else the refusing frame an error
    carries). The user message is ``reason`` or the error's own fixed text; a guard refusal's OCR'd row goes to the
    diagnostic only."""
    frames = error.frames if isinstance(error, Stop) else [getattr(error, "frame", None)]
    for index, frame in enumerate(frame for frame in frames if frame is not None):
        rt.diagnostic(f"android_mailbox_stop_{index}", pages.diagnostic_reason(error), image=frame)
    return fail(rt, reason or str(error))


def claim(rt) -> bool:
    state, frame = mail_state(rt)
    if frame is None:
        return False
    if state is None:
        raise Stop("邮件页面上找不到「领取全部」", [frame])
    if state == "disabled":
        rt.log("邮箱没有可领取的附件。")
        return True
    if state != "enabled":
        raise Stop("「领取全部」按钮的状态认不出", [frame])
    # Both tap frames must be the mail page with the label and an enabled button.
    if not pages.tap_on_page(rt, MAIL, CLAIM_ALL, verify=lambda frame: button_state(frame)[0] == "enabled"):
        return False

    # The reward layer that follows has not been seen on the device. Success only on one valid frame that is the mail
    # page with the label and a disabled button; mail frames are waited through, anything else recognised stops at
    # once with its frame, without another tap.
    def done(screen, frame, recognize):
        return _mail(screen) and actions._hit(recognize(CLAIM_ALL)) and button_state(frame)[0] == "disabled"
    try:
        if pages.wait_screen(rt, "领取邮件附件", done, _mail, 8) is None:
            return False
    except pages.FrameMismatch as error:
        raise Stop("领取后没有回到「领取全部」已变灰的邮件页面（奖励层还没有在设备上见过，没有处理）",
                   [error.frame]) from None
    rt.log("邮箱附件已全部领取。")
    return True


def fail(rt, reason) -> bool:
    from host import android_user_stop
    if not rt.stopped and not android_user_stop():  # a user stop is no failure
        rt.log(f"邮箱领取：{reason}，本项未完成。")
    return False
