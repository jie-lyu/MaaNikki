"""Developer probe for the game's Android client in an emulator, driven through MaaFramework's AdbController.

Never imported by the agent or the client; it exists to survey what the emulator route can do
(docs/android-support-plan.md). Every input it sends is appended to an input log (JSON lines,
default .build/android/input-log.jsonl) with the time, the command, its arguments and a required
--purpose. Before an input it checks that the game's activity is the focused window and refuses
otherwise. That narrows, but cannot rule out, input reaching something else: focus can change between
the check and the dispatch, and a touchable overlay can take a tap while the game keeps focus.

    python -I scripts/android/probe.py info
    python -I scripts/android/probe.py capture --out DIR [--count N] [--raw]
    python -I scripts/android/probe.py tap X Y --purpose TEXT [--hold MS] [--after DIR]
    python -I scripts/android/probe.py swipe X1 Y1 X2 Y2 --duration MS --purpose TEXT
    python -I scripts/android/probe.py gesture FILE.json --purpose TEXT
    python -I scripts/android/probe.py key KEYCODE --purpose TEXT
    python -I scripts/android/probe.py text STRING --purpose TEXT
    python -I scripts/android/probe.py shell NAME [--grep REGEX]   (fixed read-only diagnostics)
    python -I scripts/android/probe.py reco NODE [NODE ...] (--frame PNG | --live) [--any-roi]
    python -I scripts/android/probe.py reco-all --frame PNG [--filter REGEX] [--any-roi] [--json OUT]
    python -I scripts/android/probe.py steps FILE.json --purpose TEXT
    python -I scripts/android/probe.py bgcheck --out FILE.jsonl --purpose TEXT [--samples N --interval S]

Coordinates are in the agent's frame, whose short side is 720 (1280x720 on a 16:9 device; the controller scales
them to the device) unless
--raw is given, in which case they are device pixels of the raw frame. Common options: --adb PATH,
--serial ADDR, --input maatouch|minitouch|adbshell (default maatouch), --screencap
netcat|gzip|encode|encode-pull (default netcat), --log PATH.

A gesture or steps file is a JSON list of steps, run in order on one connection:

    {"down": [X, Y], "contact": 0}      touch down
    {"move": [X, Y], "contact": 0}      move a held contact
    {"up": 0}                           lift a contact
    {"drag": [X1, Y1, X2, Y2], "ms": 300, "contact": 0, "steps": 10}   down, interpolated moves, stays held
    {"tap": [X, Y], "contact": 1, "hold": 60}
    {"swipe": [X1, Y1, X2, Y2], "ms": 300}
    {"key": KEYCODE}
    {"text": "..."}
    {"input": "gamepad keyevent KEYCODE_BUTTON_A"}   one `adb shell input` key event or joystick axis
    {"wait": MS}
    {"capture": "NAME"}                 save a 720p frame as NAME.png under --out
    {"reco": ["NODE", ...]}             recognize on a fresh frame, printed and kept in the report

Contacts still held when the file ends, or when any step fails or raises, are lifted. `reco`
loads resource/ as the agent does (pipeline, images, OCR model) and registers the agent's custom
recognitions (nikki.ui_template, nikki.crown_ready) with their analyze unwrapped from the agent's task-session
guard. A result describes the node's own definition on these pixels; it skips what the agent adds around it
(pause and recapture, frame-size checks, runtime pipeline overrides some tasks apply, and the Python-side
matching of the MaaNikki_Asset_* entries, which GameUI.asset_boxes does with caller-specific methods and
thresholds). --any-roi re-runs a missed node with its ROI widened to the whole frame (not And/Or nodes, and not custom nodes
without an explicit roi). A widened hit is only a
candidate: it can be the same element moved, or a different element with the same text or shape (the HUD's
商城 shortcut satisfies the shop page title), so each one needs a look at the frame.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "agent"
RESOURCE = ROOT / "resource"
DEFAULT_LOG = ROOT / ".build" / "android" / "input-log.jsonl"
DEFAULT_ADB = "/opt/homebrew/bin/adb"
DEFAULT_SERIAL = "127.0.0.1:5555"
GAME_PACKAGE = "com.papegames.infinitynikki"
FRAME = (1280, 720)

INPUT_METHODS = {"adbshell": 1, "minitouch": 1 << 1, "maatouch": 1 << 2}
SCREENCAP_METHODS = {"encode-pull": 1, "encode": 1 << 1, "gzip": 1 << 2, "netcat": 1 << 3}


class ProbeError(Exception):
    pass


# ---------------------------------------------------------------- input log


class InputLog:
    """Append-only JSON lines: one record per input sent (or refused) with its purpose."""

    def __init__(self, path: Path, purpose: str | None):
        self.path = Path(path)
        self.purpose = purpose

    def write(self, command: str, args, ok: bool | None, **extra) -> dict:
        record = {"time": dt.datetime.now().astimezone().isoformat(timespec="milliseconds"),
                  "command": command, "args": args, "ok": ok, "purpose": self.purpose, **extra}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record


# ---------------------------------------------------------------- device


def focused_package(shell) -> str | None:
    """Package of the window that has input focus (`mCurrentFocus`), or None when there is none or it cannot be read.

    Only mCurrentFocus counts: mFocusedApp still names the game while a system dialog, the launcher or
    `mCurrentFocus=null` (no focused window) holds focus.
    """
    output = shell("dumpsys window | grep mCurrentFocus") or ""
    packages = set()
    for line in output.splitlines():
        if "mCurrentFocus" not in line:
            continue
        match = re.search(r"\su\d+\s+([\w.]+)/", line)
        packages.add(match.group(1) if match else None)
    return packages.pop() if len(packages) == 1 else None


MAX_DURATION_MS = 20000  # swipe, drag, hold and wait; MaaFramework casts a negative duration to a ~49-day hold
# Fixed read-only diagnostics for `shell NAME`; output is filtered on the host (--grep), never on the device.
DIAGNOSTICS = {
    "focus": "dumpsys window",
    "activities": "dumpsys activity activities",
    "input": "dumpsys input",
    "input-method": "dumpsys input_method",
    "input-devices": "getevent -pl",
    "display": "dumpsys display",
    "size": "wm size",
    "density": "wm density",
    "props": "getprop",
    "processes": "ps -A",
    "packages": "cmd package list packages",
}
INPUT_STEP = re.compile(r"(gamepad|dpad|keyboard|joystick) keyevent KEYCODE_[A-Z0-9_]+"
                        r"|joystick motionevent MOVE -?[01](\.[0-9]+)? -?[01](\.[0-9]+)?")


def check_duration(ms) -> int:
    value = int(ms)
    if not 0 <= value <= MAX_DURATION_MS:
        raise ProbeError(f"duration {ms!r} ms is outside 0..{MAX_DURATION_MS}")
    return value


class Device:
    """One AdbController connection plus the guard and log every input goes through.

    Every input that starts a gesture or sends a key or text (tap, swipe, down, key, text, input) is preceded by a
    check that the game's window has input focus, and moves of a held contact recheck it at most every 0.2 s.
    Releases are never held back. The check narrows the window for mistakes but is not a guarantee: focus can
    change between the check and the dispatch, and a touchable overlay can take a tap while the game keeps
    focus. Each input is logged before it is sent (an unwritable log stops it, except a release) and its outcome
    after.
    """

    FOCUS_RECHECK_S = 0.2  # during a held gesture, recheck focus before a move at most this often

    def __init__(self, controller, log: InputLog):
        self.controller = controller
        self.log = log
        self.held: set[int] = set()
        self.release_failed: set[int] = set()
        self._focus_checked = 0.0

    # Shell runs through the controller's own adb, so it shares the transport the inputs use.
    def shell(self, command: str, timeout_ms: int = 10000) -> str | None:
        job = self.controller.post_shell(command, timeout_ms).wait()
        return job.get() if job.succeeded else None

    def check_foreground(self, command="input", args=None) -> None:
        package = focused_package(self.shell)
        self._focus_checked = time.monotonic()
        if package != GAME_PACKAGE:
            self.log.write(command, args, False, refused=f"focused window is {package!r}, not the game")
            raise ProbeError(f"the focused window is {package!r}, not the game; refusing to send {command}")

    def _send(self, command: str, args, job_factory, *, starts=True) -> bool:
        if starts:
            self.check_foreground(command, args)
        self.log.write(command, args, None, phase="sending")
        started = time.perf_counter()
        try:
            ok = bool(job_factory().wait().succeeded)
        except BaseException:
            self.log.write(command, args, None, phase="interrupted", error="interrupted or raised while waiting")
            raise
        # ok means the request reached the input tool (for Maatouch a pipe write), not that the game acted on it.
        self.log.write(command, args, ok, phase="done", ms=round((time.perf_counter() - started) * 1000, 1))
        return ok

    def tap(self, x, y, contact=0, hold_ms=0) -> bool:
        hold_ms = check_duration(hold_ms)
        if hold_ms == 0:
            return self._down_up("tap", [x, y, contact], contact, lambda: self.controller.post_click(x, y, contact, 1))
        if not self.down(x, y, contact):
            return False
        time.sleep(hold_ms / 1000)
        return self.up(contact)

    def swipe(self, x1, y1, x2, y2, ms, contact=0) -> bool:
        ms = check_duration(ms)
        return self._down_up("swipe", [x1, y1, x2, y2, ms, contact], contact,
                             lambda: self.controller.post_swipe(x1, y1, x2, y2, ms, contact, 1))

    def _down_up(self, command, args, contact, job_factory) -> bool:
        """A click or swipe is down/move/up inside the core, which forgets the contact before sending the up. The
        contact counts as held from just before dispatch until the call succeeds, so a failure or an interruption
        leaves it for release_all, which sends an explicit up."""
        self.check_foreground(command, args)
        self.held.add(contact)
        ok = self._send(command, args, job_factory, starts=False)
        if ok:
            self.held.discard(contact)
        else:
            self.up(contact)
        return ok

    def down(self, x, y, contact=0) -> bool:
        self.check_foreground("touch_down", [x, y, contact])
        self.held.add(contact)  # before sending: a down that raised may still have reached the device
        return self._send("touch_down", [x, y, contact], lambda: self.controller.post_touch_down(x, y, contact, 1),
                          starts=False)

    def move(self, x, y, contact=0) -> bool:
        if contact not in self.held:
            raise ProbeError(f"touch_move on contact {contact}, which this run has not put down")
        # Android normally keeps a gesture with the window of its down, but a gesture can be transferred; recheck
        # focus during long gestures (throttled so drags keep their timing) and stop moving if it was lost.
        if time.monotonic() - self._focus_checked >= self.FOCUS_RECHECK_S:
            self.check_foreground("touch_move", [x, y, contact])
        return self._send("touch_move", [x, y, contact], lambda: self.controller.post_touch_move(x, y, contact, 1),
                          starts=False)

    def up(self, contact=0) -> bool:
        """Release a contact. Never blocked by focus, and a failing input log does not stop the release itself."""
        ok = False
        for _attempt in range(2):  # one retry: an up that failed leaves the finger on the screen
            try:
                ok = self._send("touch_up", [contact], lambda: self.controller.post_touch_up(contact), starts=False)
            except OSError:  # the log could not be written: release anyway, then report
                try:
                    ok = bool(self.controller.post_touch_up(contact).wait().succeeded)
                except Exception:
                    ok = False
                print(f"ERROR: input log unwritable while releasing contact {contact}", file=sys.stderr)
            except Exception:
                ok = False
            if ok:
                break
        if ok:
            self.held.discard(contact)
            self.release_failed.discard(contact)
        else:
            self.release_failed.add(contact)
        return ok

    def release_all(self) -> list[int]:
        """Lift every contact this connection may still hold; returns the contacts whose up failed."""
        for contact in sorted(self.held):
            self.up(contact)
        return sorted(self.release_failed)

    def key(self, code) -> bool:
        return self._send("key", [code], lambda: self.controller.post_click_key(code))

    def text(self, value) -> bool:
        # Maatouch writes text into its line-oriented command stream: a line break would start another command.
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ProbeError("text must not contain line breaks or other control characters")
        return self._send("text", [value], lambda: self.controller.post_input_text(value))

    def shell_input(self, arguments: str) -> bool:
        """`input <arguments>` on the device (for sources the controller has no call for, e.g. gamepad).

        Only the `input` command: anything else is a shell command, not an input, and goes through `shell`.
        """
        # One key event, or one joystick axis position: nothing that can leave a pointer down or send several keys
        # after a single focus check.
        if not INPUT_STEP.fullmatch(arguments):
            raise ProbeError(f"input step allows one '<source> keyevent KEYCODE_X' or 'joystick motionevent MOVE x y', "
                             f"got {arguments!r}")
        self.check_foreground("input", [arguments])
        command = f"input {arguments}; echo __exit=$?"
        self.log.write("input", [arguments], None, phase="sending")
        started = time.perf_counter()
        try:
            result = self.controller.post_shell(command, 10000).wait()
        except BaseException:
            self.log.write("input", [arguments], None, phase="interrupted", error="interrupted or raised while waiting")
            raise
        output = result.get() if result.succeeded else None
        ok = bool(output is not None and "__exit=0" in output)
        self.log.write("input", [arguments], ok, phase="done", ms=round((time.perf_counter() - started) * 1000, 1),
                       output=(output or "").replace("__exit=0", "").strip()[:300] or None)
        return ok

    def drag(self, x1, y1, x2, y2, ms, contact=0, steps=10) -> bool:
        """Touch down and move in `steps` even moves over `ms`; the contact stays held."""
        ms = check_duration(ms)
        steps = int(steps)
        if not 1 <= steps <= 200:
            raise ProbeError(f"drag steps {steps} outside 1..200")
        if not self.down(x1, y1, contact):
            return False
        for index in range(1, steps + 1):
            time.sleep(ms / 1000 / steps)
            x = round(x1 + (x2 - x1) * index / steps)
            y = round(y1 + (y2 - y1) * index / steps)
            if not self.move(x, y, contact):
                return False
        return True

    def capture(self):
        job = self.controller.post_screencap().wait()
        if not job.succeeded:
            return None  # get() would return an earlier cached frame
        frame = job.get()
        return frame if frame is not None and getattr(frame, "size", 0) else None


def connect(args):
    from maa.controller import AdbController
    from maa.tasker import Tasker

    log_dir = ROOT / ".build" / "android" / "maafw-log"
    log_dir.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(str(log_dir))
    controller = AdbController(args.adb, args.serial, screencap_methods=SCREENCAP_METHODS[args.screencap],
                               input_methods=INPUT_METHODS[args.input], config={})
    if getattr(args, "raw", False):
        controller.set_screenshot_use_raw_size(True)
    else:
        controller.set_screenshot_target_short_side(FRAME[1])
    started = time.perf_counter()
    if not controller.post_connection().wait().succeeded:
        raise ProbeError(f"AdbController could not connect to {args.serial} with input={args.input} "
                         f"screencap={args.screencap}; see {log_dir / 'maafw.log'}")
    connect_ms = (time.perf_counter() - started) * 1000
    # The core learns the frame size, which it needs to scale touch coordinates, from the first capture;
    # before that a click logs "Invalid image target size". Capture once so every input is scaled the same way.
    # A failed first capture leaves the scale unknown, and the core then sends touches to (0, 0).
    if not controller.post_screencap().wait().succeeded:
        raise ProbeError("the first capture after connecting failed; no input is sent without a known frame size")
    width, height = controller.resolution
    if width <= 0 or height <= 0:
        raise ProbeError(f"the device resolution is unknown ({width}x{height}); no input is sent")
    if width <= height or abs(width * 9 - height * 16) > 16:  # the resources and coordinates assume 16:9 landscape
        raise ProbeError(f"the device reports {width}x{height}, not 16:9 landscape; no input is sent")
    log = InputLog(Path(args.log), getattr(args, "purpose", None))
    device = Device(controller, log)
    device.connect_ms = connect_ms
    return device


# ---------------------------------------------------------------- recognition


class Recognizer:
    """resource/ loaded into a Tasker bound to `controller`, with the agent's custom recognitions.

    Nodes are judged with Context.run_recognition inside a probe action, which reports misses as
    well as hits (a task whose entry misses records no node at all). All nodes of one call share
    one capture.
    """

    ENTRY = "AndroidProbe_Recognize"
    ACTION = "android_probe.recognize"

    def __init__(self, controller):
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker

        self.resource = Resource()
        if not self.resource.post_bundle(RESOURCE).wait().succeeded:
            raise ProbeError(f"could not load {RESOURCE}")
        for name, recognition in agent_recognitions().items():
            if not self.resource.register_custom_recognition(name, recognition):
                raise ProbeError(f"could not register {name}")
        recognizer = self

        class Recognize(CustomAction):
            def run(self, context, argv):
                recognizer._results = recognizer._judge(context)
                return CustomAction.RunResult(success=True)

        self._action = Recognize()
        if not self.resource.register_custom_action(self.ACTION, self._action):
            raise ProbeError("could not register the probe action")
        self.tasker = Tasker()
        if not self.tasker.bind(self.resource, controller):
            raise ProbeError("could not bind the tasker")
        self.controller = controller
        self._request: tuple[list[str], bool, dict] = ([], False, {})
        self._results: list[dict] | None = None
        self.last_frame = None

    def node(self, name: str) -> dict:
        data = self.resource.get_node_data(name)
        if data is None:
            raise ProbeError(f"no pipeline node {name!r}")
        return data

    def _judge(self, context) -> list[dict]:
        names, any_roi, adhoc = self._request
        job = context.tasker.controller.post_screencap().wait()
        if not job.succeeded:
            return [{"node": name, "hit": False, "error": "capture failed"} for name in names]
        image = job.get()
        self.last_frame = image
        results = []
        for name in names:
            if name in adhoc:  # a probe-only node defined by its override, e.g. the mobile HUD check
                results.append(judged(name, context.run_recognition(name, image, {name: adhoc[name]})))
                continue
            result = judged(name, context.run_recognition(name, image))
            if any_roi and not result["hit"]:
                override = full_roi_override(self.node(name), image.shape[1], image.shape[0])
                if override:
                    widened = judged(name, context.run_recognition(name, image, {name: override}))
                    result.update(widened_hit=widened["hit"], widened_box=widened["box"],
                                  widened_detail=widened["detail"])
                else:
                    result["widened_hit"] = None  # And/Or/DirectHit: nothing to widen
            results.append(result)
        return results

    def run_many(self, names: list[str], *, any_roi: bool = False, adhoc: dict | None = None) -> list[dict]:
        adhoc = adhoc or {}
        for name in names:
            if name not in adhoc:
                self.node(name)
        self._request, self._results = (list(names), any_roi, adhoc), None
        override = {self.ENTRY: {"recognition": "DirectHit", "action": "Custom", "custom_action": self.ACTION,
                                 "next": [], "pre_delay": 0, "post_delay": 0}}
        self.tasker.post_task(self.ENTRY, override).wait()
        if self._results is None:
            raise ProbeError("the probe action did not run")
        return self._results

    def run(self, name: str, *, any_roi: bool = False) -> dict:
        return self.run_many([name], any_roi=any_roi)[0]


def judged(name: str, reco) -> dict:
    """JSON-friendly summary of a RecognitionDetail (None when the recognition could not start).

    A hit keeps the best result; a miss keeps the best score among all candidates and, for OCR, the
    texts that were read, so a near miss can be told from an absent element.
    """
    if reco is None:
        return {"node": name, "hit": False, "box": None, "detail": {"error": "recognition did not start"}}
    error = _find_error(reco.raw_detail) or _find_error([dataclasses.asdict(item) if dataclasses.is_dataclass(item)
                                                         else item for item in reco.all_results or []])
    best = reco.best_result
    if dataclasses.is_dataclass(best):
        detail = _short(dataclasses.asdict(best))
    else:
        candidates = []
        for item in reco.all_results or []:
            candidates.append(dataclasses.asdict(item) if dataclasses.is_dataclass(item) else item)
        scores = [_score(item) for item in candidates]
        scores = [score for score in scores if score is not None]
        detail = {"best_score": round(max(scores), 4) if scores else None}
        texts = [item.get("text") for item in candidates if isinstance(item, dict) and item.get("text")]
        if texts:
            detail["texts"] = texts[:16]
        if not candidates and isinstance(reco.raw_detail, dict):
            detail["raw"] = _short(reco.raw_detail)
    if error:
        detail = {**(detail or {}), "error": error}
    return {"node": name, "hit": bool(reco.hit), "box": list(reco.box) if reco.box else None,
            "algorithm": str(getattr(reco.algorithm, "value", reco.algorithm)), "detail": detail}


def _find_error(value, depth=0):
    """The first "error" string anywhere in a (nested) recognition detail: custom recognitions report failures that
    way, and And/Or nodes carry their children's details."""
    if depth > 8:
        return None
    if isinstance(value, dict):
        if isinstance(value.get("error"), str) and value["error"]:
            return value["error"]
        value = list(value.values())
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _find_error(item, depth + 1)
            if found:
                return found
    return None


