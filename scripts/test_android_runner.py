"""Headless Android runner (scripts/android/runner.py), offline parts: python scripts/test_android_runner.py

The device run itself is the B6 acceptance; here: the controller's masks, option resolution, the timeouts that end
in the ADB restart advice, the agent's environment and the framework-log summary.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "android"))

import runner  # noqa: E402


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.interface = runner.android_interface()

    def test_the_overlay_masks_are_maatouch_and_raw_with_gzip(self):
        self.assertEqual(runner.masks(runner.controller_definition(self.interface)), (4, 4))
        for bad in ({"input": "Minitouch", "screencap": "RawByNetcat"}, {"input": "Maatouch"}, {}):
            with self.subTest(adb=bad), self.assertRaises(runner.RunnerError):
                runner.masks({"adb": bad})
        self.assertEqual(runner.masks({"adb": {"input": ["Maatouch", "AdbShell"], "screencap": "Encode"}}), (5, 2))

    def test_options_resolve_to_defaults_or_the_chosen_case(self):
        self.assertEqual(runner.task_override(self.interface, "MaaNikki_AndroidCheck", {}), [])
        with self.assertRaises(runner.RunnerError):
            runner.task_override(self.interface, "MaaNikki_AndroidCheck", {"Nope": "x"})
        with self.assertRaises(runner.RunnerError):
            runner.task_override(self.interface, "MaaNikki_DoesNotExist", {})
        interface = {"controller": [{"name": "nikki_android", "type": "Adb"}], "resource": [], "import": [],
                     "task": [{"entry": "E", "pipeline_override": {"E": {"x": 1}}, "option": ["Mode"]},
                              {"entry": "W", "controller": ["nikki_windows"]}],
                     "option": {"Mode": {"default_case": "A", "cases": [
                         {"name": "A", "pipeline_override": {"N": {"a": 1}}},
                         {"name": "B", "pipeline_override": {"N": {"b": 1}}}]}}}
        self.assertEqual(runner.task_override(interface, "E", {}), [{"E": {"x": 1}}, {"N": {"a": 1}}])
        self.assertEqual(runner.task_override(interface, "E", {"Mode": "B"}), [{"E": {"x": 1}}, {"N": {"b": 1}}])
        with self.assertRaises(runner.RunnerError):
            runner.task_override(interface, "E", {"Mode": "C"})
        with self.assertRaises(runner.RunnerError):
            runner.task_override(interface, "W", {})  # limited to another controller

    def test_a_blocking_step_that_overruns_ends_with_the_adb_advice(self):
        self.assertEqual(runner.within(1, "quick", lambda: 7), 7)
        with self.assertRaises(runner.RunnerError) as caught:
            runner.within(0.05, "连接模拟器", lambda: time.sleep(0.5))
        self.assertIn("adb kill-server", str(caught.exception))
        with self.assertRaises(ValueError):
            runner.within(1, "raises", lambda: (_ for _ in ()).throw(ValueError("x")))

    def test_the_adb_check_has_a_timeout(self):
        with patch("runner.subprocess.run", side_effect=subprocess.TimeoutExpired("adb", 8)), \
                self.assertRaises(runner.RunnerError) as caught:
            runner.adb_alive("adb", "127.0.0.1:5555")
        self.assertIn("adb kill-server", str(caught.exception))
        done = subprocess.CompletedProcess([], 0, stdout="maanikki-ok\n", stderr="")
        with patch("runner.subprocess.run", return_value=done) as run:
            runner.adb_alive("adb", "127.0.0.1:5555")
        self.assertIn("timeout", run.call_args.kwargs)
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="error: closed")
        with patch("runner.subprocess.run", return_value=failed), self.assertRaises(runner.RunnerError):
            runner.adb_alive("adb", "127.0.0.1:5555")

    def test_the_agent_gets_the_controller_and_its_own_data_root(self):
        controller = runner.controller_definition(self.interface)
        with patch.dict("os.environ", {"pi_controller": "stale", "PI_CONTROLLER": "stale"}):
            env = runner.agent_environment(controller, Path("/tmp/runner-data"))
        self.assertEqual(json.loads(env["PI_CONTROLLER"])["type"], "Adb")
        self.assertNotIn("pi_controller", env)
        # str(Path(...)): Windows spells the same path with backslashes.
        self.assertEqual((env["MAANIKKI_DATA_ROOT"], env["MAANIKKI_RESOURCE_ROOT"]),
                         (str(Path("/tmp/runner-data")), str(ROOT)))

    def test_the_log_summary_quotes_the_method_lines(self):
        prefix = "[2026-11-25 22:40:14.177][INF][Px1][Tx2][InputAgent.cpp][L37][MaaNS::X]"
        log = "\n".join([prefix + ' [methods=4] [method_vector=["Maatouch"]]',
                         prefix + ' [methods=4] [method_set=["RawWithGzip"]]',
                         prefix + " Testing RawWithGzip ", prefix + " other line"])
        summary = runner.methods_in_log(log)
        self.assertEqual((summary["input"], summary["screencap_tested"]), (["Maatouch"], ["RawWithGzip"]))
        self.assertEqual((summary["maatouch"], summary["minitouch"]), (True, False))
        self.assertEqual(len(summary["lines"]), 3)

    def test_options_with_several_cases(self):
        interface = {"controller": [{"name": "nikki_android", "type": "Adb"}], "resource": [], "import": [],
                     "task": [{"entry": "E", "option": ["Pages"]}],
                     "option": {"Pages": {"type": "checkbox", "default_case": ["A", "B"], "cases": [
                         {"name": "A", "pipeline_override": {"N": {"a": 1}}},
                         {"name": "B", "pipeline_override": {"N": {"b": 1}}}]}}}
        self.assertEqual(runner.task_override(interface, "E", {}), [{"N": {"a": 1}}, {"N": {"b": 1}}])
        self.assertEqual(runner.task_override(interface, "E", {"Pages": "B"}), [{"N": {"b": 1}}])
        self.assertEqual(runner.task_override(interface, "E", {"Pages": "A,B"}), [{"N": {"a": 1}}, {"N": {"b": 1}}])
        single = {**interface, "option": {"Pages": {**interface["option"]["Pages"], "type": "switch",
                                                    "default_case": "A"}}}
        with self.assertRaises(runner.RunnerError):
            runner.task_override(single, "E", {"Pages": "A,B"})  # one case only
        runner.task_override(self.interface, "MaaNikki_MonthlyPass", {})  # the real list default resolves

    def fake_framework(self, *, interrupt_runs=(), on_disconnect=None, child_waits=None):
        """Fake maa modules and agent child; returns (modules, calls, children, Child)."""
        import types
        calls = {"post_stop": 0, "runs": 0}

        class Job:
            def __init__(self, interrupt):
                self.interrupt, self.succeeded = interrupt, not interrupt

            def wait(self):
                if self.interrupt:
                    raise KeyboardInterrupt
                return self

            def get(self):
                return types.SimpleNamespace(nodes=[types.SimpleNamespace(name="MaaNikki_AndroidCheck")])

        class Tasker:
            @staticmethod
            def set_log_dir(path):
                pass

            def bind(self, resource, controller):
                return True

            def post_task(self, entry, override):
                calls["runs"] += 1
                return Job(interrupt=calls["runs"] in interrupt_runs)

            def post_stop(self):
                calls["post_stop"] += 1
                if calls.get("data_root"):
                    request = Path(calls["data_root"]) / "config" / "android-input" / "stop.request"
                    calls.setdefault("stop_request_at_post_stop", []).append(request.exists())
                return types.SimpleNamespace(wait=lambda: None)

        class Controller:
            resolution = (1920, 1080)

            def __init__(self, *args, **kwargs):
                pass

            def set_screenshot_target_short_side(self, side):
                pass

            def post_connection(self):
                return types.SimpleNamespace(wait=lambda: types.SimpleNamespace(succeeded=True))

            post_screencap = post_connection

        class Resource:
            def post_bundle(self, path):
                return types.SimpleNamespace(wait=lambda: types.SimpleNamespace(succeeded=True))

        class Client:
            identifier = "test-socket"

            def bind(self, resource):
                return True

            def connect(self):
                return True

            def disconnect(self):
                if on_disconnect:
                    on_disconnect()
                return True

        children = []

        class Child:
            def __init__(self, *args, **kwargs):
                self.calls = []
                children.append(self)

            def wait(self, timeout=None):
                self.calls.append("wait")
                if child_waits and child_waits(self, len(children)):
                    raise subprocess.TimeoutExpired("agent", timeout)
                return 0

            def terminate(self):
                self.calls.append("terminate")

            def kill(self):
                self.calls.append("kill")
        modules = {"maa.agent_client": types.SimpleNamespace(AgentClient=Client),
                   "maa.controller": types.SimpleNamespace(AdbController=Controller),
                   "maa.resource": types.SimpleNamespace(Resource=Resource),
                   "maa.tasker": types.SimpleNamespace(Tasker=Tasker)}
        return modules, calls, children, Child

    def run_fakes(self, tmp, modules, Child, runs=1):
        from contextlib import ExitStack
        results, records = [], []
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, modules))
            stack.enter_context(patch.object(runner, "adb_alive"))
            stack.enter_context(patch.object(runner.subprocess, "Popen", Child))
            stack.enter_context(patch.object(runner, "LOG_ROOT", Path(tmp) / "logs"))
            stack.enter_context(patch.object(runner, "RUN_LOG", Path(tmp) / "runs.jsonl"))
            stack.enter_context(patch("builtins.print"))
            stack.enter_context(patch("sys.stderr"))
            if runner.os.name == "nt":  # no flock on Windows: inject the owner lock (the real one runs on macOS)
                from types import SimpleNamespace
                sys.path.insert(0, str(Path(runner.__file__).resolve().parents[2] / "agent"))
                import host
                stack.enter_context(patch.object(host, "take_owner_lock",
                                                 return_value=SimpleNamespace(close=lambda: None)))
            argv = ["run", "MaaNikki_AndroidCheck", "--purpose", "t", "--data-root", str(Path(tmp) / "data")]
            for index in range(runs):
                if index:
                    time.sleep(1.1)  # a new log directory name
                results.append(runner.main(argv))
        records = [json.loads(line) for line in (Path(tmp) / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
        return results, records

    def test_an_interrupted_run_stops_the_task_reaps_the_agent_and_the_next_run_works(self):
        import tempfile
        modules, calls, children, Child = self.fake_framework(
            interrupt_runs={1}, child_waits=lambda child, count: count == 1 and child.calls.count("wait") == 1)
        with tempfile.TemporaryDirectory() as tmp:
            calls["data_root"] = Path(tmp) / "data"
            results, records = self.run_fakes(tmp, modules, Child, runs=2)
        self.assertEqual(results, [2, 0])
        self.assertEqual((records[0]["status"], records[0]["stop_requested"]), ("interrupted", True))
        self.assertEqual(records[0]["touch_at_stop"], "clear")
        self.assertEqual(calls["post_stop"], 1)
        self.assertEqual(calls["stop_request_at_post_stop"], [True])  # the handshake came before the stop
        self.assertEqual(children[0].calls[:2], ["wait", "terminate"])  # reaped before the runner returned
        self.assertEqual(records[1]["status"], "succeeded")

    def test_the_stop_request_comes_first_and_a_reserved_touch_finishes_before_the_tasker_stops(self):
        import tempfile
        import threading
        order = []
        tasker = Mock()
        tasker.post_stop.side_effect = lambda: (order.append("post_stop"), Mock(wait=lambda: None))[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "config" / "android-input" / "touch.json"
            request = marker.parent / "stop.request"
            marker.parent.mkdir(parents=True)
            marker.write_text("{}")  # a touch reserved when the stop comes

            def agent():  # finishes its touch a little later
                deadline = time.monotonic() + 2
                while not request.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                order.append("request seen")
                time.sleep(0.2)
                order.append("touch released")
                marker.unlink()
            worker = threading.Thread(target=agent)
            worker.start()
            record = {}
            self.assertTrue(runner.stop_tasker(tasker, root, record))
            worker.join()
            self.assertEqual(order, ["request seen", "touch released", "post_stop"])
            self.assertEqual(record["touch_at_stop"], "clear")
            self.assertTrue(request.exists())  # left for the agent until the next run starts
            self.assertEqual(list(marker.parent.glob("*.tmp")), [])  # written atomically

            # A marker that outlives the bound: the tasker is still stopped, and the record says so.
            marker.write_text("{}")
            stopper = Mock()
            stopper.post_stop.return_value = Mock(wait=lambda: None)
            record = {}
            with patch.object(runner, "STOP_TOUCH_WAIT", 0.1), patch("sys.stderr"):
                self.assertTrue(runner.stop_tasker(stopper, root, record))
            self.assertEqual(record["touch_at_stop"], "still reserved")
            stopper.post_stop.assert_called_once()
        self.assertFalse(runner.stop_tasker(None, Path("/nonexistent"), {}))  # nothing to stop: no handshake

    def test_a_stale_stop_request_is_removed_before_the_agent_starts(self):
        import tempfile
        modules, calls, children, Child = self.fake_framework()
        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp) / "data" / "config" / "android-input" / "stop.request"
            request.parent.mkdir(parents=True)
            request.write_text("stop")
            seen = []
            original = Child.__init__

            def spawned(self, *args, **kwargs):
                seen.append(request.exists())
                original(self, *args, **kwargs)
            with patch.object(Child, "__init__", spawned):
                results, records = self.run_fakes(tmp, modules, Child)
        self.assertEqual(results, [0])
        self.assertEqual(seen, [False])

    def test_a_ctrl_c_during_teardown_is_deferred_until_the_agent_is_reaped(self):
        import _thread
        import signal
        import tempfile
        # interrupt_main raises SIGINT's handler in the main thread on every OS (os.kill(SIGINT) would terminate
        # the whole test process on Windows).
        modules, calls, children, Child = self.fake_framework(on_disconnect=_thread.interrupt_main)
        with tempfile.TemporaryDirectory() as tmp:
            results, records = self.run_fakes(tmp, modules, Child)
        self.assertEqual(results, [130])
        self.assertEqual(children[0].calls, ["wait"])  # still reaped
        self.assertIs(records[0]["interrupted_during_cleanup"], True)
        self.assertEqual(records[0]["agent_exit"], 0)
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)  # restored

    def test_a_ctrl_c_while_the_record_is_collected_still_writes_it(self):
        import _thread
        import tempfile
        modules, calls, children, Child = self.fake_framework()
        original = runner.agent_messages

        def interrupted(*args):
            _thread.interrupt_main()  # Ctrl-C while the agent's messages are collected
            return original(*args)
        with tempfile.TemporaryDirectory() as tmp, patch.object(runner, "agent_messages", interrupted):
            results, records = self.run_fakes(tmp, modules, Child)
        self.assertEqual(results, [130])
        self.assertEqual(len(records), 1)
        self.assertIs(records[0]["interrupted_during_cleanup"], True)

    def test_an_agent_that_cannot_be_reaped_fails_the_run(self):
        import tempfile
        modules, calls, children, Child = self.fake_framework(child_waits=lambda child, count: True)
        with tempfile.TemporaryDirectory() as tmp:
            results, records = self.run_fakes(tmp, modules, Child)
        self.assertEqual(results, [1])
        self.assertEqual(children[0].calls, ["wait", "terminate", "wait", "kill", "wait"])
        self.assertEqual((records[0]["status"], records[0]["agent_exit"]), ("error", "unreaped"))

    def test_a_left_unconfirmed_touch_stops_the_runner_until_cleared_by_its_fingerprint(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner.check_unconfirmed(root, None)  # nothing left: fine
            marker = root / "config" / "android-input" / "touch.json"
            marker.parent.mkdir(parents=True)
            marker.write_text(json.dumps({"pid": 1, "since": "2026-11-25T23:00:00+09:00"}))
            fingerprint = runner.marker_fingerprint(marker.read_bytes())
            with self.assertRaises(runner.RunnerError) as caught:
                runner.check_unconfirmed(root, None)
            self.assertIn(fingerprint, str(caught.exception))
            self.assertIn("需要人来决定", str(caught.exception))
            with self.assertRaises(runner.RunnerError):
                runner.check_unconfirmed(root, "000000000000")  # the wrong fingerprint
            self.assertTrue(marker.exists())
            if runner.os.name == "nt":
                return  # take_owner_lock needs fcntl
            # A later run's marker B replaced the inspected marker A: A's fingerprint does not clear B.
            marker.write_text(json.dumps({"pid": 2, "since": "2026-11-25T23:59:00+09:00"}))
            with self.assertRaises(runner.RunnerError):
                runner.check_unconfirmed(root, fingerprint)
            self.assertTrue(marker.exists())
            with patch("sys.stderr"):
                runner.check_unconfirmed(root, runner.marker_fingerprint(marker.read_bytes()))
            self.assertFalse(marker.exists())
            self.assertTrue((marker.parent / "owner.lock").exists())  # the lock file is kept
            # An empty marker (agent killed while creating it) has a stable fingerprint and can be cleared.
            marker.write_bytes(b"")
            empty = runner.marker_fingerprint(b"")
            with self.assertRaises(runner.RunnerError) as caught:
                runner.check_unconfirmed(root, None)
            self.assertIn(empty, str(caught.exception))
            with patch("sys.stderr"):
                runner.check_unconfirmed(root, empty)
            self.assertFalse(marker.exists())

    def test_a_left_marker_is_cleared_only_when_one_reading_shows_this_guest_settled(self):
        import tempfile
        serial = "127.0.0.1:5555"
        boot = "992f1bef-d353-4a1b-b401-b9867f884164"
        device = {"serial": serial, "boot_id": boot}
        settled = {"boot_id": boot, "state": "none", "queued": False, "maatouch": False}
        cases = (("settled", {"device": device}, settled, None, True),
                 ("still held", {"device": device}, {**settled, "state": "injected"}, None, False),
                 ("input still queued", {"device": device}, {**settled, "queued": True}, None, False),
                 ("a MaaTouch helper running", {"device": device}, {**settled, "maatouch": True}, None, False),
                 ("another guest behind the address", {"device": device}, {**settled, "boot_id": "0" * 8 + boot[8:]},
                  None, False),
                 ("unreadable", {"device": device}, None, None, False),
                 ("another serial", {"device": {**device, "serial": "emulator-5554"}}, settled, None, False),
                 ("old marker without a device", {"serial": serial}, settled, None, False),
                 ("empty", {}, settled, None, False),
                 ("a fingerprint that does not match", {"device": device}, settled, "000000000000", False))
        for name, content, reading, clear, cleared in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                marker = root / "config" / "android-input" / "touch.json"
                marker.parent.mkdir(parents=True)
                marker.write_text(json.dumps({"pid": 1, "since": "2026-11-26T00:00:00+09:00", **content}))
                fingerprint = runner.marker_fingerprint(marker.read_bytes())
                read = Mock(return_value=reading)
                eligible = content.get("device", {}).get("serial") == serial and clear is None
                if runner.os.name == "nt" and eligible:
                    continue  # take_owner_lock needs fcntl
                if cleared:
                    with patch("sys.stderr"):
                        done = runner.check_unconfirmed(root, clear, read, serial)
                    self.assertTrue(done["cleared_by"].startswith("device reports this guest settled"))
                    log = (root / "logs" / "android-input.jsonl").read_text(encoding="utf-8").splitlines()
                    self.assertEqual([json.loads(line)["action"] for line in log], ["RecoverMarker"])
                else:
                    with self.assertRaises(runner.RunnerError) as caught:
                        runner.check_unconfirmed(root, clear, read, serial)
                    self.assertIn(fingerprint, str(caught.exception))
                    self.assertIn("需要人来决定", str(caught.exception))
                self.assertEqual(marker.exists(), not cleared)
                self.assertEqual(read.call_count, int(eligible))  # one reading, only for a marker naming the serial

    def test_a_recovery_that_cannot_be_logged_keeps_the_marker(self):
        import contextlib
        import tempfile
        from types import SimpleNamespace
        sys.path.insert(0, str(Path(runner.__file__).resolve().parents[2] / "agent"))
        import host
        # Windows has no flock: inject the owner lock there (the real lock is exercised on macOS).
        lock = patch.object(host, "take_owner_lock", return_value=SimpleNamespace(close=lambda: None)) \
            if runner.os.name == "nt" else contextlib.nullcontext()
        device = {"serial": "127.0.0.1:5555", "boot_id": "992f1bef-d353-4a1b-b401-b9867f884164"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "config" / "android-input" / "touch.json"
            marker.parent.mkdir(parents=True)
            marker.write_text(json.dumps({"pid": 1, "device": device}))
            (root / "logs").write_text("")  # a file where the log directory goes
            reading = {"boot_id": device["boot_id"], "state": "none", "queued": False, "maatouch": False}
            with lock, self.assertRaises(runner.RunnerError):
                runner.check_unconfirmed(root, None, lambda: reading, device["serial"])
            self.assertTrue(marker.exists())

    def test_a_malformed_touch_line_is_flagged_not_hiding_later_rows(self):
        import datetime as dt
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "logs" / "android-input.jsonl"
            log.parent.mkdir()
            log.write_text("not json\n" + json.dumps({"time": "2026-11-25T23:31:34+09:00", "action": "Click",
                                                       "outcome": "confirmed"}) + "\n")
            found = runner.touches(Path(tmp), dt.datetime.fromisoformat("2026-11-25T23:00:00+09:00"))
        self.assertEqual(found[0], {"audit": "incomplete: a malformed line was skipped"})
        self.assertEqual(found[1]["outcome"], "confirmed")

    def test_an_intent_without_an_outcome_is_flagged(self):
        import datetime as dt
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "logs" / "android-input.jsonl"
            log.parent.mkdir()
            rows = [{"time": "2026-11-25T23:31:34+09:00", "action": "Click", "outcome": "sending"},
                    {"time": "2026-11-25T23:31:35+09:00", "action": "Click", "outcome": "confirmed"},
                    {"time": "2026-11-25T23:31:36+09:00", "action": "Click", "outcome": "sending"}]
            log.write_text("".join(json.dumps(r) + "\n" for r in rows))
            found = runner.touches(Path(tmp), dt.datetime.fromisoformat("2026-11-25T23:00:00+09:00"))
        self.assertEqual(found[-1], {"audit": "incomplete: 1 intent(s) without an outcome"})

    def test_the_command_line(self):
        args = runner.build_parser().parse_args(["run", "MaaNikki_AndroidCheck", "--purpose", "t", "--option", "A=B"])
        self.assertEqual((args.entry, args.option, args.func), ("MaaNikki_AndroidCheck", ["A=B"], runner.run))
        with patch("builtins.print"), patch("sys.stderr"):
            self.assertEqual(runner.main(["run", "X", "--purpose", "t", "--option", "bad"]), 2)
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            runner.build_parser().parse_args(["run", "X"])  # a purpose is required


if __name__ == "__main__":
    unittest.main()
