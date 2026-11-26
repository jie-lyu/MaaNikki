"""Headless runner: run a MaaNikki task on the Android emulator with the agent and MaaFramework, no client.

    .venv/bin/python scripts/android/runner.py run MaaNikki_AndroidCheck --purpose "why this run"
    .venv/bin/python scripts/android/runner.py run MaaNikki_Mailbox --option NAME=CASE --purpose "..."
    .venv/bin/python scripts/android/runner.py tasks          # the entries the Android interface defines
    .venv/bin/python scripts/android/runner.py run MaaNikki_AndroidWalkProbe --option hint=X,Y --option target=X,Y \
        [--option map=miraland --option timeout=S --option leash=U --option radius=U] --purpose "..."
                                                              # dev-only: a supervised joystick walk (see DEV_ENTRIES)
    .venv/bin/python scripts/android/runner.py run MaaNikki_AndroidCrystalRoute --purpose "..."
                                                              # dev-only: 星海 bell + route 3 (always route 3)

What it does, the way the client will (docs/android-support-plan.md, B6):
- connects with the nikki_android controller of build/android/interface-overlay.json (input Maatouch and capture
  RawWithGzip only, frames scaled to 720p, MaaAgentBinary for the Maatouch helper), refusing a frame that is not
  16:9 landscape;
- loads resource/ and resource_android/ before the first recognition;
- starts the agent (agent/main.py) as the client does, with PI_CONTROLLER set to that controller and its own data
  root (default .build/android/runner-data, so the client's data is not touched), and binds its actions;
- runs one task entry with its options resolved from the Android interface (defaults unless --option is given);
- reports the task's result and the input and capture methods the framework log says were used.

Every blocking ADB or framework step has a timeout; when one runs out the runner stops with a message to restart the
ADB server (adb kill-server; adb start-server): three ADB installations on this machine fight over port 5037 and the
server wedges with `adb devices` still saying "device". Inputs are sent only by the agent's own guarded paths; the
runner itself sends none.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

DEFAULT_ADB = "/opt/homebrew/bin/adb"
DEFAULT_SERIAL = "127.0.0.1:5555"
DEFAULT_DATA = ROOT / ".build" / "android" / "runner-data"
LOG_ROOT = ROOT / ".build" / "android" / "runner-logs"
RUN_LOG = ROOT / ".build" / "android" / "runner-runs.jsonl"
CONTROLLER = "nikki_android"
ADB_RESTART = "ADB 没有响应：请重启 ADB server（adb kill-server; adb start-server），确认模拟器还在，再重试。"

INPUT_METHODS = {"AdbShell": 1, "MinitouchAndAdbKey": 2, "Maatouch": 4, "EmulatorExtras": 8}
SCREENCAP_METHODS = {"EncodeToFileAndPull": 1, "Encode": 2, "RawWithGzip": 4, "RawByNetcat": 8,
                     "MinicapDirect": 16, "MinicapStream": 32, "EmulatorExtras": 64}


class RunnerError(Exception):
    pass


def masks(controller: dict) -> tuple[int, int]:
    """(input, screencap) masks from the controller's adb block; only the known method names, OR-ed."""
    adb = controller.get("adb") or {}

    def mask(value, table, what):
        names = value if isinstance(value, list) else [value]
        if not names or any(name not in table for name in names):
            raise RunnerError(f"unknown {what} method {value!r} in {CONTROLLER}; known: {sorted(table)}")
        result = 0
        for name in names:
            result |= table[name]
        return result
    return mask(adb.get("input"), INPUT_METHODS, "input"), mask(adb.get("screencap"), SCREENCAP_METHODS, "screencap")


def android_interface() -> dict:
    from android_interface import android_interface as build
    return build()


def controller_definition(interface: dict) -> dict:
    for controller in interface["controller"]:
        if controller["name"] == CONTROLLER:
            return controller
    raise RunnerError(f"{CONTROLLER} is not in the Android interface")


def interface_options(interface: dict) -> tuple[list[dict], dict]:
    """Every task and option definition: interface.json's own plus its imports (tasks/*.json, options/*.json)."""
    tasks, options = list(interface.get("task", [])), dict(interface.get("option", {}))
    for name in interface.get("import", []):
        data = json.loads((ROOT / name).read_text(encoding="utf-8"))
        tasks.extend(data.get("task", []))
        options.update(data.get("option", {}))
    return tasks, options


