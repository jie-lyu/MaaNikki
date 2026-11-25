"""Android 邮件 flow (agent/host/android/mailbox.py) and page taps (pages.py): python scripts/test_android_mailbox.py"""
from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402

from host.android import actions, mailbox, pages  # noqa: E402
from host.android.actions import Screen  # noqa: E402


def hit(box, text=""):
    return SimpleNamespace(hit=True, box=box, best_result=SimpleNamespace(text=text, box=box))


MISS = SimpleNamespace(hit=False, box=None, best_result=None)


def page(title):
    return Screen("page", {"back": [37, 22, 40, 40]}, title=title)


class Rt:
    """A runtime whose frames are scripted (screen, nodes) or (screen, nodes, image) entries (the last one repeats);
    taps, served frames and saved failure scenes are recorded."""
    TOUCH_KEY_ATTEMPTS = 3

    def __init__(self, frames):
        self.frames, self.clicks, self.logs, self.stopped, self.valid = list(frames), [], [], False, True
        self.served, self.diagnostic, self.key = [], Mock(), Mock(return_value=True)

    def _fresh_device_frame(self):
        entry = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        screen, nodes = entry[:2]
        frame = entry[2] if len(entry) > 2 else np.zeros((720, 1280, 3), np.uint8)
        self.current = (frame, screen, nodes)
        self.served.append((frame, screen))
        return frame

    def _recognizer_on(self, frame):
        _f, _screen, nodes = self.current
        return lambda node, override=None: nodes.get(node, MISS)

    def _frame_still_valid(self, frame):
        return self.valid

    def action(self, name, target, source_frame):
        self.clicks.append((name, target))
        return True

    def pause(self, seconds):
        return True

    def log(self, text):
        self.logs.append(text)

    def frame_of(self, kind):
        """The (first) frame served with a screen of ``kind``."""
        return next(frame for frame, screen in self.served if screen.kind == kind)


def scripted(rt):
    """read_screen answers with the scripted screen of the frame being recognised."""
    return patch.object(pages.actions, "read_screen", side_effect=lambda recognize: rt.current[1])


HUD = Screen("hud", {"duck": [30, 18, 54, 54]})
QUIT = Screen("quit_dialog", {"cancel": [430, 460, 130, 50]})
BLANK = Screen("unknown", reason="认不出当前画面")  # with no node recognised on it: a transition
ARROW = {actions.BACK_ARROW: hit([37, 22, 40, 40])}
# The start's look at the HUD, then the strict duck tap's look, deciding and confirming frames (all the HUD).
DUCK = [(HUD, {})] * 4


class PageTapTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(pages.actions, "read_screen", side_effect=lambda recognize: self.rt.current[1])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_target_on_the_expected_page_is_tapped_on_the_confirming_frame(self):
        nodes = {"Target": hit([530, 645, 55, 50])}
        self.rt = Rt([(page("美鸭梨"), nodes), (page("美鸭梨"), nodes)])
        self.assertTrue(pages.tap_on_page(self.rt, "美鸭梨", "Target"))
        self.assertEqual(self.rt.clicks, [("Click", [530, 645, 55, 50])])

    def test_nothing_is_tapped_off_the_page_or_without_the_target(self):
        for frames in ([(page("设置"), {"Target": hit([1, 1, 5, 5])})],
                       [(page("美鸭梨"), {})],
                       [(Screen("hud", {"duck": [30, 18, 54, 54]}), {"Target": hit([1, 1, 5, 5])})]):
            with self.subTest(frames=frames):
                self.rt = Rt(frames)
                with self.assertRaises(pages.PageMismatch):
                    pages.tap_on_page(self.rt, "美鸭梨", "Target")
                self.assertEqual(self.rt.clicks, [])

    def test_a_point_next_to_a_forbidden_word_is_not_tapped(self):
        # The whole label is read on the full frame: 删除已读 ending 25 px left of the envelope still vetoes it.
        delete = hit([380, 655, 125, 30], "删除已读")
        nodes = {"Target": hit([530, 645, 55, 50]), pages.FORBIDDEN: delete}
        self.rt = Rt([(page("美鸭梨"), nodes)])
        with self.assertRaises(pages.GuardRefusal) as caught:
            pages.tap_on_page(self.rt, "美鸭梨", "Target")
        # A fixed message for the user; the OCR'd row only in the diagnostic, with the frame it was read on.
        self.assertEqual(str(caught.exception), pages.GUARD_TAP)
        self.assertIn("删除已读", pages.diagnostic_reason(caught.exception))
        self.assertIs(caught.exception.frame, self.rt.served[0][0])
        self.assertEqual(self.rt.clicks, [])
        far = {"Target": hit([311, 650, 132, 41]), pages.FORBIDDEN: hit([60, 652, 170, 30], "删除已读")}
        self.rt = Rt([(page("邮件"), far), (page("邮件"), far)])
        self.assertTrue(pages.tap_on_page(self.rt, "邮件", "Target"))  # 81 px away: allowed

    def test_verify_must_hold_on_both_frames(self):
        nodes = {"Target": hit([311, 650, 132, 41])}
        self.rt = Rt([(page("邮件"), nodes)] * 8)
        states = iter([True, False, True, False, True, False])
        with self.assertRaises(pages.PageMismatch):
            pages.tap_on_page(self.rt, "邮件", "Target", verify=lambda frame: next(states))
        self.assertEqual(self.rt.clicks, [])

    def test_a_target_that_moved_on_the_same_page_is_decided_again(self):
        first = {"Target": hit([530, 645, 55, 50])}
        moved = {"Target": hit([600, 645, 55, 50])}
        self.rt = Rt([(page("美鸭梨"), first), (page("美鸭梨"), moved), (page("美鸭梨"), moved),
                      (page("美鸭梨"), moved)])
        self.assertTrue(pages.tap_on_page(self.rt, "美鸭梨", "Target"))
        self.assertEqual(self.rt.clicks, [("Click", [600, 645, 55, 50])])


    def test_a_recognised_other_screen_on_the_confirming_frame_ends_the_tap_with_that_frame(self):
        # Review d2-mailstart-r1, 2: the quit dialog (or a popup, another page) on the confirming frame used to be
        # decided past; the next two mail frames then sent the tap.
        target = {"Target": hit([37, 22, 40, 40])}
        others = ((QUIT, {}), (Screen("modal", {"close": [929, 177, 36, 36]}, title="兑换奖励"), {}),
                  (page("设置"), target), (page("美鸭梨挖掘"), target),
                  (Screen("unknown", reason="弹窗"), {actions.MODAL_CLOSE: hit([900, 100, 30, 30])}))
        for other, nodes in others:
            with self.subTest(other=other):
                self.rt = Rt([(page("美鸭梨"), target), (other, nodes), (page("美鸭梨"), target), (page("美鸭梨"), target)])
                with self.assertRaises(pages.FrameMismatch) as caught:
                    pages.tap_on_page(self.rt, "美鸭梨", "Target")
                self.assertIs(caught.exception.frame, self.rt.served[1][0])  # that frame, not the page before it
                self.assertIs(caught.exception.screen, other)
                self.assertEqual(self.rt.clicks, [])

    def test_a_frame_with_nothing_recognised_yet_is_decided_past(self):
        target = {"Target": hit([37, 22, 40, 40])}
        self.rt = Rt([(page("美鸭梨"), target), (BLANK, {}), (page("美鸭梨"), target), (page("美鸭梨"), target)])
        self.assertTrue(pages.tap_on_page(self.rt, "美鸭梨", "Target"))
        self.assertEqual(self.rt.clicks, [("Click", [37, 22, 40, 40])])

    def test_a_deciding_frame_that_is_not_the_page_is_carried_out(self):
        # Review d2-mailstart-r1, 3: the refusing frame goes out with the refusal (the caller saves it).
        self.rt = Rt([(QUIT, {})])
        with self.assertRaises(pages.FrameMismatch) as caught:
            pages.tap_on_page(self.rt, "邮件", actions.BACK_ARROW)
        self.assertIs(caught.exception.frame, self.rt.served[0][0])
        self.assertEqual(self.rt.clicks, [])

    def test_an_observed_title_is_left_whole_whitespace_aside(self):
        # The live mail page's three readings are one page; a longer title on the same arrow is not.
        self.rt = Rt([(page("邮件7/300 i"), ARROW), (page("邮件7/300i"), ARROW)])
        self.assertTrue(pages.tap_on_page(self.rt, "邮件 7/300 i", actions.BACK_ARROW, exact=True))
        self.assertEqual(len(self.rt.clicks), 1)
        self.rt = Rt([(page("美鸭梨挖掘"), ARROW)])
        with self.assertRaises(pages.FrameMismatch):
            pages.tap_on_page(self.rt, "美鸭梨", actions.BACK_ARROW, exact=True)
        self.assertEqual(self.rt.clicks, [])


