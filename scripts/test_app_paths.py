"""Resource/data root split: python -B scripts/test_app_paths.py.

Standard library only; runs on any OS. Checks that without the client's
variables, or with the client passing the install directory for both (the
Windows portable layout), every root resolves exactly as before the split.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
sys.path.insert(0, str(AGENT))

import data_paths  # noqa: E402  (stdlib only)

PROBE = ("import json, sys; sys.path.insert(0, sys.argv[1]); import app_paths; "
         "print(json.dumps([str(app_paths.RESOURCE_ROOT), str(app_paths.DATA_ROOT)]))")
# Modules allowed to locate themselves through __file__.
SELF_LOCATING = {"app_paths.py", "main.py", "start_game.py"}


def roots(**env) -> tuple[str, str]:
    clean = {key: value for key, value in os.environ.items()
             if key not in ("MAANIKKI_RESOURCE_ROOT", "MAANIKKI_DATA_ROOT")}
    clean.update(env)
    output = subprocess.run([sys.executable, "-B", "-c", PROBE, str(AGENT)], env=clean, check=True,
                            stdout=subprocess.PIPE, text=True).stdout
    resource, data = json.loads(output)
    return resource, data


class RootResolutionTests(unittest.TestCase):
    def test_defaults_to_install_directory(self):
        expected = str(AGENT.resolve().parent)
        self.assertEqual(roots(), (expected, expected))

    def test_blank_values_fall_back(self):
        expected = str(AGENT.resolve().parent)
        self.assertEqual(roots(MAANIKKI_RESOURCE_ROOT="  ", MAANIKKI_DATA_ROOT=""), (expected, expected))

    def test_install_directory_keeps_its_spelling(self):
        # The Windows client passes the program directory; the agent keeps the form it always used.
        expected = str(AGENT.resolve().parent)
        spelled = str(AGENT / ".." / ".")
        self.assertEqual(roots(MAANIKKI_RESOURCE_ROOT=spelled, MAANIKKI_DATA_ROOT=spelled + os.sep),
                         (expected, expected))

    def test_separate_data_root(self):
        with tempfile.TemporaryDirectory() as folder:
            resource, data = roots(MAANIKKI_DATA_ROOT=folder)
        self.assertEqual(resource, str(AGENT.resolve().parent))
        self.assertEqual(data, folder)


class DataPathTests(unittest.TestCase):
    def overlay(self, data_root: Path, resource_root: Path):
        slot = data_root / "data_overlay/slots/s1"
        for relative in data_paths._REQUIRED_OVERLAY_FILES:
            (slot / relative).parent.mkdir(parents=True, exist_ok=True)
            (slot / relative).write_text("{}", encoding="utf-8")
        (data_root / "data_overlay/active.json").write_text(json.dumps(
            {"schema_version": 1, "slot": "s1", "wardrobe_version": "2", "game_items": 1}), encoding="utf-8")
        builtin = resource_root / "resource/data/wardrobe_scoring.json"
        builtin.parent.mkdir(parents=True, exist_ok=True)
        builtin.write_text(json.dumps({"version": "1", "game_items": {"a": 1}}), encoding="utf-8")
        return slot

    def test_overlay_under_data_root_and_builtin_under_resource_root(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as resource:
            data, resource = Path(data), Path(resource)
            slot = self.overlay(data, resource)
            os.environ.pop("PI_MAANIKKI_DATA_OVERLAY", None)
            roots = {"data_root": data, "resource_root": resource}
            self.assertEqual(data_paths.active_overlay_dir(**roots), slot)
            self.assertEqual(data_paths.data_path("resource/model/blessing_badge.json", **roots),
                             slot / "resource/model/blessing_badge.json")
            self.assertEqual(data_paths.data_path("resource/data/other.json", **roots),
                             resource / "resource/data/other.json")

    def test_single_root_behaves_as_before(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            slot = self.overlay(root, root)
            os.environ.pop("PI_MAANIKKI_DATA_OVERLAY", None)
            # The old one-root calling convention: overlay and built-in files under the same root.
            self.assertEqual(data_paths.active_overlay_dir(root), slot)
            self.assertEqual(data_paths.data_path("resource/data/blessing_catalog.json", root),
                             slot / "resource/data/blessing_catalog.json")
            self.assertEqual(data_paths.data_path("./resource/data/x.json", root), root / "resource/data/x.json")

    def test_older_overlay_is_ignored(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as resource:
            data, resource = Path(data), Path(resource)
            self.overlay(data, resource)
            (resource / "resource/data/wardrobe_scoring.json").write_text(
                json.dumps({"version": "3", "game_items": {"a": 1}}), encoding="utf-8")
            os.environ.pop("PI_MAANIKKI_DATA_OVERLAY", None)
            self.assertIsNone(data_paths.active_overlay_dir(data_root=data, resource_root=resource))


class LayoutGuardTests(unittest.TestCase):
    def test_agent_modules_take_roots_from_app_paths(self):
        offenders = []
        for path in AGENT.rglob("*.py"):
            if path.name in SELF_LOCATING and path.parent == AGENT:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Name) and node.id == "__file__":
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [], "derive install/data paths from app_paths, not __file__")

    def test_client_reads_agent_output_from_the_data_root(self):
        import app_paths
        rust = (ROOT / "client/src-tauri/src/commands/file_ops.rs").read_text(encoding="utf-8")
        listed = re.search(r"const DATA_ROOT_DIRS: &\[&str\] = &\[(.*?)\];", rust, re.DOTALL).group(1)
        self.assertEqual(tuple(re.findall(r'"([^"]+)"', listed)), app_paths.DATA_DIRS)
        # Files the agent writes and the frontend polls through read_local_file.
        agent_writes = {
            "client/src/services/gameKeysReadService.ts": ["RESULT_FILE"],
            "client/src/services/routeStudioService.ts": ["RECORDING_FILE", "TRIAL_FILE"],
        }
        for relative, names in agent_writes.items():
            source = (ROOT / relative).read_text(encoding="utf-8")
            for name in names:
                value = re.search(rf"const {name} = '([^']+)'", source).group(1)
                with self.subTest(file=value):
                    self.assertIn(value.split("/", 1)[0], app_paths.DATA_DIRS)

    def test_client_passes_roots_to_every_python_process(self):
        sources = {
            "commands/maa_agent.rs": "agent",
            "commands/system.rs": "pretask",
            "commands/user_routes.rs": "route generator",
        }
        for relative, role in sources.items():
            text = (ROOT / "client/src-tauri/src" / relative).read_text(encoding="utf-8")
            with self.subTest(role=role):
                self.assertRegex(text, r"apply_agent_root_env\(&mut cmd\)")
        utils = (ROOT / "client/src-tauri/src/commands/utils.rs").read_text(encoding="utf-8")
        body = re.search(r"pub fn apply_agent_root_env\(.*?\n}\n", utils, re.DOTALL).group(0)
        self.assertIn('"MAANIKKI_RESOURCE_ROOT", dir', body)
        self.assertIn('"MAANIKKI_DATA_ROOT", dir', body)
        self.assertIn("get_exe_directory()", body)
        self.assertIn("get_app_data_dir()", body)


if __name__ == "__main__":
    unittest.main()
