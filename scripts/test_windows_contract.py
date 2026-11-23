"""Windows contract snapshot: python -B scripts/test_windows_contract.py.

Pins what Windows users get, so platform refactors can prove they changed
nothing: the effective interface after imports and the Windows controller
filter (controllers, agent, pretask, tasks, options and defaults), and the
file inventory of the Windows package. Runs on any OS with the standard
library only.

    --update          rewrite the snapshot after an intended change
    --runtime DIR     also compare a runtime prepared by
                      `scripts/release.py prepare` with the inventory
                      (Windows CI; needs the downloaded runtime)
"""
from __future__ import annotations

import argparse
from fnmatch import fnmatchcase
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_FILE = ROOT / "scripts/fixtures/windows-contract.json"
INVENTORY_FILE = ROOT / "scripts/fixtures/windows-package-files.txt"

_spec = importlib.util.spec_from_file_location("maanikki_release", ROOT / "scripts/release.py")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

# Mirrors getUnsupportedControllerTypes() in client/src/services/interfaceLoader.ts for a Windows backend.
UNSUPPORTED_ON_WINDOWS = {"MacOS", "PlayCover", "WlRoots", "Linux"}
# Interface keys that describe the Windows product. Release metadata (version,
# telemetry, statistics) and display text change without changing behaviour.
CONTRACT_KEYS = ("interface_version", "name", "mirrorchyan_rid", "mirrorchyan_multiplatform", "github",
                 "controller", "resource", "agent", "pretask", "global_option", "setting", "group",
                 "task", "option", "preset", "import")
TEXT_KEYS = {"label", "description", "pattern_msg", "title"}
# Written by release.py from downloads or the client build, not copied from source.
GENERATED_ENTRIES = (
    "MaaNikki.exe",
    "licenses/MaaFramework-LGPL-3.0.md",
    "licenses/dependencies/**",
    "maafw/*.dll",
    "maafw/MaaAgentBinary/**",
    "maafw/integrity.json",
    "python/**",
    "runtime-manifest.json",
)
# Added by `release.py package` after `prepare`, so absent from a prepared runtime.
PACKAGE_ONLY_ENTRIES = ("MaaNikki.exe", "licenses/dependencies/**")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _pretasks(value) -> list:
    if not value:
        return []
    return list(value) if isinstance(value, list) else [value]


def effective_interface(root: Path = ROOT) -> dict:
    """interface.json with imports merged as mergeImported() in interfaceLoader.ts."""
    pi = read_json(root / "interface.json")
    for imported_path in pi.get("import", []):
        imported = read_json(root / imported_path)
        if imported.get("task"):
            pi["task"] = [*(pi.get("task") or []), *imported["task"]]
        if imported.get("global_option"):
            seen = set(pi.get("global_option") or [])
            added = []
            for key in imported["global_option"]:
                if key not in seen:
                    seen.add(key)
                    added.append(key)
            if added:
                pi["global_option"] = [*(pi.get("global_option") or []), *added]
        if imported.get("option"):
            pi["option"] = {**(pi.get("option") or {}), **imported["option"]}
        if imported.get("setting"):
            pi["setting"] = [*(pi.get("setting") or []), *imported["setting"]]
        if imported.get("preset"):
            pi["preset"] = [*(pi.get("preset") or []), *imported["preset"]]
        if imported.get("group"):
            names = {group["name"] for group in pi.get("group") or []}
            added = []
            for group in imported["group"]:
                if group["name"] not in names:
                    names.add(group["name"])
                    added.append(group)
            if added:
                pi["group"] = [*(pi.get("group") or []), *added]
        pretasks = _pretasks(imported.get("pretask"))
        if pretasks:
            pi["pretask"] = [*_pretasks(pi.get("pretask")), *pretasks]
    pi["controller"] = [item for item in pi.get("controller", [])
                        if item.get("type") not in UNSUPPORTED_ON_WINDOWS]
    return pi