class MailboxTests(unittest.TestCase):
    """mailbox.run on scripted frames: the real waits and page taps (pages.py), only read_screen scripted."""

    def frame_with_button(self, state):
        frame = np.zeros((720, 1280, 3), np.uint8)
        x, y, w, h = mailbox.BUTTON_FACE
        colour = {"disabled": (150, 152, 155), "enabled": (40, 140, 200), "odd": (90, 60, 30)}[state]
        frame[y:y + h, x:x + w] = colour
        return frame

    def test_button_states(self):
        self.assertEqual(mailbox.button_state(self.frame_with_button("disabled"))[0], "disabled")
        self.assertEqual(mailbox.button_state(self.frame_with_button("enabled"))[0], "enabled")
        self.assertEqual(mailbox.button_state(self.frame_with_button("odd"))[0], "unknown")

    MAIL_NODES = {mailbox.CLAIM_ALL: hit([311, 650, 132, 41], "领取全部"), **ARROW}
    MENU_NODES = {mailbox.MENU_MAIL: hit([530, 645, 55, 50]), **ARROW}

    def mail(self, state, title="邮件7/300 i"):
        return page(title), self.MAIL_NODES, self.frame_with_button(state)

    def menu(self):
        return page("美鸭梨"), self.MENU_NODES

    def play(self, frames):
        self.rt = rt = Rt(frames)
        with scripted(rt), patch("host.android_user_stop", return_value=False):
            result = mailbox.run(rt)
        return result, rt

    def taps(self, rt):
        return [tuple(target) for _name, target in rt.clicks]

    def test_a_disabled_button_means_nothing_to_claim(self):
        result, rt = self.play(DUCK + [self.menu(), self.menu(), self.menu(), self.mail("disabled"),
                                self.mail("disabled"), self.mail("disabled"), self.mail("disabled"),
                                self.mail("disabled"), self.menu(), self.menu(), self.menu(), (HUD, {})])
        self.assertTrue(result, rt.logs)
        rt.key.assert_not_called()  # the strict duck tap, not the named action
        # The envelope, then the way back (the arrow on the mail page, then on the menu); never claim or delete.
        self.assertEqual(self.taps(rt), [(56, 44), (530, 645, 55, 50), (37, 22, 40, 40), (37, 22, 40, 40)])
        self.assertIn("邮箱没有可领取的附件。", rt.logs)

    def test_an_enabled_button_is_claimed_and_checked_on_one_mail_frame(self):
        result, rt = self.play(DUCK + [self.menu(), self.menu(), self.menu(), self.mail("enabled"),
                                self.mail("enabled"), self.mail("enabled"), self.mail("enabled"),
                                (BLANK, {}), self.mail("disabled"), self.mail("disabled"), self.mail("disabled"),
                                self.mail("disabled"), self.menu(), self.menu(), self.menu(), (HUD, {})])
        self.assertTrue(result, rt.logs)
        self.assertEqual(self.taps(rt), [(56, 44), (530, 645, 55, 50), (311, 650, 132, 41), (37, 22, 40, 40),
                                         (37, 22, 40, 40)])
        self.assertIn("邮箱附件已全部领取。", rt.logs)

    def test_another_screen_after_claiming_stops_at_once_with_that_frame(self):
        layer = Screen("unknown", reason="认不出")
        result, rt = self.play(DUCK + [self.menu(), self.menu(), self.menu(), self.mail("enabled"),
                                self.mail("enabled"), self.mail("enabled"), self.mail("enabled"),
                                (layer, {actions.MODAL_CLOSE: hit([1100, 60, 40, 40])}), self.mail("disabled")])
        self.assertFalse(result)
        self.assertNotIn("邮箱附件已全部领取。", rt.logs)
        self.assertEqual(len(rt.clicks), 3)  # duck, envelope, claim; no way back from an unexpected screen
        self.assertIs(rt.diagnostic.call_args.kwargs["image"], rt.frame_of("unknown"))

    def test_an_unexpected_screen_while_opening_the_menu_or_the_mail_stops_with_that_frame(self):
        for frames in (DUCK + [(HUD, {}), (page("设置"), ARROW), self.menu()],
                       DUCK + [self.menu(), self.menu(), self.menu(), self.menu(), (QUIT, {}),
                               self.mail("disabled")]):
            with self.subTest(frames=len(frames)):
                result, rt = self.play(frames)
                self.assertFalse(result)
                self.assertLessEqual(len(rt.clicks), 2)  # at most the duck and the envelope
                image = rt.diagnostic.call_args.kwargs["image"]
                self.assertIs(image, rt.served[-1][0])  # the unexpected frame itself
                self.assertIn(rt.served[-1][1].kind, ("page", "quit_dialog"))
                self.assertTrue(any("不该出现的画面" in line for line in rt.logs), rt.logs)

    def test_an_unclear_button_stops_without_a_claim_or_navigation(self):
        result, rt = self.play(DUCK + [self.menu(), self.menu(), self.menu(), self.mail("odd")])
        self.assertFalse(result)
        self.assertEqual(len(rt.clicks), 2)  # duck and envelope
        rt.diagnostic.assert_called_once()

    def test_a_fatal_device_error_sends_nothing_more(self):
        from host import DeviceUnreachable
        with patch.object(mailbox, "claim", side_effect=DeviceUnreachable("ADB 没有回应")):
            with self.assertRaises(DeviceUnreachable):
                self.play(DUCK + [self.menu(), self.menu(), self.menu(), self.mail("disabled")])
        self.assertEqual(len(self.rt.clicks), 2)

    def test_a_guard_refusal_ends_all_input_with_a_fixed_message(self):
        delete = {pages.FORBIDDEN: hit([480, 655, 40, 30], "删除已读 奇迹之旅即将")}
        nodes = {**self.MENU_NODES, **delete}
        result, rt = self.play(DUCK + [self.menu(), (page("美鸭梨"), nodes)])
        self.assertFalse(result)
        self.assertEqual(self.taps(rt), [(56, 44)])  # the duck; the envelope is refused
        self.assertEqual(rt.logs, [f"邮箱领取：{pages.GUARD_TAP}，本项未完成。"])  # the row is not shown ...
        self.assertIn("删除已读 奇迹之旅即将", rt.diagnostic.call_args.args[1])  # ... it is in the saved scene

    def test_a_quit_dialog_after_the_start_stops_with_its_frame_and_sends_nothing(self):
        # Review d2-mailstart-r2, 1: HUD (the start) -> quit dialog -> HUD -> HUD. The named action would cancel the
        # dialog and tap the duck; the strict duck tap stops on the dialog's frame.
        result, rt = self.play([(HUD, {}), (QUIT, {}), (HUD, {}), (HUD, {}), (HUD, {})])
        self.assertFalse(result)
        self.assertEqual(rt.clicks, [])
        rt.key.assert_not_called()
        self.assertIs(rt.diagnostic.call_args.kwargs["image"], rt.frame_of("quit_dialog"))
        self.assertEqual(len(rt.logs), 1)

    def test_starting_on_the_mail_page_leaves_it_through_its_back_arrow(self):
        # One live mail page read 「邮件 7/300 i」, 「邮件7/300 i」 and 「邮件7/300i」 on consecutive frames; the first
        # read used to be the title the next frame had to start with, so the task failed before any tap and the
        # failure message quoted that read.
        self.rt = Rt([(page("邮件 7/300 i"), ARROW), (page("邮件7/300 i"), ARROW), (page("邮件7/300i"), ARROW),
                      self.menu(), self.menu(), self.menu(), (HUD, {})])
        with scripted(self.rt):
            self.assertTrue(mailbox.to_hud(self.rt))
        self.assertEqual(self.rt.clicks, [("Click", [37, 22, 40, 40])] * 2)  # the mail page, then the menu

    def test_the_quit_dialog_on_the_confirming_frame_of_the_first_back_arrow_stops(self):
        # Review d2-mailstart-r1, 2: mail -> deciding mail -> confirming quit -> mail -> mail used to send the arrow.
        result, rt = self.play([(page("邮件 7/300 i"), ARROW), (page("邮件7/300 i"), ARROW), (QUIT, {}),
                                (page("邮件7/300i"), ARROW), (page("邮件7/300i"), ARROW), (HUD, {})])
        self.assertFalse(result)
        self.assertEqual(rt.clicks, [])
        rt.key.assert_not_called()
        self.assertEqual(rt.logs, ["邮箱领取：没能回到大世界，本项未完成。"])
        stage, reason = rt.diagnostic.call_args.args
        self.assertEqual(stage, "android_mailbox_start")
        self.assertIs(rt.diagnostic.call_args.kwargs["image"], rt.frame_of("quit_dialog"))
        self.assertNotIn("7/300", reason)

    def test_a_refusal_at_the_start_saves_the_refusing_frame(self):
        # Review d2-mailstart-r1, 3: the opening look saw mail, the deciding frame the quit dialog: that one is saved.
        result, rt = self.play([(page("邮件 7/300 i"), ARROW), (QUIT, {}), (HUD, {})])
        self.assertFalse(result)
        self.assertEqual(rt.clicks, [])
        self.assertIs(rt.diagnostic.call_args.kwargs["image"], rt.frame_of("quit_dialog"))

    def test_a_modal_at_the_start_sends_nothing(self):
        modal = Screen("modal", {"close": [929, 177, 36, 36]}, title="兑换奖励")
        result, rt = self.play([(modal, {})])
        self.assertFalse(result)
        self.assertEqual(rt.clicks, [])
        self.assertIs(rt.diagnostic.call_args.kwargs["image"], rt.served[0][0])

    def test_the_way_back_only_leaves_mail_and_menu_pages(self):
        rt = Rt([(page("商城"), ARROW)])
        with scripted(rt):
            self.assertFalse(mailbox.back_from_mail(rt))
        self.assertEqual(rt.clicks, [])


