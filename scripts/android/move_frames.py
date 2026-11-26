"""Local check of the Android movement backend (host.android.move, host.android.minimap) on the saved phase E device
frames; not part of the offline suite (.build/ is not in the repository). No device, no input.

    python -I -X utf8 scripts/android/move_frames.py [--build DIR] [--no-screens]

DIR defaults to .build/android. Reported, per measured run in DIR/phase-e-measure/m2-*/results.jsonl:
  - fixes: the reader on each saved frame with the previous logged position as the hint, against what the session
    logged (the offline adapter with a fresh Locator per frame); time per fix with one reader kept;
  - tracking: whether each frame captured right after a step passes the walk's plausibility gate;
  - the model: settled displacement / move.expected_distance for full-deflection steps;
  - the lag: settled - immediate frame distance (what LAG_U bounds);
  - the angle formula: the direction each step actually moved vs the one move.screen_angle's inverse predicts;
  - screens (unless --no-screens, needs ./maafw): move.classify on every saved HUD frame (expect "hud" except the
    「起风了」 narration frames and the 查看/拾取 interaction prompts) through the real nodes, as the walk does.
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
        screens(build)
    return 0


def screens(build: Path):
    import probe
    from host.android import actions, move
    frames = sorted((build / "phase-e-measure").glob("m*/*-hud.png")) + \
        sorted(p for p in (build / "phase-e-measure").glob("m*/*.png") if not p.name.endswith("-hud.png"))[::6]
    counts, odd = {}, []
    for path in frames:
        recognizer = probe.Recognizer(probe.frame_controller(path), android_layer=True)
        verdict = recognizer.on_one_frame(lambda recognize: move.classify(actions.read_screen(recognize),
                                                                          recognize))
        kind, reason = verdict.kind, verdict.reason
        counts[kind] = counts.get(kind, 0) + 1
        if kind != "hud":
            odd.append([str(path.relative_to(build)), kind, reason])
    print(json.dumps({"screens": counts, "not_hud": odd}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    raise SystemExit(main())
