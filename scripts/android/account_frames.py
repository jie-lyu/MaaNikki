"""Local check of the Android account reader (host.android.account) on saved device frames; not part of the offline
suite (.build/ is not in the repository). No device, no input.

    python -I -X utf8 scripts/android/account_frames.py [--build DIR] [--max-frames N] [--max-seconds S]
        [--max-rss-mb MB]

DIR defaults to .build/android. Every 1280x720 PNG/JPG under DIR whose UID corner is not masked (diagnostic images
are) is classified with host.android.actions.read_screen; on each HUD frame account.read_uid runs as identify does on
one capture (the real nodes, the walker's full-frame OCR pass). Printed: the frames seen, the HUD frames, how many
gave a UID, the refusal reasons with their counts, and how many distinct accounts the readable frames name (as
"account 1", "account 2", ... with frame counts): never a UID or an account key. One recognizer through
scripts/android/replay.py (its frame, wall-time and memory limits); run it in the foreground and never two at once.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts" / "android"))


def unmasked(image) -> bool:
    """A full device frame whose UID corner still shows (diagnostics black it out: diagnostic_images.json)."""
    if image is None or image.shape[:2] != (720, 1280):
        return False
    return int(image[678:720, 0:210].max()) >= 8


def summarise(readings) -> dict:
    """The printed summary of ``readings``: None for a frame that is not the HUD, else (digits or None, reason)."""
    from host.android.account import account_key
    hud, reasons, accounts = 0, {}, {}
    for reading in readings:
        if reading is None:
            continue
        hud += 1
        digits, reason = reading
        if digits is None:
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            key = account_key(digits)
            accounts[key] = accounts.get(key, 0) + 1
    named = {f"account {index + 1}": count for index, count in enumerate(sorted(accounts.values(), reverse=True))}
    return {"hud": hud, "read": sum(accounts.values()), "refused": reasons, "accounts": named}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", default=str(ROOT / ".build" / "android"))
    import replay
    replay.add_arguments(parser)
    args = parser.parse_args()
    build = Path(args.build)
    from host.android import account, actions
    frames = sorted(p for p in build.glob("*/**/*") if p.suffix.lower() in (".png", ".jpg") and p.is_file())

    def judge_frame(recognizer, path, image):
        def judge(recognize):
            screen = actions.read_screen(recognize)
            return None if screen.kind != "hud" else account.read_uid(screen, recognize)
        return recognizer.on_one_frame(judge)

    readings = []
    with replay.Replay(limits=replay.limits_from(args)) as session:
        for _path, reading in session.run(frames, judge_frame, accept=unmasked):
            readings.append(reading)
        print(f"replay: {session.frames} frames, peak RSS {session.peak_rss / 2 ** 20:.0f} MB", file=sys.stderr)
    print(json.dumps({"frames": len(readings), **summarise(readings)}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
