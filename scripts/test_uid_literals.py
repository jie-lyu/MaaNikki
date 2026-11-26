"""No game UID written in the repository (scripts/check_uid_literals.py): python scripts/test_uid_literals.py

Every assertion is a boolean with a fixed message: a failure never prints what was matched."""
from __future__ import annotations

import codecs
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_uid_literals as guard  # noqa: E402

FAKE = guard.FAKE_UID
OTHER = FAKE[::-1]  # a second made-up number, derived from the fake one


def flagged(text, denylist=frozenset()):
    return bool(guard.scan_text(text, denylist))


def wide(digits):
    return "".join(chr(0xFF10 + int(d)) for d in digits)


class RuleTests(unittest.TestCase):
    def test_the_fake_uid_and_shapes_pass(self):
        self.assertTrue(FAKE == "123456789", "the documented fake UID changed")
        for index, line in enumerate((f"UID {FAKE}", f"LID{FAKE}", f"UID-{FAKE}", f"UID: {FAKE}", f'("UID {FAKE}", 1)',
                                      r"(?:UID|LID|D)[\s\-]?\d{3,9}", f"UID {FAKE[:7]}", f"UID {FAKE}{FAKE[:2]}",
                                      "UID1234567A9")):
            with self.subTest(case=index):
                self.assertFalse(flagged(line), "a fake UID, a shape or a non-UID number was reported")

    def test_the_hud_form_with_any_other_number_in_any_spacing(self):
        cases = (f"UID {OTHER}", f"UID{OTHER[:8]}", f"LID-{OTHER}0", f"UID：{OTHER}", f"UID ： {OTHER}",
                 f"UID：\n{OTHER}", f"UID\t\t {OTHER}", f"x = 'UID {OTHER}'", f"UID {FAKE} and UID {OTHER}",
                 "UID " + wide(OTHER), f"UID　{OTHER}")
        for index, line in enumerate(cases):
            with self.subTest(case=index):
                self.assertTrue(flagged(line), "a UID in the HUD form was not reported")

    def test_the_label_is_a_word_of_its_own(self):
        for index, line in enumerate((f"GUID {OTHER}", f"guid={OTHER}", f"MyUID {OTHER}", f"_UID {OTHER}")):
            with self.subTest(case=index):
                self.assertFalse(flagged(line), "a label ending in UID was taken for the UID label")

    def test_line_numbers_point_at_the_label(self):
        found = guard.scan_text(f"a\nb\nUID：\n{OTHER}\n")
        self.assertTrue(found == [(3, "UID form")], "the finding's line is not the label's")

    def test_denylisted_numbers_anywhere(self):
        denylist = frozenset({OTHER})
        for index, line in enumerate((OTHER, f"id={OTHER};", f"message mentions {OTHER} once", f"D{OTHER}",
                                      wide(OTHER))):
            with self.subTest(case=index):
                self.assertTrue(flagged(line, denylist), "a denylisted number was not reported")
        for index, line in enumerate((OTHER + "1", "1" + OTHER, FAKE)):
            with self.subTest(case=index):
                self.assertFalse(flagged(line, denylist), "a number not on the denylist was reported")
        self.assertFalse(flagged(OTHER), "a bare number was reported without a denylist")

    def test_the_repository_holds_no_real_uid_list(self):
        source = (ROOT / "scripts/check_uid_literals.py").read_text(encoding="utf-8")
        self.assertFalse("KNOWN_REAL" in source or "SALT" in source or "sha256" in source,
                         "the guard holds hashes or a salt again")
        self.assertFalse(guard.DIGIT_RUN.search(source.replace(FAKE, "")) is not None,
                         "the guard's source holds an 8-10 digit number")

    def test_the_denylist_file(self):
        folder = Path(tempfile.mkdtemp())
        path = folder / "uid-denylist"
        path.write_text(f"# local only\n{OTHER}  # account\n\n", encoding="utf-8")
        numbers, problem = guard.load_denylist(path)
        self.assertTrue(numbers == frozenset({OTHER}) and problem is None, "the denylist was not read")
        path.write_text("not a number\n", encoding="utf-8")
        self.assertTrue(guard.load_denylist(path)[1] is not None, "a malformed denylist was accepted")
        self.assertTrue(guard.load_denylist(folder / "absent") == (frozenset(), None), "a missing denylist failed")
        self.assertTrue(guard.DEFAULT_DENYLIST.relative_to(ROOT).parts[0] == ".build", "the default denylist moved")