def task_override(interface: dict, entry: str, chosen: dict[str, str]) -> list[dict]:
    """The pipeline override for ``entry``: the task's own override, then each of its options (and the controller's
    and resource's) at the chosen case or its default case. Unknown options or cases are errors."""
    tasks, options = interface_options(interface)
    matches = [task for task in tasks if task.get("entry") == entry]
    if not matches:
        raise RunnerError(f"no task with entry {entry!r} in the Android interface")
    task = matches[0]
    allowed = task.get("controller")
    if allowed and CONTROLLER not in allowed:
        raise RunnerError(f"{entry} is limited to controllers {allowed}")
    overrides = [task["pipeline_override"]] if task.get("pipeline_override") else []
    names = list(controller_definition(interface).get("option", []))
    for resource in interface.get("resource", []):
        names.extend(resource.get("option", []))
    names.extend(task.get("option", []))
    unknown = set(chosen) - set(names)
    if unknown:
        raise RunnerError(f"{entry} has no option {sorted(unknown)}; its options: {names}")
    for name in names:
        definition = options.get(name)
        if definition is None:
            raise RunnerError(f"option {name} is not defined")
        cases = {case["name"]: case for case in definition.get("cases", [])}
        checkbox = definition.get("type") == "checkbox"
        if name in chosen and "," in chosen[name] and not checkbox:
            raise RunnerError(f"option {name} takes one case, got {chosen[name]!r}")
        selected = (chosen[name].split(",") if checkbox else chosen[name]) if name in chosen \
            else definition.get("default_case")
        if selected is None:
            if definition.get("cases"):
                raise RunnerError(f"option {name} has no default case; pass --option {name}=CASE ({sorted(cases)})")
            continue  # an input-type option without cases: not supported here
        # A checkbox option selects several cases (its default is a list; on the command line A,B).
        for case_name in selected if isinstance(selected, list) else [selected]:
            if case_name not in cases:
                raise RunnerError(f"option {name} has no case {case_name!r}; cases: {sorted(cases)}")
            if cases[case_name].get("pipeline_override"):
                overrides.append(cases[case_name]["pipeline_override"])
    return overrides


# Dev-only entries: not in the Android interface (so no client offers them) nor in agent/host/android/tasks.json. The
# runner alone runs them: their --option values are free values (NAME=VALUE, not cases) turned into the entry node's
# custom_action_param, and the agent of that one run is told it may run the entry (host.android.DEV_ENTRIES_ENV; the
# agent accepts only names in its own fixed host.android.DEV_ENTRIES).
DEV_ENTRIES_ENV = "MAANIKKI_ANDROID_DEV_ENTRIES"
WALK_PROBE = "MaaNikki_AndroidWalkProbe"
WALK_OPTIONS = {"map": str, "hint": "point", "target": "point", "timeout": float, "leash": float, "radius": float}
# The 星海 crystal route (host.android.crystal_route): no options; it always walks xinghai_crystal_3.
CRYSTAL_ROUTE = "MaaNikki_AndroidCrystalRoute"
RUNNER_DEV_ENTRIES = (WALK_PROBE, CRYSTAL_ROUTE)


def dev_override(entry: str, chosen: dict[str, str]) -> list[dict]:
    """The pipeline override of a dev-only entry from its --option values (WALK_PROBE: map, hint=X,Y, target=X,Y,
    timeout, leash, radius; hint and target required; CRYSTAL_ROUTE: none). The agent checks the ranges again."""
    if entry == CRYSTAL_ROUTE:
        if chosen:
            raise RunnerError(f"{entry} 没有选项（固定走 3 号路线）")
        return [{entry: {"custom_action_param": {"kind": "android_crystal_route"}}}]
    if entry != WALK_PROBE:
        raise RunnerError(f"{entry} 不是开发用的入口")
    unknown = set(chosen) - set(WALK_OPTIONS)
    if unknown:
        raise RunnerError(f"{entry} 没有选项 {sorted(unknown)}；可用的选项：{sorted(WALK_OPTIONS)}")
    missing = {"hint", "target"} - set(chosen)
    if missing:
        raise RunnerError(f"{entry} 需要 --option {'、'.join(f'{name}=X,Y' for name in sorted(missing))}（地图坐标）")
    param = {"kind": "android_walk_probe", "map": "miraland"}
    for name, value in chosen.items():
        kind = WALK_OPTIONS[name]
        try:
            if kind == "point":
                x, y = (float(v) for v in value.split(","))
                if not (math.isfinite(x) and math.isfinite(y)):
                    raise ValueError(value)
                param[name] = [x, y]
            else:
                param[name] = kind(value)
                if kind is float and not math.isfinite(param[name]):
                    raise ValueError(value)
        except ValueError:
            form = "X,Y（两个数，用英文逗号分开）" if kind == "point" else "一个数" if kind is float else "地图名"
            raise RunnerError(f"选项 {name}={value!r} 无效，应为{form}") from None
    return [{entry: {"custom_action_param": param}}]


