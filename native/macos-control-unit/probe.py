"""Exercise libMaaMacOSControlUnit.dylib through the stock MaaFramework core.

Developer tool for macOS; never imported by the agent. It drives the
MacOSController of the pinned maafw Python binding against a runtime
directory, so swapping only the control unit into a copy of that directory
shows whether a self-built unit works with the stock core.

    python probe.py windows
    python probe.py lifecycle --runtime DIR (--window ID | --game) --log-dir DIR --expect-unit SHA256|stock
    python probe.py capture --runtime DIR --game --out DIR

`lifecycle` connects, captures, destroys the controller, then connects and
captures again, for each round. It passes only when every round connects and
captures, the log shows one unit Destroy per Create, the loaded core and
MaaUtils match upstream.lock.json and the loaded unit has --expect-unit. `capture` saves one raw frame and one frame at
the agent's 720 short side. Neither sends input.

`input-check` DOES send input and must only run with the maintainer at the
machine, the game in fullscreen-window mode, in the open world with no menu
open: M (posted to the game), a global click on the big map's back button,
and with --release, W held across a controller destroy.

`--runtime` is the directory holding libMaaFramework.dylib and the control
units; the binding reads it from MAAFW_BINARY_PATH before it is imported,
so the probe re-executes itself with that variable set.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import sys
import time
from pathlib import Path

GAME_BUNDLE_ID = "com.papegames.infinitynikki"
GAME_TITLES = ("infinity nikki", "无限暖暖", "無限暖暖")


def _ensure_runtime(runtime: str | None) -> None:
    if not runtime:
        return
    wanted = str(Path(runtime).resolve())
    if os.environ.get("MAAFW_BINARY_PATH") == wanted:
        return
    os.environ["MAAFW_BINARY_PATH"] = wanted
    os.execv(sys.executable, [sys.executable, *sys.argv])


def _game_windows(toolkit):
    windows = []
    for window in toolkit.find_desktop_windows():
        title = (window.window_name or "").strip().lower()
        if GAME_BUNDLE_ID in (window.class_name or "") and title in GAME_TITLES:
            windows.append(window)
    return windows


def _pick_window(args, toolkit) -> int:
    if args.window is not None:
        return args.window
    if args.game:
        windows = _game_windows(toolkit)
        if len(windows) != 1:
            sys.exit(f"expected exactly one titled game window, found {len(windows)}")
        return int(windows[0].hwnd)
    sys.exit("pass --window ID or --game")


# MaaNikki profile of the patched unit: display-window capture at native pixels
# (screencap 1 << 1) with the hybrid input (GlobalEvent | PostToPid).
PROFILES = {"stock": (1, 2), "maanikki": (1 << 1, 1 | 1 << 1)}
_profile = "stock"


def _connect(window_id: int, *, raw: bool):
    from maa.controller import MacOSController

    screencap_method, input_method = PROFILES[_profile]
    controller = MacOSController(window_id, screencap_method, input_method)
    if raw:
        controller.set_screenshot_use_raw_size(True)
    else:
        controller.set_screenshot_target_short_side(720)
    started = time.perf_counter()
    ok = controller.post_connection().wait().succeeded
    return controller, ok, (time.perf_counter() - started) * 1000


def _grab(controller):
    started = time.perf_counter()
    job = controller.post_screencap().wait()
    elapsed = (time.perf_counter() - started) * 1000
    if not job.succeeded:
        # get() would return the core's cached image from an earlier successful capture.
        return None, elapsed
    frame = job.get()
    if frame is None or getattr(frame, "size", 0) == 0:
        return None, elapsed
    return frame, elapsed


LOCK = json.loads((Path(__file__).resolve().parent / "upstream.lock.json").read_text(encoding="utf-8"))
CHECKED_LIBRARIES = ("libMaaFramework.dylib", "libMaaUtils.dylib", "libMaaMacOSControlUnit.dylib")


def _loaded_libraries() -> dict[str, dict]:
    """Path and SHA-256 of the MaaFramework libraries dyld actually loaded into this process."""
    import ctypes
    import hashlib

    libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    libsystem._dyld_image_count.restype = ctypes.c_uint32
    libsystem._dyld_get_image_name.restype = ctypes.c_char_p
    libsystem._dyld_get_image_name.argtypes = [ctypes.c_uint32]
    loaded = {}
    for index in range(libsystem._dyld_image_count()):
        path = Path(libsystem._dyld_get_image_name(index).decode())
        if path.name in CHECKED_LIBRARIES:
            loaded[path.name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return loaded


def cmd_windows(_args) -> int:
    from maa.toolkit import Toolkit

    for window in Toolkit.find_desktop_windows():
        print(f"id={window.hwnd} owner={window.class_name!r} title={window.window_name!r}")
    return 0


def cmd_lifecycle(args) -> int:
    from maa.library import Library
    from maa.toolkit import Toolkit

    from maa.tasker import Tasker

    if args.rounds < 2 or args.frames < 1:
        sys.exit("lifecycle needs at least two rounds (to reconnect) and one frame per round")
    expected_unit = LOCK["stock_runtime"][CHECKED_LIBRARIES[2]] if args.expect_unit == "stock" else args.expect_unit
    log_dir = Path(args.log_dir).resolve()
    if log_dir.exists() and any(log_dir.iterdir()):
        sys.exit(f"{log_dir} is not empty; the create/destroy count needs a fresh log")
    log_dir.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(log_dir)
    window_id = _pick_window(args, Toolkit)
    report = {"runtime": str(Library.framework_libpath.parent), "window_id": window_id, "rounds": []}
    failed = False
    for round_index in range(args.rounds):
        controller, ok, connect_ms = _connect(window_id, raw=args.raw)
        entry = {"round": round_index, "connected": ok, "connect_ms": round(connect_ms)}
        if ok:
            info = controller.info
            entry["info"] = info
            timings, shape = [], None
            for _ in range(args.frames):
                frame, elapsed = _grab(controller)
                if frame is None:
                    entry["capture_failed"] = True
                    failed = True
                    break
                shape = list(frame.shape)
                timings.append(elapsed)
            entry["frame_shape"] = shape
            if timings:
                entry["capture_ms_first"] = round(timings[0])
                entry["capture_ms_p50"] = round(statistics.median(timings))
                entry["capture_ms_max"] = round(max(timings))
        else:
            failed = True
        del controller
        gc.collect()  # MacOSController.__del__ destroys the native controller and its unit
        entry["destroyed"] = True
        report["rounds"].append(entry)
    # The core and MaaUtils must be the locked official binaries, the unit the one under test.
    loaded = _loaded_libraries()
    report["loaded"] = loaded
    expected = {name: LOCK["stock_runtime"][name] for name in CHECKED_LIBRARIES[:2]}
    expected[CHECKED_LIBRARIES[2]] = expected_unit
    report["mismatched"] = sorted(name for name, sha in expected.items() if loaded.get(name, {}).get("sha256") != sha)
    # The control unit logs its C entry points; one Destroy per Create proves teardown.
    lines = (log_dir / "maafw.log").read_text(encoding="utf-8", errors="replace").splitlines()
    report["unit_creates"] = sum("MaaMacOSControlUnitCreate(uint32_t" in line and "| enter" in line for line in lines)
    report["unit_destroys"] = sum("MaaMacOSControlUnitDestroy(MaaMacOSControlUnitHandle)] [handle=true] | enter" in line
                                  for line in lines)
    failed = (failed or bool(report["mismatched"])
              or report["unit_creates"] != args.rounds or report["unit_destroys"] != args.rounds)
    report["passed"] = not failed
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failed else 0


def cmd_capture(args) -> int:
    import cv2
    from maa.toolkit import Toolkit

    window_id = _pick_window(args, Toolkit)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for raw in (True, False):
        controller, ok, _ = _connect(window_id, raw=raw)
        if not ok:
            sys.exit("connection failed")
        frame, elapsed = _grab(controller)
        if frame is None:
            sys.exit("capture failed")
        path = out / f"{stamp}-{'raw' if raw else 'short720'}.png"
        cv2.imwrite(str(path), frame)
        print(f"{path}: {frame.shape[1]}x{frame.shape[0]} in {elapsed:.0f} ms")
        del controller
        gc.collect()
    return 0


# macOS virtual key codes (Carbon kVK_ANSI_M, kVK_ANSI_W).
KEY_M, KEY_W = 46, 13
# Back button of the big map, in 1280x720 game coordinates (planning-stage measurement).
MAP_BACK = (46, 31)


def _changed(before, after) -> float:
    import cv2

    if before.shape != after.shape:
        return 100.0
    return round(float((cv2.absdiff(before, after).max(axis=2) > 24).mean() * 100), 1)


def _front_bundle_id() -> str | None:
    import re
    import subprocess

    front = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
    info = subprocess.run(["lsappinfo", "info", "-only", "bundleid", front], capture_output=True, text=True).stdout
    match = re.search(r'bundleID="([^"]+)"', info)
    return match.group(1) if match else None


def cmd_refusal_check(args) -> int:
    """Presses aimed at a window whose app is not focused must all be refused; nothing is sent."""
    from maa.tasker import Tasker
    from maa.toolkit import Toolkit

    if _profile != "maanikki":
        sys.exit("refusal-check exercises the patched unit; pass --profile maanikki")
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(out)
    window_id = _pick_window(args, Toolkit)
    owner = next((w for w in Toolkit.find_desktop_windows() if int(w.hwnd) == window_id), None)
    front = _front_bundle_id()
    if owner is None or not front or front == owner.class_name:
        sys.exit(f"the target's app ({owner.class_name if owner else '?'}) may be focused ({front!r}); "
                 "refusing to run a check that could send input")
    controller, ok, _ = _connect(window_id, raw=False)
    if not ok:
        sys.exit("connection failed")
    for _ in range(2):  # a click needs a settled frame
        if _grab(controller)[0] is None:
            sys.exit("capture failed")
    results = {
        "front_bundle_id": front,
        "window_owner": owner.class_name,
        "click": controller.post_click(640, 360).wait().succeeded,
        "key_down": controller.post_key_down(105).wait().succeeded,  # F13
        "key_up": controller.post_key_up(105).wait().succeeded,
        "relative_move": controller.post_relative_move(10, 0).wait().succeeded,
    }
    del controller
    gc.collect()
    lines = (out / "maafw.log").read_text(encoding="utf-8", errors="replace").splitlines()
    results["refusals_logged"] = sum("not sending" in line for line in lines)
    results["passed"] = (not results["click"] and not results["key_down"] and not results["relative_move"]
                         and results["refusals_logged"] >= 3)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if results["passed"] else 1


def cmd_input_check(args) -> int:
    """Sends input to the game. Only with the maintainer at the machine; see the README."""
    import cv2
    from maa.tasker import Tasker
    from maa.toolkit import Toolkit

    if _profile != "maanikki":
        sys.exit("input-check exercises the patched unit; pass --profile maanikki")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(out)
    window_id = _pick_window(args, Toolkit)
    # Time to click into the game: the patched unit refuses clicks while another app is frontmost.
    for remaining in range(args.delay, 0, -1):
        print(f"starting in {remaining} s, bring the game to the front", flush=True)
        time.sleep(1)
    controller, ok, _ = _connect(window_id, raw=False)
    if not ok:
        sys.exit("connection failed")
    steps = []

    def frame(name):
        image, _ = _grab(controller)
        if image is None:
            sys.exit(f"capture failed at {name}")
        if image.shape[:2] != (720, 1280):
            sys.exit(f"frame is {image.shape[1]}x{image.shape[0]}, expected 1280x720; is the game in fullscreen-window mode?")
        cv2.imwrite(str(out / f"{len(steps):02d}-{name}.png"), image)
        return image

    def step(name, ok_, before, after):
        entry = {"step": name, "sent": bool(ok_), "changed_pct": _changed(before, after)}
        steps.append(entry)
        print(json.dumps(entry), flush=True)

    start = frame("start")
    sent = controller.post_key_down(KEY_M).wait().succeeded and controller.post_key_up(KEY_M).wait().succeeded
    time.sleep(1.5)
    map_open = frame("after-key-m")
    step("key M posted to the game process (expect the big map)", sent, start, map_open)

    sent = controller.post_click(*MAP_BACK).wait().succeeded
    time.sleep(1.5)
    map_closed = frame("after-click-back")
    step("global click on the map back button (expect the world again)", sent, map_open, map_closed)
    steps.append({"step": "world again vs start", "changed_pct": _changed(start, map_closed)})
    print(json.dumps(steps[-1]), flush=True)

    if args.release:
        # Hold W, then destroy the controller without a key-up: the unit must release W itself.
        sent = controller.post_key_down(KEY_W).wait().succeeded
        time.sleep(0.6)
        del controller
        gc.collect()
        time.sleep(0.4)
        observer, ok, _ = _connect(window_id, raw=False)
        settled = _grab(observer)[0]
        time.sleep(1.0)
        later = _grab(observer)[0]
        cv2.imwrite(str(out / "release-settled.png"), settled)
        cv2.imwrite(str(out / "release-later.png"), later)
        entry = {"step": "W held across destroy (expect no movement afterwards)", "sent": bool(sent),
                 "changed_pct_after_destroy": _changed(settled, later)}
        steps.append(entry)
        print(json.dumps(entry), flush=True)
    print(f"frames and maafw.log in {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("windows", "lifecycle", "capture", "input-check", "refusal-check"):
        command = sub.add_parser(name)
        command.add_argument("--runtime", help="directory with libMaaFramework.dylib and the control units")
        command.add_argument("--profile", choices=sorted(PROFILES), default="stock",
                             help="stock: ScreenCaptureKit + PostToPid; maanikki: the patched unit's own methods")
        if name == "windows":
            continue
        command.add_argument("--window", type=int, help="CGWindowID to connect")
        command.add_argument("--game", action="store_true", help="connect the titled Infinity Nikki window")
        if name == "lifecycle":
            command.add_argument("--frames", type=int, default=10)
            command.add_argument("--rounds", type=int, default=3)
            command.add_argument("--raw", action="store_true", help="capture at raw size instead of 720 short side")
            command.add_argument("--log-dir", required=True,
                                 help="new or empty directory for maafw.log; checks one unit Destroy per Create")
            command.add_argument("--expect-unit", required=True, metavar="SHA256|stock",
                                 help="SHA-256 the loaded control unit must have (build.json), or 'stock' for the official one")
        else:
            command.add_argument("--out", required=True)
        if name == "input-check":
            command.add_argument("--delay", type=int, default=5, help="seconds to bring the game to the front")
            command.add_argument("--release", action="store_true",
                                 help="also hold W across a controller destroy (the character walks briefly)")
    args = parser.parse_args()
    _ensure_runtime(args.runtime)
    global _profile
    _profile = args.profile
    commands = {"windows": cmd_windows, "lifecycle": cmd_lifecycle, "capture": cmd_capture,
                "input-check": cmd_input_check, "refusal-check": cmd_refusal_check}
    return commands[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