class FileTests(unittest.TestCase):
    def scan(self, data: bytes):
        path = Path(tempfile.mkdtemp()) / "f.txt"
        path.write_bytes(data)
        return guard.scan_file(path)

    def test_encodings(self):
        text = f"first\nUID ： {OTHER}\n"
        for index, data in enumerate((text.encode("utf-8"), codecs.BOM_UTF8 + text.encode("utf-8"),
                                      text.encode("utf-16"), codecs.BOM_UTF16_BE + text.encode("utf-16-be"))):
            with self.subTest(case=index):
                found, warning = self.scan(data)
                self.assertTrue(found == [(2, "UID form")] and warning is None, "an encoded UID was missed")
        found, warning = self.scan(text.encode("utf-16-le"))  # no BOM
        self.assertTrue(found == [] and warning is not None, "UTF-16 without a BOM was not warned about")
        found, warning = self.scan(b"\x89PNG\r\n\x1a\n\0\0\0" + text.encode("utf-8"))
        self.assertTrue(found == [] and warning is None, "a binary file was scanned or warned about")

    def test_paths_and_output_never_show_digits(self):
        import contextlib
        import io
        from unittest.mock import patch
        self.assertTrue(guard.masked(f"fixtures/{OTHER}.txt") == "fixtures/***.txt", "a path was not masked")
        out = io.StringIO()
        with patch.object(guard, "scan_tree", return_value=([("a.py", 3, "UID form")], [("b.bin", "skipped")])), \
                patch.object(guard, "load_denylist", return_value=(frozenset(), None)), \
                contextlib.redirect_stdout(out):
            self.assertTrue(guard.main([]) == 1, "a finding did not fail the check")
        self.assertTrue(out.getvalue() == "warning: b.bin: skipped\na.py:3: UID form (write 123456789 or the UID's "
                        "shape instead)\n1 finding(s)\n", "the report's wording changed")

    def test_commit_messages(self):
        log = f"aaaaaaaaaaaa\0subject\n\nUID：\n{OTHER}\n\0bbbbbbbbbbbb\0clean {FAKE}\n\0"
        self.assertTrue(guard.scan_log(log) == [("aaaaaaaaaa", 3, "UID form")], "a commit message UID was missed")
        self.assertTrue(guard.scan_log(f"cccc\0{OTHER}\n\0", frozenset({OTHER})) == [("cccc", 1, "denylisted UID")],
                        "a denylisted number in a commit message was missed")


def _git_ok(*args) -> bool:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True).returncode == 0


def push_range() -> str:
    """The commits a push from this branch would add: origin/android-emulator..HEAD when that ref exists, else the
    last commit (HEAD~1..HEAD, or HEAD alone in a one-commit shallow clone)."""
    if _git_ok("rev-parse", "--verify", "-q", "origin/android-emulator") and \
            _git_ok("merge-base", "origin/android-emulator", "HEAD"):
        return "origin/android-emulator..HEAD"
    return "HEAD~1..HEAD" if _git_ok("rev-parse", "--verify", "-q", "HEAD~1") else "-1"


@unittest.skipUnless((ROOT / ".git").exists() and shutil.which("git"), "needs a git checkout")
class TreeTests(unittest.TestCase):
    def test_no_tracked_file_holds_a_uid(self):
        findings, _warnings = guard.scan_tree(guard.load_denylist()[0])
        self.assertTrue(not findings, "a tracked file holds a UID: run scripts/check_uid_literals.py locally")

    def test_no_commit_message_in_the_push_range_holds_a_uid(self):
        found = guard.scan_commits(push_range(), guard.load_denylist()[0])
        self.assertTrue(not found, "a commit message holds a UID: run check_uid_literals.py --commits locally")


if __name__ == "__main__":
    unittest.main()