class StartFromMailTests(unittest.TestCase):
    """The mail task stopped on the mail page, then another task started (review d2-mailstart-r1, 1): every task
    leaves the observed page by its whole title, whitespace aside, and names it without its OCR'd title."""

    MAIL = [(page("邮件 7/300 i"), ARROW), (page("邮件7/300 i"), ARROW), (page("邮件7/300i"), ARROW)]

    def frames(self):
        # Look, decide, confirm on the mail page; the mail page again while it goes (another reading), then the menu
        # (look, decide, confirm), then the HUD.
        menu = (page("美鸭梨"), ARROW)
        return self.MAIL + [(page("邮件 7/300i"), ARROW), menu, menu, menu, menu, (HUD, {})]

    def test_monthly_and_weekly_leave_the_mail_page_one_arrow_at_a_time(self):
        from host.android import monthly, shop
        for name, module, shortcut in (("monthly", monthly, "monthly"), ("weekly", shop, "shop")):
            with self.subTest(task=name):
                rt = Rt(self.frames())
                # The strict shortcut tap ends the run right after the start (False: as after a stop).
                with scripted(rt), patch("host.android_user_stop", return_value=False), \
                        patch.object(pages, "LEAVE_PAGE_WAIT", 1), \
                        patch.object(pages, "tap_shortcut", return_value=False) as tap:
                    module.run(rt)
                self.assertEqual(rt.clicks, [("Click", [37, 22, 40, 40])] * 2)
                self.assertEqual(rt.served[-1][1].kind, "hud")
                tap.assert_called_once_with(rt, shortcut)  # got past the start
                rt.key.assert_not_called()  # never the named action, which cancels a quit dialog and goes on
                rt.diagnostic.assert_not_called()

    def test_redeem_leaves_the_mail_page_and_its_messages_quote_no_title(self):
        from host.android import redeem
        rt = Rt(self.frames())
        with scripted(rt):
            self.assertTrue(redeem.to_hud(rt, "开始前回到大世界", any_page=True))
        self.assertEqual(rt.clicks, [("Click", [37, 22, 40, 40])] * 2)
        # A confirming frame that is the quit dialog stops, with that frame and a message without the title.
        rt = Rt(self.MAIL[:2] + [(QUIT, {})])
        with scripted(rt), patch("host.android_user_stop", return_value=False):
            self.assertFalse(redeem.guarded(rt, lambda: redeem.to_hud(rt, "开始前回到大世界", any_page=True)))
        self.assertEqual(rt.clicks, [])
        self.assertIs(rt.diagnostic.call_args.kwargs["image"], rt.frame_of("quit_dialog"))
        self.assertFalse(any("7/300" in line for line in rt.logs), rt.logs)
        self.assertTrue(rt.logs)

    def test_a_page_staying_up_under_another_spacing_is_not_left(self):
        # Review d2-mailstart-r1, 4: OCR spacing alone is the same page, so no second arrow is decided on it.
        rt = Rt(self.MAIL + [(page("邮件7/300i"), ARROW)])
        rejected = []
        with scripted(rt), patch.object(pages, "LEAVE_PAGE_WAIT", 0):
            self.assertFalse(pages.to_hud(rt, single=True, rejected=lambda f, r: rejected.append(r)))
        self.assertEqual(len(rt.clicks), 1)
        self.assertEqual(rejected, ["点了返回箭头之后，页面一直没有离开"])


