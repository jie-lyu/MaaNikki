"""Layered game resources (resource/ plus resource_android/ for the Android target):
python/python.exe -B scripts/test_resource_layers.py.

Windows and native macOS load only resource/, and every image path must resolve exactly as before; the Android
target loads resource_android/ on top. The last test loads two bundles into MaaFramework itself to show what
attach_resource_path relies on: with both layers loaded before the first recognition, the later layer's nodes and
same-name images win.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2  # noqa: F401  (loaded once before maa, as the agent does)
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))

import host  # noqa: E402
import app_paths  # noqa: E402
from app_paths import RESOURCE_ROOT, resolve_resource_image, resource_layers  # noqa: E402

BASE = RESOURCE_ROOT / "resource"
WINDOWS_CONTROLLER = {"name": "nikki_windows", "type": "Win32"}
MACOS_CONTROLLER = {"name": "nikki_macos", "type": "MacOS"}
ADB_CONTROLLER = {"name": "nikki_android", "type": "Adb"}


@contextmanager
def target(desktop, controller=None):
    env = {k: v for k, v in os.environ.items() if k != "PI_CONTROLLER"}
    if controller is not None:
        env["PI_CONTROLLER"] = json.dumps(controller)
    with patch.dict(os.environ, env, clear=True), patch.object(host, "platform_backend", return_value=desktop):
        yield


def shipped_templates() -> set[str]:
    """Every image path the shipped resources name: catalog assets and materials, pipeline templates (built-in and
    nikki.ui_template, including fallback_all)."""
    names = set()
    data = json.loads((BASE / "image/game/catalog.json").read_text(encoding="utf-8"))
    for group in ("assets", "materials"):
        for entry in data.get(group, {}).values():
            if isinstance(entry, dict) and isinstance(entry.get("template"), str):
                names.add(entry["template"])

    def collect(value):
        if isinstance(value, dict):
            template = value.get("template")
            for item in (template if isinstance(template, list) else [template]):
                if isinstance(item, str) and item:
                    names.add(item)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    for path in (BASE / "pipeline").glob("*.json"):
        collect(json.loads(path.read_text(encoding="utf-8")))
    return names


def symlink_or_skip(test, link: Path, target_path: Path, *, directory=False):
    """Symlinks need a privilege Windows may not grant; these checks are then skipped (listed as allowed skips)."""
    try:
        link.symlink_to(target_path, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        test.skipTest(f"cannot create symlinks here: {error}")


def write_png(path: Path, value: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = np.zeros((24, 24, 3), np.uint8)
    image[4:20, 4:20] = value
    cv2.imwrite(str(path), image)


class LayerSelectionTests(unittest.TestCase):
    def test_desktop_targets_load_only_the_main_resource(self):
        for desktop, controller in (("win32", None), ("win32", WINDOWS_CONTROLLER),
                                    ("macos", None), ("macos", MACOS_CONTROLLER), ("win32", ADB_CONTROLLER)):
            with self.subTest(desktop=desktop, controller=controller), target(desktop, controller):
                self.assertEqual(resource_layers(), (BASE,))

    def test_the_android_target_adds_resource_android_on_top(self):
        with target("macos", ADB_CONTROLLER):
            self.assertEqual(resource_layers(), (BASE, RESOURCE_ROOT / "resource_android"))


class WindowsResolutionUnchangedTests(unittest.TestCase):
    def test_every_shipped_image_resolves_exactly_as_before(self):
        names = shipped_templates()
        self.assertGreater(len(names), 300)
        root = (BASE / "image").resolve()
        for desktop, controller in (("win32", None), ("win32", WINDOWS_CONTROLLER), ("macos", MACOS_CONTROLLER)):
            with target(desktop, controller):
                for name in names:
                    self.assertEqual(resolve_resource_image(name, BASE), (root / name).resolve(), name)

    def test_starlight_crystals_loads_its_edge_ring_from_the_android_layer(self):
        from daily.starsea import Starsea
        tmp = Path(tempfile.mkdtemp(prefix="maanikki-ring-"))
        try:
            base, android = tmp / "resource", tmp / "resource_android"
            write_png(base / "image/nikki/common/crystal_edge_ring.png", 40)
            write_png(android / "image/nikki/common/crystal_edge_ring.png", 220)
            loaded = []
            starsea = Starsea.__new__(Starsea)
            starsea.resource = base
            with target("macos", ADB_CONTROLLER), \
                    patch("daily.starsea.prepared_template", side_effect=lambda path, **kw: loaded.append(path)):
                starsea.crystal_markers(None, [])
            self.assertEqual(loaded, [(android / "image/nikki/common/crystal_edge_ring.png").resolve()])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_starlight_crystals_loads_its_edge_ring_as_before(self):
        from daily.starsea import Starsea
        loaded = []
        starsea = Starsea.__new__(Starsea)
        starsea.resource = BASE
        with target("win32", WINDOWS_CONTROLLER), \
                patch("daily.starsea.prepared_template", side_effect=lambda path, **kw: loaded.append(path)):
            starsea.crystal_markers(None, [])
        self.assertEqual(loaded, [(BASE / "image/nikki/common/crystal_edge_ring.png").resolve()])

    def test_escaping_paths_are_still_refused(self):
        with target("win32", WINDOWS_CONTROLLER):
            self.assertIsNone(resolve_resource_image("../../interface.json", BASE))
            self.assertIsNone(resolve_resource_image("/etc/passwd", BASE))

    def test_the_catalog_is_the_main_one(self):
        from daily.ui import catalog
        with target("win32", WINDOWS_CONTROLLER):
            merged = catalog(BASE)
        data = json.loads((BASE / "image/game/catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(merged, {"assets": data["assets"], "materials": data["materials"]})


class AndroidOverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="maanikki-layers-"))
        self.base = self.tmp / "resource"
        self.android = self.tmp / "resource_android"
        write_png(self.base / "image/game/a.png", 50)
        write_png(self.base / "image/game/b.png", 60)
        write_png(self.android / "image/game/a.png", 200)  # the phone-layout version
        (self.base / "image/game").mkdir(parents=True, exist_ok=True)
        (self.base / "image/game/catalog.json").write_text(json.dumps({"assets": {
            "A": {"template": "game/a.png", "roi": [0, 0, 10, 10]},
            "B": {"template": "game/b.png", "roi": [1, 1, 10, 10]}}, "materials": {"M": {"type": "plant"}}}))
        (self.android / "image/game/catalog.json").write_text(json.dumps({"assets": {
            "A": {"roi": [5, 5, 12, 12]},
            "PhoneOnly": {"template": "game/phone.png", "roi": [0, 0, 8, 8]}}}))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_top_layer_wins_and_the_base_is_the_fallback(self):
        with target("macos", ADB_CONTROLLER):
            self.assertEqual(resolve_resource_image("game/a.png", self.base),
                             (self.android / "image/game/a.png").resolve())
            self.assertEqual(resolve_resource_image("game/b.png", self.base),
                             (self.base / "image/game/b.png").resolve())
            self.assertEqual(resolve_resource_image("game/missing.png", self.base),
                             (self.base / "image/game/missing.png").resolve())
            self.assertIsNone(resolve_resource_image("../../x.png", self.base))

    def test_an_overlay_file_is_not_vetoed_by_the_base_and_an_escaping_overlay_is_skipped(self):
        outside = self.tmp / "outside.png"
        write_png(outside, 10)
        symlink_or_skip(self, self.base / "image/game/c.png", outside)     # base file leaves image/
        write_png(self.android / "image/game/c.png", 210)                    # overlay holds a proper file
        symlink_or_skip(self, self.android / "image/game/b.png", outside)  # overlay file leaves image/
        with target("macos", ADB_CONTROLLER):
            self.assertEqual(resolve_resource_image("game/c.png", self.base),
                             (self.android / "image/game/c.png").resolve())
            self.assertEqual(resolve_resource_image("game/b.png", self.base),
                             (self.base / "image/game/b.png").resolve())

    def test_a_linked_image_directory_resolves_the_same_in_game_ui_and_ui_template(self):
        from daily.ui import GameUI
        shared = self.tmp / "shared-sprites"
        shutil.move(str(self.base / "image"), str(shared))
        symlink_or_skip(self, self.base / "image", shared, directory=True)
        from recognition.ui_template import UITemplateRecognition
        frame = np.zeros((40, 40, 3), np.uint8)
        frame[10:26, 10:26] = 60  # b.png's brightness
        param = {"roi": [0, 0, 40, 40], "template": "game/b.png", "gray_limit": [40, 255], "threshold": .9}
        argv = SimpleNamespace(image=frame, custom_recognition_param=json.dumps(param), node_name="X")
        analyze = getattr(UITemplateRecognition.analyze, "__wrapped__", UITemplateRecognition.analyze)
        with target("macos", MACOS_CONTROLLER), patch("recognition.ui_template.RESOURCE_ROOT", self.tmp):
            self.assertEqual(GameUI(None, self.base).asset_path("game/b.png"), (shared / "game/b.png").resolve())
            result = analyze(UITemplateRecognition(), None, argv)  # the real entry point and its root
        self.assertIsNotNone(result.box, result.detail)
        self.assertEqual(result.detail["template"], "game/b.png")

    def test_catalog_entries_merge_with_the_top_layer_winning(self):
        from daily.ui import catalog
        with target("macos", ADB_CONTROLLER):
            merged = catalog(self.base)
        self.assertEqual(merged["assets"]["A"], {"template": "game/a.png", "roi": [5, 5, 12, 12]})  # field by field
        self.assertEqual(merged["assets"]["B"]["roi"], [1, 1, 10, 10])
        self.assertIn("PhoneOnly", merged["assets"])
        self.assertEqual(merged["materials"], {"M": {"type": "plant"}})
        with target("win32", WINDOWS_CONTROLLER):
            self.assertEqual(catalog(self.base)["assets"]["A"]["roi"], [0, 0, 10, 10])  # no layer: as before

    def test_game_ui_and_ui_template_load_the_top_layer_image(self):
        from daily.ui import GameUI
        from recognition.ui_template import UITemplateRecognition
        with target("macos", ADB_CONTROLLER):
            self.assertEqual(GameUI(None, self.base).asset_path("game/a.png"),
                             (self.android / "image/game/a.png").resolve())
            frame = np.zeros((40, 40, 3), np.uint8)
            frame[10:26, 10:26] = 200  # matches only the phone-layout template's brightness
            box, detail = UITemplateRecognition._match_spec(
                frame, {"roi": [0, 0, 40, 40], "template": "game/a.png", "gray_limit": [150, 255], "threshold": .9},
                self.base)
        self.assertIsNotNone(box, detail)


FRAMEWORK_CHECK = r"""
import json, sys, tempfile, shutil
from pathlib import Path
import cv2
import numpy as np
from maa.controller import CustomController
from maa.resource import Resource
from maa.tasker import Tasker

