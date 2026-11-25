"""Headless runner: run a MaaNikki task on the Android emulator with the agent and MaaFramework, no client.

    .venv/bin/python scripts/android/runner.py run MaaNikki_AndroidCheck --purpose "why this run"
    .venv/bin/python scripts/android/runner.py run MaaNikki_Mailbox --option NAME=CASE --purpose "..."
    .venv/bin/python scripts/android/runner.py tasks          # the entries the Android interface defines

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
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

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


def agent_environment(controller: dict, data_root: Path) -> dict:
    env = {key: value for key, value in os.environ.items() if key.upper() != "PI_CONTROLLER"}
    env.update({"PI_CONTROLLER": json.dumps(controller, ensure_ascii=False),
                "MAANIKKI_DATA_ROOT": str(data_root), "MAANIKKI_RESOURCE_ROOT": str(ROOT), "PYTHONUTF8": "1"})
    return env


def run(args) -> int:
    from maa.agent_client import AgentClient
    from maa.controller import AdbController
    from maa.resource import Resource
    from maa.tasker import Tasker

    interface = android_interface()
    controller_def = controller_definition(interface)
    input_mask, screencap_mask = masks(controller_def)
    chosen = dict(option.split("=", 1) for option in args.option)
    override = task_override(interface, args.entry, chosen)
    started = dt.datetime.now().astimezone()
    log_dir = LOG_ROOT / started.strftime("%Y%m%d-%H%M%S")
    log_dir.mkdir(parents=True, exist_ok=True)
    data_root = Path(args.data_root).resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    record = {"time": started.isoformat(timespec="seconds"), "entry": args.entry, "options": chosen,
              "purpose": args.purpose, "serial": args.serial, "log_dir": str(log_dir)}

    adb_alive(args.adb, args.serial)
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
                             env=agent_environment(controller_def, data_root), stdout=agent_log,
                             stderr=subprocess.STDOUT)
    tasker = None
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
        record["status"] = "interrupted"
        record["stop_requested"] = stop_tasker(tasker)
        raise RunnerError("运行被中断（Ctrl-C）；已请求停止任务。") from None
    except RunnerError as error:
        record["status"] = "error"
        record["error"] = str(error)
        record["stop_requested"] = stop_tasker(tasker)
        raise
    finally:
        # Once the agent exists, a Ctrl-C during cleanup is deferred: the child is always reaped and the record
        # always written.
        deferred = []
        previous = signal.signal(signal.SIGINT, lambda *_: deferred.append(True)) \
            if threading.current_thread() is threading.main_thread() else None
        try:
            try:
                within(15, "断开 agent", client.disconnect)
            except (RunnerError, Exception):
                record["disconnect"] = "timed out or failed"
            record["agent_exit"] = reap(child)
            agent_log.close()
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)
        if deferred:
            record["interrupted_during_cleanup"] = True
        if record["agent_exit"] == "unreaped":
            record["status"] = "error"
            record["error"] = "agent 子进程没能结束（终止和强制结束都没有确认）"
        log_file = log_dir / "maafw.log"
        record["methods"] = methods_in_log(log_file.read_text(encoding="utf-8", errors="replace")) \
            if log_file.exists() else None
        record["agent_messages"] = agent_messages(data_root, started)
        RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with RUN_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(json.dumps(record, ensure_ascii=False, indent=1))
    if record.get("interrupted_during_cleanup"):
        return 130
    return 0 if record.get("status") == "succeeded" else 1


def stop_tasker(tasker) -> bool:
    """Ask a running task to stop and wait a bounded time; False when there was nothing to stop or it overran."""
    if tasker is None:
        return False
    try:
        within(20, "停止任务", lambda: tasker.post_stop().wait())
        return True
    except (RunnerError, Exception):
        return False


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


def agent_messages(data_root: Path, since: dt.datetime) -> list[str]:
    """The agent's user-facing messages written during this run (its message log under the data root)."""
    messages = []
    folder = data_root / "debug" / "messages"
    for path in sorted(folder.glob("*.log")) if folder.is_dir() else []:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                entry = json.loads(line)
                if dt.datetime.fromisoformat(entry["timestamp"]) >= since:
                    messages.append(str(entry.get("message", "")))
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
    p.add_argument("--option", action="append", default=[], metavar="NAME=CASE")
    p.add_argument("--purpose", required=True, help="why this run; recorded with it")
    p.add_argument("--data-root", default=str(DEFAULT_DATA), help="the agent's data root (not the client's)")
    p.add_argument("--timeout", type=float, default=600, help="seconds the task may run")
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