FRAMEWORK_CHECK = r"""
import json, sys
from pathlib import Path
import cv2
import numpy as np
from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.resource import Resource
from maa.tasker import Tasker

repo = Path(sys.argv[1])
sys.path.insert(0, str(repo / "agent"))
from host.android import actions

fixtures = repo / "scripts/fixtures/android"
frames = {}
for name in ("mail_title_spaced", "mail_title_joined"):  # the top-left 400x80 of two live mail page frames
    crop = cv2.imread(str(fixtures / f"{name}.png"))
    frame = np.full((720, 1280, 3), 90, np.uint8)
    frame[:crop.shape[0], :crop.shape[1]] = crop
    frames[name] = frame
current, results = {}, {}

class Frame(CustomController):
    def connect(self): return True
    def request_uuid(self): return "android-mailbox-test"
    def screencap(self): return current["frame"]
    def refuse(self, *args): return False
    start_app = stop_app = click = swipe = touch_down = touch_move = touch_up = refuse
    click_key = input_text = key_down = key_up = refuse

class Classify(CustomAction):
    def run(self, context, argv):
        image = context.tasker.controller.post_screencap().wait().get()
        screen = actions.read_screen(lambda node, override=None: context.run_recognition(
            node, image, {node: override} if override else None))
        results[current["name"]] = [screen.kind, screen.title]
        return CustomAction.RunResult(success=True)

resource = Resource()
for bundle in (repo / "resource", repo / "resource_android"):
    assert resource.post_bundle(bundle).wait().succeeded, bundle
action = Classify()
assert resource.register_custom_action("classify", action)
controller = Frame()
assert controller.post_connection().wait().succeeded
tasker = Tasker()
assert tasker.bind(resource, controller)
entry = {"Classify": {"recognition": "DirectHit", "action": "Custom", "custom_action": "classify", "next": []}}
for name, frame in frames.items():
    current.update(name=name, frame=frame)
    tasker.post_task("Classify", entry).wait()
print(json.dumps(results, ensure_ascii=True))
"""


