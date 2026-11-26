"""Which game account the Android client shows: the UID in the open-world HUD's bottom-left corner, as an account key.

Android records that speak for one game account (the 兑换码 record, the 星海 bell attempts) are kept per account under
config/android/accounts/<key>/ in the data directory (record_path). The key is made from the UID only here and the
raw UID never leaves this module: it is not returned, stored, logged or put in a message; every reason below is a
fixed text. Frames handed to diagnostics are masked there (diagnostics.diagnostic_image, uid_roi covers the corner).

Reading (identify): two separate fresh frames, each a positively recognised HUD (host.android.actions.read_screen "hud",
and the full-frame OCR pass itself reading at least move.MIN_LABELS shortcut labels in place), on each the full-frame
OCR (move.SCREEN_TEXT, the walker's own pass) read in move's UID region; the rows there, with move's score and noise
filters, must be exactly the UID text in its slot (move.HUD_SLOTS, BLOCK_PX) and optionally the ping at its place; the
UID text must be exactly a prefix form seen on the device (UID_PREFIX: 「UID」, misread 「LID」 or 「D」; then a space, a
hyphen or nothing) and UID_DIGITS digits; both frames must give the same digits. Anything else (not the HUD, OCR not run, a
cut-short UID or one with a letter misread among the digits, two UIDs, any other text in the corner, a focus change, the
two reads disagreeing) raises Unidentified, and the task refuses before any record-dependent decision (it neither skips
nor acts on another account's record). On the saved device frames the corner reads 「UID 」 and nine digits (box
~[0,700,90,18], text rows y 703-714); the length range is that one observation. Over the 499 saved HUD frames
(scripts/android/account_frames.py) 483 read a UID, all the same account; 16 refused, 13 of them cut short (8, 6 or
3 digits at OCR score 0.95-1.0) or misread, which a looser length would have turned into another account's key.

Records kept before the per-account split (config/android/redeem_codes.json, config/android/bell_attempts.json, one
per data directory) belong to an unknown account: they are left in place and never read (docs/android-support-plan.md).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

# The UID text's shape, for any account (shared with host.android.move's UID slot, so the two cannot drift): the
# label as seen on the device frames, 「UID」 or misread 「LID」 / 「D」, then a space, a hyphen or nothing, then the
# digits. No UID itself is written anywhere in the code or the tests (they use made-up digits).
UID_PREFIX = r"(?:UID|LID|D)[\s\-]?"
UID_DIGITS = (9, 9)  # digits of the UIDs seen on the device (one account so far: nine)
UID_TEXT = re.compile(UID_PREFIX + r"(\d+)")
READS = 2  # separate frames that must agree
READ_GAP_S = 0.3  # between the two captures (no input in between)
KEY_SALT = b"MaaNikki/android-account/v1\x00"
KEY_HEX = 16
KEY_FORM = re.compile(r"[0-9a-f]{%d}" % KEY_HEX)
ACCOUNTS_DIR = ("config", "android", "accounts")


class Unidentified(Exception):
    """The account could not be told from the HUD (a fixed Chinese reason, never OCR text); ``frames``: the frames it
    was decided on (diagnostics mask the UID)."""

    def __init__(self, reason, frames=()):
        super().__init__(reason)
        self.frames = list(frames)


def account_key(uid: str) -> str:
    """The account key of ``uid``: the first KEY_HEX hex digits of SHA-256 over KEY_SALT and the digits. A pseudonym
    for file names, not a secret (a nine-digit UID can be found from it by trying them all)."""
    if not isinstance(uid, str) or not uid.isascii() or not uid.isdigit():
        raise ValueError("UID 无效")
    return hashlib.sha256(KEY_SALT + uid.encode("ascii")).hexdigest()[:KEY_HEX]


def record_path(root, key, name) -> Path:
    """Where the record ``name`` of the account ``key`` lives in the data directory ``root``."""
    if not isinstance(key, str) or not KEY_FORM.fullmatch(key):
        raise ValueError("游戏账号的记录键无效")
    return Path(root).joinpath(*ACCOUNTS_DIR, key, name)


def _uid_text(text):
    """The digits of an exact UID text (see the module's docstring), else None."""
    match = UID_TEXT.fullmatch(text.strip())
    if match is None or not UID_DIGITS[0] <= len(match.group(1)) <= UID_DIGITS[1]:
        return None
    return match.group(1)


def uid_on(result):
    """``(digits, reason)`` from one full-frame OCR result (move.SCREEN_TEXT): digits when the UID region holds exactly
    the UID text in its slot (and at most the ping at its place), reason "" then; else (None, a fixed reason)."""
    from host.android import actions, pages
    from host.android.move import (ASCII_SCORE, BLOCK_PX, HUD_REGIONS, HUD_SLOTS, PING_FORM, PING_PLACE, TEXT_SCORE,
                                   _cjk, _inside, counts_as_text)
    region = dict(HUD_REGIONS)["uid"]
    _name, sx0, sx1, scy, _ping = next(slot for slot in HUD_SLOTS if slot[0] == "uid")
    found, other = set(), False
    if result is not None and actions._hit(result):
        for row in pages._rows(result):
            box = getattr(row, "box", None) if row is not None else None
            if box is None:
                continue
            text, box = str(getattr(row, "text", "") or ""), [int(v) for v in box]
            score = float(getattr(row, "score", 0) or 0)
            if not _inside(box, region):
                continue
            if score < (TEXT_SCORE if _cjk(text) else ASCII_SCORE) or not counts_as_text(text):
                continue  # move.read_rows' noise filters: what the walker does not count either
            x0, x1, cy = box[0], box[0] + box[2], box[1] + box[3] / 2
            if PING_FORM.fullmatch(text.strip()) and x0 >= PING_PLACE[0] and x1 <= PING_PLACE[1] \
                    and PING_PLACE[2] <= cy <= PING_PLACE[3]:
                continue
            digits = _uid_text(text)
            if digits is None or not (x0 < sx1 and sx0 < x1 and abs(cy - scy) <= BLOCK_PX):
                other = True
                continue
            found.add(digits)
    if other:
        return None, "左下角 UID 的位置读到了不完整或别的文字"
    if not found:
        return None, "左下角没有读到 UID"
    if len(found) > 1:
        return None, "左下角读到了不止一个 UID"
    return next(iter(found)), ""


def read_uid(screen, recognize):
    """``(digits, reason)`` on one frame: ``screen`` (actions.read_screen) the HUD, the full-frame OCR pass reading
    the shortcut labels in place (move.read_rows, MIN_LABELS) and uid_on its UID; else (None, a fixed reason)."""
    from host.android.move import MIN_LABELS, SCREEN_TEXT, read_rows
    if screen is None or screen.kind != "hud":
        return None, "画面不是大世界"
    result = recognize(SCREEN_TEXT)
    if result is None:
        return None, "全画面文字识别没能完成"
    if read_rows(result).labels < MIN_LABELS:
        return None, "全画面文字里没有在原位读到大世界的快捷栏"
    return uid_on(result)


def identify(rt) -> str | None:
    """The account key from READS separate fresh frames (pages.screen_now; nothing is sent) that each read the same
    UID (read_uid). None when the run stopped meanwhile; Unidentified (with the frames) when the account cannot be
    told."""
    from host.android import pages
    seen, frames = None, []
    for index in range(READS):
        if index and not rt.pause(READ_GAP_S):
            return None
        screen, frame, recognize = pages.screen_now(rt)
        if frame is None:
            return None
        if not rt._frame_still_valid(frame):
            raise Unidentified("读游戏账号时焦点变了", [frame])
        if any(frame is earlier for earlier in frames):
            raise Unidentified("读游戏账号的两张截图不是分开截的", [frame])
        frames.append(frame)
        digits, reason = read_uid(screen, recognize)
        if digits is None:
            raise Unidentified(reason, [frame])
        if seen is not None and digits != seen:
            raise Unidentified("两张截图上读到的 UID 不一样", frames)
        seen = digits
    return account_key(seen)


# Text that may be a UID, masked in what an Android failure scene records (scrub): a UID label with three or more
# digits (a cut-short read included), or a bare run of 8-10 digits (the UID read apart from its label).
UID_LIKE = re.compile(UID_PREFIX + r"\d{3,}|(?<!\d)\d{8,10}(?!\d)")
UID_MASK = "UID ***"


def scrub(value):
    """``value`` (text, or lists, tuples and dicts of them, as a failure scene's reason, details and observations)
    with every UID-like text replaced by UID_MASK; other values unchanged."""
    if isinstance(value, str):
        return UID_LIKE.sub(UID_MASK, value)
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(scrub(item) for item in value)
    return value


def refusal(error) -> str:
    """The user-facing line for ``error`` (Unidentified): its fixed reason, never the UID."""
    return f"认不出现在登录的是哪个游戏账号（{error}）"
