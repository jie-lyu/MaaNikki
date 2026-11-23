"""Agent import boundary: python/python.exe -B scripts/test_host_imports.py.

Importing agent/main.py registers every custom action and recognition. This
must work without loading any platform backend (host.win32 and future
siblings), so the same task code can be registered on macOS. The registered
names are pinned so Windows keeps exactly the handlers it has today.

Needs the pinned dependencies and ./maafw (MaaFramework binaries).
    --update   rewrite scripts/fixtures/agent-registrations.json
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts/fixtures/agent-registrations.json"

PROBE = r"""
import importlib.abc, json, runpy, sys

class BlockBackends(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "host.win32" or name.startswith("host.win32."):
            raise ImportError(f"platform backend imported during registration: {name}")
        return None

sys.meta_path.insert(0, BlockBackends())
runpy.run_path(sys.argv[1], run_name="maanikki_import_check")
import registration
loaded = sorted(name for name in sys.modules if name == "host" or name.startswith("host."))
print(json.dumps({"actions": sorted(registration._ACTION_TYPES),
                  "recognitions": sorted(registration._RECOGNITION_TYPES),
                  "host_modules": loaded,
                  "streams": [sys.stdout.encoding.lower(), sys.stdout.errors, sys.stderr.errors]}))
"""


def registrations() -> dict:
    env = dict(os.environ, PYTHONUTF8="1")
    completed = subprocess.run([sys.executable, "-B", "-c", PROBE, str(ROOT / "agent/main.py")], cwd=ROOT, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    if completed.returncode:
        raise AssertionError(f"agent import failed:\n{completed.stderr[-4000:]}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


class HostImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = registrations()

    def test_registration_loads_no_platform_backend(self):
        backends = [name for name in self.result["host_modules"] if name not in ("host",)]
        self.assertEqual(backends, [])

    @unittest.skipUnless(os.name == "nt", "the Win32 backend only imports on Windows")
    def test_win32_overlay_backend_imports(self):
        # Registration never loads the overlays; import them here so a broken move fails before a HUD opens.
        sys.path.insert(0, str(ROOT / "agent"))
        from host import overlay_backend
        overlays = overlay_backend()
        self.assertIsNotNone(overlays)
        for name in ("GameHudOverlay", "OverlayWindow", "RouteMinimapOverlay"):
            self.assertTrue(callable(getattr(overlays, name)), name)

    def test_agent_output_is_utf8_with_replacement(self):
        self.assertEqual(self.result["streams"], ["utf-8", "replace", "replace"])

    def test_registered_handlers_match_snapshot(self):
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(self.result["actions"], expected["actions"])
        self.assertEqual(self.result["recognitions"], expected["recognitions"])


if __name__ == "__main__":
    if "--update" in sys.argv:
        result = registrations()
        FIXTURE.write_text(json.dumps({key: result[key] for key in ("actions", "recognitions")}, indent=1) + "\n",
                           encoding="utf-8")
        print(f"Updated {FIXTURE.relative_to(ROOT)}")
        sys.exit(0)
    unittest.main()
