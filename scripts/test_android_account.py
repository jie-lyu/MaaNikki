"""Android game account (agent/host/android/account.py) and the per-account records:
python scripts/test_android_account.py

The saved device frames show the UID, so none is in the repository (the fixtures are UID-free crops): the UID text
here is constructed OCR output at the place and size the device frames read it (box ~[0,700,90,18]), with made-up
digits. scripts/android/account_frames.py checks the reader on the saved frames locally."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts" / "android"))

import numpy as np  # noqa: E402

from host.android import account, crystal_route, move, pages, redeem  # noqa: E402
from host.android.actions import Screen  # noqa: E402

UID_A = "123456789"  # the documented fake UID (scripts/check_uid_literals.py)
UID_B = UID_A[::-1]  # a second made-up one, derived from it
UID_BOX = [0, 700, 90, 18]  # where the device frames read the UID (move's tests and the saved sequences)
HUD = Screen("hud", {"duck": [30, 18, 54, 54]})
PAGE = Screen("page", {"back": [37, 22, 40, 40]}, title="设置")
LABEL_ROW = (("商城", [638, 62, 38, 22]), ("共鸣", [708, 62, 38, 22]),
             ("奇迹之旅活动大厅奇想日历无限之心", [761, 62, 284, 22]), ("任务", [1059, 62, 38, 22]),
             ("设计图", [1125, 62, 54, 22]), ("换装", [1203, 62, 38, 22]))  # where the saved frames read them
OTHER_HUD = (("登上鸣鸣列车", [110, 235, 120, 25]), ("1049米", [120, 262, 60, 20]), ("心誓5×1", [520, 668, 200, 22]))


class Row:
    def __init__(self, text, box, score=0.99):
        self.text, self.box, self.score = text, list(box), score


class Ocr:
    def __init__(self, rows):
        self.hit, self.box = bool(rows), (rows[0].box if rows else None)
        self.filtered_results, self.best_result = rows, (rows[0] if rows else None)


def ocr(*corner, labels=True):
    """A full-frame OCR result: the HUD's label row (unless not ``labels``), its other text, and ``corner`` rows
    ((text, box[, score]))."""
    rows = [Row(*row) for row in (LABEL_ROW if labels else ()) + OTHER_HUD + tuple(corner)]
    return Ocr(rows)


def recognizer(result):
    def recognize(node, override=None):
        assert node == move.SCREEN_TEXT, node
        return result
    return recognize


def read(*corner, screen=HUD, labels=True):
    return account.read_uid(screen, recognizer(ocr(*corner, labels=labels)))


class ReadTests(unittest.TestCase):
    """One frame: the UID only from the HUD, only in its slot, only in a form seen on the device, nine digits."""

    def test_the_forms_seen_on_the_device(self):
        for text in (f"UID {UID_A}", f"UID-{UID_A}", f"LID{UID_A}", f"D{UID_A}", f"UID{UID_A}"):
            with self.subTest(text=text):
                self.assertEqual(read((text, UID_BOX)), (UID_A, ""))
        # with the ping at its place (46ms, 47m, 45mm on the saved frames)
        self.assertEqual(read((f"UID {UID_A}", UID_BOX), ("46ms", [140, 702, 30, 12])), (UID_A, ""))

    def test_a_cut_short_or_misread_uid_is_refused(self):
        # The saved frames: cut short to 8, 6 or 3 digits at score 0.95-1.0, and a letter among the digits.
        for text in (f"UID {UID_A[:8]}", f"UID{UID_A[:6]}", f"UID {UID_A[:3]}", "UID1234567A9", f"UID {UID_A}0",
                     f"XUID {UID_A}", f"UID {UID_A} 46ms", f"UID {UID_A[:4]} {UID_A[4:]}", f"UID:{UID_A}",
                     f"UID：{UID_A}"):  # (no colon was ever read there)
            with self.subTest(text=text):
                digits, reason = read((text, UID_BOX))
                self.assertIsNone(digits)
                self.assertTrue(reason)
                self.assertNotIn(UID_A[:3], reason)

    def test_anything_else_in_the_corner_refuses(self):
        cases = {
            "two uids": ((f"UID {UID_A}", UID_BOX), (f"UID {UID_B}", [0, 690, 90, 18])),
            "uid outside its slot": ((f"UID {UID_A}", [120, 700, 90, 18]),),
            "uid above its slot": ((f"UID {UID_A}", [0, 668, 90, 18]),),
            "a misread ping": ((f"UID {UID_A}", UID_BOX), ("23Gms", [120, 700, 56, 17])),
            "other text": ((f"UID {UID_A}", UID_BOX), ("领取奖励", [100, 690, 80, 20])),
        }
        for name, corner in cases.items():
            with self.subTest(name):
                digits, reason = read(*corner)
                self.assertIsNone(digits)
                self.assertTrue(reason)

    def test_a_digit_fragment_right_next_to_the_uid_refuses(self):
        # Account review round 2, finding 4: a longer UID split by OCR (its tenth digit a separate fragment right of
        # the UID box, dropped by the walker's noise filters as a single character) gave the nine-digit prefix's key.
        for name, fragment in (("a high-score digit", ("0", [90, 701, 8, 16], 0.99)),
                               ("a low-score digit", ("7", [88, 700, 9, 17], 0.3)),
                               ("two digits", ("01", [92, 701, 14, 16], 0.95)),
                               ("a full-width digit", ("０", [91, 701, 8, 16], 0.99))):
            with self.subTest(name):
                digits, reason = read((f"UID {UID_A}", UID_BOX), fragment)
                self.assertIsNone(digits)
                self.assertIn("紧挨着还读到了数字", reason)
                self.assertNotIn(UID_A[:3], reason)
        # not a continuation: no digit, a digit further right (the ping's side), a digit on another row
        for name, fragment in (("an icon glyph", ("口", [90, 701, 8, 16], 0.4)),
                               ("a digit 22 px right", ("7", [112, 702, 8, 12], 0.5)),
                               ("a digit above the row", ("7", [92, 686, 8, 10], 0.5))):
            with self.subTest(name):
                self.assertEqual(read((f"UID {UID_A}", UID_BOX), fragment), (UID_A, ""))

    def test_no_uid_read(self):
        self.assertEqual(read(), (None, "左下角没有读到 UID"))
        # below the walker's score floor for ASCII text: not counted, as in move.read_rows
        self.assertEqual(read((f"UID {UID_A}", UID_BOX, 0.7)), (None, "左下角没有读到 UID"))
        self.assertEqual(read((f"UID {UID_A}", UID_BOX, 0.8)), (UID_A, ""))

    def test_only_on_a_positively_recognised_hud(self):
        corner = (f"UID {UID_A}", UID_BOX)
        self.assertEqual(read(corner, screen=PAGE), (None, "画面不是大世界"))
        self.assertEqual(read(corner, screen=Screen("unknown", reason="x")), (None, "画面不是大世界"))
        self.assertEqual(read(corner, labels=False)[0], None)  # the OCR pass itself must read the label row
        self.assertEqual(account.read_uid(HUD, recognizer(None)), (None, "全画面文字识别没能完成"))

    def test_the_uid_box_is_the_footers_and_lies_under_the_diagnostic_mask(self):
        # Geometry from the saved device frames (full reads x0 0-1, y0 698-701, height 15-19, right edge 86-89), the
        # area diagnostics black out (resource/data/diagnostic_images.json) and move's UID region.
        region = dict(move.HUD_REGIONS)["uid"]
        mask = json.loads((ROOT / "resource/data/diagnostic_images.json").read_text(encoding="utf-8"))["uid_roi"]
        for box in (UID_BOX, [1, 701, 85, 15], [0, 698, 88, 19]):
            self.assertTrue(account.in_footer(box) and move._inside(box, region), box)
        mx, my, mw, mh = mask
        self.assertTrue(mx <= account.UID_BOX_X0[0] and account.UID_BOX_X1[1] <= mx + mw
                        and my <= account.UID_BOX_Y0[0] and my + mh == 720)  # down to the frame's bottom
        # account review round 1, finding 3: a D-prefixed number starting at x 91 (one pixel into the old slot),
        # a box shifted, too tall or too short is no UID read
        for box in ([91, 700, 90, 18], [10, 700, 85, 18], [0, 680, 88, 18], [0, 700, 88, 30], [0, 700, 60, 18]):
            with self.subTest(box=box):
                self.assertFalse(account.in_footer(box))
                self.assertIsNone(read((f"D{UID_A}", box))[0])
        x, y, w, h = UID_BOX
        frame = np.full((720, 1280, 3), 200, np.uint8)
        from diagnostics import diagnostic_image
        masked, _box = diagnostic_image(frame)
        self.assertEqual(int(masked[y:y + h, x:x + w].max()), 0)

    def test_only_labels_read_as_themselves_count(self):
        # Account review round 1, finding 3: a misread block covering the label row counts no label here (the
        # walker's labels count includes covered places; the account reader needs MIN_LABELS read positively).
        misread = (("商城共鸣奇迹之旅活动人厅奇想日历无限之心任务设计图换装", [630, 62, 620, 22]),)
        rows = [Row(*row) for row in misread + OTHER_HUD + ((f"UID {UID_A}", UID_BOX),)]
        self.assertGreaterEqual(move.read_rows(Ocr(rows)).labels, move.MIN_LABELS)
        self.assertEqual(move.read_rows(Ocr(rows)).read_labels, frozenset())
        self.assertEqual(account.read_uid(HUD, recognizer(Ocr(rows))),
                         (None, "全画面文字里没有在原位读到大世界的快捷栏"))
        self.assertEqual(len(move.read_rows(ocr()).read_labels), len(move.LABEL_CENTRES))


def rt_with(frames, *, valid=True, pause=True):
    """A runtime whose captures (pages.screen_now) are ``frames``: [(screen, corner rows) or None (stopped)];
    ``valid``: every focus check's answer, or the answers in turn (a list)."""
    checks = Mock(side_effect=list(valid)) if isinstance(valid, list) else Mock(return_value=valid)
    rt = SimpleNamespace(stopped=False, logs=[], pause=Mock(return_value=pause), _frame_still_valid=checks)
    rt.log = rt.logs.append
    shots = []
    for item in frames:
        if item is None:
            shots.append((None, None, None))
            continue
        screen, corner = item
        shots.append((screen, np.zeros((720, 1280, 3), np.uint8), recognizer(ocr(*corner))))
    return rt, shots