def _jsonable(value):
    """json.dumps fallback: recognition details can carry images (And/Or sub-results)."""
    shape = getattr(value, "shape", None)
    if shape is not None:
        return f"<array {list(shape)}>"
    return str(value)


def _score(item):
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("score"), (int, float)):
        return float(item["score"])
    inner = item.get("detail")
    if isinstance(inner, dict) and isinstance(inner.get("score"), (int, float)):
        return float(inner["score"])
    return None


def full_roi_override(node: dict, width: int = FRAME[0], height: int = FRAME[1]) -> dict:
    """Pipeline override that widens a node's ROI to the whole width x height frame (built-in and nikki.ui_template).

    Returns {} for And/Or (their sub-recognitions carry their own ROIs) and anything else without one. Widening
    also drops the positional constraint between a ui_template's main template and its fallback_all templates.
    """
    whole = [0, 0, int(width), int(height)]
    reco = node.get("recognition") or {}
    kind = reco.get("type") if isinstance(reco, dict) else reco
    param = reco.get("param", {}) if isinstance(reco, dict) else {}
    if kind in ("TemplateMatch", "OCR", "FeatureMatch", "ColorMatch", "NeuralNetworkDetect"):
        return {"recognition": {"type": kind, "param": {**param, "roi": whole, "roi_offset": [0, 0, 0, 0]}}}
    if kind == "Custom":
        custom = json.loads(json.dumps(param.get("custom_recognition_param") or {}))
        if not isinstance(custom, dict) or "roi" not in custom:
            return {}
        custom["roi"] = whole
        for spec in custom.get("fallback_all") or []:
            if isinstance(spec, dict) and "roi" in spec:
                spec["roi"] = whole
        return {"recognition": {"type": kind, "param": {**param, "custom_recognition_param": custom}}}
    return {}


