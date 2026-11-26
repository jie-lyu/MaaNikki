"""Android game account (agent/host/android/account.py) and the per-account records: python scripts/test_android_account.py

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

UID_A, UID_B = "123456789", "987654321"  # made up
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

    def test_the_slot_is_the_walkers_and_lies_under_the_diagnostic_mask(self):
        # Geometry from the saved device frames: the UID's box (~[0,700,90,18]; text rows y 703-714), move's UID
        # region and slot, and the area diagnostics black out (resource/data/diagnostic_images.json).
        region = dict(move.HUD_REGIONS)["uid"]
        slot = next(s for s in move.HUD_SLOTS if s[0] == "uid")
        mask = json.loads((ROOT / "resource/data/diagnostic_images.json").read_text(encoding="utf-8"))["uid_roi"]
        x, y, w, h = UID_BOX
        self.assertTrue(move._inside(UID_BOX, region))
        self.assertTrue(x < slot[2] and slot[1] < x + w and abs(y + h / 2 - slot[3]) <= move.BLOCK_PX)
        mx, my, mw, mh = mask
        for box in (UID_BOX, [slot[1], int(slot[3] - h / 2), slot[2] - slot[1], h]):
            self.assertTrue(mx <= box[0] and box[0] + box[2] <= mx + mw and my <= box[1]
                            and box[1] + box[3] <= my + mh, box)
        # a masked frame (as every saved diagnostic frame): nothing is read there, so no account
        frame = np.full((720, 1280, 3), 200, np.uint8)
        from diagnostics import diagnostic_image
        masked, _box = diagnostic_image(frame)
        self.assertEqual(int(masked[y:y + h, x:x + w].max()), 0)


def rt_with(frames, *, valid=True, pause=True):
    """A runtime whose captures (pages.screen_now) are ``frames``: [(screen, corner rows) or None (stopped)]."""
    rt = SimpleNamespace(stopped=False, logs=[], pause=Mock(return_value=pause),
                         _frame_still_valid=Mock(return_value=valid))
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
        key, error, captures, rt = identify([A, A])
        self.assertIsNone(error)
        self.assertEqual((key, captures), (account.account_key(UID_A), 2))
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
        rt, shots = rt_with([A])
        with patch.object(pages, "screen_now", side_effect=[shots[0], shots[0]]):
            with self.assertRaises(account.Unidentified) as caught:
                account.identify(rt)
        self.assertIn("不是分开截的", str(caught.exception))

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


class PatternTests(unittest.TestCase):
    def test_the_walkers_uid_slot_shares_the_account_pattern(self):
        # One prefix for both (no drift); the walker also lets a cut-short UID through (it judges the layout only),
        # the account reader never does.
        self.assertTrue(move.UID_FORM.pattern.startswith(account.UID_PREFIX))
        for text in (f"UID {UID_A}", f"UID-{UID_A}", f"LID{UID_A}", f"D{UID_A}", f"UID{UID_A}"):
            self.assertTrue(move.UID_FORM.fullmatch(text) and account._uid_text(text) == UID_A, text)
        self.assertTrue(move.UID_FORM.fullmatch(f"UID {UID_A[:8]}"))
        self.assertIsNone(account._uid_text(f"UID {UID_A[:8]}"))
        for text in ("UID1234567A9", f"UID:{UID_A}", f"XUID{UID_A}"):
            self.assertFalse(move.UID_FORM.fullmatch(text) or account._uid_text(text), text)


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
    """Failure scenes on Android keep no UID in their text (the image is masked by diagnostics)."""

    def test_uid_like_text_is_masked_and_other_numbers_kept(self):
        for text in (f"UID {UID_B}", f"LID{UID_B}", f"UID-{UID_B[:5]}", f"读到「{UID_B}」", f"x{UID_B}0"):
            with self.subTest(text=text):
                self.assertNotIn(UID_B[:5], account.scrub(text))
                self.assertIn(account.UID_MASK, account.scrub(text))
        for text in ("46ms", "1049米", "X10000", "本周限购0/1", "-25%", "2026-11-26T00:00:00", "¥648"):
            with self.subTest(text=text):
                self.assertEqual(account.scrub(text), text)
        nested = {"rows": [(f"UID {UID_B}", [0, 700, 90, 18])], "score": 0.9, "n": 123456789012}
        self.assertEqual(account.scrub(nested), {"rows": [(account.UID_MASK, [0, 700, 90, 18])], "score": 0.9,
                                                 "n": 123456789012})

    def test_an_android_failure_scene_keeps_no_uid(self):
        import diagnostics
        from daily import runtime
        root = Path(tempfile.mkdtemp())
        messages = []
        rt = runtime.Runtime.__new__(runtime.Runtime)
        rt._evidence, rt._focus_generation = diagnostics.FailureEvidence(), 0
        rt._evidence.observe("recognition", node=move.SCREEN_TEXT, results=[{"text": f"UID {UID_B}", "score": 0.99,
                                                                              "box": [0, 700, 90, 18]}])
        frame = np.full((720, 1280, 3), 120, np.uint8)
        with patch.object(runtime, "active_session", return_value=None), \
                patch("host.android_user_stop", return_value=False), \
                patch.object(runtime, "game_backend", return_value="android"), \
                patch.object(diagnostics, "DATA_ROOT", root), \
                patch.object(diagnostics, "write_message", side_effect=lambda text, *a: messages.append(text)):
            reference = rt.diagnostic("android_test_stop_0", f"守卫拦下了一行「UID {UID_B}」",
                                      image=frame, details={"row": f"UID {UID_B}", "digits": UID_B})
        self.assertEqual(reference["image_status"], "saved")
        written = [path.read_text(encoding="utf-8") for path in (root / "debug/on_error").glob("*.json")]
        self.assertEqual(len(written), 1)
        for text in (*written, *messages):
            self.assertNotIn(UID_B[:5], text)
            self.assertIn(account.UID_MASK, text)
        # the desktop path is untouched: what it is given is what it saves
        rt._evidence = Mock()
        with patch.object(runtime, "active_session", return_value=None), \
                patch("host.android_user_stop", return_value=False), \
                patch.object(runtime, "game_backend", return_value="win32"):
            rt.diagnostic("stage", f"UID {UID_B}", details={"x": UID_B})
        self.assertEqual(rt._evidence.save.call_args.args[1], f"UID {UID_B}")
        self.assertEqual(rt._evidence.save.call_args.kwargs["details"], {"x": UID_B})


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
