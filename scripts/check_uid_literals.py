"""No game UID in the repository: python scripts/check_uid_literals.py [--commits A..B]

The game shows the player's UID in the HUD's corner ("UID 123456789"); a real one must never be written into code,
tests, fixtures, docs or commit messages. Code and tests use the documented fake UID FAKE_UID (123456789). Checked:

  1. the HUD form: "UID" (or OCR's "LID"), an optional space, colon or hyphen, then 8-10 digits: any number other than
     FAKE_UID is reported, real or not (write a UID by its shape, or the fake one);
  2. any bare run of 8-10 digits whose salted SHA-256 (SALT) is in KNOWN_REAL: the UIDs of the accounts seen on the
     device, kept as salted hashes only, never as numbers. Other long numbers (item IDs such as those in
     agent/collection, lock-file hashes, timestamps) are not UIDs and are not reported.

Without arguments: every tracked text file (git ls-files; files with a NUL byte in their first 8 KB are binary and
skipped). With --commits A..B: the messages of those commits instead (before a push: --commits origin/BRANCH..HEAD).
Findings name the file and line (or commit) and the rule, never the digits. Exit status 1 when anything is found.
Run by scripts/test_uid_literals.py in the offline suite (CI).

To add an account: python -c "import check_uid_literals as c; print(c.digest('<its uid>'))" from scripts/, in a
shell whose history you clear, and add the printed hash to KNOWN_REAL.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FAKE_UID = "123456789"  # the one UID code, tests and docs may write (docs/android-support-plan.md)
SALT = b"MaaNikki/uid-literal-guard/v1\x00"
KNOWN_REAL = frozenset({
    "7855e49a99869bc7db7c904e05ee343e5b68cfe56ec03b32fa0941e331654a86",  # account 1 (Mac client and emulator)
})
HUD_FORM = re.compile(r"(?:UID|LID)[\s:：\-]{0,2}(\d{8,10})(?!\d)")
DIGIT_RUN = re.compile(r"(?<!\d)\d{8,10}(?!\d)")
BINARY_PROBE = 8192


def digest(number: str) -> str:
    return hashlib.sha256(SALT + number.encode("ascii")).hexdigest()


def scan_text(text: str, known=KNOWN_REAL):
    """[(line number, rule)] for ``text``: "UID form" (rule 1) or "known UID" (rule 2)."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if any(match.group(1) != FAKE_UID for match in HUD_FORM.finditer(line)):
            found.append((number, "UID form"))
        elif any(digest(match.group(0)) in known for match in DIGIT_RUN.finditer(line)):
            found.append((number, "known UID"))
    return found


def _git(*args) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def tracked_files():
    return [name for name in _git("ls-files", "-z").decode("utf-8").split("\0") if name]


def scan_tree(known=KNOWN_REAL):
    """[(path, line, rule)] over every tracked text file as it is in the working tree."""
    found = []
    for name in tracked_files():
        path = ROOT / name
        try:
            data = path.read_bytes()
        except OSError:  # deleted in the working tree
            continue
        if b"\0" in data[:BINARY_PROBE]:
            continue
        found += [(name, line, rule) for line, rule in scan_text(data.decode("utf-8", "replace"), known)]
    return found


def scan_commits(revisions: str, known=KNOWN_REAL):
    """[(commit, line, rule)] over the messages of the commits in ``revisions`` (e.g. origin/main..HEAD)."""
    found = []
    output = _git("log", "--format=%H%x00%B%x00", revisions).decode("utf-8", "replace")
    parts = output.split("\0")
    for sha, message in zip(parts[0::2], parts[1::2]):
        found += [(sha.strip()[:10], line, rule) for line, rule in scan_text(message, known)]
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Report game UIDs written into the repository (never the digits).")
    parser.add_argument("--commits", metavar="A..B", help="scan these commits' messages instead of the tracked files")
    args = parser.parse_args(argv)
    found = scan_commits(args.commits) if args.commits else scan_tree()
    for where, line, rule in found:
        print(f"{where}:{line}: {rule} (write {FAKE_UID} or the UID's shape instead)")
    print(f"{len(found)} finding(s)" if found else "no UID literal found")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