repo = Path(sys.argv[1])
tmp = Path(tempfile.mkdtemp(prefix="maanikki-bundles-"))
try:
    rng = np.random.default_rng(7)
    present = rng.integers(0, 255, (24, 24, 3), dtype=np.uint8)  # the pattern in the frame
    absent = rng.integers(0, 255, (24, 24, 3), dtype=np.uint8)   # a different, equally textured pattern
    for bundle, image in (("base", absent), ("android", present)):
        (tmp / bundle / "pipeline").mkdir(parents=True)
        (tmp / bundle / "image").mkdir()
        cv2.imwrite(str(tmp / bundle / "image" / "mark.png"), image)
    node = {"recognition": "TemplateMatch", "template": "mark.png", "threshold": 0.95, "action": "DoNothing"}
    # Both layers search the whole region where the pattern is; only the image differs between them.
    (tmp / "base/pipeline/p.json").write_text(json.dumps({"Mark": {**node, "roi": [0, 0, 128, 128]}}))
    (tmp / "android/pipeline/p.json").write_text(json.dumps({"Mark": {**node, "roi": [0, 0, 120, 120]}}))
    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[20:44, 20:44] = present

    class Frame(CustomController):
        def connect(self): return True
        def request_uuid(self): return "resource-layers-test"
        def screencap(self): return frame
        def refuse(self, *args): return False
        start_app = stop_app = click = swipe = touch_down = touch_move = touch_up = refuse
        click_key = input_text = key_down = key_up = refuse

    controller = Frame()
    assert controller.post_connection().wait().succeeded

    def recognize(bundles):
        resource = Resource()
        for bundle in bundles:
            assert resource.post_bundle(bundle if isinstance(bundle, Path) else tmp / bundle).wait().succeeded
        tasker = Tasker()
        assert tasker.bind(resource, controller)
        detail = tasker.post_task("Mark").wait().get()
        hit = bool(detail and detail.nodes and detail.nodes[-1].recognition and detail.nodes[-1].recognition.hit)
        return resource.get_node_data("Mark")["recognition"]["param"]["roi"], hit

    outcome = {"base": recognize(["base"]), "layered": recognize(["base", "android"])}
    (tmp / "android/image/mark.png").unlink()  # control: same layered nodes, no overriding image
    outcome["layered_without_image"] = recognize(["base", "android"])
    # The repository's own layer loads on top of the real resources (a source archive without it: base only).
    real = Resource()
    layers = [repo / "resource"] + ([repo / "resource_android"] if (repo / "resource_android").is_dir() else [])
    outcome["real_layers"] = [real.post_bundle(layer).wait().succeeded for layer in layers]
    print(json.dumps(outcome))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