def identify(frames, **kwargs):
    rt, shots = rt_with(frames, **kwargs)
    with patch.object(pages, "screen_now", side_effect=shots) as capture:
        try:
            return account.identify(rt), None, capture.call_count, rt
        except account.Unidentified as error:
            return None, error, capture.call_count, rt


A = (HUD, ((f"UID {UID_A}", UID_BOX),))
B = (HUD, ((f"UID {UID_B}", UID_BOX),))


class IdentifyTests(unittest.TestCase):
    """Two separate frames that agree; anything else refuses; the UID itself is never given out."""

    def test_two_agreeing_frames_give_the_key(self):
        binding, error, captures, rt = identify([A, A])
        self.assertIsNone(error)
        self.assertEqual((binding.key, captures), (account.account_key(UID_A), 2))
        rt.pause.assert_called_once_with(account.READ_GAP_S)  # the two captures are apart, nothing sent between

    def test_disagreeing_frames_refuse(self):
        key, error, _captures, _rt = identify([A, B])
        self.assertIsNone(key)
        self.assertEqual(str(error), "两张截图上读到的 UID 不一样")
        self.assertEqual(len(error.frames), 2)

    def test_an_unreadable_frame_refuses_without_a_second_look(self):
        for first in ((HUD, ()), (PAGE, ((f"UID {UID_A}", UID_BOX),)), (HUD, ((f"UID {UID_A[:8]}", UID_BOX),))):
            with self.subTest(first=first[0].kind):
                key, error, captures, _rt = identify([first, A])
                self.assertIsNone(key)
                self.assertIsInstance(error, account.Unidentified)
                self.assertEqual((captures, len(error.frames)), (1, 1))
        key, error, captures, _rt = identify([A, (HUD, ())])
        self.assertEqual((key, captures, len(error.frames)), (None, 2, 1))

    def test_a_stale_or_repeated_frame_refuses(self):
        key, error, _captures, _rt = identify([A, A], valid=False)
        self.assertEqual((key, str(error)), (None, "读游戏账号时焦点变了"))
        # account review round 1, finding 2: a focus change during the second OCR voids both frames
        key, error, _captures, _rt = identify([A, A], valid=[True, True, True, False])
        self.assertEqual((key, str(error), len(error.frames)), (None, "读游戏账号时焦点变了", 2))
        rt, shots = rt_with([A])
        with patch.object(pages, "screen_now", side_effect=[shots[0], shots[0]]):
            with self.assertRaises(account.Unidentified) as caught:
                account.identify(rt)
        self.assertIn("不是分开截的", str(caught.exception))

    def test_two_split_reads_of_a_longer_uid_refuse(self):
        # Account review round 2, finding 4, as reproduced: two separate frames, each the UID in its box and a
        # high-confidence tenth digit right of it.
        split = (HUD, ((f"UID {UID_A}", UID_BOX), ("0", [90, 701, 8, 16], 0.99)))
        key, error, captures, _rt = identify([split, split])
        self.assertIsNone(key)
        self.assertIsInstance(error, account.Unidentified)
        self.assertEqual(captures, 1)

    def test_a_stop_gives_nothing(self):
        self.assertEqual(identify([None])[:2], (None, None))
        self.assertEqual(identify([A, None])[:2], (None, None))
        key, error, captures, _rt = identify([A, A], pause=False)
        self.assertEqual((key, error, captures), (None, None, 1))

    def test_the_uid_is_never_in_a_reason_or_a_line(self):
        for frames in ([A, B], [(HUD, ((f"UID {UID_A[:8]}", UID_BOX),))], [(HUD, ((f"UID {UID_A}", UID_BOX),
                                                                                    ("23Gms", [120, 700, 56, 17])))]):
            _key, error, _captures, rt = identify(frames)
            for text in (str(error), account.refusal(error), *rt.logs):
                self.assertNotIn(UID_A[:6], text)
                self.assertNotIn(UID_B[:6], text)