def _short(value):
    if isinstance(value, dict):
        keep = {k: value[k] for k in ("score", "text", "template", "box", "error", "blocking", "page",
                                      "detail") if k in value}
        if isinstance(keep.get("detail"), dict):
            keep["detail"] = _short(keep["detail"])
        if isinstance(keep.get("score"), float):
            keep["score"] = round(keep["score"], 4)
        return keep or {k: value[k] for k in list(value)[:4]}
    if isinstance(value, list):
        return [_short(item) for item in value[:3]]
    return value


def agent_recognitions() -> dict:
    """The agent's custom recognitions, with their analyze unwrapped from the task-session guard."""
    if str(AGENT) not in sys.path:
        sys.path.insert(0, str(AGENT))
    # Importing maa.agent reopens the binding in AgentServer mode, after which Resource calls in this
    # process go to the wrong library and crash. registration only needs the AgentServer name, which
    # local registration never calls, so stand in a placeholder for it.
    if "maa.agent" not in sys.modules:
        import types
        package, server = types.ModuleType("maa.agent"), types.ModuleType("maa.agent.agent_server")
        package.__path__ = []

        class AgentServer:  # never used under _LOCAL_REGISTRATION
            pass

        server.AgentServer = AgentServer
        package.agent_server = server
        sys.modules["maa.agent"], sys.modules["maa.agent.agent_server"] = package, server
    import registration
    token = registration._LOCAL_REGISTRATION.set(True)  # do not register on AgentServer
    try:
        import recognition.crown  # noqa: F401
        import recognition.ui_template  # noqa: F401
    finally:
        registration._LOCAL_REGISTRATION.reset(token)
    instances = {}
    for name, cls in registration._RECOGNITION_TYPES.items():
        original = getattr(cls.analyze, "__wrapped__", cls.analyze)
        unguarded = type(cls.__name__ + "Probe", (cls,), {"analyze": original})
        instances[name] = unguarded()
    return instances


