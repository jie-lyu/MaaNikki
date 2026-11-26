"""No game UID written in the repository (scripts/check_uid_literals.py): python scripts/test_uid_literals.py"""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_uid_literals as guard  # noqa: E402

MADE_UP = "555555555"  # made up; stands in for a real UID by its hash in these tests


class RuleTests(unittest.TestCase):
    def test_the_hud_form_with_any_number_but_the_fake_one(self):
        self.assertEqual(guard.FAKE_UID, "123456789")
        for line in ("UID 123456789", "LID123456789", "UID-123456789", "UID: 123456789", '("UID 123456789", box)'):
            with self.subTest(line=line):
                self.assertEqual(guard.scan_text(line), [])
        other = "9876" + "54321"  # made up, built here so that this file itself holds no UID form
        for line in (f"UID {other}", f"UID{other[:8]}", f"LID-{other}0", f"UID：{other}", f"x = 'UID {other}'",
                     f"UID 123456789 and UID {other}"):
            with self.subTest(line=line):
                self.assertEqual(guard.scan_text(line), [(1, "UID form")])
        # a shape, a cut-short read or a longer number is no UID form
        for line in (r"(?:UID|LID|D)[\s\-]?\d{3,9}", "UID 1234567", "UID 98765432101", "UID1234567A9"):
            with self.subTest(line=line):
                self.assertEqual(guard.scan_text(line), [])

    def test_a_known_uid_anywhere_by_its_salted_hash(self):
        known = frozenset({guard.digest(MADE_UP)})
        for line in (MADE_UP, f"id={MADE_UP};", f"commit message mentions {MADE_UP} once", f"D{MADE_UP}"):
            with self.subTest(line=line):
                self.assertEqual(guard.scan_text(line, known), [(1, "known UID")])
        for line in (MADE_UP + "1", "1" + MADE_UP, "992617511", "555555556"):  # other numbers are no UID
            with self.subTest(line=line):
                self.assertEqual(guard.scan_text(line, known), [])
        self.assertEqual(guard.scan_text(f"a\nb\n{MADE_UP}\n", known), [(3, "known UID")])

    def test_the_known_list_holds_salted_hashes_only(self):
        self.assertTrue(guard.KNOWN_REAL)
        for entry in guard.KNOWN_REAL:
            self.assertRegex(entry, r"^[0-9a-f]{64}$")
        self.assertNotEqual(guard.digest(MADE_UP), __import__("hashlib").sha256(MADE_UP.encode()).hexdigest())
        source = (ROOT / "scripts/check_uid_literals.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(?<![0-9a-f])\d{8,10}(?![0-9a-f])", source.replace(guard.FAKE_UID, "")))

    def test_findings_never_print_the_digits(self):
        import contextlib
        import io
        from unittest.mock import patch
        out = io.StringIO()
        with patch.object(guard, "scan_tree", return_value=[("a.py", 3, "UID form")]), contextlib.redirect_stdout(out):
            self.assertEqual(guard.main([]), 1)
        self.assertEqual(out.getvalue(), "a.py:3: UID form (write 123456789 or the UID's shape instead)\n1 finding(s)\n")


@unittest.skipUnless((ROOT / ".git").exists() and shutil.which("git"), "needs a git checkout")
class TreeTests(unittest.TestCase):
    def test_no_tracked_file_holds_a_uid(self):
        self.assertEqual(guard.scan_tree(), [])

    def test_the_latest_commit_message_holds_no_uid(self):
        self.assertEqual(guard.scan_commits("-1"), [])


if __name__ == "__main__":
    unittest.main()
