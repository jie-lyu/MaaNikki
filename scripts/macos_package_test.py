"""Offline checks of the macOS package inputs: python -B scripts/macos_package_test.py

Pure standard library; runs on any OS. Named outside the test_*.py pattern on purpose: the Windows
offline runner (scripts/run_offline_tests.py) discovers scripts/test_*.py against a Windows baseline,
and these checks belong to the macOS workflows.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import macos_interface as mi  # noqa: E402

WINDOWS_LOCK = ROOT / "build" / "runtime.lock.json"
MACOS_LOCK = ROOT / "build" / "macos" / "runtime.lock.json"
UPSTREAM_LOCK = ROOT / "native" / "macos-control-unit" / "upstream.lock.json"
SHA256 = re.compile(r"[0-9a-f]{64}")


class RuntimeLockTests(unittest.TestCase):
    def setUp(self):
        self.windows = mi.read_json(WINDOWS_LOCK)
        self.macos = mi.read_json(MACOS_LOCK)

    def test_same_versions_as_the_windows_runtime(self):
        self.assertEqual(self.macos["python_version"], self.windows["python_version"])
        self.assertEqual(self.macos["maafw_version"], self.windows["maafw_version"])
        self.assertEqual({(w["package"], w["version"]) for w in self.macos["wheels"]},
                         {(w["package"], w["version"]) for w in self.windows["wheels"]})

    def test_requirements_txt_lists_the_same_packages(self):
        lines = [line.strip() for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
        self.assertEqual(sorted(lines), sorted(f"{w['package']}=={w['version']}" for w in self.macos["wheels"]))

    def test_wheels_are_arm64_macos_or_pure_python(self):
        for wheel in self.macos["wheels"]:
            with self.subTest(wheel=wheel["name"]):
                tag = wheel["name"].removesuffix(".whl").split("-")[-1]
                self.assertTrue(tag == "any" or re.fullmatch(r"macosx_\d+_\d+_arm64", tag), tag)
                if tag != "any":
                    major = int(tag.split("_")[1])
                    self.assertLessEqual(major, int(self.macos["minimum_macos"].split(".")[0]))
                python = wheel["name"].split("-")[-3]
                self.assertIn(python, ("py3", "cp312", "cp37"))
                self.assertRegex(wheel["sha256"], SHA256)
                self.assertTrue(wheel["url"].startswith("https://files.pythonhosted.org/"), wheel["url"])
                self.assertTrue(wheel["url"].endswith("/" + wheel["name"]), wheel["url"])

    def test_python_is_the_pinned_version_for_arm64(self):
        python = self.macos["python"]
        self.assertRegex(python["sha256"], SHA256)
        self.assertTrue(python["name"].startswith(f"cpython-{self.macos['python_version']}+{python['release']}-"))
        self.assertTrue(python["name"].endswith("-aarch64-apple-darwin-install_only_stripped.tar.gz"))
        self.assertTrue(python["url"].startswith(
            f"https://github.com/astral-sh/python-build-standalone/releases/download/{python['release']}/"))

    def test_maaframework_runtime_comes_from_the_control_unit_lock(self):
        upstream = mi.read_json(UPSTREAM_LOCK)
        self.assertEqual(upstream["maafw"]["tag"], "v" + self.macos["maafw_version"])
        self.assertIn("macos-aarch64", upstream["stock_runtime"]["name"])


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.repository_bytes = mi.INTERFACE.read_bytes()
        self.repository = json.loads(self.repository_bytes)
        self.overlay = mi.read_json(mi.TEST_BUILD_OVERLAY)

    def test_repository_interface_is_only_read(self):
        mi.macos_interface()
        mi.macos_interface(test_build_commit="abcdef1")
        self.assertEqual(mi.INTERFACE.read_bytes(), self.repository_bytes)

    def test_development_interface_adds_the_mac_controller_and_agent(self):
        interface = mi.macos_interface()
        names = [controller["name"] for controller in interface["controller"]]
        self.assertEqual(names[:-1], [c["name"] for c in self.repository["controller"]])
        self.assertEqual(names[-1], "nikki_macos")
        self.assertEqual(interface["agent"]["child_exec"], "./python/bin/python3")
        self.assertIn("telemetry", interface)  # the development home keeps the repository's settings

    def test_test_package_drops_telemetry_and_mirror_and_names_the_commit(self):
        interface = mi.macos_interface(test_build_commit="0123abc")
        for key in self.overlay["remove"]:
            self.assertNotIn(key, interface)
        text = mi.render(interface)
        self.assertNotIn(self.repository["telemetry"]["sentry"]["dsn"], text)
        self.assertNotIn('"mirrorchyan_rid"', text)
        self.assertNotIn(self.repository["statistics"]["endpoint"], text)
        # The client skips update checks and hides the update page only when both are absent.
        self.assertNotIn("github", interface)
        self.assertNotIn(self.repository["github"], text)
        self.assertIn("0123abc", interface["title"])
        self.assertIn("测试版", interface["title"])
        self.assertEqual(interface["version"], self.repository["version"])  # the updater parses it
        # The bundle's Python: isolated from PYTHON* variables and user site, no bytecode writes.
        self.assertEqual(interface["agent"]["child_exec"], "./python/bin/python3")
        self.assertEqual(interface["agent"]["child_args"], ["-I", "-B", "-X", "utf8", "./agent/main.py"])

    def test_a_removed_value_moved_elsewhere_is_caught(self):
        packaged = mi.apply_test_build_overlay(mi.macos_interface(), self.overlay, "0123abc")
        packaged["description"] = self.repository["telemetry"]["sentry"]["dsn"]
        problems = mi.test_build_violations(packaged, self.repository, self.overlay)
        self.assertTrue(any("telemetry" in problem for problem in problems), problems)

    def test_a_removed_key_left_in_place_is_caught(self):
        packaged = dict(mi.macos_interface())
        problems = mi.test_build_violations(packaged, self.repository, self.overlay)
        self.assertIn("key 'telemetry' is still present", problems)

    def test_a_copied_production_url_is_caught_even_if_the_repository_has_it_twice(self):
        repository = json.loads(self.repository_bytes)
        endpoint = repository["telemetry"]["sentry"]["failure_upload_endpoint"]
        repository["description"] = endpoint  # also present outside the removed keys
        packaged = mi.apply_test_build_overlay(mi.apply_overlay(repository, mi.read_json(mi.OVERLAY)),
                                               self.overlay, "0123abc")
        problems = mi.test_build_violations(packaged, repository, self.overlay)
        self.assertTrue(any(endpoint[:40] in problem for problem in problems), problems)

    def test_ordinary_text_is_not_mistaken_for_a_removed_value(self):
        packaged = mi.apply_test_build_overlay(mi.macos_interface(), self.overlay, "0123abc")
        packaged["description"] = "Local reproduction steps for MaaNikki"
        self.assertEqual(mi.test_build_violations(packaged, self.repository, self.overlay), [])

    def test_the_four_services_are_required_even_if_the_overlay_forgets_one(self):
        overlay = dict(self.overlay, remove=["telemetry"])
        packaged = mi.apply_test_build_overlay(mi.macos_interface(), overlay, "0123abc")
        problems = mi.test_build_violations(packaged, self.repository, overlay)
        for key in ("statistics", "mirrorchyan_rid", "github"):
            self.assertIn(f"key {key!r} is still present", problems)

    def test_commit_must_be_a_revision(self):
        for bad in ("", "main", "abc", "0123abc; rm", "0123ABC"):
            with self.subTest(commit=bad), self.assertRaises(ValueError):
                mi.apply_test_build_overlay(mi.macos_interface(), self.overlay, bad)


if __name__ == "__main__":
    unittest.main()