def frame_controller(path: Path):
    """A controller whose every capture is the saved frame at `path`, scaled to 720p by the core like a
    live capture. It refuses every input. (The pip runtime has no MaaDbgControlUnit.)"""
    import cv2
    from maa.controller import CustomController

    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise ProbeError(f"could not read {path}")
    height, width = image.shape[:2]
    if abs(width * 9 - height * 16) > 16:  # resources are 1280x720; another aspect would not scale to it
        raise ProbeError(f"{path} is {width}x{height}, not 16:9")

    class FrameController(CustomController):
        def connect(self):
            return True

        def request_uuid(self):
            return f"android-probe-frame:{path}"

        def screencap(self):
            return image

        def start_app(self, intent):
            return False

        stop_app = start_app

        def click(self, x, y):
            return False

        def swipe(self, x1, y1, x2, y2, duration):
            return False

        def touch_down(self, contact, x, y, pressure):
            return False

        touch_move = touch_down

        def touch_up(self, contact):
            return False

        def click_key(self, keycode):
            return False

        def input_text(self, text):
            return False

        key_down = key_up = click_key

    controller = FrameController()
    controller.set_screenshot_target_short_side(FRAME[1])
    if not controller.post_connection().wait().succeeded:
        raise ProbeError(f"could not load {path} as a frame")
    return controller


