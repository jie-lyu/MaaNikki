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
    """A runtime whose frames are scripted (screen, nodes) pairs; taps are recorded."""
    TOUCH_KEY_ATTEMPTS = 3

    def __init__(self, frames):
        self.frames, self.clicks, self.logs, self.stopped, self.valid = list(frames), [], [], False, True

    def _fresh_device_frame(self):
        screen, nodes = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        frame = np.zeros((720, 1280, 3), np.uint8)
        self.current = (frame, screen, nodes)
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
        self.assertIn("删除", str(caught.exception))
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

    def test_a_target_that_moved_or_a_page_that_changed_is_decided_again(self):
        first = {"Target": hit([530, 645, 55, 50])}
        moved = {"Target": hit([600, 645, 55, 50])}
        self.rt = Rt([(page("美鸭梨"), first), (page("美鸭梨"), moved), (page("美鸭梨"), moved),
                      (page("美鸭梨"), moved)])
        self.assertTrue(pages.tap_on_page(self.rt, "美鸭梨", "Target"))
        self.assertEqual(self.rt.clicks, [("Click", [600, 645, 55, 50])])


class MailboxTests(unittest.TestCase):
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

    def run_flow(self, button, after=("disabled",), guard=False):
        rt = SimpleNamespace(stopped=False, logs=[], key=Mock(return_value=True), leave_page=Mock(return_value=True),
                             _frame_still_valid=Mock(return_value=True), pause=Mock(return_value=True))
        rt.log = rt.logs.append
        claim_node = {mailbox.CLAIM_ALL: hit([311, 650, 132, 41], "领取全部")}
        recognize = lambda node, override=None: claim_node.get(node, MISS)  # noqa: E731
        hud = (Screen("hud", {"duck": [30, 18, 54, 54]}), self.frame_with_button("disabled"), lambda *a: MISS)
        mail = lambda state: (page("邮件8/300"), self.frame_with_button(state), recognize)  # noqa: E731
        settings = (page("设置"), self.frame_with_button("disabled"), lambda *a: MISS)
        afterwards = [{"disabled": mail("disabled"), "settings": settings}[name] for name in after]
        sequence = iter([hud, mail(button)] + afterwards + [mail("disabled"), hud, hud])
        taps = []

        def tap(rt_, title, node, **kwargs):
            if guard and node == mailbox.MENU_MAIL:
                raise pages.GuardRefusal("要点的地方旁边有「删除」，没有点，任务停止")
            taps.append(node)
            return True
        with patch.object(pages, "screen_now", side_effect=lambda rt_: next(sequence)), \
                patch.object(pages, "wait_for", side_effect=lambda rt_, accept, seconds, step=.4: page("x")), \
                patch.object(pages, "tap_on_page", side_effect=tap), \
                patch("time.monotonic", side_effect=iter(range(0, 100, 3)).__next__):
            result = mailbox.run(rt)
        return result, rt, taps

    def test_a_disabled_button_means_nothing_to_claim(self):
        result, rt, taps = self.run_flow("disabled", after=())
        self.assertTrue(result)
        # The envelope, then the way back (the arrow on the mail page); never the claim or delete buttons.
        self.assertEqual(taps, [mailbox.MENU_MAIL, actions.BACK_ARROW])
        rt.key.assert_called_once_with("menu")
        self.assertIn("邮箱没有可领取的附件。", rt.logs)

    def test_an_enabled_button_is_claimed_and_checked_on_one_mail_frame(self):
        result, rt, taps = self.run_flow("enabled")
        self.assertTrue(result)
        self.assertEqual(taps, [mailbox.MENU_MAIL, mailbox.CLAIM_ALL, actions.BACK_ARROW])
        self.assertIn("邮箱附件已全部领取。", rt.logs)

    def test_another_page_after_claiming_is_not_success_and_sends_nothing_more(self):
        result, rt, taps = self.run_flow("enabled", after=("settings", "settings", "settings", "settings"))
        self.assertFalse(result)
        self.assertNotIn("邮箱附件已全部领取。", rt.logs)
        rt.leave_page.assert_not_called()  # no way back from an unexpected screen

    def test_an_unclear_button_stops_without_a_claim_or_navigation(self):
        result, rt, taps = self.run_flow("odd", after=())
        self.assertFalse(result)
        self.assertEqual(taps, [mailbox.MENU_MAIL])
        rt.leave_page.assert_not_called()

    def test_a_fatal_device_error_sends_nothing_more(self):
        from host import DeviceUnreachable
        with patch.object(mailbox, "claim", side_effect=DeviceUnreachable("ADB 没有回应")):
            with self.assertRaises(DeviceUnreachable):
                self.run_flow("disabled", after=())

    def test_the_way_to_the_hud_at_the_start_is_guarded_too(self):
        rt = SimpleNamespace(stopped=False, _frame_still_valid=Mock(return_value=True))
        shop = (page("商城"), np.zeros((720, 1280, 3), np.uint8), lambda *a: MISS)
        hud = (Screen("hud", {"duck": [30, 18, 54, 54]}), np.zeros((720, 1280, 3), np.uint8), lambda *a: MISS)
        frames = iter([shop, hud])
        with patch.object(pages, "screen_now", side_effect=lambda rt_: next(frames)), \
                patch.object(pages, "tap_on_page", return_value=True) as tap:
            self.assertTrue(mailbox.to_hud(rt))
        tap.assert_called_once_with(rt, "商城", actions.BACK_ARROW)  # the guarded page tap, not leave_page
        modal = (Screen("modal", {"close": [929, 177, 36, 36]}, title="兑换奖励"), hud[1], lambda *a: MISS)
        with patch.object(pages, "screen_now", return_value=modal), patch.object(pages, "tap_on_page") as tap:
            self.assertFalse(mailbox.to_hud(rt))
        tap.assert_not_called()
        # 删除已读 beside the arrow: the guarded tap refuses, at the start as at the end.
        with patch.object(pages, "screen_now", return_value=shop), \
                patch.object(pages, "tap_on_page", side_effect=pages.GuardRefusal("要点的地方旁边有「删除已读」")):
            with self.assertRaises(pages.GuardRefusal):
                mailbox.to_hud(rt)

    def test_starting_on_the_mail_page_leaves_it_through_its_back_arrow(self):
        # One live mail page read 「邮件 7/300 i」, 「邮件7/300 i」 and 「邮件7/300i」 on consecutive frames; the first
        # read used to be the title the next frame had to start with, so the task failed before any tap and the
        # failure message quoted that read.
        arrow = {actions.BACK_ARROW: hit([37, 22, 40, 40])}
        hud = Screen("hud", {"duck": [30, 18, 54, 54]})
        self.rt = Rt([(page("邮件 7/300 i"), arrow), (page("邮件7/300 i"), arrow), (page("邮件7/300i"), arrow),
                      (page("美鸭梨"), arrow), (page("美鸭梨"), arrow), (page("美鸭梨"), arrow), (hud, {})])
        with patch.object(pages.actions, "read_screen", side_effect=lambda recognize: self.rt.current[1]):
            self.assertTrue(mailbox.to_hud(self.rt))
        self.assertEqual(self.rt.clicks, [("Click", [37, 22, 40, 40])] * 2)  # the mail page, then the menu
        self.assertEqual((pages.page_name("邮件 7/300 i"), pages.page_name("美鸭梨"), pages.page_name("7/300")),
                         ("邮件", "美鸭梨", ""))

    def test_a_back_arrow_that_cannot_be_confirmed_stops_cleanly_with_the_frame(self):
        frame = np.zeros((720, 1280, 3), np.uint8)
        mail = (page("邮件 7/300 i"), frame, lambda *a: MISS)
        rt = SimpleNamespace(stopped=False, logs=[], key=Mock(), diagnostic=Mock(),
                             _frame_still_valid=Mock(return_value=True))
        rt.log = rt.logs.append
        with patch.object(pages, "screen_now", return_value=mail), \
                patch.object(pages, "tap_on_page", side_effect=pages.PageMismatch("当前画面不是「邮件」页面")) as tap, \
                patch("host.android_user_stop", return_value=False):
            self.assertFalse(mailbox.run(rt))
        tap.assert_called_once_with(rt, "邮件", actions.BACK_ARROW)  # expected by its name, not the OCR'd title
        rt.key.assert_not_called()
        self.assertEqual(rt.logs, ["邮箱领取：没能回到大世界，本项未完成。"])
        stage, reason = rt.diagnostic.call_args.args
        self.assertEqual(stage, "android_mailbox_start")
        self.assertIs(rt.diagnostic.call_args.kwargs["image"], frame)
        self.assertNotIn("7/300", reason)
        # A title that does not start with a page name is not tapped at all.
        odd = (page("i 邮件"), frame, lambda *a: MISS)
        rejected = []
        with patch.object(pages, "screen_now", return_value=odd), patch.object(pages, "tap_on_page") as tap:
            self.assertFalse(pages.to_hud(rt, rejected=lambda f, r: rejected.append((f, r))))
        tap.assert_not_called()
        self.assertEqual(len(rejected), 1)
        self.assertIs(rejected[0][0], frame)

    def test_the_way_back_only_leaves_mail_and_menu_pages(self):
        rt = SimpleNamespace(stopped=False, _frame_still_valid=Mock(return_value=True))
        shop = (page("商城"), np.zeros((720, 1280, 3), np.uint8), lambda *a: MISS)
        with patch.object(pages, "screen_now", return_value=shop), patch.object(pages, "tap_on_page") as tap:
            self.assertFalse(mailbox.back_from_mail(rt))
        tap.assert_not_called()

    def test_a_guard_refusal_ends_all_input(self):
        result, rt, taps = self.run_flow("disabled", after=(), guard=True)
        self.assertFalse(result)
        self.assertEqual(taps, [])
        rt.leave_page.assert_not_called()
        self.assertTrue(any("删除" in text for text in rt.logs))


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
        self.assertEqual((pages.page_name(spaced), pages.page_name(joined)), ("邮件", "邮件"))
        self.assertTrue(pages.same_title(spaced, joined), (spaced, joined))


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
