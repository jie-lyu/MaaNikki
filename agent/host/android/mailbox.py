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


def to_hud(rt, attempts: int = 4) -> bool:
    """Back to the open-world HUD before the task: the back arrow, tapped through tap_on_page on the page that is
    showing (both tap frames, with the forbidden-label check); a modal, the quit dialog or an unrecognised screen
    stops the task without a tap."""
    for _ in range(attempts):
        screen, frame, _ = pages.screen_now(rt)
        if frame is None or not rt._frame_still_valid(frame):
            return False
        if screen.kind == "hud":
            return True
        if screen.kind != "page" or not pages.tap_on_page(rt, screen.title, actions.BACK_ARROW):
            return False
    return False


def back_from_mail(rt, attempts: int = 3) -> bool:
    """The way back after the task: the back arrow, tapped only on the 邮件 and 美鸭梨 pages (both tap frames,
    with the forbidden-label check); any other screen stops input."""
    for _ in range(attempts):
        screen, frame, _ = pages.screen_now(rt)
        if frame is None or not rt._frame_still_valid(frame):
            return False
        if screen.kind == "hud":
            return True
        title = MAIL if pages.is_page(screen, MAIL) else MENU if pages.is_page(screen, MENU) else None
        if title is None or not pages.tap_on_page(rt, title, actions.BACK_ARROW):
            return False
    return False


class Stop(Exception):
    """Something unexpected: report it and send nothing more (not even the way back)."""


def mail_state(rt):
    """``(state, frame)`` from one fresh frame that is the mail page with the claim-all label on it, else
    ``(None, frame)``: title, label and colour always come from the same frame."""
    screen, frame, recognize = pages.screen_now(rt)
    if frame is None:
        return None, None
    if not pages.is_page(screen, MAIL) or not actions._hit(recognize(CLAIM_ALL)) or not rt._frame_still_valid(frame):
        return None, frame
    return button_state(frame)[0], frame


def run(rt) -> bool:
    from navigation.models import NavigationError
    claimed = None
    try:
        if not to_hud(rt):
            return fail(rt, "没能回到大世界")
        if not rt.key("menu"):
            return False
        if pages.wait_for(rt, lambda screen: pages.is_page(screen, MENU), 5) is None:
            return fail(rt, "没能打开美鸭梨菜单")
        if not pages.tap_on_page(rt, MENU, MENU_MAIL):
            return False
        if pages.wait_for(rt, lambda screen: pages.is_page(screen, MAIL), 5) is None:
            return fail(rt, "没能打开邮件页面")
        claimed = claim(rt)
    except (pages.GuardRefusal, Stop, actions.ScreenUnrecognised, NavigationError) as error:
        fail(rt, str(error))
    # The way back only after a successful result: after a stop, a guard refusal or a fatal error (raised past this
    # point, e.g. the device stopped answering) nothing more is sent.
    if not claimed:
        return False
    if rt.stopped or not back_from_mail(rt):
        return fail(rt, "没能回到大世界")
    return True


def claim(rt) -> bool:
    state, frame = mail_state(rt)
    if frame is None:
        return False
    if state is None:
        return fail(rt, "邮件页面上找不到「领取全部」")
    if state == "disabled":
        rt.log("邮箱没有可领取的附件。")
        return True
    if state != "enabled":
        raise Stop("「领取全部」按钮的状态认不出")
    # Both tap frames must be the mail page with the label and an enabled button.
    if not pages.tap_on_page(rt, MAIL, CLAIM_ALL, verify=lambda frame: button_state(frame)[0] == "enabled"):
        return False
    # The reward layer that follows has not been seen on the device. Success only on one valid frame that is the mail
    # page with the label and a disabled button; anything else stops without another tap.
    import time
    deadline = time.monotonic() + 8
    while not rt.stopped and time.monotonic() < deadline:
        state, frame = mail_state(rt)
        if frame is None:
            return False
        if state == "disabled":
            rt.log("邮箱附件已全部领取。")
            return True
        rt.pause(.5)
    raise Stop("领取后没有回到「领取全部」已变灰的邮件页面（奖励层还没有在设备上见过，没有处理）")


def fail(rt, reason) -> bool:
    if not rt.stopped:
        rt.log(f"邮箱领取：{reason}，本项未完成。")
    return False