def recognition_nodes(pattern: str | None) -> list[str]:
    """Names of pipeline nodes that recognize something (not DirectHit), optionally filtered by regex."""
    names = []
    for path in sorted((RESOURCE / "pipeline").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for name, node in data.items():
            if not isinstance(node, dict):
                continue
            reco = node.get("recognition", "DirectHit")
            kind = reco.get("type") if isinstance(reco, dict) else reco
            if kind in (None, "DirectHit"):
                continue
            if pattern and not re.search(pattern, name):
                continue
            names.append(name)
    return names


# ---------------------------------------------------------------- steps


def run_steps(device: Device, steps: list, *, out: Path | None, recognizer=None) -> list[dict]:
    """Run a gesture/steps list on one connection; held contacts are lifted whatever happens."""
    import cv2

    report = []
    try:
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or len(step.keys() - {"contact", "ms", "steps", "hold", "note"}) != 1:
                raise ProbeError(f"step {index} is not one action: {step!r}")
            contact = int(step.get("contact", 0))
            entry = {"step": index, **step}
            started = time.perf_counter()
            if "wait" in step:
                time.sleep(check_duration(step["wait"]) / 1000)
                ok = True
            elif "capture" in step:
                frame = device.capture()
                ok = frame is not None
                if ok and out is not None:
                    out.mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(out / f"{step['capture']}.png"), frame)
            elif "reco" in step:
                if recognizer is None:
                    raise ProbeError("reco step without a recognizer")
                entry["results"] = recognizer.run_many(list(step["reco"]))
                # A capture or recognition error is not a miss: stop before any later input relies on it.
                ok = not any(isinstance(r.get("detail"), dict) and r["detail"].get("error") or r.get("error")
                             for r in entry["results"])
            else:
                ok = _input_step(device, step, contact)
            entry["ok"] = ok
            entry["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
            report.append(entry)
            print(json.dumps(entry, ensure_ascii=False, default=_jsonable), flush=True)
            if not ok:
                raise ProbeError(f"step {index} failed: {step!r}")
    finally:
        failed = device.release_all()
        if failed:
            print(f"ERROR: could not lift contacts {failed}; check the device before sending more input",
                  file=sys.stderr, flush=True)
    if failed:
        raise ProbeError(f"contacts {failed} may still be held on the device")
    return report


def _input_step(device: Device, step: dict, contact: int) -> bool:
    if "down" in step:
        return device.down(*step["down"], contact)
    if "move" in step:
        return device.move(*step["move"], contact)
    if "up" in step:
        if int(step["up"]) not in device.held:
            raise ProbeError(f"up on contact {step['up']}, which this run has not put down")
        return device.up(int(step["up"]))
    if "drag" in step:
        return device.drag(*step["drag"], int(step.get("ms", 300)), contact, int(step.get("steps", 10)))
    if "tap" in step:
        return device.tap(*step["tap"], contact, int(step.get("hold", 0)))
    if "swipe" in step:
        return device.swipe(*step["swipe"], int(step.get("ms", 300)), contact)
    if "key" in step:
        return device.key(int(step["key"]))
    if "text" in step:
        return device.text(str(step["text"]))
    if "input" in step:
        return device.shell_input(str(step["input"]))
    raise ProbeError(f"unknown step {step!r}")


def load_steps(path: Path) -> list:
    steps = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(steps, list):
        raise ProbeError(f"{path} is not a JSON list of steps")
    return steps


# ---------------------------------------------------------------- background check

# The mobile HUD's shortcut row carries text labels (换装 at its right end, 奇想日历 among them). The quit
# confirmation (opened by BACK in the open world) leaves that row visible behind it, so it is vetoed on the same frame.
HUD_CHECK = {
    "AndroidProbe_HudLabels": {"recognition": {"type": "OCR", "param": {"roi": [600, 55, 680, 40], "expected": ["换装"]}}},
    "AndroidProbe_QuitDialog": {"recognition": {"type": "OCR", "param": {
        "roi": [440, 160, 400, 80], "expected": ["退出游戏"]}}},
    "AndroidProbe_QuitCancel": {"recognition": {"type": "OCR", "param": {
        "roi": [380, 440, 240, 80], "expected": ["^取消$"]}}},
}
# OCR merges the densely packed HUD labels into one box over the whole row, so 奇想日历 is looked for in narrow
# overlapping windows along it. In each window that reads the label, its centre is estimated from its position in the
# (short) text; the median over those windows gives the shortcut's position.
CALENDAR_LABEL_WINDOWS = {
    f"AndroidProbe_CalendarLabel{x}": {"recognition": {"type": "OCR", "param": {
        "roi": [x, 55, 90, 40], "expected": ["奇想日历"]}}}
    for x in range(600, 1191, 10)}


def hud_state(recognizer) -> dict:
    """On one frame: whether the open-world HUD shows with no quit dialog, where 奇想日历's shortcut is, and the
    quit dialog's 取消 button if that dialog is open."""
    checks = {**HUD_CHECK, **CALENDAR_LABEL_WINDOWS}
    results = recognizer.run_many(list(checks), adhoc=checks)  # one capture for all of them
    labels, quit_dialog, cancel = results[:3]
    # The guard fails closed: a quit title that could not be recognised (an error) or a visible 取消 also rules the
    # HUD out, even if the HUD labels are readable behind it.
    guard_error = _find_error(quit_dialog.get("detail")) or _find_error(cancel.get("detail"))
    state = {"hud": bool(labels["hit"] and not quit_dialog["hit"] and not cancel["hit"] and not guard_error),
             "quit_dialog": bool(quit_dialog["hit"])}
    if guard_error:
        state["guard_error"] = guard_error
    centres, tops = [], []
    for result in results[3:]:
        text = str((result.get("detail") or {}).get("text") or "")
        at = text.find("奇想日历")
        if result["hit"] and result["box"] and at >= 0:
            x, y, w, _h = result["box"]
            centres.append(x + w * (at + 2) / len(text))
            tops.append(y)
    if len(centres) >= 2:
        centres.sort()
        median = centres[len(centres) // 2]
        if all(abs(c - median) <= 25 for c in centres):  # the windows agree on where the label is
            state["calendar_icon"] = [round(median), max(0, min(tops) - 15)]  # the icon sits just above its label
    if quit_dialog["hit"] and cancel["hit"] and cancel["box"]:
        x, y, w, h = cancel["box"]
        state["quit_cancel"] = [x + w // 2, y + h // 2]
    return state
EMULATOR_OWNERS = ("BlueStacks",)


def _window_rows(option: int) -> list[dict]:
    import ctypes

    if str(AGENT) not in sys.path:
        sys.path.insert(0, str(AGENT))
    from host.macos import quartz as q

    cf, cg = q._cf(), q._cg()
    rows = []
    array = cg.CGWindowListCopyWindowInfo(option, 0)
    if not array:  # the window server can return NULL (e.g. no session); report the state as unknown
        return []
    try:
        for index in range(cf.CFArrayGetCount(array)):
            entry = cf.CFArrayGetValueAtIndex(array, index)

            def get(name, entry=entry):
                return cf.CFDictionaryGetValue(entry, q._cg_key(name))
            rect = q.CGRect()
            bounds = get("kCGWindowBounds")
            if not bounds or not cg.CGRectMakeWithDictionaryRepresentation(bounds, ctypes.byref(rect)):
                continue
            on_screen = get("kCGWindowIsOnscreen")
            rows.append({"id": int(q._number(get("kCGWindowNumber")) or 0),
                         "owner": q._string(get("kCGWindowOwnerName")), "layer": int(q._number(get("kCGWindowLayer")) or 0),
                         "on_screen": bool(on_screen and cf.CFBooleanGetValue(on_screen)),
                         "bounds": (rect.origin.x, rect.origin.y, rect.size.width, rect.size.height)})
    finally:
        cf.CFRelease(array)
    return rows


# Window layers that hide what is under them: normal (0), floating (3) and modal panels (8). The Dock (20), the menu
# bar (24) and system overlays (cursor, screen-sharing tools) sit higher and are mostly transparent; they are
# reported but not counted as covering.
COVERING_LAYERS = range(0, 20)


def _is_emulator_window(row) -> bool:
    return row["owner"] in EMULATOR_OWNERS and row["layer"] == 0 and row["bounds"][2] > 300 and row["bounds"][3] > 200


def mac_window_state() -> dict:
    """Where the emulator's main window is, read from the window server (no permission needed): on screen or
    not, its bounds, how much of it windows in front cover, and which application is active."""
    if sys.platform != "darwin":
        return {"available": False}
    from host.macos import quartz as q  # noqa: F401  (sys.path is set by _window_rows)

    on_screen = _window_rows(1 | 16)  # kCGWindowListOptionOnScreenOnly | ExcludeDesktopElements, front to back
    if not on_screen:
        return {"available": False, "reason": "the window server returned no windows"}
    front = q.front_pid() if "host.macos.quartz" in sys.modules else -1
    state = {"available": True, "front_app": Path(q.process_path(front)).name if front > 0 else None}
    index = next((i for i, row in enumerate(on_screen) if _is_emulator_window(row)), None)
    if index is None:
        # Not on screen: minimised, hidden, on another Space, or not running. The full list (not in z-order) tells
        # whether the window still exists.
        exists = any(_is_emulator_window(row) for row in _window_rows(0))
        return {**state, "window": {"on_screen": False, "exists": exists}}
    window = on_screen[index]
    above = [row for row in on_screen[:index] if row["owner"] not in EMULATOR_OWNERS]
    covering = [row for row in above if row["layer"] in COVERING_LAYERS]
    # The first large emulator window; with one BlueStacks instance this is the one ADB drives (not checked).
    state["window"] = {"id": window["id"], "on_screen": True, "bounds": [round(v) for v in window["bounds"]]}
    # Rectangle overlap only: transparent parts of covering windows count as opaque, opaque windows above layer 19
    # are not counted, and the grid rounds edges up. An estimate, not what is visible.
    state["covered_estimate"] = round(covered_fraction(window["bounds"], [row["bounds"] for row in covering]), 3)
    state["covered_by"] = sorted({row["owner"] for row in covering
                                  if covered_fraction(window["bounds"], [row["bounds"]]) > 0})
    state["overlays_above"] = sorted({row["owner"] for row in above if row["layer"] not in COVERING_LAYERS})
    return state


def covered_fraction(target, others, cell=8) -> float:
    """Fraction of the target rectangle covered by the union of the other rectangles (grid of `cell` points)."""
    import numpy as np

    x, y, w, h = target
    cols, rows = max(1, int(w // cell)), max(1, int(h // cell))
    grid = np.zeros((rows, cols), dtype=bool)
    for ox, oy, ow, oh in others:
        c0, c1 = int(max(0, (ox - x) // cell)), int(min(cols, -(-(ox + ow - x) // cell)))
        r0, r1 = int(max(0, (oy - y) // cell)), int(min(rows, -(-(oy + oh - y) // cell)))
        if c0 < c1 and r0 < r1:
            grid[r0:r1, c0:c1] = True
    return float(grid.mean())


def frame_change(a, b) -> float:
    """Mean absolute difference of two frames (0 means identical pixels)."""
    import cv2

    return float(cv2.absdiff(a, b).mean())


def background_sample(device: Device, recognizer) -> dict:
    """One measurement: window state, frame freshness, and (from the HUD only) a harmless input with an unambiguous
    result: open 奇想日历 by its shortcut, and close it with BACK only once the calendar page is recognised. The quit
    dialog is a guard: wherever it is seen, its 取消 is tapped, its dismissal is checked on a fresh frame, and nothing
    else is sent. Stops sending at the first input that fails. Whenever input was attempted, the window state is
    recorded again afterwards."""
    sample = {"time": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "window": mac_window_state()}
    started = time.perf_counter()
    first = device.capture()
    first_ms = (time.perf_counter() - started) * 1000
    time.sleep(1)
    started = time.perf_counter()
    second = device.capture()
    sample["capture_ms"] = [round(first_ms), round((time.perf_counter() - started) * 1000)]
    if first is None or second is None:
        return {**sample, "capture": "failed"}
    # A stale frame repeats pixel for pixel; a live scene with motion (water, foliage) does not. A static scene would
    # also repeat, so a zero here is a question, not proof of staleness.
    sample["change_1s"] = round(frame_change(first, second), 2)
    try:
        _sample_input(device, recognizer, sample)
    except ProbeError as error:  # e.g. focus left the game after an input: keep what was done, stop sending
        sample.update(error=str(error), stop=True)
    finally:
        if any(key in sample for key in ("tap_ok", "quit_cancelled")):
            sample["window_after"] = mac_window_state()
    return sample


def _sample_input(device: Device, recognizer, sample: dict) -> None:
    state = hud_state(recognizer)
    sample.update(hud=state["hud"], quit_dialog=state["quit_dialog"])
    if "guard_error" in state:
        sample.update(input="the quit-dialog guard could not be evaluated; nothing sent", stop=True)
        return
    if state["quit_dialog"]:
        _cancel_quit(device, recognizer, state, sample)
        return
    if not state["hud"] or "calendar_icon" not in state:
        sample["input"] = "skipped: the open-world HUD or its 奇想日历 shortcut is not showing"
        return
    sample["tap"] = state["calendar_icon"]
    sample["tap_ok"] = device.tap(*state["calendar_icon"])
    if not sample["tap_ok"]:
        sample["input"] = "the tap failed; nothing else sent"
        return
    time.sleep(1.5)
    sample["calendar_opened"] = recognizer.run("MaaNikki_CalendarReady")["hit"]
    if not sample["calendar_opened"]:
        sample["input"] = "the tap did not open the calendar; nothing else sent"
        return
    sample["back_ok"] = device.key(4)
    if not sample["back_ok"]:
        sample["input"] = "BACK failed; nothing else sent"
        return
    time.sleep(1.5)
    after = hud_state(recognizer)
    sample.update(hud_after=after["hud"], quit_dialog_after=after["quit_dialog"])
    if "guard_error" in after:
        sample.update(stop=True)
    elif after["quit_dialog"]:
        _cancel_quit(device, recognizer, after, sample)


def _cancel_quit(device: Device, recognizer, state: dict, sample: dict) -> None:
    """Tap the quit dialog's 取消 and confirm on a fresh frame that the dialog is gone; anything else stops the run.
    Results go straight into `sample`, so an exception during the check still leaves the tap on record."""
    if "quit_cancel" not in state:
        sample.update(input="quit dialog open but 取消 not found; nothing sent", stop=True)
        return
    sample["quit_cancelled"] = device.tap(*state["quit_cancel"])
    if not sample["quit_cancelled"]:
        sample["stop"] = True
        return
    time.sleep(1.5)
    # Dismissed only on a fresh frame that shows the HUD with both quit checks clear (a failed capture or a still
    # visible 取消 leaves hud False).
    sample["quit_dismissed"] = bool(hud_state(recognizer)["hud"])
    if not sample["quit_dismissed"]:
        sample["stop"] = True


# ---------------------------------------------------------------- commands


def cmd_info(args) -> int:
    device = connect(args)
    info = {"connect_ms": round(device.connect_ms), "controller": device.controller.info,
            "resolution": list(device.controller.resolution), "focused": focused_package(device.shell)}
    for key, command in (("android", "getprop ro.build.version.release"), ("sdk", "getprop ro.build.version.sdk"),
                         ("abi", "getprop ro.product.cpu.abi"), ("wm_size", "wm size"),
                         ("density", "wm density"), ("rotation", "dumpsys input | grep -m1 -i 'orientation'")):
        info[key] = (device.shell(command) or "").strip()
    print(json.dumps(info, ensure_ascii=False, indent=1, default=str))
    return 0


def cmd_capture(args) -> int:
    import cv2

    device = connect(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    timings = []
    for index in range(args.count):
        started = time.perf_counter()
        frame = device.capture()
        timings.append(round((time.perf_counter() - started) * 1000, 1))
        if frame is None:
            raise ProbeError(f"capture {index} failed")
        name = f"{args.prefix}{'raw' if args.raw else '720'}-{index:02d}.png"
        cv2.imwrite(str(out / name), frame)
        if args.interval:
            time.sleep(args.interval / 1000)
    print(json.dumps({"connect_ms": round(device.connect_ms), "shape": list(frame.shape),
                      "capture_ms": timings}, ensure_ascii=False))
    return 0


def _with_after(device: Device, args, action) -> int:
    import cv2

    try:
        ok = action()
    finally:
        failed = device.release_all()
    if failed:
        print(f"ERROR: could not lift contacts {failed}; check the device before sending more input", file=sys.stderr)
        ok = False
    if args.after:
        time.sleep(args.after_ms / 1000)
        frame = device.capture()
        if frame is not None:
            out = Path(args.after)
            out.mkdir(parents=True, exist_ok=True)
            name = f"after-{dt.datetime.now().strftime('%H%M%S-%f')[:-3]}.png"
            cv2.imwrite(str(out / name), frame)
            print(f"after frame: {out / name}")
    print(json.dumps({"ok": ok}))
    return 0 if ok else 1


def cmd_tap(args) -> int:
    device = connect(args)
    return _with_after(device, args, lambda: device.tap(args.x, args.y, args.contact, args.hold))


def cmd_swipe(args) -> int:
    device = connect(args)
    return _with_after(device, args, lambda: device.swipe(args.x1, args.y1, args.x2, args.y2, args.duration))


def cmd_key(args) -> int:
    device = connect(args)
    return _with_after(device, args, lambda: device.key(args.keycode))


def cmd_text(args) -> int:
    device = connect(args)
    return _with_after(device, args, lambda: device.text(args.value))


def cmd_shell(args) -> int:
    """One of the fixed read-only DIAGNOSTICS; --grep filters its lines here, not on the device."""
    command = DIAGNOSTICS[args.name]
    pattern = re.compile(args.grep) if args.grep else None
    device = connect(args)
    output = device.shell(command, args.timeout)
    if output is None:
        print("shell command failed", file=sys.stderr)
        return 1
    for line in output.splitlines():
        if pattern is None or pattern.search(line):
            print(line)
    return 0


def cmd_steps(args) -> int:
    steps = load_steps(args.file)
    device = connect(args)
    recognizer = Recognizer(device.controller) if any(isinstance(s, dict) and "reco" in s for s in steps) else None
    report = run_steps(device, steps, out=Path(args.out) if args.out else None, recognizer=recognizer)
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=_jsonable),
                                      encoding="utf-8")
    return 0


def cmd_bgcheck(args) -> int:
    """Sample the emulator window's state over time, with freshness and a harmless input each time (JSON lines).

    Stops, with a failing exit status, when a contact may still be held, an input failed, or the quit dialog could
    not be cancelled."""
    if args.raw:
        raise ProbeError("bgcheck works on the 720p frame its coordinates and checks assume; drop --raw")
    device = connect(args)
    recognizer = Recognizer(device.controller)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    status = 0
    try:
        for index in range(args.samples):
            if index:
                time.sleep(args.interval)
            try:
                sample = background_sample(device, recognizer)
            except ProbeError as error:  # e.g. focus left the game: record it and stop sending
                sample = {"time": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                          "window": mac_window_state(), "error": str(error), "stop": True}
            if device.held:
                sample["held_after_sample"] = sorted(device.held)
            with out.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(sample, ensure_ascii=False, default=_jsonable) + "\n")
            print(json.dumps(sample, ensure_ascii=False, default=_jsonable), flush=True)
            failed_input = sample.get("tap_ok") is False or sample.get("back_ok") is False \
                or sample.get("quit_cancelled") is False or sample.get("quit_dismissed") is False
            if sample.get("stop") or failed_input or device.held:
                status = 1
                break
    finally:
        failed = device.release_all()
        if failed:
            print(f"ERROR: could not lift contacts {failed}; check the device before sending more input",
                  file=sys.stderr, flush=True)
            status = 2
    return status


def cmd_reco(args) -> int:
    if args.live:
        controller = connect(args).controller
    else:
        controller = frame_controller(Path(args.frame))
    for result in Recognizer(controller).run_many(args.nodes, any_roi=args.any_roi):
        print(json.dumps(result, ensure_ascii=False, default=_jsonable))
    return 0


def cmd_reco_all(args) -> int:
    recognizer = Recognizer(frame_controller(Path(args.frame)))
    results = recognizer.run_many(recognition_nodes(args.filter), any_roi=args.any_roi)
    for result in results:
        if result["hit"] or result.get("widened_hit"):
            print(json.dumps(result, ensure_ascii=False, default=_jsonable), flush=True)
    summary = {"frame": str(args.frame), "nodes": len(results), "hit": sum(r["hit"] for r in results),
               "widened_hit": sum(bool(r.get("widened_hit")) for r in results)}
    print(json.dumps(summary, ensure_ascii=False))
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False,
                                              indent=1, default=_jsonable), encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--adb", default=os.environ.get("MAANIKKI_ADB", DEFAULT_ADB))
    parser.add_argument("--serial", default=os.environ.get("MAANIKKI_ADB_SERIAL", DEFAULT_SERIAL))
    parser.add_argument("--input", choices=sorted(INPUT_METHODS), default="maatouch")
    parser.add_argument("--screencap", choices=sorted(SCREENCAP_METHODS), default="netcat")
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--raw", action="store_true", help="device pixels instead of the 1280x720 frame")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_purpose(p):
        p.add_argument("--purpose", required=True, help="why this input is sent; written to the input log")
        p.add_argument("--after", help="save a frame to this directory after the input")
        p.add_argument("--after-ms", type=int, default=600)
        return p

    sub.add_parser("info").set_defaults(func=cmd_info)
    p = sub.add_parser("capture")
    p.add_argument("--out", required=True)
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--interval", type=int, default=0)
    p.add_argument("--prefix", default="")
    p.set_defaults(func=cmd_capture)
    p = with_purpose(sub.add_parser("tap"))
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--contact", type=int, default=0)
    p.add_argument("--hold", type=int, default=0, help="hold milliseconds (down/up instead of click)")
    p.set_defaults(func=cmd_tap)
    p = with_purpose(sub.add_parser("swipe"))
    for name in ("x1", "y1", "x2", "y2"):
        p.add_argument(name, type=int)
    p.add_argument("--duration", type=int, default=300)
    p.set_defaults(func=cmd_swipe)
    p = with_purpose(sub.add_parser("key"))
    p.add_argument("keycode", type=int, help="Android KEYCODE_* number")
    p.set_defaults(func=cmd_key)
    p = with_purpose(sub.add_parser("text"))
    p.add_argument("value")
    p.set_defaults(func=cmd_text)
    p = sub.add_parser("shell")
    p.add_argument("name", choices=sorted(DIAGNOSTICS))
    p.add_argument("--grep", help="keep only lines matching this regular expression")
    p.add_argument("--timeout", type=int, default=10000)
    p.set_defaults(func=cmd_shell)
    for name in ("steps", "gesture"):
        p = sub.add_parser(name)
        p.add_argument("file")
        p.add_argument("--purpose", required=True)
        p.add_argument("--out", help="directory for capture steps")
        p.add_argument("--report", help="write the step report as JSON")
        p.set_defaults(func=cmd_steps)
    p = sub.add_parser("bgcheck")
    p.add_argument("--purpose", required=True)
    p.add_argument("--out", required=True, help="JSON lines file to append samples to")
    p.add_argument("--samples", type=int, default=1)
    p.add_argument("--interval", type=float, default=120)
    p.set_defaults(func=cmd_bgcheck)
    p = sub.add_parser("reco")
    p.add_argument("nodes", nargs="+")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--frame")
    group.add_argument("--live", action="store_true")
    p.add_argument("--any-roi", action="store_true")
    p.set_defaults(func=cmd_reco)
    p = sub.add_parser("reco-all")
    p.add_argument("--frame", required=True)
    p.add_argument("--filter")
    p.add_argument("--any-roi", action="store_true")
    p.add_argument("--json")
    p.set_defaults(func=cmd_reco_all)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ProbeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
