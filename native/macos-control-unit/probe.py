"""Exercise libMaaMacOSControlUnit.dylib through the stock MaaFramework core.

Developer tool for macOS; never imported by the agent. It drives the
MacOSController of the pinned maafw Python binding against a runtime
directory, so swapping only the control unit into a copy of that directory
shows whether a self-built unit works with the stock core.

    python probe.py windows
    python probe.py lifecycle --runtime DIR (--window ID | --game) --log-dir DIR --expect-unit SHA256|stock
    python probe.py capture --runtime DIR --game --out DIR
    python probe.py camera-check --profile maanikki --runtime DIR --game --out DIR [--option]
    python probe.py key-sequence-check --profile maanikki --runtime DIR --game --out DIR --sequence face-camera|starsea-fall
    python probe.py timing [--activity none|user-initiated|latency-critical]
    python probe.py photo-mode-check|text-input-check --profile maanikki --runtime DIR --game --out DIR
    python probe.py hover-check --profile maanikki --runtime DIR --game --out DIR --point X Y
    python probe.py scene-button-check --profile maanikki --runtime DIR --game --out DIR [--button middle]
        [--no-turn] [--hold SECONDS] [--repeat N] [--with-forward] [--expect-ability NAME [--select-slot N]]

`lifecycle` connects, captures, destroys the controller, then connects and
captures again, for each round. It passes only when every round connects and
captures, the log shows one unit Destroy per Create, the loaded core and
MaaUtils match upstream.lock.json and the loaded unit has --expect-unit. `capture` saves one raw frame and one frame at
the agent's 720 short side. Neither sends input.

`input-check` DOES send input and must only run with the maintainer at the
machine, the game in fullscreen-window mode, in the open world with no menu
open: M (posted to the game), a global click on the big map's back button,
and with --release, W held across a controller destroy. It waits for the game
to be in front without activating it, and stops at the first step whose
result the captures do not confirm.

`refusal-check` expects the patched unit to refuse a click, a key press and a
relative move aimed at a window whose app is not in front. Use a disposable
target: if that app comes to the front during the run, the input is real.

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
# Back button of the big map, in 1280x720 game coordinates (planning-stage measurement), and a
# 40x40 crop of it from a fullscreen-window capture. It scored 1.0 on the open map and 0.45-0.57 in
# the open world on the device.
MAP_BACK = (46, 31)
MAP_BACK_LANDMARK = Path(__file__).resolve().parent / "fixtures" / "map-back-button.png"
MAP_OPEN_SCORE, MAP_CLOSED_SCORE = 0.85, 0.75


def _map_back_score(image) -> float:
    """Best landmark score among positions whose matched box contains the click point MAP_BACK."""
    import cv2

    template = cv2.imread(str(MAP_BACK_LANDMARK))
    height, width = template.shape[:2]
    x, y = MAP_BACK
    # Top-left corners for which the template box still covers (x, y).
    left, top = max(0, x - width + 1), max(0, y - height + 1)
    region = image[top:y + height, left:x + width]
    scores = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
    return round(float(scores.max()), 3)


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
    """Expected-refusal test: presses aimed at a window whose app is not in front must be refused.

    The front check happens once before the presses; bringing the target forward meanwhile would let
    real input through. Use a disposable target window.
    """
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


def _ax_process_at(points):
    """pid owning each global point per the accessibility hit test (what a click would reach), with the AX error."""
    import ctypes

    ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    ax.AXUIElementCreateSystemWide.restype = ctypes.c_void_p
    ax.AXUIElementSetMessagingTimeout.argtypes = [ctypes.c_void_p, ctypes.c_float]
    ax.AXUIElementCopyElementAtPosition.argtypes = [ctypes.c_void_p, ctypes.c_float, ctypes.c_float,
                                                    ctypes.POINTER(ctypes.c_void_p)]
    ax.AXUIElementGetPid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    system = ax.AXUIElementCreateSystemWide()
    ax.AXUIElementSetMessagingTimeout(system, 0.25)
    results = []
    for x, y in points:
        element = ctypes.c_void_p()
        error = ax.AXUIElementCopyElementAtPosition(system, x, y, ctypes.byref(element))
        pid = ctypes.c_int(-1)
        if error == 0 and element.value:
            ax.AXUIElementGetPid(element, ctypes.byref(pid))
            cf.CFRelease(element)
        results.append((pid.value, error))
    cf.CFRelease(system)
    return results


def _front_pid() -> int:
    import ctypes

    hi = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    psn = (ctypes.c_uint32 * 2)()
    pid = ctypes.c_int(-1)
    if hi.GetFrontProcess(ctypes.byref(psn)) == 0:
        hi.GetProcessPID(ctypes.byref(psn), ctypes.byref(pid))
    return pid.value


def _bundle_pid(bundle_id: str) -> int | None:
    import re
    import subprocess

    asn = subprocess.run(["lsappinfo", "find", f"bundleid={bundle_id}"], capture_output=True, text=True).stdout.split()
    if len(asn) != 1:
        return None
    info = subprocess.run(["lsappinfo", "info", "-only", "pid", asn[0]], capture_output=True, text=True).stdout
    match = re.search(r'"?pid"?\s*=\s*(\d+)', info)
    return int(match.group(1)) if match else None


def cmd_hit_test(args) -> int:
    """No input. Maps a grid of 1280x720 points through the patched unit's capture geometry and asks
    the accessibility hit test which process a click there would reach."""
    import re
    from maa.tasker import Tasker
    from maa.toolkit import Toolkit

    if _profile != "maanikki":
        sys.exit("hit-test reads the patched unit's capture geometry; pass --profile maanikki")
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(out)
    window_id = _pick_window(args, Toolkit)
    controller, ok, _ = _connect(window_id, raw=False)
    if not ok or _grab(controller)[0] is None:
        sys.exit("connection or capture failed")
    log = (out / "maafw.log").read_text(encoding="utf-8", errors="replace")
    target = [line for line in log.splitlines() if "capture target" in line][-1]
    value = lambda name: float(re.search(re.escape(f"[{name}=") + r"([-0-9.]+)\]", target).group(1))
    origin_x, origin_y = value("visible.origin.x"), value("visible.origin.y")
    width, height = value("visible.size.width"), value("visible.size.height")
    owner = next((w for w in Toolkit.find_desktop_windows() if int(w.hwnd) == window_id), None)
    game_pid = _bundle_pid(owner.class_name) if owner else None
    samples = {f"{x},{y}": (x, y) for x in (4, 46, 320, 640, 960, 1276) for y in (4, 31, 180, 360, 540, 716)}
    globals_ = {name: (origin_x + x * width / 1280, origin_y + y * height / 720) for name, (x, y) in samples.items()}
    hits = _ax_process_at(list(globals_.values()))
    report = {"front_pid": _front_pid(), "visible": [origin_x, origin_y, width, height], "target_pid": game_pid,
              "hits": {name: {"global": [round(c, 1) for c in globals_[name]], "pid": pid, "ax_error": error}
                       for name, (pid, error) in zip(globals_, hits)}}
    print(json.dumps(report, indent=2))
    return 0


def cmd_input_check(args) -> int:
    """Sends input to the game. Only with the maintainer at the machine; see the README.

    Each step runs only if the previous one did what it should, judged from captures; the first
    failure stops the run with a non-zero exit, leaving at most the big map open.
    """
    import cv2
    from maa.tasker import Tasker
    from maa.toolkit import Toolkit

    if _profile != "maanikki":
        sys.exit("input-check exercises the patched unit; pass --profile maanikki")
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(out)
    window_id = _pick_window(args, Toolkit)
    game_pid = _bundle_pid(GAME_BUNDLE_ID)
    # Wait for the operator to bring the game forward; never activate it from here.
    for _ in range(args.wait):
        if _front_pid() == game_pid:
            break
        time.sleep(1)
    else:
        sys.exit("the game never became the front application; nothing sent")
    controller, ok, _ = _connect(window_id, raw=False)
    if not ok:
        sys.exit("connection failed; nothing sent")
    steps = []

    def frame(name):
        image, _ = _grab(controller)
        if image is None or image.shape[:2] != (720, 1280):
            fail(f"no 1280x720 frame at {name}")
        cv2.imwrite(str(out / f"{len(steps):02d}-{name}.png"), image)
        return image

    def fail(reason):
        steps.append({"failed": reason})
        print(json.dumps(steps, ensure_ascii=False, indent=2))
        sys.exit(1)

    start = frame("start")
    if _map_back_score(start) >= MAP_CLOSED_SCORE:
        fail("the map back button is already visible; start in the open world")
    # Held ~80 ms: the game reads input once per frame and missed a 12 ms press on the device.
    sent = controller.post_key_down(KEY_M).wait().succeeded
    time.sleep(0.08)
    sent = controller.post_key_up(KEY_M).wait().succeeded and sent
    time.sleep(1.5)
    map_open = frame("after-key-m")
    steps.append({"step": "key M", "sent": sent, "changed_pct": _changed(start, map_open),
                  "map_back_score": _map_back_score(map_open)})
    # Only the map's own back button authorizes the click, not just a large change.
    if not sent or steps[-1]["map_back_score"] < MAP_OPEN_SCORE:
        fail("M was refused or the big map's back button is not where the click would go; not clicking")

    sent = controller.post_click(*MAP_BACK).wait().succeeded
    time.sleep(1.5)
    map_closed = frame("after-click-back")
    steps.append({"step": "click map back", "sent": sent, "changed_pct": _changed(map_open, map_closed),
                  "vs_start_pct": _changed(start, map_closed), "map_back_score": _map_back_score(map_closed)})
    if not sent or steps[-1]["map_back_score"] >= MAP_CLOSED_SCORE:
        fail("the click was refused or the map is still open")

    if args.release:
        # Walking moves the whole view; standing still changes the full frame by about 5% (idle
        # animation, grass, lighting; measured on the device), a camera turn by 55%. Judge on the full
        # frame against its own change between two still frames before W, and say when that change is
        # too large to tell walking from standing.
        still_a = frame("ambient-a")
        time.sleep(1.0)
        still_b = frame("ambient-b")
        ambient = _changed(still_a, still_b)
        if ambient > 20:
            fail(f"cannot judge the W release: the scene changed {ambient}% between two still frames before W")
        # Hold W, then destroy the controller without a key-up: the unit must release W itself.
        sent = controller.post_key_down(KEY_W).wait().succeeded
        time.sleep(0.6)
        del controller
        gc.collect()
        time.sleep(1.0)
        observer, ok, _ = _connect(window_id, raw=False)
        settled = _grab(observer)[0]
        time.sleep(1.0)
        later = _grab(observer)[0]
        if settled is None or later is None:
            fail("no frames after the destroy")
        cv2.imwrite(str(out / "release-settled.png"), settled)
        cv2.imwrite(str(out / "release-later.png"), later)
        limit = max(20.0, ambient * 2 + 5)
        steps.append({"step": "W held across destroy", "sent": sent, "ambient_changed_pct": ambient,
                      "scene_changed_pct": _changed(settled, later), "limit_pct": round(limit, 1)})
        if not sent or steps[-1]["scene_changed_pct"] > limit:
            fail("W was refused, or the whole view still moves after the destroy (W may be held)")
        # Below the limit only means no sustained movement was seen: W held against an obstacle may
        # barely change the frame. Signing off the release also needs the character to have moved
        # while W was held (the frames saved above) and to be still afterwards.
        steps[-1]["verdict"] = "no sustained movement observed (check the frames: moved while held, still after)"
    print(json.dumps(steps, ensure_ascii=False, indent=2))
    print(f"frames and maafw.log in {out}")
    return 0


KEY_OPTION = 58


def _cursor() -> tuple[float, float]:
    import ctypes

    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    cg = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    cg.CGEventCreate.restype = ctypes.c_void_p
    cg.CGEventCreate.argtypes = [ctypes.c_void_p]
    cg.CGEventGetLocation.restype = CGPoint
    cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    event = cg.CGEventCreate(None)
    point = cg.CGEventGetLocation(event)
    cf.CFRelease(event)
    return round(point.x, 1), round(point.y, 1)


def cmd_camera_check(args) -> int:
    """Turn the camera by a relative move of +PIXELS then -PIXELS, judged from captures.

    Sends only relative mouse moves (posted to the game process; the real cursor must not move) and,
    with --option, two presses of Option (the Alt binding that toggles the game's cursor mode) to
    see what the navigation engine's first move does on macOS. Spends nothing in the game.
    """
    import cv2
    from maa.tasker import Tasker
    from maa.toolkit import Toolkit

    if _profile != "maanikki":
        sys.exit("camera-check exercises the patched unit; pass --profile maanikki")
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(out)
    window_id = _pick_window(args, Toolkit)
    game_pid = _bundle_pid(GAME_BUNDLE_ID)
    for _ in range(args.wait):
        if _front_pid() == game_pid:
            break
        time.sleep(1)
    else:
        sys.exit("the game never became the front application; nothing sent")
    controller, ok, _ = _connect(window_id, raw=False)
    if not ok:
        sys.exit("connection failed; nothing sent")
    steps = []

    def frame(name):
        image, _ = _grab(controller)
        if image is None or image.shape[:2] != (720, 1280):
            fail(f"no 1280x720 frame at {name}")
        cv2.imwrite(str(out / f"{len(steps):02d}-{name}.png"), image)
        return image

    def fail(reason):
        steps.append({"failed": reason})
        print(json.dumps(steps, ensure_ascii=False, indent=2))
        sys.exit(1)

    # The scene above the HUD: what a camera turn changes and the character's idle animation barely does.
    band = (slice(120, 520), slice(200, 1080))
    start = frame("start")
    if _map_back_score(start) >= MAP_CLOSED_SCORE:
        fail("the big map is open; start in the open world")

    def option_press(name):
        before = frame(f"before-{name}")
        sent = controller.post_click_key(KEY_OPTION).wait().succeeded
        time.sleep(0.8)
        after = frame(f"after-{name}")
        steps.append({"step": name, "sent": sent, "changed_pct": _changed(before, after), "cursor": _cursor()})
        if not sent:
            fail(f"the unit refused {name} (game not in front?)")
        return after

    if args.option:
        # An experiment on the Option key, not the navigation order (navigation sends no Option on
        # macOS): Option, then a 1-pixel move to spend the move the game swallows after Option
        # (device, 2026-11-24), then the turn.
        option_press("option-before-turn")
        after_option = _cursor()  # Option moves the pointer to the window centre
        steps.append({"step": "cursor after Option", "cursor": after_option})
        if not controller.post_relative_move(1, 0).wait().succeeded:
            fail("the unit refused the absorbing move")
        time.sleep(0.4)
        absorbed = _cursor()
        steps.append({"step": "absorbing move +1", "cursor": absorbed,
                      "cursor_moved_by": (round(absorbed[0] - after_option[0], 1), round(absorbed[1] - after_option[1], 1))})
        if (abs(absorbed[0] - after_option[0]) > 1 or abs(absorbed[1] - after_option[1]) > 1) and not args.record_cursor:
            fail("the real cursor moved after the absorbing move: relative moves must not move it")
        start = frame("after-absorb")

    # Every step is judged before the next input is sent: the first anomaly stops the probe.
    baseline = _cursor()  # after Option, which moves the pointer to the window centre
    steps.append({"step": "cursor baseline", "cursor": baseline})
    previous = baseline
    for sign, name in ((1, f"relative move +{args.pixels}"), (-1, f"relative move -{args.pixels}")):
        before = start if sign > 0 else turned
        sent = controller.post_relative_move(sign * args.pixels, 0).wait().succeeded
        if not sent:
            fail(f"the unit refused {name} (game not in front?)")
        time.sleep(0.8)
        after = frame("after-turn" if sign > 0 else "after-turn-back")
        changed = _changed(before[band], after[band])
        cursor = _cursor()
        steps.append({"step": name, "sent": sent, "scene_changed_pct": changed, "cursor": cursor,
                      **({"vs_start_pct": _changed(start[band], after[band])} if sign < 0 else {})})
        # Reported per move; judged (strict mode) against the baseline, so small drifts cannot add up unnoticed.
        steps[-1]["cursor_moved_by"] = (round(cursor[0] - previous[0], 1), round(cursor[1] - previous[1], 1))
        previous = cursor
        if (abs(cursor[0] - baseline[0]) > 1 or abs(cursor[1] - baseline[1]) > 1) and not args.record_cursor:
            fail(f"the real cursor moved after {name}: relative moves must not move it")
        if changed < 1:
            fail(f"{name} did not change the scene: the camera did not turn")
        if sign > 0:
            turned = after
    if args.option:
        option_press("option-after-turn")  # back to the cursor mode the game started in
    # Both turns changed the scene; whether they are clean horizontal turns and the second comes back
    # is judged from the saved frames, not from these numbers.
    steps.append({"verdict": "needs_review", "frames": str(out)})
    print(json.dumps(steps, ensure_ascii=False, indent=2))
    print(f"needs review: look at the frames in {out}. The turn passes when +{args.pixels} turns the camera "
          f"horizontally and -{args.pixels} brings the view back near the start.")
    return 0


# The agent's own key sequences, in macOS key codes (Control 59, W 13, Space 49, Q 12):
# face-camera is ForegroundInput.face_camera() (Ctrl held, W pulsed 80 ms, W released before Ctrl);
# starsea-fall is resource/routes/macros/starsea_fall.json without its leading 1 s wait.
KEY_SEQUENCES = {
    "face-camera": (("down", 59), ("down", 13), ("wait", 0.08), ("up", 13), ("up", 59)),
    "starsea-fall": (("down", 49), ("wait", 0.1587), ("up", 49), ("wait", 0.2323), ("down", 12), ("wait", 0.1004),
                     ("up", 12)),
}
# How each sequence's waits are timed, as the agent times them: face_camera() starts its 80 ms clock before
# submitting W (submission counts toward the pulse); the route macro runner waits the full step after the
# press has returned.
WAIT_INCLUDES_SUBMISSION = {"face-camera": True, "starsea-fall": False}
SEQUENCE_EXPECTED = {
    "face-camera": "The character should turn to face the camera's direction, moving at most a step.",
    "starsea-fall": "The character should jump and then plunge (急坠) straight down.",
}


def cmd_key_sequence_check(args) -> int:
    """Send one of the agent's key sequences to the game, judged from captures and the operator's eyes.

    Keys only (posted to the game process); every key pressed is released whatever happens, and an
    unconfirmed release asks inactive(). Spends nothing in the game.
    """
    import cv2
    from maa.tasker import Tasker
    from maa.toolkit import Toolkit

    if _profile != "maanikki":
        sys.exit("key-sequence-check exercises the patched unit; pass --profile maanikki")
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(out)
    window_id = _pick_window(args, Toolkit)
    game_pid = _bundle_pid(GAME_BUNDLE_ID)
    for _ in range(args.wait):
        if _front_pid() == game_pid:
            break
        time.sleep(1)
    else:
        sys.exit("the game never became the front application; nothing sent")
    controller, ok, _ = _connect(window_id, raw=False)
    if not ok:
        sys.exit("connection failed; nothing sent")
    steps = []

    def frame(name):
        image, _ = _grab(controller)
        if image is None or image.shape[:2] != (720, 1280):
            fail(f"no 1280x720 frame at {name}")
        cv2.imwrite(str(out / f"{len(steps):02d}-{name}.png"), image)
        return image

    def fail(reason):
        steps.append({"failed": reason})
        print(json.dumps(steps, ensure_ascii=False, indent=2))
        sys.exit(1)

    start = frame("start")
    if _map_back_score(start) >= MAP_CLOSED_SCORE:
        fail("the big map is open; start in the open world")
    cursor = _cursor()
    time.sleep(1.0)
    still = frame("still")
    steps.append({"step": "still frames", "frame_changed_pct": _changed(start, still), "cursor": cursor})
    held = []
    began = time.perf_counter()
    down_started = None
    try:
        for kind, value in KEY_SEQUENCES[args.sequence]:
            at = round(time.perf_counter() - began, 3)
            if kind == "wait":
                # Timed as the agent times this sequence (WAIT_INCLUDES_SUBMISSION).
                from_submission = WAIT_INCLUDES_SUBMISSION[args.sequence] and down_started is not None
                origin = down_started if from_submission else time.perf_counter()
                time.sleep(max(0.0, value - (time.perf_counter() - origin)))
                down_started = None
                continue
            if kind == "down":
                held.append(value)  # counted before submitting: once submitted it may have reached the unit
                down_started = time.perf_counter()
                sent = controller.post_key_down(value).wait().succeeded
            else:
                down_started = None
                # Forgotten only once confirmed: a refused or interrupted release is retried by the cleanup.
                sent = controller.post_key_up(value).wait().succeeded
                if sent:
                    held.remove(value)
            steps.append({"step": f"key {kind} {value}", "at_s": at, "sent": sent})
            if not sent:
                fail(f"the unit refused key {kind} {value}")
    finally:
        # Release every key still held, last pressed first, whatever stopped the sequence; only then, if any
        # release was not confirmed, ask inactive() and stop.
        unconfirmed = []
        for key in reversed(held):
            try:
                released = controller.post_key_up(key).wait().succeeded
            except BaseException:
                released = False
            steps.append({"step": f"cleanup key up {key}", "released": released})
            if not released:
                unconfirmed.append(key)
        if unconfirmed:
            try:
                inactive = controller.post_inactive().wait().succeeded
            except BaseException:
                inactive = False
            steps.append({"step": "inactive", "succeeded": inactive})
            fail(f"the release of key(s) {unconfirmed} was not confirmed; inactive() was asked to release them "
                 f"({'confirmed' if inactive else 'NOT confirmed'})")
    if args.sequence == "face-camera":
        # The agent judges the alignment 0.4 s after releasing Ctrl.
        time.sleep(0.4)
        settled = frame("settled")
        steps.append({"step": "settled (0.4 s)", "frame_changed_pct": _changed(still, settled)})
    time.sleep(1.0)
    after = frame("after")
    steps.append({"step": "after", "frame_changed_pct": _changed(still, after), "cursor": _cursor()})
    steps.append({"verdict": "needs_review", "frames": str(out)})
    print(json.dumps(steps, ensure_ascii=False, indent=2))
    print(f"needs review: frames in {out}. {SEQUENCE_EXPECTED[args.sequence]}")
    return 0


def cmd_timing(args) -> int:
    """Measure how long short sleeps really take in this process; sends nothing, needs no game or runtime.

    With --activity, declares an NSProcessInfo activity for the measurement (the candidate fix for the
    stretched short sleeps seen while the game is in front).
    """
    import ctypes
    import subprocess

    token = info = None
    if args.activity != "none":
        objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        objc.objc_getClass.restype, objc.objc_getClass.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
        objc.sel_registerName.restype, objc.sel_registerName.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]

        def message(restype, *argtypes):
            return ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(("objc_msgSend", objc))
        info = message(ctypes.c_void_p)(objc.objc_getClass(b"NSProcessInfo"), objc.sel_registerName(b"processInfo"))
        options = 0x00FFFFFF & ~(1 << 20)  # NSActivityUserInitiatedAllowingIdleSystemSleep
        if args.activity == "latency-critical":
            options |= 0xFF00000000  # NSActivityLatencyCritical
        cf.CFRelease.argtypes, cf.CFRelease.restype = [ctypes.c_void_p], None
        reason = cf.CFStringCreateWithCString(None, b"MaaNikki timing probe", 0x08000100)
        token = message(ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p)(
            info, objc.sel_registerName(b"beginActivityWithOptions:reason:"), options, reason)
        cf.CFRelease(reason)  # the activity keeps its own reference
        if not token:
            sys.exit("NSProcessInfo returned no activity; nothing measured")

    def hid_idle():
        hid = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True).stdout
        idle = next((int(line.split()[-1]) / 1e9 for line in hid.splitlines() if "HIDIdleTime" in line), None)
        return None if idle is None else round(idle, 1)

    front_start, idle_start = _front_bundle_id(), hid_idle()
    try:
        samples = []
        for _ in range(args.samples):
            started = time.perf_counter()
            time.sleep(args.request_ms / 1000)
            samples.append((time.perf_counter() - started) * 1000)
    finally:
        if token:
            message(None, ctypes.c_void_p)(info, objc.sel_registerName(b"endActivity:"), token)
    samples.sort()

    def percentile(p):  # nearest rank: the smallest sample with at least p% of the samples at or below it
        return round(samples[max(0, -(-len(samples) * p // 100) - 1)], 1)
    print(json.dumps({"request_ms": args.request_ms, "activity": args.activity, "samples": args.samples,
                      "p50_ms": percentile(50), "p95_ms": percentile(95), "max_ms": round(samples[-1], 1),
                      "front_bundle": [front_start, _front_bundle_id()], "hid_idle_s": [idle_start, hid_idle()]}))
    return 0


def _agent_ui_template():
    """The agent's nikki.ui_template recognition class (MaaNikki_MainDetected uses it), loaded without the
    agent's registration module: that imports maa.agent.agent_server, which switches the binding into
    agent-server mode where this process can no longer create its own Resource."""
    import types
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))
    stand_in = types.ModuleType("registration")
    stand_in.agent_recognition = lambda name: (lambda cls: cls)
    saved = sys.modules.get("registration")
    sys.modules["registration"] = stand_in
    try:
        from recognition.ui_template import UITemplateRecognition
    finally:
        if saved is None:
            sys.modules.pop("registration", None)
        else:
            sys.modules["registration"] = saved
    return UITemplateRecognition


class _UiSession:
    """Shared setup of the UI probes (camera-check, text-input-check, hover-check): wait for the game to be in
    front (never activate it), connect, save numbered frames, and recognise with the agent's own pipeline
    nodes from resource/ on fresh frames (OCR and templates; the agent's custom recognitions are not
    registered, so nodes using them cannot be asked)."""

    def __init__(self, args):
        import cv2
        from maa.tasker import Tasker
        from maa.toolkit import Toolkit

        self.cv2 = cv2
        if _profile != "maanikki":
            sys.exit(f"{args.command} exercises the patched unit; pass --profile maanikki")
        self.out = Path(args.out).resolve()
        if self.out.exists() and any(self.out.iterdir()):
            sys.exit(f"{self.out} is not empty")
        self.out.mkdir(parents=True, exist_ok=True)
        Tasker.set_log_dir(self.out)
        window_id = _pick_window(args, Toolkit)
        game_pid = _bundle_pid(GAME_BUNDLE_ID)
        for _ in range(args.wait):
            if _front_pid() == game_pid:
                break
            time.sleep(1)
        else:
            sys.exit("the game never became the front application; nothing sent")
        self.controller, ok, _ = _connect(window_id, raw=False)
        if not ok:
            sys.exit("connection failed; nothing sent")
        self.steps = []
        self.image = None
        self.hits = {}
        self.jobs = {}
        self._bind_resource()

    def _bind_resource(self):
        """Recognition runs on its own in-memory controller: a Tasker calls inactive() on its controller after
        every task, which on the game's controller would release contacts and put the cursor back."""
        import numpy as np
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        UITemplateRecognition = _agent_ui_template()
        session = self

        class FrameOnly(CustomController):
            """Serves the frame being judged; every input is a no-op that touches nothing."""
            def connect(self): return True
            def request_uuid(self): return "maanikki-probe-recognition"
            def screencap(self):
                return session.image if session.image is not None else np.zeros((720, 1280, 3), np.uint8)
            def click(self, *a): return True
            def swipe(self, *a): return True
            def touch_down(self, *a): return True
            def touch_move(self, *a): return True
            def touch_up(self, *a): return True
            def click_key(self, *a): return True
            def input_text(self, *a): return True
            def key_down(self, *a): return True
            def key_up(self, *a): return True
            def start_app(self, *a): return True
            def stop_app(self, *a): return True
            def scroll(self, *a): return True

        class Recognise(CustomAction):
            def run(self, context, argv):
                param = json.loads(argv.custom_action_param)
                detail = context.run_recognition(param["node"], session.image, param.get("override") or {})
                session.hits[param["node"]] = detail
                session.jobs[param["node"]] = True
                return True
        self.resource = Resource()
        if not self.resource.post_bundle(str(RESOURCE)).wait().succeeded:
            sys.exit("could not load resource/; nothing sent")
        self.resource.register_custom_action("ProbeRecognise", Recognise())
        self.resource.register_custom_recognition("nikki.ui_template", UITemplateRecognition())
        self.frame_only = FrameOnly()
        if not self.frame_only.post_connection().wait().succeeded:
            sys.exit("could not set up the recognition controller; nothing sent")
        self.tasker = Tasker()
        self.tasker.bind(self.resource, self.frame_only)
        if not self.tasker.inited:
            sys.exit("could not bind the resource for recognition; nothing sent")

    def frame(self, name):
        image, _ = _grab(self.controller)
        if image is None or image.shape[:2] != (720, 1280):
            self.fail(f"no 1280x720 frame at {name}")
        self.cv2.imwrite(str(self.out / f"{len(self.steps):02d}-{name}.png"), image)
        return image

    def fail(self, reason):
        self.steps.append({"failed": reason})
        print(json.dumps(self.steps, ensure_ascii=False, indent=2))
        sys.exit(1)

    def detail(self, node, image, override=None):
        """The recognition detail of an agent pipeline node (or an override node) on this image; stops the
        probe when the recognition itself could not run (that is not a miss)."""
        self.image = image
        self.hits.pop(node, None)
        self.jobs.pop(node, None)
        job = self.tasker.post_task("ProbeRecognise", {"ProbeRecognise": {
            "action": "Custom", "custom_action": "ProbeRecognise",
            "custom_action_param": {"node": node, "override": override or {}}}}).wait()
        # A genuine miss is a detail with hit=False; None (or a failed job) means it never ran.
        if not job.succeeded or not self.jobs.get(node) or self.hits.get(node) is None:
            self.fail(f"the recognition of {node} could not run")
        return self.hits.get(node)

    def hit(self, node, image, override=None):
        found = self.detail(node, image, override)
        return bool(found and found.hit)

    def text(self, roi, image):
        """OCR text inside roi in reading order (top to bottom, left to right), spaces removed; "" when nothing
        is read."""
        found = self.detail("ProbeReadText", image, {"ProbeReadText": {"recognition": "OCR", "roi": roi}})
        results = list(getattr(found, "filtered_results", None) or [])
        results.sort(key=lambda item: (getattr(item, "box", (0, 0))[1], getattr(item, "box", (0, 0))[0]))
        return "".join(getattr(item, "text", "") for item in results).replace(" ", "")

    def wait_for(self, name, test, seconds):
        for attempt in range(int(seconds / 0.3) + 1):  # a fixed number of looks, 0.3 s apart
            image = self.frame(f"{name}-{attempt}" if attempt else name)
            if test(image):
                return image
            time.sleep(0.3)
        return None

    def release(self, post, what):
        try:
            released = post().wait().succeeded
        except BaseException:
            released = False
        if released:
            return
        self.steps.append({"step": f"release of {what} not confirmed"})
        try:
            inactive = self.controller.post_inactive().wait().succeeded
        except BaseException:
            inactive = False
        self.steps.append({"step": "inactive", "succeeded": inactive})
        self.fail(f"the release of {what} was not confirmed; inactive() was asked to release it")

    def tap(self, code, what):
        """An 80 ms key tap like the agent's ClickKey; the release is asked for whatever happens."""
        sent = False
        try:
            sent = self.controller.post_key_down(code).wait().succeeded
            time.sleep(0.08)
        finally:
            self.release(lambda: self.controller.post_key_up(code), what)
        self.steps.append({"step": f"tap {what}", "sent": sent})
        if not sent:
            self.fail(f"the unit refused {what}")

    def click(self, point, what):
        """A UI click: the unit hovers 0.3 s, presses, releases and puts the cursor back. A click that fails or
        is interrupted may have left the press held (the core sends down and up): ask inactive()."""
        sent = False
        try:
            sent = self.controller.post_click(*point).wait().succeeded
        finally:
            self.steps.append({"step": f"click {what}", "point": list(point), "sent": sent})
            if not sent:
                try:
                    inactive = self.controller.post_inactive().wait().succeeded
                except BaseException:
                    inactive = False
                self.steps.append({"step": "inactive", "succeeded": inactive})
                self.fail(f"the click on {what} failed; inactive() was asked to release it "
                          f"({'confirmed' if inactive else 'NOT confirmed'})")

    def done(self, message):
        self.steps.append({"verdict": "needs_review", "frames": str(self.out)})
        print(json.dumps(self.steps, ensure_ascii=False, indent=2))
        print(f"needs review: frames in {self.out}. {message}")
        return 0


KEY_P, KEY_ESC = 35, 53  # kVK_ANSI_P, kVK_Escape (the photo and menu bindings' defaults)


def cmd_photo_mode_check(args) -> int:
    """Open the camera with P and leave it with Esc; never the shutter. Spends nothing.

    P is sent only in the open world (big map closed); Esc only after the agent's own camera landmark
    (MaaNikki_Daily_PhotoReady: the 大喵相机 title or the mode tab) is seen. Anything else stops the probe
    with no further input.
    """
    ui = _UiSession(args)
    start = ui.frame("start")
    if not ui.hit("MaaNikki_MainDetected", start) or ui.hit("MaaNikki_Daily_PhotoReady", start):
        ui.fail("the open-world HUD (MaaNikki_MainDetected) is not recognised, or the camera already looks open; "
                "start in the open world. Nothing sent")
    ui.tap(KEY_P, "P (camera)")
    opened = ui.wait_for("camera", lambda image: ui.hit("MaaNikki_Daily_PhotoReady", image), 5)
    ui.steps.append({"step": "camera landmark", "seen": opened is not None})
    if opened is None:
        ui.fail("the camera landmark never appeared after P; Esc not sent (look at the last frame)")
    ui.tap(KEY_ESC, "Esc (leave the camera)")
    closed = ui.wait_for("after-esc", lambda image: not ui.hit("MaaNikki_Daily_PhotoReady", image)
                         and ui.hit("MaaNikki_MainDetected", image), 5)
    ui.steps.append({"step": "camera closed", "seen": closed is not None})
    if closed is None:
        ui.fail("the open-world HUD did not come back after Esc; nothing more sent")
    return ui.done("P should open the camera (大喵相机) and Esc should bring back the open world. "
                   "The shutter, saving and deleting photos are not exercised.")


COMPENDIUM_SEARCH_BOX = [850, 20, 380, 48]  # agent/wardrobe/scanner.py SEARCH_BOX
TEXT_DEFAULT = "x7Ab"  # not starting with A: keycode 0 is the A key, so a lone "A" told nothing (G2, 2026-11-24)


def cmd_text_input_check(args) -> int:
    """Type one --text into the 博物图鉴 检索 field with the unit's input_text and read it back. Spends nothing.

    The operator opens 博物图鉴 → 检索 with an empty field (and empties it again between runs: one trial per
    run, so a clear button that misses cannot hide the next result). The probe clicks the field, posts the
    text one character per request like the agent on macOS (no Enter, no clipboard, nothing submitted) and
    reads the field back by OCR. Stops at the first refusal or unexpected page.
    """
    ui = _UiSession(args)
    start = ui.frame("start")
    if not ui.hit("MaaNikki_CompendiumSearchReady", start):
        ui.fail("the 博物图鉴 检索 page is not recognised (MaaNikki_CompendiumSearchReady); nothing sent")
    box = COMPENDIUM_SEARCH_BOX
    centre = (box[0] + box[2] // 2, box[1] + box[3] // 2)
    before = ui.text(box, start)
    placeholder = args.placeholder or ""
    ui.steps.append({"step": "field before", "text": before, "placeholder": placeholder})
    if before not in ("", placeholder):
        ui.fail(f"the search field does not read as empty ({before!r}); clear it first, or pass the exact empty-field "
                "reading from a capture as --placeholder. Nothing sent")

    def on_search_page(name):
        image = ui.frame(name)
        if not ui.hit("MaaNikki_CompendiumSearchReady", image):
            ui.fail(f"the search page is no longer recognised ({name}); nothing more sent")
        return image

    wanted = args.text
    ui.click(centre, "the search field")
    time.sleep(0.3)
    on_search_page("focused")  # verified on this frame before any text is posted
    sent = True
    for index, character in enumerate(wanted):
        if index:
            time.sleep(0.03)
        sent = ui.controller.post_input_text(character).wait().succeeded
        if not sent:
            break
    time.sleep(1.0)
    typed = on_search_page("typed")
    read = ui.text(box, typed)
    ui.steps.append({"step": f"input_text {wanted!r} (one character per request)", "sent": sent, "field_reads": read,
                     "exact_match": read == wanted,
                     **({} if read == wanted else {"ocr_inconclusive": "judge the typed frame by eye"})})
    if not sent:
        ui.fail("the unit refused input_text")
    return ui.done(f"The typed frame should show exactly {wanted!r} in the search field; the OCR reading is listed "
                   "above. The search may filter the list; nothing is submitted or saved. Empty the field by hand "
                   "before the next run.")


HOVER_PARK = (900, 32)  # agent/collection/locking.py park point on the 祝福闪光 warehouse


def cmd_hover_check(args) -> int:
    """Hover a card on the 祝福闪光 warehouse without pressing, then park and put the cursor back. Spends nothing.

    The operator opens the warehouse and selects a card; --point is a point on that card (1280x720) read from
    a capture beforehand. Only bare touch moves (the unit's hover) and the end-of-task cleanup are sent.
    """
    ui = _UiSession(args)
    start = ui.frame("start")
    if not ui.hit("MaaNikki_Blessing_InventoryReady", start):
        ui.fail("the 祝福闪光 warehouse is not recognised (MaaNikki_Blessing_InventoryReady); nothing sent")
    original = _cursor()
    ui.steps.append({"step": "cursor before", "cursor": original})
    failure = None
    try:
        for name, point in (("parked", HOVER_PARK), ("hovered", tuple(args.point)), ("parked-again", HOVER_PARK)):
            moved = ui.controller.post_touch_move(contact=0, x=point[0], y=point[1], pressure=0).wait().succeeded
            time.sleep(0.6)
            image, _ = _grab(ui.controller)  # not ui.frame(): a capture failure must not skip the report below
            saved = bool(image is not None and image.shape[:2] == (720, 1280)
                         and ui.cv2.imwrite(str(ui.out / f"{len(ui.steps):02d}-{name}.png"), image))
            ui.steps.append({"step": f"hover {name}", "point": list(point), "sent": moved, "cursor": _cursor(),
                             "frame_saved": saved})
            if not moved:
                failure = f"the unit refused the hover to {name} (see maafw.log)"
                break
            if not saved:
                failure = f"no 1280x720 frame saved after the hover to {name}"
                break
    except BaseException as error:  # an interrupt: still restore and report before exiting
        failure = f"interrupted during the hovers ({type(error).__name__})"
    finally:
        # The end-of-task cleanup the core sends: the unit puts the user's cursor back.
        try:
            restored = ui.controller.post_inactive().wait().succeeded
        except BaseException:
            restored = False
        after = _cursor()
        back = abs(after[0] - original[0]) <= 1 and abs(after[1] - original[1]) <= 1
        ui.steps.append({"step": "inactive (cursor back)", "succeeded": restored, "cursor": after,
                         "back_where_it_was": back})
    problems = [p for p in (failure, None if restored else "inactive() was not confirmed",
                            None if back else "the cursor did not come back where it was") if p]
    if problems:
        ui.fail("; ".join(problems))
    return ui.done("The hovered frame should show the selected card's tooltip and both parked frames none; "
                   "the cursor should end where it started.")


SCENE_PRESS_PRESSURE = 0x4D4E5343  # HybridInput::kScenePressPressure: press where the cursor is, no hover
SCENE_BUTTONS = {"left": 0, "right": 1, "middle": 2}
SCENE_BUTTON_EXPECTED = {
    "middle": "The camera should swing back behind the character.",
    "left": "The character should purify (净化) once per press; nothing else should be hit.",
    "right": "The selected ability (the HUD button, bottom right) should act once per press, or the game "
             "should say there is nothing to use it on.",
}
HOLD_RANGE_S = (0.05, 3.0)  # 0.05: the agent's click_mouse; 3.0: well past any task's scene-button hold
HELD_FRAME_MIN_S, HELD_FRAME_AT_S = 0.4, 0.25  # holds this long also save a frame while the button is down
REPEAT_MAX, REPEAT_GAP_S = 8, 0.15  # fishing reels in with presses about this far apart
FORWARD_LEAD_S, FORWARD_TAIL_S = 0.4, 0.2  # W held this long before the first press and after the last


# The HUD ability button (bottom right) and the abilities the agent recognises there: the same pipeline
# nodes and matching as agent/navigation/ability.py ability_matches(), read from the repository so the
# probe cannot drift from the agent.
RESOURCE = Path(__file__).resolve().parents[2] / "resource"
ABILITY_NODES = {
    "insect": ("navigation.json", "MaaNikki_Navigation_InsectAbility"),
    "fishing": ("fishing.json", "MaaNikki_Navigation_FishingAbility"),
    "rhythms": ("fishing.json", "MaaNikki_Navigation_RhythmsAbility"),
    "star_collect": ("home.json", "MaaNikki_Navigation_StarCollectAbility"),
    "gingerbread": ("route_studio.json", "MaaNikki_Navigation_GingerbreadAbility"),
}
ABILITY_MATCH = 0.78  # ability_matches()
HUD_CROP = (slice(600, 720), slice(1000, 1280))  # the ability and jump buttons, saved for review
# macOS key codes of 1-8 (Carbon kVK_ANSI_1..8): the game's default 切换能力 keys.
SLOT_KEYS = {1: 0x12, 2: 0x13, 3: 0x14, 4: 0x15, 5: 0x17, 6: 0x16, 7: 0x1A, 8: 0x1C}


def _ability_scores(image) -> dict[str, float]:
    """Match score of each known ability against the HUD ability button, as ability_matches() computes it."""
    import cv2
    import numpy as np

    def white(value, low, high):
        return cv2.inRange(cv2.cvtColor(value, cv2.COLOR_BGR2HSV), np.array(low), np.array(high))

    scores = {}
    for name, (pipeline, node) in ABILITY_NODES.items():
        definition = json.loads((RESOURCE / "pipeline" / pipeline).read_text(encoding="utf-8"))[node]
        template = cv2.imread(str(RESOURCE / "image" / definition["template"]))
        scale = .73 * 2 / 3  # game/ templates are 1080p assets
        icon = cv2.resize(white(template, [0, 0, 230], [180, 60, 255]), None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_LINEAR)
        x, y, w, h = definition["roi"]
        patch = white(image[y:y + h, x:x + w], [0, 0, 200], [180, 60, 255])
        bx, by, bw, bh = cv2.boundingRect(icon)
        if not bw or not bh or patch.shape[0] < bh or patch.shape[1] < bw:
            scores[name] = 0.0
            continue
        scores[name] = float(cv2.matchTemplate(patch, icon[by:by + bh, bx:bx + bw], cv2.TM_CCORR_NORMED).max())
    return scores


def _selected_ability(image) -> tuple[str | None, dict[str, float]]:
    """The ability the HUD shows (None when no known one matches), with every score rounded for the report.

    Compared unrounded, as ability_matches() does: a raw 0.7804 matches there and must match here.
    """
    scores = _ability_scores(image)
    best = max(scores, key=scores.get)
    return (best if scores[best] > ABILITY_MATCH else None), {name: round(value, 3) for name, value in scores.items()}


def _hold_seconds(text: str) -> float:
    value = float(text)
    if not HOLD_RANGE_S[0] <= value <= HOLD_RANGE_S[1]:
        raise argparse.ArgumentTypeError(f"--hold must be within {HOLD_RANGE_S[0]}-{HOLD_RANGE_S[1]} s")
    return value


def _game_window_bounds(window_id: int):
    """(x, y, width, height) of the connected window in global points, from the agent's Quartz helper."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))
    from host.macos import quartz
    window = quartz.window(window_id)
    return window.bounds if window else None


def cmd_scene_button_check(args) -> int:
    """Press a scene mouse button where the cursor already is, judged from captures.

    Verifies the patched unit's scene press (touch_down with kScenePressPressure: no hover move,
    which would turn the camera) on the game without any outfit. Middle: turns the camera away with
    one relative move (verified on the device), then the press recentres it behind the character.
    Left (净化) and right (the selected ability) are pressed without a turn. --hold, --repeat and
    --with-forward (W held across the presses) cover how tasks use them. Spends nothing in the game
    as long as nothing is in reach of the left or right button's action (the operator checks).
    """
    import cv2
    from maa.tasker import Tasker
    from maa.toolkit import Toolkit

    if _profile != "maanikki":
        sys.exit("scene-button-check exercises the patched unit; pass --profile maanikki")
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(out)
    window_id = _pick_window(args, Toolkit)
    game_pid = _bundle_pid(GAME_BUNDLE_ID)
    for _ in range(args.wait):
        if _front_pid() == game_pid:
            break
        time.sleep(1)
    else:
        sys.exit("the game never became the front application; nothing sent")
    controller, ok, _ = _connect(window_id, raw=False)
    if not ok:
        sys.exit("connection failed; nothing sent")
    steps = []

    def frame(name):
        image, _ = _grab(controller)
        if image is None or image.shape[:2] != (720, 1280):
            fail(f"no 1280x720 frame at {name}")
        cv2.imwrite(str(out / f"{len(steps):02d}-{name}.png"), image)
        return image

    def fail(reason):
        steps.append({"failed": reason})
        print(json.dumps(steps, ensure_ascii=False, indent=2))
        sys.exit(1)

    # Only a unit from patch level 3 knows the scene press; an older one would hover-move and press.
    try:
        patch = json.loads(controller.info).get("maanikki_patch") if isinstance(controller.info, str) else controller.info.get("maanikki_patch")
    except (ValueError, AttributeError, TypeError):
        patch = None
    steps.append({"step": "unit", "maanikki_patch": patch})
    if type(patch) is not int or patch < 3:
        fail("the connected unit does not report maanikki_patch >= 3; nothing sent")
    band = (slice(120, 520), slice(200, 1080))
    start = frame("start")
    if _map_back_score(start) >= MAP_CLOSED_SCORE:
        fail("the big map is open; start in the open world")
    cursor = _cursor()
    bounds = _game_window_bounds(window_id)
    steps.append({"step": "cursor baseline", "cursor": cursor, "window_bounds": bounds})
    if bounds is None or not (bounds[0] <= cursor[0] < bounds[0] + bounds[2] and bounds[1] <= cursor[1] < bounds[1] + bounds[3]):
        fail("the pointer is not over the game window; the unit would refuse a scene press. Nothing sent")

    def release(post, what):
        """Ask for a release. Anything but a confirmed one (refused, an error, an interrupt while waiting)
        asks inactive() to release everything and stops the probe."""
        try:
            released = post().wait().succeeded
        except BaseException:
            released = False
        if released:
            return
        steps.append({"step": f"release of {what} not confirmed"})
        try:
            inactive = controller.post_inactive().wait().succeeded
        except BaseException:
            inactive = False
        steps.append({"step": "inactive", "succeeded": inactive})
        fail(f"the release of {what} was not confirmed; inactive() was asked to release it "
             "(the unit also retries when the controller is destroyed, which is not guaranteed to succeed)")

    def tap_key(code, what):
        """Press and release a key like the agent's 80 ms taps; the release is asked for whatever happens."""
        sent = False
        try:
            # Once submitted, the press may have reached the unit even if waiting for it is interrupted.
            sent = controller.post_key_down(code).wait().succeeded
            time.sleep(0.08)  # the game missed a 12 ms press on the device
        finally:
            release(lambda: controller.post_key_up(code), what)
        return sent

    if args.button == "right":
        # The right button uses whatever ability is selected: confirm it is the expected one first, and
        # select it with its slot key if asked. Anything else: nothing pressed.
        ability, scores = _selected_ability(start)
        cv2.imwrite(str(out / "hud-before.png"), start[HUD_CROP])  # what the reading saw
        steps.append({"step": "selected ability", "ability": ability, "scores": scores})
        if ability != args.expect_ability and args.select_slot is not None:
            sent = tap_key(SLOT_KEYS[args.select_slot], f"slot key {args.select_slot}")
            time.sleep(1.0)
            start = frame("after-select")
            ability, scores = _selected_ability(start)
            cv2.imwrite(str(out / "hud-after-select.png"), start[HUD_CROP])
            steps.append({"step": f"slot key {args.select_slot}", "sent": sent, "ability": ability, "scores": scores})
            if not sent:
                fail(f"the unit refused the slot key {args.select_slot}; no button pressed")
        if ability != args.expect_ability:
            fail(f"the HUD shows {ability or 'no known ability'}, not {args.expect_ability}; no button pressed"
                 + ("" if args.select_slot is not None else " (pass --select-slot N to select it)"))
    # The band's own change between two still frames: the idle animation alone moved it 6% on the
    # device, a camera turn 55-66%. The middle button is pressed only after a clear turn.
    time.sleep(1.0)
    still = frame("still")
    if args.button == "right":
        # The last frame before the press: the ability must still be the expected one.
        ability, scores = _selected_ability(still)
        steps.append({"step": "selected ability before the press", "ability": ability, "scores": scores})
        if ability != args.expect_ability:
            fail(f"the HUD now shows {ability or 'no known ability'}, not {args.expect_ability}; no button pressed")
    ambient = _changed(start[band], still[band])
    turn_limit = max(20.0, ambient * 3 + 5)
    if turn_limit >= 100:
        # No change can exceed the limit: a press could never be judged, so do not send one.
        steps.append({"step": "still frames", "ambient_changed_pct": ambient, "change_limit_pct": round(turn_limit, 1)})
        fail("the scene changes too much while standing still to judge a turn or a recentre; no scene button pressed")
    # Only the middle button needs the camera turned away first (so that a recentre has something to
    # undo). Left (净化) and right (the selected ability) act on their own; their result is judged
    # from the saved frames.
    middle = args.button == "middle"
    if args.no_turn or not middle:
        turned = still
        steps.append({"step": "no turn", "ambient_changed_pct": ambient, "change_limit_pct": round(turn_limit, 1)})
    elif not controller.post_relative_move(args.pixels, 0).wait().succeeded:
        fail("the unit refused the relative move")
    else:
        time.sleep(0.8)
        turned = frame("turned-away")
        turn_pct = _changed(still[band], turned[band])
        steps.append({"step": f"relative move +{args.pixels}", "ambient_changed_pct": ambient, "scene_changed_pct": turn_pct,
                      "turn_limit_pct": round(turn_limit, 1), "cursor": _cursor()})
        if turn_pct <= turn_limit:
            fail("no clear camera turn (no more change than standing still); no button pressed")
        if abs(steps[-1]["cursor"][0] - cursor[0]) > 1 or abs(steps[-1]["cursor"][1] - cursor[1]) > 1:
            fail("the real cursor moved with the relative move; no button pressed")
    button = SCENE_BUTTONS[args.button]

    def press_once(index):
        pressed = False
        held_frame = None
        held_s = None
        try:
            # The binding's order is (x, y, contact, pressure): name them. Once submitted, the press may have
            # reached the unit even if waiting for it is interrupted.
            pressed = controller.post_touch_down(x=0, y=0, contact=button, pressure=SCENE_PRESS_PRESSURE).wait().succeeded
            down_at = time.perf_counter()
            if pressed and args.hold >= HELD_FRAME_MIN_S:
                # A capture sends no input; it shows what the game does while the button is down. Capturing
                # and saving take time, so the hold is measured, not assumed.
                time.sleep(HELD_FRAME_AT_S)
                held_frame = frame(f"held-{index}")
            time.sleep(max(0.0, args.hold - (time.perf_counter() - down_at)))
            held_s = round(time.perf_counter() - down_at, 3)
        finally:
            # Whatever happened since the press was submitted (an interrupt, a failure), ask for the release.
            release(lambda: controller.post_touch_up(contact=button), f"the {args.button} button")
        steps.append({"step": f"scene press {args.button} #{index}", "pressed": pressed, "released": True,
                      "hold_s": args.hold, "measured_hold_s": held_s,
                      **({"held_changed_pct": _changed(turned, held_frame)} if held_frame is not None else {})})
        if not pressed:
            fail("the unit refused the scene press (see maafw.log for the reason)")

    forward_sent = False
    try:
        if args.with_forward:
            # W first, so the press lands while the character is already walking (insect catching holds
            # the capture button while moving). Released in finally, before anything else can fail.
            forward_sent = controller.post_key_down(KEY_W).wait().succeeded
            if not forward_sent:
                fail("the unit refused W; no button pressed")
            time.sleep(FORWARD_LEAD_S)
            steps.append({"step": "W down", "lead_s": FORWARD_LEAD_S})
        for index in range(1, args.repeat + 1):
            press_once(index)
            if index < args.repeat:
                time.sleep(REPEAT_GAP_S)
        if args.with_forward:
            time.sleep(FORWARD_TAIL_S)
    finally:
        if args.with_forward:
            release(lambda: controller.post_key_up(KEY_W), "W")
            steps.append({"step": "W up", "released": True})
    time.sleep(1.0)
    after = frame("after-press")
    after_cursor = _cursor()
    steps.append({"step": "after", "scene_changed_pct": _changed(turned[band], after[band]),
                  "frame_changed_pct": _changed(turned, after), "vs_start_pct": _changed(start[band], after[band]),
                  "ambient_changed_pct": ambient, "cursor": after_cursor})
    if abs(after_cursor[0] - cursor[0]) > 1 or abs(after_cursor[1] - cursor[1]) > 1:
        fail("the real cursor moved: a scene press must not move it")
    if middle and not args.with_forward and (
            steps[-1]["scene_changed_pct"] < 1 or (args.no_turn and steps[-1]["scene_changed_pct"] <= turn_limit)):
        fail(f"no recentre observed: the {args.button} press was accepted but the screen changed no more than standing still")
    steps.append({"verdict": "needs_review", "frames": str(out)})
    print(json.dumps(steps, ensure_ascii=False, indent=2))
    print(f"needs review: frames in {out}. {SCENE_BUTTON_EXPECTED[args.button]} maafw.log should show "
          f"'scene press accepted' and 'release posted' for contact {button}, {args.repeat} time(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    timing = sub.add_parser("timing")
    timing.add_argument("--activity", choices=("none", "user-initiated", "latency-critical"), default="none")
    timing.add_argument("--request-ms", type=float, default=30.0)
    timing.add_argument("--samples", type=int, choices=range(5, 1001), default=30, metavar="5-1000")
    for name in ("windows", "lifecycle", "capture", "input-check", "refusal-check", "hit-test", "camera-check",
                 "scene-button-check", "key-sequence-check", "photo-mode-check", "text-input-check", "hover-check"):
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
        if name == "key-sequence-check":
            command.add_argument("--sequence", choices=sorted(KEY_SEQUENCES), required=True)
        if name == "text-input-check":
            command.add_argument("--text", default=TEXT_DEFAULT, help="the text to type (one trial per run)")
            command.add_argument("--placeholder", default="",
                                 help="the exact OCR reading of the empty field, from a capture beforehand")
        if name == "hover-check":
            command.add_argument("--point", type=int, nargs=2, required=True, metavar=("X", "Y"),
                                 help="a point on the selected card, in 1280x720 frame coordinates")
        if name in ("input-check", "camera-check", "scene-button-check", "key-sequence-check", "photo-mode-check",
                    "text-input-check", "hover-check"):
            command.add_argument("--wait", type=int, default=60, help="seconds to wait for the game to be in front")
        if name in ("camera-check", "scene-button-check"):
            command.add_argument("--pixels", type=int, default=400, help="relative move (the plan measured 400)")
        if name == "scene-button-check":
            command.add_argument("--button", choices=sorted(SCENE_BUTTONS), default="middle",
                                 help="scene button to press where the cursor is (middle: the camera recentre)")
            command.add_argument("--no-turn", action="store_true",
                                 help="no relative move: the camera is already turned away; judge the press alone")
            command.add_argument("--hold", type=_hold_seconds, default=HOLD_RANGE_S[0],
                                 help=f"seconds each press is held ({HOLD_RANGE_S[0]}-{HOLD_RANGE_S[1]})")
            command.add_argument("--repeat", type=int, choices=range(1, REPEAT_MAX + 1), default=1, metavar=f"1-{REPEAT_MAX}",
                                 help=f"presses, {REPEAT_GAP_S} s apart")
            command.add_argument("--expect-ability", choices=sorted(ABILITY_NODES),
                                 help="right button only, required: the ability the HUD must show before the press")
            command.add_argument("--select-slot", type=int, choices=sorted(SLOT_KEYS), metavar="1-8",
                                 help="right button only: press this slot's key (default 1-8) if the HUD shows another ability")
            command.add_argument("--with-forward", action="store_true",
                                 help="hold W from before the first press until after the last (the character walks)")
        if name == "camera-check":
            command.add_argument("--record-cursor", action="store_true",
                                 help="record real-pointer movement after each move instead of stopping (drift investigation)")
            command.add_argument("--option", action="store_true",
                                 help="experiment: Option, a 1-pixel absorbing move, the turn, Option again (navigation sends no Option on macOS)")
        if name == "input-check":
            command.add_argument("--release", action="store_true",
                                 help="also hold W across a controller destroy (the character walks briefly)")
    args = parser.parse_args()
    if args.command == "scene-button-check":
        if args.button == "right" and args.expect_ability is None:
            parser.error("--button right needs --expect-ability: the press uses whatever ability is selected")
        if args.button != "right" and (args.expect_ability is not None or args.select_slot is not None):
            parser.error("--expect-ability and --select-slot apply to --button right only")
    _ensure_runtime(getattr(args, "runtime", None))
    global _profile
    _profile = getattr(args, "profile", "stock")
    commands = {"windows": cmd_windows, "lifecycle": cmd_lifecycle, "capture": cmd_capture,
                "input-check": cmd_input_check, "refusal-check": cmd_refusal_check, "hit-test": cmd_hit_test,
                "camera-check": cmd_camera_check, "scene-button-check": cmd_scene_button_check,
                "key-sequence-check": cmd_key_sequence_check, "timing": cmd_timing,
                "photo-mode-check": cmd_photo_mode_check, "text-input-check": cmd_text_input_check,
                "hover-check": cmd_hover_check}
    return commands[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