class ConfirmTests(unittest.TestCase):
    """One fresh HUD read must equal the key told before; the binding is void after any focus change."""

    def confirm(self, corner, *, valid=True, frame=True):
        rt = SimpleNamespace(_frame_still_valid=Mock(side_effect=valid if callable(valid) else lambda f: valid))
        shot = np.zeros((720, 1280, 3), np.uint8)
        rt._fresh_device_frame = Mock(return_value=shot if frame else None)
        rt._recognizer_on = lambda f: recognizer(ocr(*corner))
        binding = account.Binding(account.account_key(UID_A), "earlier frame")
        with patch("host.android.actions.read_screen", return_value=HUD):
            try:
                return account.confirm(rt, binding), None, shot
            except account.Unidentified as error:
                return None, error, shot

    def test_the_same_account_renews_the_binding_on_the_fresh_frame(self):
        renewed, error, shot = self.confirm(((f"UID {UID_A}", UID_BOX),))
        self.assertIsNone(error)
        self.assertEqual((renewed.key, renewed.frame is shot), (account.account_key(UID_A), True))

    def test_another_account_an_unreadable_uid_or_a_focus_change_refuses(self):
        cases = {"another account": (((f"UID {UID_B}", UID_BOX),), True, "现在登录的不是开始时认出的那个游戏账号"),
                 "unreadable": (((f"UID {UID_A[:8]}", UID_BOX),), True, "左下角 UID 的位置读到了不完整或别的文字"),
                 "focus change": (((f"UID {UID_A}", UID_BOX),), False, "确认游戏账号时焦点变了")}
        for name, (corner, valid, reason) in cases.items():
            with self.subTest(name):
                renewed, error, _shot = self.confirm(corner, valid=valid)
                self.assertIsNone(renewed)
                self.assertEqual(str(error), reason)
                self.assertNotIn(UID_B[:5], account.refusal(error))
        self.assertEqual(self.confirm(((f"UID {UID_A}", UID_BOX),), frame=False)[:2], (None, None))  # stopped

    def test_a_binding_is_void_after_a_focus_change(self):
        frame = object()
        binding = account.Binding("0" * 16, frame)
        self.assertTrue(binding.valid(SimpleNamespace(_frame_still_valid=lambda f: f is frame)))
        self.assertFalse(binding.valid(SimpleNamespace(_frame_still_valid=lambda f: False)))


