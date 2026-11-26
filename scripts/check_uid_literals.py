"""No game UID in the repository: python scripts/check_uid_literals.py [--commits A..B]

The game shows the player's UID in the HUD's corner ("UID 123456789"); a real one must never be written into code,
tests, fixtures, docs or commit messages. Code, tests and docs use the documented fake UID FAKE_UID (123456789), or
write a UID by its shape. Checked, on the decoded text after NFKC normalisation (full-width digits, colons and spaces
count as their ASCII forms):

  1. the HUD form: the label "UID" (or OCR's "LID") as a word of its own (not the end of GUID or another word), then
     any run of whitespace (line breaks included), colons or hyphens, then 8-10 digits: any number other than
     FAKE_UID is reported, real or not;
  2. any bare run of 8-10 digits listed in the local denylist: the UIDs of the accounts actually played, one per line
     (# comments allowed), in an untracked file: $MAANIKKI_UID_DENYLIST, else .build/uid-denylist (.build/ is
     ignored by git). The repository holds no real UID in any form, not even a hash; CI has no denylist and checks the
     HUD form only. A denylist file that git tracks is itself reported.

Without arguments: every tracked text file (git ls-files). UTF-8 (with or without BOM) and UTF-16/32 with a BOM are
decoded; a file with a NUL byte in its first 8 KB and no BOM is binary and skipped, with a warning when it looks like
UTF-16 without a BOM. With --commits A..B: the messages of those commits instead (before a push:
--commits origin/android-emulator..HEAD). Findings name the file (digit runs in its name masked) and line, or the
commit, and the rule, never the digits. Exit status 1 when anything is found. scripts/test_uid_literals.py runs both
checks in the offline suite.
"""
from __future__ import annotations

import argparse
import codecs
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
FAKE_UID = "123456789"  # the one UID code, tests and docs may write (docs/android-support-plan.md)
DENYLIST_ENV = "MAANIKKI_UID_DENYLIST"
DEFAULT_DENYLIST = ROOT / ".build" / "uid-denylist"
HUD_FORM = re.compile(r"(?<![0-9A-Za-z_])(?:UID|LID)[\s:\-]*([0-9]{8,10})(?![0-9])")
DIGIT_RUN = re.compile(r"(?<![0-9])[0-9]{8,10}(?![0-9])")
BINARY_PROBE = 8192
BOMS = ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"), (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"))


def normal(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def scan_text(text: str, denylist=frozenset()):
    """[(line number, rule)] for ``text``: "UID form" (rule 1) or "denylisted UID" (rule 2), at most one per line."""
    text = normal(text)
    found = {}
    for match in HUD_FORM.finditer(text):
        if match.group(1) != FAKE_UID:
            found.setdefault(text.count("\n", 0, match.start()) + 1, "UID form")
    if denylist:
        for match in DIGIT_RUN.finditer(text):
            if match.group(0) in denylist:
                found.setdefault(text.count("\n", 0, match.start()) + 1, "denylisted UID")
    return sorted(found.items())


def decode(data: bytes):
    """``(text, warning)``: text None for a binary file (warning set when it looks like UTF-16 without a BOM)."""
    for bom, encoding in BOMS:
        if data.startswith(bom):
            return data.decode(encoding, "replace"), None
    probe = data[:BINARY_PROBE]
    if b"\0" in probe:
        for zeros, chars in ((probe[1::2], probe[0::2]), (probe[0::2], probe[1::2])):
            text = sum(1 for byte in chars if byte in (9, 10, 13) or 32 <= byte < 127)
            if zeros.count(0) > 0.9 * max(len(zeros), 1) and text > 0.9 * max(len(chars), 1):
                return None, "looks like UTF-16 without a BOM; skipped (add a BOM or save it as UTF-8)"
        return None, None
    return data.decode("utf-8", "replace"), None


def masked(name: str) -> str:
    """``name`` with every digit run of 8 or more masked (a file named after a UID is reported without it)."""
    return re.sub(r"[0-9]{8,}", "***", name)


def scan_file(path: Path, denylist=frozenset()):
    """``(findings, warning)`` for one file: findings [(line, rule)]."""
    try:
        data = path.read_bytes()
    except OSError:  # deleted in the working tree
        return [], None
    text, warning = decode(data)
    return ([] if text is None else scan_text(text, denylist)), warning


def _git(*args) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def tracked_files():
    return [name for name in _git("ls-files", "-z").decode("utf-8").split("\0") if name]


def load_denylist(path=None):
    """``(numbers, problem)``: the local denylist's numbers (empty without the file) and a problem to report (the file
    tracked by git, or a line that is no 8-10 digit number) or None."""
    path = Path(path or os.environ.get(DENYLIST_ENV) or DEFAULT_DENYLIST)
    if not path.is_file():
        return frozenset(), None
    try:
        relative = path.resolve().relative_to(ROOT.resolve())
        if _git("ls-files", "--", str(relative)).strip():
            return frozenset(), "the UID denylist is tracked by git (it must stay untracked)"
    except (ValueError, OSError, subprocess.CalledProcessError):
        pass  # outside the repository, or no git: nothing to be tracked
    numbers = set()
    for line in normal(path.read_text(encoding="utf-8-sig", errors="replace")).splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        if not re.fullmatch(r"[0-9]{8,10}", entry):
            return frozenset(), "a line of the UID denylist is no 8-10 digit number"
        numbers.add(entry)
    return frozenset(numbers), None


def scan_tree(denylist=frozenset()):
    """``(findings, warnings)`` over every tracked text file as it is in the working tree: findings [(path, line,
    rule)], warnings [(path, text)], paths masked."""
    findings, warnings = [], []
    for name in tracked_files():
        found, warning = scan_file(ROOT / name, denylist)
        findings += [(masked(name), line, rule) for line, rule in found]
        if warning:
            warnings.append((masked(name), warning))
    return findings, warnings


def scan_log(output: str, denylist=frozenset()):
    """[(commit, line, rule)] over ``git log --format=%H%x00%B%x00`` output."""
    found = []
    parts = output.split("\0")
    for sha, message in zip(parts[0::2], parts[1::2]):
        found += [(sha.strip()[:10], line, rule) for line, rule in scan_text(message, denylist)]
    return found


def scan_commits(revisions: str, denylist=frozenset()):
    """[(commit, line, rule)] over the messages of the commits in ``revisions`` (e.g. origin/android-emulator..HEAD)."""
    return scan_log(_git("log", "--format=%H%x00%B%x00", revisions).decode("utf-8", "replace"), denylist)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Report game UIDs written into the repository (never the digits).")
    parser.add_argument("--commits", metavar="A..B", help="scan these commits' messages instead of the tracked files")
    args = parser.parse_args(argv)
    denylist, problem = load_denylist()
    if args.commits:
        findings, warnings = scan_commits(args.commits, denylist), []
    else:
        findings, warnings = scan_tree(denylist)
    for where, text in warnings:
        print(f"warning: {where}: {text}")
    if problem:
        print(f"problem: {problem}")
    for where, line, rule in findings:
        print(f"{where}:{line}: {rule} (write {FAKE_UID} or the UID's shape instead)")
    print(f"{len(findings)} finding(s)" if findings else "no UID literal found")
    return 1 if findings or problem else 0


if __name__ == "__main__":
    sys.exit(main())