@unittest.skipUnless((ROOT / "maafw").exists(), "needs the MaaFramework runtime in ./maafw")
class FrameworkTests(unittest.TestCase):
    """The real nodes and OCR, both layers loaded, on UID-free crops of the live mail page (fresh interpreter)."""

    def test_the_mail_page_is_one_page_whatever_spacing_ocr_reads_in_its_title(self):
        if not (ROOT / "resource_android").is_dir():  # the release source archive does not list it (plan risk 13)
            self.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
            return
        env = {**os.environ, "MAAFW_BINARY_PATH": str(ROOT / "maafw")}
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", "-c", FRAMEWORK_CHECK, str(ROOT)],
                                capture_output=True, text=True, encoding="utf-8", timeout=300, env=env,
                                cwd=tempfile.gettempdir())
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        (kind_a, spaced), (kind_b, joined) = outcome["mail_title_spaced"], outcome["mail_title_joined"]
        self.assertEqual((kind_a, kind_b), ("page", "page"))
        self.assertTrue(pages.same_title(spaced, joined), (spaced, joined))
        self.assertTrue(pages.is_page(Screen("page", title=spaced), mailbox.MAIL))


class ResourceTests(unittest.TestCase):
    def test_nodes_and_tier(self):
        tiers = json.loads((ROOT / "agent/host/android/tasks.json").read_text(encoding="utf-8"))
        self.assertIn("MaaNikki_Mailbox", tiers["experimental"])
        self.assertEqual(tiers["entries"], [])  # verified only after device runs
        if not (ROOT / "resource_android").is_dir():  # the release source archive does not list it (plan risk 13)
            self.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
            return
        nodes = json.loads((ROOT / "resource_android/pipeline/android_mailbox.json").read_text(encoding="utf-8"))
        self.assertEqual(set(nodes), {mailbox.MENU_MAIL, mailbox.CLAIM_ALL, pages.FORBIDDEN})
        self.assertTrue(nodes[mailbox.MENU_MAIL]["green_mask"])
        self.assertIn("删除", nodes[pages.FORBIDDEN]["expected"])


if __name__ == "__main__":
    unittest.main()