class PatternTests(unittest.TestCase):
    def test_the_walkers_uid_slot_shares_the_account_pattern(self):
        # One prefix for both (no drift); the walker also lets a cut-short UID through (it judges the layout only),
        # the account reader never does.
        self.assertTrue(move.UID_FORM.pattern.startswith(account.UID_PREFIX))
        for text in (f"UID {UID_A}", f"UID-{UID_A}", f"LID{UID_A}", f"D{UID_A}", f"UID{UID_A}"):
            self.assertTrue(move.UID_FORM.fullmatch(text) and account._uid_text(text) == UID_A, text)
        self.assertTrue(move.UID_FORM.fullmatch(f"UID {UID_A[:8]}"))
        self.assertIsNone(account._uid_text(f"UID {UID_A[:8]}"))
        for text in ("UID1234567A9", f"UID:{UID_A}", f"XUID{UID_A}", "46ms"):  # a ping is judged by its place
            self.assertFalse(move.UID_FORM.fullmatch(text) or account._uid_text(text), text)


    def test_the_uid_slot_is_left_anchored_for_full_and_cut_short_reads(self):
        # Account review round 2, finding 7: the walker's UID slot is the account reader's, without its right-edge
        # floor (a cut-short read ends earlier).
        for box in (UID_BOX, [0, 700, 40, 18], [2, 702, 60, 14]):
            self.assertTrue(account.in_slot(box), box)
        self.assertFalse(account.in_footer([0, 700, 40, 18]))
        for box in ([185, 702, 30, 14], [10, 700, 80, 18], [0, 680, 88, 18], [0, 700, 100, 18]):
            self.assertFalse(account.in_slot(box), box)


