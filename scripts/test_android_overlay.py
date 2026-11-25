"""Android development interface, home and read-only check task: python scripts/test_android_overlay.py

build/android/interface-overlay.json applied by scripts/android_interface.py, scripts/android_dev_home.py, the
MaaNikki_AndroidCheck entry in resource_android/ and its agent action (agent/host/android/check.py).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))

import android_interface  # noqa: E402


def android_layer_present(test) -> bool:
    """resource_android/ is required in a checkout; the release source archive does not list it (plan risk 13)."""
    if (ROOT / "resource_android").is_dir():
        return True
    test.assertFalse((ROOT / ".git").exists(), "resource_android/ is missing from the checkout")
    return False


class InterfaceTests(unittest.TestCase):
    def test_the_android_interface(self):
        before = (ROOT / "interface.json").read_bytes()
        result = android_interface.android_interface()
        self.assertEqual((ROOT / "interface.json").read_bytes(), before)  # read only
        controllers = {c["name"]: c for c in result["controller"]}
        android = controllers["nikki_android"]
        self.assertEqual((android["type"], android["display_short_side"]), ("Adb", 720))
        self.assertEqual(android["attach_resource_path"], ["./resource_android"])
        self.assertEqual(android["adb"], {"input": "Maatouch", "screencap": "RawByNetcat"})
        self.assertNotIn("nikki_macos", controllers)  # the stock runtime cannot drive the native Mac game
        agents = result["agent"] if isinstance(result["agent"], list) else [result["agent"]]
        self.assertTrue(all(agent["child_exec"] == "./python/bin/python3" for agent in agents))
        tasks = [task for task in result["task"] if task["entry"] == "MaaNikki_AndroidCheck"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["controller"], ["nikki_android"])
        self.assertIs(tasks[0]["default_check"], False)

    def test_the_repository_interface_has_nothing_android(self):
        text = (ROOT / "interface.json").read_text(encoding="utf-8")
        for word in ("nikki_android", "AndroidCheck", "resource_android", '"Adb"'):
            self.assertNotIn(word, text)

    def test_the_check_entry_and_its_tier(self):
        tiers = json.loads((ROOT / "agent/host/android/tasks.json").read_text(encoding="utf-8"))
        self.assertEqual(tiers["entries"], [])
        self.assertIn("MaaNikki_AndroidCheck", tiers["experimental"])
        if not android_layer_present(self):
            return
        nodes = json.loads((ROOT / "resource_android/pipeline/android_check.json").read_text(encoding="utf-8"))
        self.assertEqual(nodes["MaaNikki_AndroidCheck"]["custom_action"], "nikki.gameplay")
        self.assertEqual(nodes["MaaNikki_AndroidCheck"]["custom_action_param"], {"kind": "android_check"})
        self.assertEqual(nodes["MaaNikki_AndroidCheck"]["next"], [])


@unittest.skipUnless(sys.platform == "darwin", "the Android development home is for macOS hosts")
class DevHomeTests(unittest.TestCase):
    def test_the_home_links_the_repository_and_writes_the_interface(self):
        import android_dev_home
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            venv, runtime, home = tmp / "venv", tmp / "runtime", tmp / "home"
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "python3").write_text("")
            runtime.mkdir()
            for name in ("libMaaFramework.dylib", "libMaaAdbControlUnit.dylib"):
                (runtime / name).write_text("")
            helper = venv / "lib/python3.12/site-packages/MaaAgentBinary" / android_dev_home.MAATOUCH
            helper.parent.mkdir(parents=True)
            helper.write_text("")
            for _ in range(2):  # building again over its own home is fine
                with patch("builtins.print"):
                    self.assertEqual(android_dev_home.main(["--home", str(home), "--python", str(venv),
                                                            "--runtime", str(runtime)]), 0)
            self.assertEqual((home / "resource_android").resolve(), (ROOT / "resource_android").resolve())
            self.assertEqual((home / "maafw/libMaaFramework.dylib").resolve(), (runtime / "libMaaFramework.dylib").resolve())
            # Where the client points MaaFramework for the Maatouch helper (maa_core.rs: <maafw>/MaaAgentBinary).
            self.assertTrue((home / "maafw/MaaAgentBinary" / android_dev_home.MAATOUCH).is_file())
            written = json.loads((home / "interface.json").read_text(encoding="utf-8"))
            self.assertEqual(written, android_interface.android_interface())
            (tmp / "other").mkdir()
            (tmp / "other" / "file").write_text("")
            with self.assertRaises(SystemExit):  # never writes into a directory it did not create
                android_dev_home.main(["--home", str(tmp / "other"), "--python", str(venv), "--runtime", str(runtime)])

    def test_a_home_without_the_maatouch_helper_is_refused(self):
        import android_dev_home
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "venv/bin").mkdir(parents=True)
            (tmp / "venv/bin/python3").write_text("")
            (tmp / "runtime").mkdir()
            for name in ("libMaaFramework.dylib", "libMaaAdbControlUnit.dylib"):
                (tmp / "runtime" / name).write_text("")
            with self.assertRaises(SystemExit):
                android_dev_home.main(["--home", str(tmp / "home"), "--python", str(tmp / "venv"),
                                       "--runtime", str(tmp / "runtime")])
            self.assertFalse((tmp / "home").exists())

    def test_a_runtime_without_the_adb_unit_is_refused(self):
        import android_dev_home
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "venv/bin").mkdir(parents=True)
            (tmp / "venv/bin/python3").write_text("")
            (tmp / "runtime").mkdir()
            (tmp / "runtime/libMaaFramework.dylib").write_text("")
            with self.assertRaises(SystemExit):
                android_dev_home.main(["--home", str(tmp / "home"), "--python", str(tmp / "venv"),
                                       "--runtime", str(tmp / "runtime")])


class CheckTaskTests(unittest.TestCase):
    def test_the_check_reports_the_screen_and_sends_nothing(self):
        import numpy as np
        from host.android import check
        from test_android_actions import Scripted
        cases = {"hud": (Scripted(duck=True), "当前画面：大世界"),
                 "page": (Scripted(arrow=True), "当前画面：页面（设置）"),
                 "quit": (Scripted(quit=True, cancel=True, duck=True), "当前画面：退出确认框"),
                 "unknown": (Scripted(duck=True, arrow=True), "当前画面：认不出的画面")}
        for name, (recognize, first_line) in cases.items():
            with self.subTest(name=name):
                frame = np.zeros((720, 1280, 3), np.uint8)
                runtime = SimpleNamespace(capture=Mock(return_value=frame), log=Mock(),
                                          _recognizer_on=Mock(return_value=recognize), action=Mock(), key=Mock(),
                                          _device_focus_now=Mock(return_value=True),
                                          _frame_still_valid=Mock(return_value=True))
                self.assertTrue(check.check(runtime))
                text = runtime.log.call_args.args[0]
                self.assertEqual(text.splitlines()[0].split("——")[0], first_line)
                self.assertEqual(len(text.splitlines()), 1 + 7)  # one line per named action
                runtime._recognizer_on.assert_called_once_with(frame)
                runtime.action.assert_not_called()
                runtime.key.assert_not_called()
        self.assertIn("· 打开美鸭梨菜单：会点 [30, 18, 54, 54]", check.describe(*self._plans(Scripted(duck=True))))

    @staticmethod
    def _plans(recognize):
        from host.android import actions
        screen = actions.read_screen(recognize)
        plans = {}
        for action in actions.ACTIONS:
            try:
                plans[action] = actions.plan_tap(action, screen, recognize)
            except actions.ScreenUnrecognised as error:
                plans[action] = str(error)
        return screen, plans

    def test_a_focus_loss_while_deciding_is_never_reported(self):
        import numpy as np
        from host.android import check
        from test_android_actions import Scripted
        frames = [np.zeros((720, 1280, 3), np.uint8) for _ in range(3)]
        for valid, expected in (([False, True], True), ([False, False, False], False)):
            with self.subTest(valid=valid):
                runtime = SimpleNamespace(capture=Mock(side_effect=list(frames)), log=Mock(),
                                          _recognizer_on=Mock(return_value=Scripted(duck=True)),
                                          _device_focus_now=Mock(return_value=True),
                                          _frame_still_valid=Mock(side_effect=valid))
                self.assertIs(check.check(runtime), expected)
                self.assertEqual(runtime.capture.call_count, len(valid))
                text = runtime.log.call_args.args[0]
                self.assertEqual(text.startswith("当前画面：大世界"), expected)  # only a decision that held
        runtime = SimpleNamespace(capture=Mock(side_effect=list(frames)), log=Mock(),
                                  _recognizer_on=Mock(return_value=Scripted(duck=True)),
                                  _device_focus_now=Mock(side_effect=[False, True]),
                                  _frame_still_valid=Mock(return_value=True))
        self.assertTrue(check.check(runtime))
        self.assertEqual(runtime.capture.call_count, 2)

    def test_no_frame_means_no_report(self):
        from host.android import check
        runtime = SimpleNamespace(capture=Mock(return_value=None), log=Mock(), _recognizer_on=Mock())
        self.assertFalse(check.check(runtime))
        runtime.log.assert_not_called()

    def test_the_check_runs_through_the_gameplay_action_without_recovery(self):
        from action import gameplay
        runtime = SimpleNamespace(stopped=False, log=Mock(), main=Mock())

        def run(entry, param, **check_behaviour):
            argv = SimpleNamespace(custom_action_param=param, task_detail=SimpleNamespace(entry=entry))
            with patch.object(gameplay, "Executors", return_value=runtime), \
                    patch("host.android.check.check", **check_behaviour) as check:
                return gameplay.GameplayAction.run.__wrapped__(None, None, argv), check
        entry, param = "MaaNikki_AndroidCheck", json.dumps({"kind": "android_check"})
        result, check = run(entry, param, return_value=True)
        self.assertTrue(result)
        check.assert_called_once_with(runtime)
        result, _ = run(entry, param, side_effect=RuntimeError("capture failed"))
        self.assertFalse(result)  # a failed read-only check does not "recover" by sending input
        for bad_entry, bad_param in ((entry, ""), (entry, "{}"), (entry, json.dumps({"kind": "main"})),
                                     (entry, json.dumps({"kind": "mailbox"})), ("MaaNikki_Mailbox", param)):
            with self.subTest(entry=bad_entry, param=bad_param):
                result, check = run(bad_entry, bad_param, return_value=True)
                self.assertFalse(result)
                check.assert_not_called()
        runtime.main.assert_not_called()

if __name__ == "__main__":
    unittest.main()
