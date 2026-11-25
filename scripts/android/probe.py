"""Developer probe for the game's Android client in an emulator, driven through MaaFramework's AdbController.

Never imported by the agent or the client; it exists to survey what the emulator route can do
(docs/android-support-plan.md). Every input it sends is appended to an input log (JSON lines,
default .build/android/input-log.jsonl) with the time, the command, its arguments and a required
--purpose. Before any input it checks that the game's activity is the focused window and refuses
otherwise, so a tap never lands in the launcher or another app.

    python -I scripts/android/probe.py info
    python -I scripts/android/probe.py capture --out DIR [--count N] [--raw]
    python -I scripts/android/probe.py tap X Y --purpose TEXT [--hold MS] [--after DIR]
    python -I scripts/android/probe.py swipe X1 Y1 X2 Y2 --duration MS --purpose TEXT
    python -I scripts/android/probe.py gesture FILE.json --purpose TEXT
    python -I scripts/android/probe.py key KEYCODE --purpose TEXT
    python -I scripts/android/probe.py text STRING --purpose TEXT
    python -I scripts/android/probe.py shell COMMAND
    python -I scripts/android/probe.py reco NODE [NODE ...] (--frame PNG | --live) [--any-roi]
    python -I scripts/android/probe.py reco-all --frame PNG [--filter REGEX] [--any-roi] [--json OUT]
    python -I scripts/android/probe.py steps FILE.json --purpose TEXT

Coordinates are in the agent's 1280x720 frame (the controller scales them to the device) unless
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
    {"wait": MS}
    {"capture": "NAME"}                 save a 720p frame as NAME.png under --out
    {"reco": ["NODE", ...]}             recognize on a fresh frame, printed and kept in the report

Contacts still held when the file ends, or when any step fails or raises, are lifted. `reco`
loads resource/ as the agent does (pipeline, images, OCR model) and registers the agent's custom
recognitions (nikki.ui_template, nikki.crown_ready) unguarded, so nodes are judged as the agent
would judge them. --any-roi re-runs a missed node with its ROI widened to the whole frame, which
tells "the element exists but moved" apart from "the element is not there".
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
    """Package of the focused window from `dumpsys window`, or None when it cannot be read."""
    output = shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'") or ""
    for line in output.splitlines():
        match = re.search(r"\su0\s+([\w.]+)/", line)
        if match:
            return match.group(1)
    return None


class Device:
    """One AdbController connection plus the guard and log every input goes through."""

    def __init__(self, controller, log: InputLog, *, raw: bool, guard: bool = True):
        self.controller = controller
        self.log = log
        self.raw = raw
        self.guard = guard
        self.held: set[int] = set()

    # Shell runs through the controller's own adb, so it shares the transport the inputs use.
    def shell(self, command: str, timeout_ms: int = 10000) -> str | None:
        job = self.controller.post_shell(command, timeout_ms).wait()
        return job.get() if job.succeeded else None

    def check_foreground(self) -> None:
        if not self.guard:
            return
        package = focused_package(self.shell)
        if package != GAME_PACKAGE:
            raise ProbeError(f"the focused window is {package!r}, not the game; refusing to send input")

    def _send(self, command: str, args, job_factory) -> bool:
        started = time.perf_counter()
        try:
            ok = bool(job_factory().wait().succeeded)
        except BaseException:
            self.log.write(command, args, None, error="interrupted or raised while waiting")
            raise
        self.log.write(command, args, ok, ms=round((time.perf_counter() - started) * 1000, 1))
        return ok

    def tap(self, x, y, contact=0, hold_ms=0) -> bool:
        if hold_ms <= 0:
            return self._send("tap", [x, y, contact], lambda: self.controller.post_click(x, y, contact, 1))
        if not self.down(x, y, contact):
            return False
        time.sleep(hold_ms / 1000)
        return self.up(contact)

    def swipe(self, x1, y1, x2, y2, ms) -> bool:
        return self._send("swipe", [x1, y1, x2, y2, ms],
                          lambda: self.controller.post_swipe(x1, y1, x2, y2, ms))

    def down(self, x, y, contact=0) -> bool:
        self.held.add(contact)  # before sending: a down that raised may still have reached the device
        return self._send("touch_down", [x, y, contact], lambda: self.controller.post_touch_down(x, y, contact, 1))

    def move(self, x, y, contact=0) -> bool:
        return self._send("touch_move", [x, y, contact], lambda: self.controller.post_touch_move(x, y, contact, 1))

    def up(self, contact=0) -> bool:
        ok = self._send("touch_up", [contact], lambda: self.controller.post_touch_up(contact))
        if ok:
            self.held.discard(contact)
        return ok

    def release_all(self) -> list[int]:
        """Lift every contact this connection may still hold; returns the contacts whose up failed."""
        failed = []
        for contact in sorted(self.held):
            try:
                if not self.up(contact):
                    failed.append(contact)
            except Exception:
                failed.append(contact)
        return failed

    def key(self, code) -> bool:
        return self._send("key", [code], lambda: self.controller.post_click_key(code))

    def text(self, value) -> bool:
        return self._send("text", [value], lambda: self.controller.post_input_text(value))

    def drag(self, x1, y1, x2, y2, ms, contact=0, steps=10) -> bool:
        """Touch down and move in `steps` even moves over `ms`; the contact stays held."""
        if not self.down(x1, y1, contact):
            return False
        steps = max(1, int(steps))
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
    log = InputLog(Path(args.log), getattr(args, "purpose", None))
    device = Device(controller, log, raw=getattr(args, "raw", False), guard=not getattr(args, "no_guard", False))
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
        self._request: tuple[list[str], bool] = ([], False)
        self._results: list[dict] | None = None
        self.last_frame = None

    def node(self, name: str) -> dict:
        data = self.resource.get_node_data(name)
        if data is None:
            raise ProbeError(f"no pipeline node {name!r}")
        return data

    def _judge(self, context) -> list[dict]:
        names, any_roi = self._request
        job = context.tasker.controller.post_screencap().wait()
        if not job.succeeded:
            return [{"node": name, "hit": False, "error": "capture failed"} for name in names]
        image = job.get()
        self.last_frame = image
        results = []
        for name in names:
            result = judged(name, context.run_recognition(name, image))
            if any_roi and not result["hit"]:
                override = full_roi_override(self.node(name))
                if override:
                    widened = judged(name, context.run_recognition(name, image, {name: override}))
                    result.update(widened_hit=widened["hit"], widened_box=widened["box"],
                                  widened_detail=widened["detail"])
                else:
                    result["widened_hit"] = None  # And/Or/DirectHit: nothing to widen
            results.append(result)
        return results

    def run_many(self, names: list[str], *, any_roi: bool = False) -> list[dict]:
        for name in names:
            self.node(name)
        self._request, self._results = (list(names), any_roi), None
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
    return {"node": name, "hit": bool(reco.hit), "box": list(reco.box) if reco.box else None,
            "algorithm": str(getattr(reco.algorithm, "value", reco.algorithm)), "detail": detail}


def _score(item):
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("score"), (int, float)):
        return float(item["score"])
    inner = item.get("detail")
    if isinstance(inner, dict) and isinstance(inner.get("score"), (int, float)):
        return float(inner["score"])
    return None


def full_roi_override(node: dict) -> dict:
    """Pipeline override that widens a node's ROI to the whole frame (built-in and nikki.ui_template).

    Returns {} for And/Or (their sub-recognitions carry their own ROIs) and anything else without one.
    """
    whole = [0, 0, FRAME[0], FRAME[1]]
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
                time.sleep(float(step["wait"]) / 1000)
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
                ok = True
            else:
                ok = _input_step(device, step, contact)
            entry["ok"] = ok
            entry["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
            report.append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
            if not ok:
                raise ProbeError(f"step {index} failed: {step!r}")
    finally:
        failed = device.release_all()
        if failed:
            print(f"WARNING: could not lift contacts {failed}", file=sys.stderr, flush=True)
    return report


def _input_step(device: Device, step: dict, contact: int) -> bool:
    if "down" in step:
        return device.down(*step["down"], contact)
    if "move" in step:
        return device.move(*step["move"], contact)
    if "up" in step:
        return device.up(int(step["up"]))
    if "drag" in step:
        return device.drag(*step["drag"], int(step.get("ms", 300)), contact, int(step.get("steps", 10)))
    if "tap" in step:
        return device.tap(*step["tap"], contact, int(step.get("hold", 0)))
    if "swipe" in step:
        return device.swipe(*step["swipe"], int(step.get("ms", 300)))
    if "key" in step:
        return device.key(int(step["key"]))
    if "text" in step:
        return device.text(str(step["text"]))
    raise ProbeError(f"unknown step {step!r}")


def load_steps(path: Path) -> list:
    steps = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(steps, list):
        raise ProbeError(f"{path} is not a JSON list of steps")
    return steps


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

    device.check_foreground()
    try:
        ok = action()
    finally:
        failed = device.release_all()
    if failed:
        print(f"WARNING: could not lift contacts {failed}", file=sys.stderr)
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
    device = connect(args)
    output = device.shell(args.command, args.timeout)
    if output is None:
        print("shell command failed", file=sys.stderr)
        return 1
    print(output, end="" if output.endswith("\n") else "\n")
    return 0


def cmd_steps(args) -> int:
    steps = load_steps(args.file)
    device = connect(args)
    recognizer = Recognizer(device.controller) if any(isinstance(s, dict) and "reco" in s for s in steps) else None
    device.check_foreground()
    report = run_steps(device, steps, out=Path(args.out) if args.out else None, recognizer=recognizer)
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def cmd_reco(args) -> int:
    if args.live:
        controller = connect(args).controller
    else:
        controller = frame_controller(Path(args.frame))
    for result in Recognizer(controller).run_many(args.nodes, any_roi=args.any_roi):
        print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


def cmd_reco_all(args) -> int:
    recognizer = Recognizer(frame_controller(Path(args.frame)))
    results = recognizer.run_many(recognition_nodes(args.filter), any_roi=args.any_roi)
    for result in results:
        if result["hit"] or result.get("widened_hit"):
            print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
    summary = {"frame": str(args.frame), "nodes": len(results), "hit": sum(r["hit"] for r in results),
               "widened_hit": sum(bool(r.get("widened_hit")) for r in results)}
    print(json.dumps(summary, ensure_ascii=False))
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False,
                                              indent=1, default=str), encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--adb", default=os.environ.get("MAANIKKI_ADB", DEFAULT_ADB))
    parser.add_argument("--serial", default=os.environ.get("MAANIKKI_ADB_SERIAL", DEFAULT_SERIAL))
    parser.add_argument("--input", choices=sorted(INPUT_METHODS), default="maatouch")
    parser.add_argument("--screencap", choices=sorted(SCREENCAP_METHODS), default="netcat")
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--raw", action="store_true", help="device pixels instead of the 1280x720 frame")
    parser.add_argument("--no-guard", action="store_true", help="skip the focused-window check (non-game tests)")
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
    p.add_argument("command")
    p.add_argument("--timeout", type=int, default=10000)
    p.set_defaults(func=cmd_shell)
    for name in ("steps", "gesture"):
        p = sub.add_parser(name)
        p.add_argument("file")
        p.add_argument("--purpose", required=True)
        p.add_argument("--out", help="directory for capture steps")
        p.add_argument("--report", help="write the step report as JSON")
        p.set_defaults(func=cmd_steps)
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