def within(seconds: float, what: str, function):
    """``function()`` in a worker thread; RunnerError with the ADB restart advice if it does not return in time
    (the native call cannot be cancelled, so the runner then has to exit)."""
    box = {}

    def work():
        try:
            box["value"] = function()
        except BaseException as error:  # handed to the caller
            box["error"] = error
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise RunnerError(f"{what} 超过 {seconds:g} 秒没有完成。{ADB_RESTART}")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def adb_alive(adb: str, serial: str, seconds: float = 8) -> None:
    """A shell round trip through the ADB server, with a timeout: `adb devices` can say "device" while every
    shell call hangs."""
    try:
        result = subprocess.run([adb, "-s", serial, "shell", "echo", "maanikki-ok"], capture_output=True,
                                text=True, timeout=seconds)
    except subprocess.TimeoutExpired:
        raise RunnerError(f"adb shell 超过 {seconds:g} 秒没有回应。{ADB_RESTART}") from None
    except OSError as error:
        raise RunnerError(f"无法运行 {adb}：{error}") from None
    if result.returncode != 0 or "maanikki-ok" not in result.stdout:
        raise RunnerError(f"adb shell 失败（{result.stderr.strip() or result.returncode}）。{ADB_RESTART}")


def methods_in_log(log_text: str) -> dict:
    """What the framework log says about the ADB input and capture methods in use (lines quoted, not parsed)."""
    words = ("method_vector", "method_set", "Testing ", "[method=")
    lines = [line.strip() for line in log_text.splitlines() if any(word in line for word in words)]
    text = "\n".join(lines)
    return {"input": [name for name in INPUT_METHODS if f'"{name}"' in text],
            "screencap_tested": [name for name in SCREENCAP_METHODS if f"Testing {name}" in text],
            "maatouch": '"Maatouch"' in text, "minitouch": "initouch" in text.lower(),
            "lines": [line[line.find("]", line.find("][", 60)) + 1:][:200] for line in lines][-8:]}


def agent_environment(controller: dict, data_root: Path, dev_entry: str | None = None) -> dict:
    """The agent's environment; DEV_ENTRIES_ENV names ``dev_entry`` only (an inherited value is never passed on)."""
    env = {key: value for key, value in os.environ.items() if key.upper() not in ("PI_CONTROLLER", DEV_ENTRIES_ENV)}
    env.update({"PI_CONTROLLER": json.dumps(controller, ensure_ascii=False),
                "MAANIKKI_DATA_ROOT": str(data_root), "MAANIKKI_RESOURCE_ROOT": str(ROOT), "PYTHONUTF8": "1"})
    if dev_entry is not None:
        env[DEV_ENTRIES_ENV] = dev_entry
    return env


DEVICE_BUSY = "另一个 MaaNikki 进程正在驱动这台模拟器（MaaNikki 客户端，或另一个运行器），请等它结束后再运行。"


# The fixed directory of the host-wide device locks, the client's android_input::DEVICE_LOCK_DIR: never TMPDIR or the
# data root, so the runner and the client lock the same file for one serial however each was launched.
DEVICE_LOCK_DIR = Path("/tmp")


def device_lock_dir() -> Path:
    """The fixed DEVICE_LOCK_DIR (never follows TMPDIR)."""
    return DEVICE_LOCK_DIR


def device_lock_path(serial: str, directory: Path | None = None) -> Path:
    """The host-wide lock file for one emulator, the same name the client uses (android_input::device_lock_path):
    maanikki-android-<serial>.lock with every character outside [A-Za-z0-9._-] replaced by _."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", serial)
    return (device_lock_dir() if directory is None else directory) / f"maanikki-android-{name}.lock"


def take_device_lock(serial: str):
    """An open handle holding an exclusive, non-blocking flock on the emulator's device lock, held for the whole run:
    the client takes the same lock while its Android run slot is busy, so the runner and the client (whose data roots
    and owner locks differ) never drive one emulator at the same time. RunnerError when it cannot be taken. The lock file
    is never removed."""
    try:
        import fcntl
    except ImportError:
        raise RunnerError("这台电脑上无法独占模拟器（没有 flock），运行器不运行。") from None
    path = device_lock_path(serial)
    try:
        handle = open(path, "a+b")
    except OSError as error:
        raise RunnerError(f"无法打开 {path}：{error}") from None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RunnerError(f"{DEVICE_BUSY}（{path}）") from None
    except OSError as error:
        handle.close()
        raise RunnerError(f"无法锁定 {path}：{error}") from None
    return handle


class SignalDeferral:
    """Once started, SIGINT and SIGTERM are recorded instead of acting, until restore(): from the first signal of a stop
    until the stop handshake, the agent's reaping and the run record are done. Main thread only (elsewhere a no-op)."""

    def __init__(self):
        self.signals: list[int] = []
        self.previous: dict[int, object] | None = None

    def start(self) -> None:
        if self.previous is not None or threading.current_thread() is not threading.main_thread():
            return
        self.previous = {}
        for number in (signal.SIGINT, signal.SIGTERM):
            self.previous[number] = signal.signal(number, lambda signum, _frame: self.signals.append(signum))

    def restore(self) -> None:
        if self.previous is not None:
            for number, handler in self.previous.items():
                signal.signal(number, handler)
            self.previous = None