"""


@unittest.skipUnless((ROOT / "maafw").exists(), "needs the MaaFramework runtime in ./maafw")
class FrameworkLayeringTests(unittest.TestCase):
    """What attach_resource_path relies on, in MaaFramework 5.16.1 itself (in a fresh interpreter: importing the
    agent's recognition modules above puts this process's binding in AgentServer mode)."""

    def test_the_later_bundle_overrides_nodes_and_same_name_images_when_loaded_before_recognition(self):
        import subprocess
        env = {**os.environ, "MAAFW_BINARY_PATH": str(ROOT / "maafw")}
        result = subprocess.run([sys.executable, "-I", "-B", "-c", FRAMEWORK_CHECK, str(ROOT)], capture_output=True,
                                text=True, timeout=300, env=env, cwd=tempfile.gettempdir())
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(outcome["base"], [[0, 0, 128, 128], False])
        self.assertEqual(outcome["layered"], [[0, 0, 120, 120], True])  # the later bundle's node and image
        # Without the overriding image the same nodes miss: the hit came from the image, not the region.
        self.assertEqual(outcome["layered_without_image"], [[0, 0, 120, 120], False])
        # A git checkout (CI, development) must ship the Android layer; only the release source archive, which does
        # not list it yet (plan risk 13), checks the base layer alone.
        if (ROOT / ".git").exists():
            self.assertTrue((ROOT / "resource_android").is_dir(), "resource_android/ is missing from the checkout")
            self.assertEqual(outcome["real_layers"], [True, True])
        else:
            self.assertEqual(outcome["real_layers"][0], True)


if __name__ == "__main__":
    unittest.main()
