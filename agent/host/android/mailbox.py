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
    """Leave pages until the open-world HUD shows (each leave decided on its own frames)."""
    for _ in range(attempts):
        screen, frame, _ = pages.screen_now(rt)
        if frame is None:
            return False
        if screen.kind == "hud" and rt._frame_still_valid(frame):
            return True
        if not rt.leave_page():
            return False
    return pages.wait_for(rt, lambda screen: screen.kind == "hud", 3) is not None


def run(rt) -> bool:
    from navigation.models import NavigationError
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
    except (actions.ScreenUnrecognised, NavigationError) as error:
        fail(rt, str(error))
        claimed = None
    finally:
        returned = not rt.stopped and to_hud(rt)
    if claimed is None:
        return False
    if not returned:
        return fail(rt, "没能回到大世界")
    return claimed


def claim(rt) -> bool:
    screen, frame, recognize = pages.screen_now(rt)
    if frame is None:
        return False
    if not pages.is_page(screen, MAIL) or not actions._hit(recognize(CLAIM_ALL)):
        return fail(rt, "邮件页面上找不到「领取全部」")
    state, gold, gray = button_state(frame)
    if state == "disabled":
        rt.log("邮箱没有可领取的附件。")
        return True
    if state != "enabled":
        return fail(rt, f"「领取全部」按钮的状态认不出（金色 {gold:.2f}，灰色 {gray:.2f}）")
    if not pages.tap_on_page(rt, MAIL, CLAIM_ALL):
        return False
    # After claiming, the game shows a reward layer; it is not yet seen on the device. Wait for the mail page with a
    # disabled button; anything else stops the task (nothing more is tapped here).
    if pages.wait_for(rt, lambda screen: pages.is_page(screen, MAIL), 8) is None:
        return fail(rt, "领取后没有回到邮件页面（奖励层还没有在设备上见过，没有处理）")
    screen, frame, _ = pages.screen_now(rt)
    if frame is None or button_state(frame)[0] != "disabled":
        return fail(rt, "领取后没能确认附件已全部领取")
    rt.log("邮箱附件已全部领取。")
    return True


def fail(rt, reason) -> bool:
    if not rt.stopped:
        rt.log(f"邮箱领取：{reason}，本项未完成。")
    return False