def run(args) -> int:
    """One task run, holding the emulator's device lock from before the runner touches the device until it returns."""
    device_lock = take_device_lock(args.serial)
    try:
        return _run(args)
    finally:
        device_lock.close()


def _run(args) -> int:
    from maa.agent_client import AgentClient
    from maa.controller import AdbController
    from maa.resource import Resource
    from maa.tasker import Tasker

    interface = android_interface()
    controller_def = controller_definition(interface)
    input_mask, screencap_mask = masks(controller_def)
    chosen = dict(option.split("=", 1) for option in args.option)
    dev_entry = args.entry if args.entry in RUNNER_DEV_ENTRIES else None
    override = dev_override(args.entry, chosen) if dev_entry else task_override(interface, args.entry, chosen)
    started = dt.datetime.now().astimezone()
    log_dir = LOG_ROOT / started.strftime("%Y%m%d-%H%M%S")
    log_dir.mkdir(parents=True, exist_ok=True)
    data_root = Path(args.data_root).resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    record = {"time": started.isoformat(timespec="seconds"), "entry": args.entry, "options": chosen,
              "purpose": args.purpose, "serial": args.serial, "log_dir": str(log_dir)}

    adb_alive(args.adb, args.serial)
    record["marker_recovery"] = check_unconfirmed(data_root, args.clear_unconfirmed,
                                                  lambda: device_reading(args.adb, args.serial), args.serial)
    clear_stop_request(data_root)
    Tasker.set_log_dir(str(log_dir))
    agent_binary = ROOT / ".venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / \
        "site-packages" / "MaaAgentBinary"
    controller = AdbController(args.adb, args.serial, screencap_methods=screencap_mask, input_methods=input_mask,
                               config={}, agent_path=str(agent_binary))
    controller.set_screenshot_target_short_side(controller_def.get("display_short_side", 720))
    if not within(30, "连接模拟器", lambda: controller.post_connection().wait().succeeded):
        raise RunnerError(f"无法连接 {args.serial}；见 {log_dir / 'maafw.log'}")
    if not within(15, "第一次截图", lambda: controller.post_screencap().wait().succeeded):
        raise RunnerError("连接后的第一次截图失败")
    width, height = controller.resolution
    if width <= height or abs(width * 9 - height * 16) > 16:
        raise RunnerError(f"模拟器分辨率 {width}x{height} 不是 16:9 横屏")

    resource = Resource()
    for bundle in (ROOT / "resource", ROOT / "resource_android"):
        if not within(120, f"加载 {bundle.name}", lambda bundle=bundle: resource.post_bundle(bundle).wait().succeeded):
            raise RunnerError(f"无法加载 {bundle}")
    client = AgentClient()
    if not client.bind(resource):
        raise RunnerError("agent 客户端无法绑定资源")
    agent_log = open(log_dir / "agent.log", "w", encoding="utf-8")
    child = subprocess.Popen([sys.executable, str(ROOT / "agent" / "main.py"), client.identifier], cwd=str(ROOT),
                             env=agent_environment(controller_def, data_root, dev_entry), stdout=agent_log,
                             stderr=subprocess.STDOUT)
    tasker = None
    # From the first SIGINT or SIGTERM on, further ones are deferred (SignalDeferral): the stop handshake, which may wait
    # for an in-flight click, is never cut short. While the task runs, SIGTERM stops it the way Ctrl-C does.
    deferral = SignalDeferral()
    before_run = None  # the handlers to restore at the end, when this run installed its own
    if threading.current_thread() is threading.main_thread():
        def stop_signal(_signum, _frame):
            deferral.start()
            raise KeyboardInterrupt
        before_run = {number: signal.signal(number, stop_signal) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        if not within(60, "连接 agent", client.connect):
            raise RunnerError(f"agent 没有连上；见 {log_dir / 'agent.log'}")
        tasker = Tasker()
        if not tasker.bind(resource, controller):
            raise RunnerError("Tasker 无法绑定资源和控制器")
        job = tasker.post_task(args.entry, override)
        detail = within(args.timeout, f"任务 {args.entry}", lambda: job.wait().get())
        record["status"] = "succeeded" if job.succeeded else "failed"
        record["nodes"] = [getattr(node, "name", None) for node in (getattr(detail, "nodes", None) or [])][-10:]
    except KeyboardInterrupt:
        deferral.start()  # before the handshake: a second signal must not cut it short
        record["status"] = "interrupted"
        record["stop_requested"] = stop_tasker(tasker, data_root, record, child, client)
        raise RunnerError("运行被中断（Ctrl-C 或 SIGTERM）；已请求停止任务。") from None
    except RunnerError as error:
        deferral.start()
        record["status"] = "error"
        record["error"] = str(error)
        record["stop_requested"] = stop_tasker(tasker, data_root, record, child, client)
        raise
    finally:
        # Once the agent exists, a signal during cleanup is deferred until the child is reaped and the run record is
        # written and printed (and, on a stop, already during the handshake above).
        deferral.start()
        if before_run is not None and deferral.previous is not None:
            deferral.previous = before_run  # restored at the end, not the stop handler installed while the task ran
        deferred = deferral.signals
        try:
            try:
                within(15, "断开 agent", client.disconnect)
            except (RunnerError, Exception):
                record["disconnect"] = "timed out or failed"
            record["agent_exit"] = reap(child)
            agent_log.close()
            # The agent is gone (nothing more is posted); the controller is dropped when this returns.
            drain_controller(controller, data_root, record)
            if record["agent_exit"] == "unreaped":
                record["status"] = "error"
                record["error"] = "agent 子进程没能结束（终止和强制结束都没有确认）"
            log_file = log_dir / "maafw.log"
            record["methods"] = methods_in_log(log_file.read_text(encoding="utf-8", errors="replace")) \
                if log_file.exists() else None
            record["agent_messages"] = agent_messages(data_root, started)
            record["touches"] = touches(data_root, started)
            if deferred:
                record["interrupted_during_cleanup"] = True
            RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
            with RUN_LOG.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(json.dumps(record, ensure_ascii=False, indent=1))
        finally:
            deferral.restore()
        if deferred:
            record["interrupted_during_cleanup"] = True
    if record.get("interrupted_during_cleanup"):
        return 130
    return 0 if record.get("status") == "succeeded" else 1


RELEASE_ADVICE = ("设备没有报告触点已经全部抬起（或者标记记的不是现在连着的这台设备（序列号和启动号）、标记读不出来），重启 ADB 或看画面都证明不了"
                  "那个触点已经抬起，所以运行器停下，需要人来决定：先报告协调者或维护者；确认可以继续时，用 --clear-unconfirmed "
                  "加上上面的指纹重跑。")


DRAIN_SECONDS = 10.0


def drain_controller(controller, data_root: Path, record: dict, seconds: float | None = None) -> None:
    """Before the controller is dropped: when a touch is still reserved (touch.json present: a gesture's lift may
    still be queued behind a slow DOWN, which MaaTouch keeps held, as the agent's DOWN has no auto-up), wait at most
    ``seconds`` for one harmless job (a shell ``true``, no touch) posted after everything else: the controller runs
    jobs in order, so the queued UP has run once it completes. Destroying the controller earlier lets its worker
    exit after the running DOWN without the UP. The marker itself stays for the recovery to decide."""
    if not (input_dir(data_root) / "touch.json").exists():
        return
    try:
        drained = within(DRAIN_SECONDS if seconds is None else seconds, "排空控制器队列",
                         lambda: controller.post_shell("true", 2000).wait().succeeded)
        record["controller_drain"] = "drained" if drained else "not confirmed"
    except Exception as error:  # noqa: BLE001 - recorded; the teardown goes on
        record["controller_drain"] = f"failed: {error}"


def _adb_shell(adb: str, serial: str, command: list[str], seconds: float = 10) -> str | None:
    try:
        result = subprocess.run([adb, "-s", serial, "shell", *command], capture_output=True, text=True,
                                timeout=seconds)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return result.stdout if result.returncode == 0 else None


def device_reading(adb: str, serial: str) -> dict | None:
    """The guest's boot id, touch state, queued input and MaaTouch helpers, read in one shell invocation
    (host.android.target.recovery_reading), or None."""
    sys.path.insert(0, str(ROOT / "agent"))
    from host.android.target import RECOVERY_READING, recovery_reading
    return recovery_reading(_adb_shell(adb, serial, [RECOVERY_READING]))


def marker_fingerprint(raw: bytes) -> str:
    """A short identity of a marker's raw bytes (an empty marker, left by an agent killed while creating it, has one
    too)."""
    import hashlib
    return hashlib.sha256(raw).hexdigest()[:12]


def check_unconfirmed(data_root: Path, clear: str | None, read_device=None, serial: str | None = None):
    """The runner is the recovery boundary for its own data root, as MaaNikki's start is for the client's: a touch
    marker left by an earlier run (a touch that was never confirmed released) stops the runner until a person has
    decided (RELEASE_ADVICE), unless the device itself shows the marker is settled. That needs, from one reading
    taken under the agent's owner lock before the runner connects (read_device, device_reading()): the marker names
    this very guest (serial and boot id), no contact is held, no input is queued in the dispatcher and no MaaTouch
    helper is running, so nothing an earlier sender wrote can still arrive. The recovery is written to the input log
    before the marker is removed. Clearing by hand names the marker by the fingerprint printed with the refusal (a
    fingerprint that does not match refuses outright); either way the marker's bytes are checked under the owner
    lock, so a different, later marker is never cleared. Returns what was done (None when there was no marker)."""
    marker = data_root / "config" / "android-input" / "touch.json"
    if not marker.exists():
        return None
    try:
        raw = marker.read_bytes()
    except OSError as error:
        raise RunnerError(f"读不了留下的触摸标记 {marker}：{error}") from None
    fingerprint = marker_fingerprint(raw)
    try:
        content = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        content = None
    content = content if isinstance(content, dict) else {}
    since, named = content.get("since"), content.get("device")
    by_device = clear is None and read_device is not None and bool(serial) and isinstance(named, dict) \
        and named.get("serial") == serial and bool(named.get("boot_id"))

    def refuse(why):
        given = "" if clear is None else f"（给的指纹 {clear} 对不上）"
        return RunnerError(f"上一次运行留下了没有确认抬起的触摸{given}：{marker}，时间 {since or '不明'}，指纹 {fingerprint}"
                           f"{'，' + why if why else ''}。{RELEASE_ADVICE}")
    if clear != fingerprint and not by_device:
        raise refuse(f"标记记的设备 {named or '没有'}，现在连的是 {serial or '不明'}" if clear is None else "")
    sys.path.insert(0, str(ROOT / "agent"))
    from host import UnsupportedInput, take_owner_lock
    try:
        handle = take_owner_lock(marker.parent / "owner.lock")
    except UnsupportedInput as error:
        raise RunnerError(f"处理触摸标记之前没能拿到 owner.lock：{error}") from None
    how = "fingerprint given by a person"
    try:
        try:
            current = marker.read_bytes()
        except FileNotFoundError:
            return {"marker": fingerprint, "cleared_by": "already gone"}
        if marker_fingerprint(current) != fingerprint:
            raise RunnerError("触摸标记在检查期间换了（可能有另一次运行留下了新的），没有清除，请重跑。")
        if by_device:
            reading = read_device()  # one shell call, under the owner lock: no agent can touch the device meanwhile
            settled = reading is not None and reading["boot_id"] == named["boot_id"] and reading["state"] == "none" \
                and not reading["queued"] and not reading["maatouch"]
            if not settled:
                raise refuse(f"设备读数 {reading or '读不出'}（要同一台设备、没有按住的触点、没有排队的输入、没有 MaaTouch 在运行）")
            how = "device reports this guest settled: no contact held, no queued input, no MaaTouch running"
            if not log_recovery(data_root, fingerprint, since, named, how):
                raise refuse("设备读数正常，但写不进输入记录，没有清除")
        marker.unlink()
    finally:
        handle.close()
    print(f"已清除指纹 {fingerprint} 的触摸标记（{since or '时间不明'}；{how}）", file=sys.stderr)
    return {"marker": fingerprint, "since": since, "cleared_by": how}


def log_recovery(data_root: Path, fingerprint: str, since, device, how: str) -> bool:
    """Append the recovery to the agent's input log (logs/android-input.jsonl) before the marker goes, so it is on
    record even when the run is interrupted right after."""
    entry = {"time": dt.datetime.now().astimezone().isoformat(timespec="milliseconds"), "pid": os.getpid(),
             "action": "RecoverMarker", "outcome": f"clearing marker {fingerprint} since {since} for {device}: {how}"}
    try:
        path = data_root / "logs" / "android-input.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


STOP_TOUCH_WAIT = 5.0
STOP_TOUCH_POLL = 0.05


def input_dir(data_root: Path) -> Path:
    return data_root / "config" / "android-input"


def clear_stop_request(data_root: Path) -> None:
    """Before the agent is spawned: remove a stop request a stopped run left (config/android-input/stop.request), under
    the agent's owner lock so no agent of this data root is running meanwhile. The lock file is never removed."""
    request = input_dir(data_root) / "stop.request"
    if not request.exists():
        return
    sys.path.insert(0, str(ROOT / "agent"))
    from host import UnsupportedInput, take_owner_lock
    try:
        handle = take_owner_lock(input_dir(data_root) / "owner.lock")
    except UnsupportedInput as error:
        raise RunnerError(f"清除上一次的停止请求之前没能拿到 owner.lock：{error}") from None
    try:
        request.unlink(missing_ok=True)
    except OSError as error:
        raise RunnerError(f"无法清除上一次的停止请求 {request}：{error}") from None
    finally:
        handle.close()


def request_stop(data_root: Path, seconds: float | None = None, poll: float | None = None) -> str:
    """The stop handshake with the agent (DeviceInputState.stop_requested): write stop.request atomically, then wait
    (bounded) until touch.json is gone, so a touch already reserved finishes before the tasker is stopped and no
    new one is sent. Returns "clear", "still reserved" (the marker outlived the wait: the touch may be unconfirmed,
    and the agent's own marker handling keeps input stopped) or "request not written: ..."."""
    directory = input_dir(data_root)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        staging = directory / f"stop.request.{os.getpid()}.tmp"
        staging.write_text(json.dumps({"pid": os.getpid(), "time": dt.datetime.now().astimezone().isoformat(),
                                       "nonce": os.urandom(8).hex()}), encoding="utf-8")
        os.replace(staging, directory / "stop.request")
    except OSError as error:
        return f"request not written: {error}"
    marker = directory / "touch.json"
    deadline = time.monotonic() + (STOP_TOUCH_WAIT if seconds is None else seconds)
    while marker.exists():
        if time.monotonic() >= deadline:
            return "still reserved"
        time.sleep(STOP_TOUCH_POLL if poll is None else poll)
    return "clear"


STOP_TASKER_WAIT = 10.0
STOP_TASKER_POLL = 0.1
STOP_NOTICE_INTERVAL = 30.0


def stop_tasker(tasker, data_root: Path | None = None, record: dict | None = None, child=None, client=None) -> bool:
    """Stop a running task without cutting a touch; False when there was nothing to stop or it did not stop in time.

    With a data root, the stop handshake (request_stop) comes first; its result goes to record["touch_at_stop"] and
    the path taken to record["stop_path"]:
    - "post_stop": the reserved touch (if any) finished after the request, so the agent sends nothing more and the
      framework's stop cannot cut a gesture;
    - "agent terminated": the marker outlived the wait or the request could not be written. post_stop is never called
      then (MaaFramework's stop sends UP for a contact the in-flight click recorded, and the click then sends its
      DOWN). The agent child is killed instead: its touches run in this process's controller (context.run_action_direct
      through the AgentClient), so a click already handed over finishes whole here and no new one can start once the
      agent is gone; the client's timeout is bounded so the pending custom action returns, and the task, whose entry
      node has no next node, ends by itself. If it is still running after STOP_TASKER_WAIT the runner keeps waiting,
      printing a notice every STOP_NOTICE_INTERVAL, and does not exit while the tasker runs: this process's controller
      executes the click, and exiting between its DOWN and UP would leave a contact held. record["stop_path"] then says
      "agent terminated; tasker stopped late". Still no post_stop.
    """
    if tasker is None:
        return False
    if data_root is not None:
        touch = request_stop(data_root)
        if record is not None:
            record["touch_at_stop"] = touch
        if touch != "clear":
            return _stop_by_terminating_agent(tasker, touch, record, child, client)
    if record is not None:
        record["stop_path"] = "post_stop"
    try:
        within(20, "停止任务", lambda: tasker.post_stop().wait())
        return True
    except (RunnerError, Exception):
        return False


def _stop_by_terminating_agent(tasker, touch: str, record: dict | None, child, client) -> bool:
    _best_effort_notice(f"停止时触摸标记的状态：{touch}（可能还有一次触摸在进行）；不调用框架的停止，改为结束 agent 子进程，等任务自己结束")
    if child is not None:
        try:
            child.kill()
            child.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if client is not None:
        try:
            client.set_timeout(5000)  # a pending custom action call then returns instead of waiting forever
        except Exception:
            pass
    started = time.monotonic()
    deadline = started + STOP_TASKER_WAIT
    while _running(tasker) and time.monotonic() < deadline:
        time.sleep(STOP_TASKER_POLL)
    late = _running(tasker)
    next_notice = time.monotonic()
    while _running(tasker):
        # Never exit while the tasker runs: a click stalled between DOWN and UP (ADB wedged) finishes only in this
        # process; exiting would leave the contact held.
        if time.monotonic() >= next_notice:
            _best_effort_notice(f"结束 agent 之后任务已经 {time.monotonic() - started:.0f} 秒没有自己停下；运行器不调用框架的停止，也不在任务停下之前退出"
                                "（正在进行的点击要在这个进程里做完，否则会留下按住的触点）。ADB 可能卡住了：检查模拟器和 ADB；一直停不下来才强制结束"
                                "（kill -9），之后下一次运行在发送前读设备的触点状态。")
            next_notice = time.monotonic() + STOP_NOTICE_INTERVAL
        time.sleep(STOP_TASKER_POLL)
    if record is not None:
        record["stop_path"] = "agent terminated; tasker stopped late" if late else "agent terminated"
        if late:
            record["tasker_stop_seconds"] = round(time.monotonic() - started, 1)
    return True


def _best_effort_notice(text: str) -> None:
    """Print to stderr, ignoring a closed or broken stream (its reader gone after the agent was killed, say): a failed
    diagnostic must never end the wait while this process owns an unfinished gesture."""
    try:
        print(text, file=sys.stderr, flush=True)
    except (OSError, ValueError):  # BrokenPipeError is an OSError; ValueError: write to a closed file
        pass


def _running(tasker) -> bool:
    try:
        return bool(tasker.running)
    except Exception:
        return True


def reap(child) -> int | str:
    """The agent child's exit code: waited for, then terminated, then killed; it never outlives the runner."""
    for step in ("wait", "terminate", "kill"):
        if step != "wait":
            getattr(child, step)()
        try:
            return child.wait(timeout=15 if step == "wait" else 10)
        except subprocess.TimeoutExpired:
            continue
    return "unreaped"


def touches(data_root: Path, since: dt.datetime) -> list[dict]:
    """The touches the agent sent during this run (logs/android-input.jsonl under its data root)."""
    path = data_root / "logs" / "android-input.jsonl"
    found = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except OSError:
        return [{"audit": "unreadable"}]
    for line in lines:
        try:
            entry = json.loads(line)
            if dt.datetime.fromisoformat(entry["time"]) >= since:
                found.append(entry)
        except (ValueError, KeyError, TypeError):
            found.append({"audit": "incomplete: a malformed line was skipped"})
    # A touch whose intent was logged but whose outcome was not (the outcome write failed, or the agent died).
    open_intents = 0
    for entry in found:
        outcome = entry.get("outcome")
        if outcome == "sending":
            open_intents += 1
        elif outcome is not None and open_intents:
            open_intents -= 1
    if open_intents:
        found.append({"audit": f"incomplete: {open_intents} intent(s) without an outcome"})
    return found


def agent_messages(data_root: Path, since: dt.datetime) -> list[str]:
    """The agent's user-facing messages written during this run (its message log under the data root): only their
    display text, or a result's report text (written before its display, without one: the walk probe's terminal
    report, the crystal route's results; message_log.write_message's report, fixed text with numbers, never OCR);
    rows with neither (details, failure scenes, which may quote OCR) stay in the log and diagnostics."""
    messages = []
    folder = data_root / "debug" / "messages"
    for path in sorted(folder.glob("*.log")) if folder.is_dir() else []:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                entry = json.loads(line)
                display = entry.get("display") or entry.get("report")
                if dt.datetime.fromisoformat(entry["timestamp"]) >= since and isinstance(display, str) and display:
                    messages.append(display)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return messages[-20:]


def list_tasks(args) -> int:
    interface = android_interface()
    tasks, _options = interface_options(interface)
    for task in tasks:
        allowed = task.get("controller")
        if not allowed or CONTROLLER in allowed:
            print(f"{task['entry']}\t{task.get('label', '')}\t{','.join(task.get('option', []))}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--adb", default=os.environ.get("MAANIKKI_ADB", DEFAULT_ADB))
    parser.add_argument("--serial", default=os.environ.get("MAANIKKI_ADB_SERIAL", DEFAULT_SERIAL))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="run one task entry on the emulator")
    p.add_argument("entry")
    p.add_argument("--option", action="append", default=[], metavar="NAME=CASE",
                   help="an option's case; for a dev-only entry (MaaNikki_AndroidWalkProbe) NAME=VALUE")
    p.add_argument("--purpose", required=True, help="why this run; recorded with it")
    p.add_argument("--data-root", default=str(DEFAULT_DATA), help="the agent's data root (not the client's)")
    p.add_argument("--timeout", type=float, default=600, help="seconds the task may run")
    p.add_argument("--clear-unconfirmed", metavar="FINGERPRINT", default=None,
                   help="after a person decided, clear the left-over touch marker with this fingerprint")
    p.set_defaults(func=run)
    sub.add_parser("tasks", help="entries the Android interface offers").set_defaults(func=list_tasks)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    for option in getattr(args, "option", []):
        if "=" not in option:
            print(f"error: --option takes NAME=CASE, got {option!r}", file=sys.stderr)
            return 2
    try:
        return args.func(args)
    except RunnerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # before the agent started: nothing to stop
        print("error: 运行被中断（Ctrl-C）", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