def _digest(value) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _strip(value):
    """Drop display text; summarize pipeline overrides by digest."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in TEXT_KEYS:
                continue
            if key == "pipeline_override":
                result["pipeline_override_sha256"] = _digest(item)
            else:
                result[key] = _strip(item)
        return result
    if isinstance(value, list):
        return [_strip(item) for item in value]
    return value


def windows_contract(root: Path = ROOT) -> dict:
    pi = effective_interface(root)
    contract = {key: _strip(pi[key]) for key in CONTRACT_KEYS if key in pi}
    contract["binary_zip_name"] = release.BINARY_ZIP_NAME
    return contract


def _tracked_files(root: Path, paths) -> list[str]:
    if (root / ".git").exists():
        result = subprocess.run(["git", "ls-files", "-z", "--cached", "--", *paths], cwd=root,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return [entry for entry in result.stdout.decode("utf-8").split("\0") if entry]
    found = []
    for name in paths:
        path = root / name
        if path.is_file():
            found.append(name)
        elif path.is_dir():
            found.extend(item.relative_to(root).as_posix() for item in path.rglob("*") if item.is_file())
    return found


def _shipped(relative: str) -> bool:
    path = PurePosixPath(relative)
    return (path.parts[0] not in release.UNSHIPPED_RUNTIME_DIRS
            and "__pycache__" not in path.parts and path.suffix not in release.GENERATED_SUFFIXES)


def package_inventory(root: Path = ROOT) -> list[str]:
    """Source-derived files of the Windows package, as release.prepare() copies them from a clean checkout."""
    files = set()
    for relative in _tracked_files(root, release.RUNTIME_DIRS):
        path = root / relative
        # runtime_ignored() is applied per directory by copytree; any ignored ancestor excludes the file.
        if any(release.runtime_ignored(str(parent.parent), [parent.name])
               for parent in [path, *path.parents] if parent != root and root in parent.parents):
            continue
        files.add(relative)
    files.update(_tracked_files(root, ["licenses", "docs/images"]))
    files.update(name for name in ["interface.json", *release.PACKAGED_DOCS] if (root / name).is_file())
    return sorted(name for name in files if _shipped(name))


def render_inventory(files: list[str]) -> str:
    lines = ["# Windows package inventory. Regenerate: python -B scripts/test_windows_contract.py --update",
             "# Generated by scripts/release.py, not copied from source:"]
    lines += [f"# generated: {entry}" for entry in GENERATED_ENTRIES]
    return "\n".join([*lines, *files]) + "\n"


def parse_inventory(text: str) -> tuple[list[str], list[str]]:
    files, generated = [], []
    for line in text.splitlines():
        if line.startswith("# generated: "):
            generated.append(line[len("# generated: "):])
        elif line and not line.startswith("#"):
            files.append(line)
    return files, generated


def render_contract(contract: dict) -> str:
    return json.dumps(contract, ensure_ascii=False, indent=2) + "\n"


def _matches(relative: str, patterns) -> bool:
    for pattern in patterns:
        if pattern.endswith("/**"):
            if relative.startswith(pattern[:-2]):
                return True
        elif "/" in pattern and fnmatchcase(relative, pattern) and relative.count("/") == pattern.count("/"):
            return True
        elif relative == pattern:
            return True
    return False


def compare_runtime(runtime: Path, files: list[str], generated: list[str]) -> list[str]:
    """Differences between a prepared runtime and the inventory, as messages."""
    runtime = runtime.resolve()
    actual = {path.relative_to(runtime).as_posix() for path in release.release_files(runtime)}
    expected = set(files)
    problems = [f"missing from runtime: {name}" for name in sorted(expected - actual)]
    problems += [f"not in inventory: {name}" for name in sorted(actual - expected)
                 if not _matches(name, generated)]
    for pattern in generated:
        if pattern not in PACKAGE_ONLY_ENTRIES and not any(_matches(name, [pattern]) for name in actual):
            problems.append(f"generated entry absent from runtime: {pattern}")
    return problems


class WindowsContractTests(unittest.TestCase):
    runtime: Path | None = None

    def test_effective_interface_matches_snapshot(self):
        self.assertTrue(CONTRACT_FILE.is_file(), f"missing {CONTRACT_FILE}; run with --update")
        expected = read_json(CONTRACT_FILE)
        actual = json.loads(render_contract(windows_contract()))
        for key in sorted(set(expected) | set(actual)):
            with self.subTest(key=key):
                self.assertEqual(actual.get(key), expected.get(key),
                                 f"Windows contract changed at '{key}'. If intended, rerun with --update "
                                 "and review the snapshot diff.")

    def test_package_inventory_matches_snapshot(self):
        self.assertTrue(INVENTORY_FILE.is_file(), f"missing {INVENTORY_FILE}; run with --update")
        files, generated = parse_inventory(INVENTORY_FILE.read_text(encoding="utf-8"))
        actual = package_inventory()
        self.assertEqual(generated, list(GENERATED_ENTRIES))
        added, removed = sorted(set(actual) - set(files)), sorted(set(files) - set(actual))
        self.assertFalse(added or removed,
                         f"Windows package inventory changed; added={added[:20]} removed={removed[:20]}. "
                         "If intended, rerun with --update.")

    def test_windows_controller_is_the_only_desktop_controller(self):
        controllers = windows_contract()["controller"]
        self.assertEqual([(item["name"], item["type"]) for item in controllers], [("nikki_windows", "Win32")])

    def test_prepared_runtime_matches_inventory(self):
        if self.runtime is None:
            self.skipTest("pass --runtime DIR to compare a prepared runtime")
        files, generated = parse_inventory(INVENTORY_FILE.read_text(encoding="utf-8"))
        problems = compare_runtime(self.runtime, files, generated)
        self.assertFalse(problems, "\n".join(problems[:50]))


class ContractHelperTests(unittest.TestCase):
    def test_merge_follows_client_rules(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "interface.json").write_text(json.dumps({
                "controller": [{"name": "w", "type": "Win32"}, {"name": "m", "type": "MacOS"}],
                "global_option": ["A"], "group": [{"name": "g", "label": "first"}],
                "option": {"A": {"type": "switch", "default_case": "Yes"}},
                "import": ["one.json", "two.json"],
            }), encoding="utf-8")
            (root / "one.json").write_text(json.dumps({
                "global_option": ["A", "B"], "group": [{"name": "g", "label": "second"}, {"name": "h"}],
                "option": {"A": {"type": "switch", "default_case": "No"}}, "pretask": {"name": "p"},
                "task": [{"name": "t1"}],
            }), encoding="utf-8")
            (root / "two.json").write_text(json.dumps({"task": [{"name": "t2"}], "pretask": [{"name": "q"}]}),
                                           encoding="utf-8")
            pi = effective_interface(root)
        self.assertEqual([item["name"] for item in pi["controller"]], ["w"])
        self.assertEqual(pi["global_option"], ["A", "B"])
        self.assertEqual([(item["name"], item.get("label")) for item in pi["group"]], [("g", "first"), ("h", None)])
        self.assertEqual(pi["option"]["A"]["default_case"], "No")
        self.assertEqual([item["name"] for item in pi["pretask"]], ["p", "q"])
        self.assertEqual([item["name"] for item in pi["task"]], ["t1", "t2"])

    def test_strip_keeps_behaviour_and_drops_text(self):
        stripped = _strip({"label": "x", "description": "y", "default_case": "Yes",
                           "cases": [{"name": "Yes", "label": "z", "pipeline_override": {"N": {"enabled": True}}}]})
        self.assertEqual(set(stripped), {"default_case", "cases"})
        self.assertEqual(stripped["cases"][0]["name"], "Yes")
        self.assertEqual(len(stripped["cases"][0]["pipeline_override_sha256"]), 16)

    def test_runtime_comparison_reports_both_directions(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            for name in ("interface.json", "agent/main.py", "maafw/MaaFramework.dll", "maafw/integrity.json",
                         "python/python.exe", "runtime-manifest.json", "maafw/MaaAgentBinary/x/y.bin",
                         "licenses/MaaFramework-LGPL-3.0.md", "debug/maa.log", "stray.txt"):
                (runtime / name).parent.mkdir(parents=True, exist_ok=True)
                (runtime / name).write_text("x", encoding="utf-8")
            problems = compare_runtime(runtime, ["interface.json", "agent/main.py", "agent/missing.py"],
                                       list(GENERATED_ENTRIES))
        self.assertEqual(problems, ["missing from runtime: agent/missing.py", "not in inventory: stray.txt"])


def update():
    CONTRACT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT_FILE.write_text(render_contract(windows_contract()), encoding="utf-8")
    INVENTORY_FILE.write_text(render_inventory(package_inventory()), encoding="utf-8")
    print(f"Updated {CONTRACT_FILE.relative_to(ROOT)} and {INVENTORY_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--runtime", type=Path)
    args, rest = parser.parse_known_args()
    if args.update:
        update()
        sys.exit(0)
    WindowsContractTests.runtime = args.runtime
    unittest.main(argv=[sys.argv[0], *rest])