class KeyTests(unittest.TestCase):
    def test_the_key_is_a_salted_sha256_prefix(self):
        import hashlib
        key = account.account_key(UID_A)
        self.assertRegex(key, r"^[0-9a-f]{16}$")
        self.assertEqual(key, hashlib.sha256(b"MaaNikki/android-account/v1\x00" + UID_A.encode()).hexdigest()[:16])
        self.assertNotEqual(key, hashlib.sha256(UID_A.encode()).hexdigest()[:16])
        self.assertNotEqual(key, account.account_key(UID_B))
        self.assertNotIn(UID_A, key)
        for bad in ("", "12a", None, "１２３"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                account.account_key(bad)

    def test_record_paths_are_one_folder_per_account(self):
        root = Path(tempfile.mkdtemp())
        key = account.account_key(UID_A)
        self.assertEqual(account.record_path(root, key, "x.json"), root / "config/android/accounts" / key / "x.json")
        for bad in ("../../etc", "", None, key.upper(), key + "0", UID_A):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                account.record_path(root, bad, "x.json")


KEY_A, KEY_B = account.account_key(UID_A), account.account_key(UID_B)


class RecordTests(unittest.TestCase):
    """Each account its own records; the un-keyed records of before are never read or attributed."""

    def test_redeem_records_are_separate_per_account(self):
        root = Path(tempfile.mkdtemp())
        redeem.Record(root, KEY_A).record("CODE1", "redeemed")
        self.assertEqual(redeem.Record(root, KEY_A).outcomes(), {"CODE1": "redeemed"})
        self.assertEqual(redeem.Record(root, KEY_B).outcomes(), {})
        self.assertEqual(redeem.pending_codes([("CODE1", False)], redeem.Record(root, KEY_B).outcomes()), ["CODE1"])
        data = json.loads(redeem.Record(root, KEY_A).path.read_text(encoding="utf-8"))
        self.assertEqual((data["scope"], data["account"]), ("android_game_account", KEY_A))
        self.assertNotIn(UID_A, json.dumps(data))

    def test_bell_records_are_separate_per_account(self):
        import datetime as dt
        day = dt.date(2026, 11, 26)
        root = Path(tempfile.mkdtemp())
        crystal_route.BellRecord(root, KEY_A).mark(day, "pressed")
        self.assertEqual(crystal_route.BellRecord(root, KEY_A).attempt(day)["state"], "pressed")
        self.assertIsNone(crystal_route.BellRecord(root, KEY_B).attempt(day))
        with self.assertRaises(crystal_route.RecordError):
            crystal_route.BellRecord(root, None)

    def test_the_old_unkeyed_records_are_left_unread(self):
        root = Path(tempfile.mkdtemp())
        old_codes = root.joinpath(*redeem.LEGACY_RECORD_FILE)
        old_bell = root.joinpath(*crystal_route.LEGACY_RECORD_FILE)
        old_codes.parent.mkdir(parents=True)
        old_codes.write_text(json.dumps({"schema_version": 1, "scope": "android_data_directory", "items": {
            "CODE1": {"outcome": "redeemed", "confirmed_at": "2026-11-26T00:00:00+00:00"}}}), encoding="utf-8")
        old_bell.write_text(json.dumps({"schema_version": 1, "scope": "android_data_directory", "attempts": {
            "2026-11-26": {"state": "pressed", "at": "2026-11-26T00:00:00+00:00"}}}), encoding="utf-8")
        before = (old_codes.read_bytes(), old_bell.read_bytes())
        import datetime as dt
        self.assertEqual(redeem.Record(root, KEY_A).outcomes(), {})
        self.assertIsNone(crystal_route.BellRecord(root, KEY_A).attempt(dt.date(2026, 11, 26)))
        redeem.Record(root, KEY_A).record("CODE2", "already_used")
        crystal_route.BellRecord(root, KEY_A).mark(dt.date(2026, 11, 27), "pressed")
        self.assertEqual((old_codes.read_bytes(), old_bell.read_bytes()), before)  # left in place, untouched

    def test_a_record_from_another_accounts_folder_is_refused(self):
        root = Path(tempfile.mkdtemp())
        redeem.Record(root, KEY_A).record("CODE1", "redeemed")
        crystal_route.BellRecord(root, KEY_A).mark(__import__("datetime").date(2026, 11, 26), "pressed")
        for make in (lambda key: redeem.Record(root, key), lambda key: crystal_route.BellRecord(root, key)):
            source, copy = make(KEY_A).path, make(KEY_B).path
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(source.read_bytes())
            with self.assertRaises((ValueError, crystal_route.RecordError)):
                make(KEY_B).load()


class ScrubTests(unittest.TestCase):
    """What Android writes or shows keeps no UID in its text (the image is masked by diagnostics); the desktop is
    unchanged."""

    def test_uid_like_text_numbers_and_keys_are_masked_and_other_numbers_kept(self):
        for text in (f"UID {UID_B}", f"LID{UID_B}", f"UID-{UID_B[:5]}", f"读到「{UID_B}」", f"UID：{UID_B}",
                     f"page {UID_B} title", "UID " + "".join(chr(0xFF10 + int(d)) for d in UID_B),
                     # account review round 2, finding 5: a colon, a period or a slash next to the bare number
                     f"账号：{UID_B}", f"账号:{UID_B}", f"读到 {UID_B}.", f"{UID_B}。", f"a/{UID_B}/b", f"{UID_B}:",
                     f"x.{UID_B}", "账号：" + "".join(chr(0xFF10 + int(d)) for d in UID_B)):
            with self.subTest(text=len(text)):
                self.assertNotIn(UID_B[:5], account.scrub(text))
                self.assertIn(account.UID_MASK, account.scrub(text))
        for text in ("46ms", "1049米", "X10000", "本周限购0/1", "-25%", "2026-11-26T00:00:00", "¥648",
                     "on_error/20261126-031003-668252-action_exception-f22705bb.jpg", "ab12345678cd"):
            with self.subTest(text=text):
                self.assertEqual(account.scrub(text), text)
        nested = {"rows": [(f"UID {UID_B}", [0, 700, 90, 18])], "score": 0.9, "n": 123456789012, "uid": int(UID_B),
                  UID_B: True, "ok": True}
        self.assertEqual(account.scrub(nested), {"rows": [(account.UID_MASK, [0, 700, 90, 18])], "score": 0.9,
                                                 "n": 123456789012, "uid": account.UID_MASK, account.UID_MASK: True,
                                                 "ok": True})

    def boundaries(self, backend, uid_text=f"UID {UID_B}"):
        """A failure scene saved the way registration saves it (FailureEvidence.save), one through
        Runtime.diagnostic, a message line, and a Runtime.log whose message file cannot be written (the display
        fallback): every text written or shown, on ``backend``, each holding ``uid_text``."""
        import diagnostics
        import message_log
        from daily import runtime
        root = Path(tempfile.mkdtemp())
        shown = []
        evidence = diagnostics.FailureEvidence()
        evidence.observe("recognition", node=move.SCREEN_TEXT, results=[{"text": uid_text, "score": 0.99}])
        rt = runtime.Runtime.__new__(runtime.Runtime)
        rt._evidence, rt._focus_generation = evidence, 0
        rt.context = SimpleNamespace(run_task=lambda node, override: shown.append(json.dumps(override,
                                                                                            ensure_ascii=False)))
        frame = np.full((720, 1280, 3), 120, np.uint8)
        with patch("host.game_backend", return_value=backend), \
                patch.object(runtime, "active_session", return_value=None), \
                patch("host.android_user_stop", return_value=False), \
                patch.object(diagnostics, "DATA_ROOT", root), \
                patch.object(message_log, "_DIRECTORY", root / "debug/messages"):
            evidence.save("action_exception", f"异常：页面标题「{uid_text}」", details={"row": uid_text})
            rt.diagnostic("android_test_stop_0", f"守卫拦下了一行「{uid_text}」", image=frame,
                          details={"digits": UID_B})
            message_log.write_message(f"详细：{uid_text}", uid_text, report=uid_text)
            with patch.object(runtime, "write_message", return_value=False), \
                    patch.object(runtime.Runtime, "stopped", False, create=True):
                rt.log(f"读到 {uid_text}")
        written = [path.read_text(encoding="utf-8") for path in root.rglob("*") if path.suffix in (".json", ".log")]
        return written, shown

    def test_android_writes_and_shows_no_uid(self):
        written, shown = self.boundaries("android")
        self.assertEqual(len([text for text in written if text.lstrip().startswith("{\n")]), 2)  # two scenes
        self.assertTrue(written and shown)
        for text in (*written, *shown):
            self.assertNotIn(UID_B[:5], text)
        self.assertTrue(all(account.UID_MASK in text for text in written))

    def test_the_desktop_writes_what_it_is_given(self):
        written, shown = self.boundaries("win32")
        self.assertTrue(all(UID_B in text for text in written) and all(UID_B in text for text in shown))

    def test_a_number_after_a_colon_is_masked_at_every_android_boundary(self):
        # Account review round 2, finding 5: 「账号：」 + the number survived the message log, the failure JSON and
        # the failed-log UI fallback; the desktop keeps it.
        for uid_text in (f"账号：{UID_B}", f"账号:{UID_B}.", f"路径 a/{UID_B}/b"):
            with self.subTest(text=len(uid_text)):
                written, shown = self.boundaries("android", uid_text)
                self.assertTrue(written and shown)
                for text in (*written, *shown):
                    self.assertNotIn(UID_B[:5], text)
                written, shown = self.boundaries("win32", uid_text)
                self.assertTrue(all(UID_B in text for text in written) and all(UID_B in text for text in shown))

    def test_time_stamps_and_file_names_keep_their_structured_fields(self):
        # The message log's "timestamp" and the failure scene's "time" and image name are not scrubbed.
        written, _shown = self.boundaries("android", f"账号：{UID_B}")
        scenes = [json.loads(text) for text in written if text.lstrip().startswith("{\n")]
        lines = [json.loads(line) for text in written if not text.lstrip().startswith("{\n")
                 for line in text.splitlines() if line.strip()]
        self.assertTrue(scenes and lines)
        for scene in scenes:
            self.assertRegex(scene["time"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}")
        for line in lines:
            self.assertRegex(line["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}")
        for kept in ("2026-11-27T10:15:30.123+08:00", "on_error/20261127-101530-123456-action_exception-ab12cd34.jpg",
                     "20261127_101530.png", "v5.16.1", "12:30:45", "1/12345678-x"):
            with self.subTest(text=kept):
                self.assertEqual(account.scrub(kept), kept)


class ExceptionOutputTests(unittest.TestCase):
    def test_registration_tracebacks_are_masked_on_android_only(self):
        import contextlib
        import io
        import registration
        for backend, kept in (("android", False), ("win32", True)):
            with self.subTest(backend=backend):
                out = io.StringIO()
                with patch("host.game_backend", return_value=backend), \
                        patch.object(registration, "game_backend", return_value=backend), \
                        patch.object(registration.traceback, "print_exc",
                                     side_effect=lambda: out.write(registration.traceback.format_exc())), \
                        contextlib.redirect_stderr(out):
                    try:
                        raise RuntimeError(f"页面标题「UID {UID_B}」")
                    except RuntimeError:
                        registration._print_exc()
                self.assertEqual(UID_B in out.getvalue(), kept)
                self.assertIn("RuntimeError", out.getvalue())


    def test_the_exception_line_shown_in_the_ui_is_masked_on_android_only(self):
        # Account review round 2, finding 5: stderr was masked, the message_override sent to the UI kept the digits.
        from contextlib import nullcontext
        import contextlib
        import io
        import registration
        token = registration._LOCAL_REGISTRATION.set(True)
        try:
            @registration.agent_action("test.android_account.exception_override")
            class Probe:
                def run(self, context, argv):
                    raise RuntimeError(f"页面标题「账号：{UID_B}」")
        finally:
            registration._LOCAL_REGISTRATION.reset(token)
        self.addCleanup(registration._ACTION_TYPES.pop, "test.android_account.exception_override", None)
        for backend, kept in (("android", False), ("win32", True)):
            with self.subTest(backend=backend):
                shown = []
                context = SimpleNamespace(run_task=lambda entry, override: shown.append(
                    override["MaaNikki_Daily_Message"]["focus"]["Node.Action.Starting"]))
                with patch("host.game_backend", return_value=backend), \
                        patch.object(registration, "game_backend", return_value=backend), \
                        patch.object(registration, "android_user_stop", return_value=False), \
                        patch.object(registration, "task_session", return_value=nullcontext(None)), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertFalse(Probe().run(context, SimpleNamespace(node_name="probe")))
                self.assertEqual(len(shown), 1)
                self.assertEqual(UID_B in shown[0], kept)
                if kept:  # the desktop line, byte for byte as before
                    self.assertEqual(shown[0], f"probe 未完成：页面标题「账号：{UID_B}」。")
                else:
                    self.assertIn(account.UID_MASK, shown[0])


class UidShapedTests(unittest.TestCase):
    def test_uid_shaped_candidate_texts(self):
        # Account review round 2, finding 6: a UID pasted as a redeem code (the manual parser accepts UID + digits).
        wide = "".join(chr(0xFF10 + int(d)) for d in UID_A)
        for text in (f"UID{UID_A}", f"uid{UID_A}", f"LID{UID_A}", UID_A, f"D{UID_A}", f"UID{UID_A[:4]}", wide,
                     f"码{UID_A}", f"{UID_A}.", f"UID：{UID_A}"):
            with self.subTest(text=len(text)):
                self.assertTrue(account.uid_shaped(text))
        for text in ("ABC123", "NIKKI2025", "拾光二载暖赴新章", "GOLD2025", "NIKKI20251205", "AB12345678CD", "", None):
            with self.subTest(text=text):
                self.assertFalse(account.uid_shaped(text))


class FramesToolTests(unittest.TestCase):
    def test_the_summary_names_accounts_without_their_uid_or_key(self):
        from account_frames import summarise, unmasked
        summary = summarise([None, (UID_A, ""), (UID_A, ""), (UID_B, ""), (None, "左下角没有读到 UID")])
        self.assertEqual(summary, {"hud": 4, "read": 3, "refused": {"左下角没有读到 UID": 1},
                                   "accounts": {"account 1": 2, "account 2": 1}})
        text = json.dumps(summary, ensure_ascii=False)
        for secret in (UID_A, UID_B, KEY_A, KEY_B):
            self.assertNotIn(secret, text)
        frame = np.full((720, 1280, 3), 90, np.uint8)
        self.assertTrue(unmasked(frame))
        frame[678:720, 0:210] = 0
        self.assertFalse(unmasked(frame))
        self.assertFalse(unmasked(None))


if __name__ == "__main__":
    unittest.main()
