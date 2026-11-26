"""Local check of the Android movement backend (host.android.move, host.android.minimap) on the saved phase E device
frames; not part of the offline suite (.build/ is not in the repository). No device, no input.

    python -I -X utf8 scripts/android/move_frames.py [--build DIR] [--no-screens] [--all-frames]
        [--max-frames N] [--max-seconds S] [--max-rss-mb MB]

DIR defaults to .build/android. Reported, per measured run in DIR/phase-e-measure/m2-*/results.jsonl:
  - fixes: the reader on each saved frame with the previous logged position as the hint, against what the session
    logged (the offline adapter with a fresh Locator per frame); time per fix with one reader kept;
  - tracking: whether each frame captured right after a step passes the walk's plausibility gate;
  - the model: settled displacement / move.expected_distance for full-deflection steps;
  - the lag: settled - immediate frame distance (what LAG_U bounds);
  - the angle formula: the direction each step actually moved vs the one move.screen_angle's inverse predicts;
  - screens (unless --no-screens, needs ./maafw): move.classify on every saved HUD frame (expect "hud" except the
    「起风了」 narration frames, "hud_prompt" for the 查看/拾取 interaction prompts) through the real nodes and on the
    frame's own pixels, as the walk does. With --all-frames: every 1280x720 PNG under DIR (not reviews/ or the later
    walk runs and investigations: phase-e-walk*, heading-investigation) whose screen read_screen calls the HUD (the
    495 frames of the 2026-11-26 replay: 473 hud, 4 narration, 8 misread, and the 10 prompt frames).
The screens run on one recognizer through scripts/android/replay.py (its frame, wall-time and memory limits; the
--all-frames set needs no raised limit). Run it in the foreground and never two at once.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts" / "android"))


def runs(build: Path):
    for path in sorted((build / "phase-e-measure").glob("m*/results.jsonl")):
        yield path.parent, [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", default=str(ROOT / ".build" / "android"))
    parser.add_argument("--no-screens", action="store_true")
    parser.add_argument("--all-frames", action="store_true")
    import replay
    replay.add_arguments(parser)
    args = parser.parse_args()
    build = Path(args.build)
    import cv2
    from host.android import move
    from host.android.minimap import MinimapReader

    reader = MinimapReader("miraland", ROOT / "resource")
    deviations, scores, timings, gates, ratios, lags, angle_errors = [], [], [], [0, 0], [], [], []
    for folder, records in runs(build):
        for record in records:
            if record.get("kind") not in ("gesture", "goto", "camera"):
                continue
            before = record["before"]
            for key in ("after", "settled"):
                observed = record.get(key)
                frame = cv2.imread(str(folder / f"{record['tag']}-{'a' if key == 'after' else 'b'}.png"))
                if observed is None or frame is None:
                    continue
                started = time.perf_counter()
                fix = reader.fix(frame, (before["x"], before["y"]), move.TRACK_SEARCH)
                timings.append((time.perf_counter() - started) * 1000)
                deviations.append(math.dist((fix.x, fix.y), (observed["x"], observed["y"])))
                scores.append(fix.score)
                if key == "after" and record["kind"] != "camera" and before.get("head") is not None:
                    seconds = record.get("ms", 0) / 1000
                    gates[0] += 1
                    gates[1] += fix.reliable and math.dist((fix.x, fix.y), (before["x"], before["y"])) \
                        <= move.plausible_distance(seconds)
            if record["kind"] == "camera" or record.get("mag", 1.0) != 1.0:
                continue
            after, settled, seconds = record["after"], record["settled"], record["ms"] / 1000
            moved = math.dist((settled["x"], settled["y"]), (before["x"], before["y"]))
            ratios.append((seconds, moved / move.expected_distance(seconds)))
            lags.append(math.dist((settled["x"], settled["y"]), (after["x"], after["y"])))
            if moved >= 1.5 and before.get("head") is not None:
                predicted = (before["head"] + record["angle"] - 270) % 360
                actual = move.bearing(settled["x"] - before["x"], settled["y"] - before["y"])
                angle_errors.append((actual - predicted + 180) % 360 - 180)
    print(json.dumps({
        "fixes": {"n": len(deviations), "max_deviation_from_log_u": round(max(deviations), 3),
                  "score_min": round(min(scores), 3), "score_median": round(statistics.median(scores), 3),
                  "ms_median": round(statistics.median(timings), 1), "ms_max": round(max(timings), 1)},
        "tracking_gate": {"frames_right_after_a_step": gates[0], "accepted": gates[1]},
        "model": {f"{s:.1f}s": [round(min(r for t, r in ratios if t == s), 2),
                                round(statistics.median(r for t, r in ratios if t == s), 2),
                                round(max(r for t, r in ratios if t == s), 2)] for s in sorted({t for t, _ in ratios})},
        "lag_u": {"n": len(lags), "max": round(max(lags), 2), "median": round(statistics.median(lags), 2)},
        "angle_error_deg": {"n": len(angle_errors), "median_abs": round(statistics.median(map(abs, angle_errors)), 1),
                            "max_abs": round(max(map(abs, angle_errors)), 1)},
    }, ensure_ascii=False, indent=1))
    if not args.no_screens:
        try:
            screens(build, args.all_frames, replay.limits_from(args))
        except replay.ReplayLimit as error:
            print(error, file=sys.stderr)
            return 2
    return 0


LATER = ("reviews", "phase-e-walk", "phase-e-walk2", "heading-investigation")  # not in the --all-frames set


def screens(build: Path, all_frames: bool = False, limits=None):
    import replay
    from host.android import actions, move
    if all_frames:
        frames = sorted(p for p in build.glob("*/**/*.png") if p.relative_to(build).parts[0] not in LATER)
    else:
        frames = sorted((build / "phase-e-measure").glob("m*/*-hud.png")) + \
            sorted(p for p in (build / "phase-e-measure").glob("m*/*.png") if not p.name.endswith("-hud.png"))[::6]

    def judge_frame(recognizer, path, image):
        def judge(recognize):
            screen = actions.read_screen(recognize)
            if all_frames and screen.kind != "hud":
                return None  # not an open-world frame: outside this set
            return move.classify(screen, recognize, image)
        return recognizer.on_one_frame(judge)

    counts, odd = {}, []
    # one recognizer for every frame, under the replay's frame, time and memory limits
    with replay.Replay(limits=limits or replay.Limits()) as session:
        for path, verdict in session.run(frames, judge_frame,
                                         accept=lambda image: image is not None and image.shape[:2] == (720, 1280)):
            if verdict is None:
                continue
            kind, reason = verdict.kind, verdict.reason
            counts[kind] = counts.get(kind, 0) + 1
            if kind != "hud":
                odd.append([str(path.relative_to(build)), kind, reason])
        print(f"replay: {session.frames} frames, peak RSS {session.peak_rss / 2 ** 20:.0f} MB", file=sys.stderr)
    print(json.dumps({"screens": counts, "not_hud": odd}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    raise SystemExit(main())
