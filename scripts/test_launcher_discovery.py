"""Official launcher discovery with a fake registry: python/python.exe -B scripts/test_launcher_discovery.py.

The start task takes the first valid installation; closing the game collects
every installation and requires exactly one to contain the game that ran.
Runs on any OS: winreg is replaced by a fake module.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))

import game_close  # noqa: E402
import start_game  # noqa: E402

NAMES = ("InfinityNikki Launcher", "InfinityNikkiBili Launcher", "InfinityNikkiGlobal Launcher")


def fake_winreg(entries: dict[str, str]):
    module = types.ModuleType("winreg")
    module.HKEY_CURRENT_USER = object()

    class Key:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(root, path):
        name = path.split("\\", 1)[1]
        if name not in entries:
            raise OSError(2, "not found")
        return Key(entries[name])

    module.OpenKey = open_key
    module.QueryValueEx = lambda key, name: (key.value, 1)
    return module


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name)
        self.local = self.base / "Local"

    def install(self, relative: str) -> Path:
        launcher = self.base / relative / "launcher.exe"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_bytes(b"MZ")
        return launcher

    def config(self, name: str, game_dir: str | None, body: str | None = None):
        path = self.local / name / "config.ini"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body if body is not None else f'[Download]\ngameDir="{game_dir}"\n', encoding="utf-8")

    def discover(self, registry: dict[str, str]):
        with patch.dict(sys.modules, {"winreg": fake_winreg(registry)}), \
                patch.dict(os.environ, {"LOCALAPPDATA": str(self.local)}):
            try:
                first = start_game.resolve_program("")
            except ValueError as error:
                first = error
            return first, game_close._launcher_candidates()

    def test_registry_first_then_config_in_name_order(self):
        cn = self.install("CN")
        bili = self.install("Bili")
        glob = self.install("Global/game")
        self.config(NAMES[2], str(self.base / "Global/game"))
        first, candidates = self.discover({NAMES[1]: str(bili.parent), NAMES[0]: str(cn.parent)})
        self.assertEqual(first, cn.resolve())
        self.assertEqual(candidates, [cn, bili, glob])

    def test_config_game_dir_or_its_parent(self):
        launcher = self.install("Global")
        self.config(NAMES[2], str(self.base / "Global/InfinityNikki"))
        first, candidates = self.discover({})
        self.assertEqual(first, launcher.resolve())
        self.assertEqual(candidates, [launcher])

    def test_missing_launchers_and_duplicates(self):
        launcher = self.install("CN")
        self.config(NAMES[0], str(self.base / "CN"))
        first, candidates = self.discover({NAMES[0]: str(launcher.parent), NAMES[1]: str(self.base / "absent")})
        self.assertEqual(first, launcher.resolve())
        self.assertEqual(candidates, [launcher])
        first, candidates = self.discover({NAMES[1]: str(self.base / "absent")})
        self.assertEqual(first, launcher.resolve())  # found through config.ini
        (self.local / NAMES[0] / "config.ini").unlink()
        first, candidates = self.discover({})
        self.assertIsInstance(first, ValueError)
        self.assertEqual(candidates, [])

    def test_malformed_config_fails_the_start_task_but_not_the_close_task(self):
        self.config(NAMES[0], None, body="not an ini file\n")
        launcher = self.install("Global")
        self.config(NAMES[2], str(self.base / "Global"))
        with patch.dict(sys.modules, {"winreg": fake_winreg({})}), \
                patch.dict(os.environ, {"LOCALAPPDATA": str(self.local)}):
            with self.assertRaises(Exception) as raised:
                start_game.resolve_program("")
            self.assertNotIsInstance(raised.exception, ValueError)
            # The close task stops scanning config files at the first unreadable one.
            self.assertEqual(game_close._launcher_candidates(), [])
        self.assertTrue(launcher.is_file())

    def test_close_requires_the_one_install_containing_the_game(self):
        cn, glob = Path("C:/Games/CN/launcher.exe"), Path("C:/Games/Global/launcher.exe")
        self.assertEqual(game_close.select_launcher_for_game([cn, glob], "C:/Games/CN/X6Game/Binaries/x.exe"), cn)
        nested = Path("C:/Games/CN/Global/launcher.exe")
        self.assertIsNone(game_close.select_launcher_for_game([cn, nested], "C:/Games/CN/Global/x.exe"))
        self.assertIsNone(game_close.select_launcher_for_game([cn], "D:/Other/x.exe"))


if __name__ == "__main__":
    unittest.main()
