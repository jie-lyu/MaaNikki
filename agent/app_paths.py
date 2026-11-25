"""Read-only resource root and writable data root, passed by the client.

RESOURCE_ROOT holds what ships with the program: interface.json, agent/,
resource/, tasks/, maafw/ and the embedded Python. DATA_ROOT holds what the
program writes: config/, logs/, debug/, cache/ and data_overlay/.

The client passes both to the agent and every Python subprocess through
MAANIKKI_RESOURCE_ROOT and MAANIKKI_DATA_ROOT. Without them (scripts, tests,
older clients) both are the install directory that contains agent/, which is
the portable layout used on Windows: there the client passes that same
directory for both, so nothing moves.

A function that takes a ``root`` argument receives the data root when it
stores user data (accounts, statistics, inventory, adventure reports, client
config) and the resource root when it loads game resources (route
generation, material catalogs). It reads the other root from this module.
"""
from __future__ import annotations

import os
from pathlib import Path

# Top-level folders under DATA_ROOT; the client reads these from its data directory too.
DATA_DIRS = ("config", "logs", "debug", "cache", "data_overlay")

RESOURCE_ROOT_ENV = "MAANIKKI_RESOURCE_ROOT"
DATA_ROOT_ENV = "MAANIKKI_DATA_ROOT"

INSTALL_ROOT = Path(__file__).resolve().parents[1]


def _same_directory(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def root_from_env(name: str, default: Path = INSTALL_ROOT) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    path = Path(value)
    # Keep the spelling this process has always used for the install directory
    # (resolved, as before), so paths in logs and reports do not change form.
    if _same_directory(path, default):
        return default
    return path


RESOURCE_ROOT = root_from_env(RESOURCE_ROOT_ENV)
DATA_ROOT = root_from_env(DATA_ROOT_ENV)


# Game resources in load order, the last overriding the earlier. Every target loads resource/; an Android emulator
# target also loads resource_android/ on top of it (the controller's attach_resource_path in interface.json), which
# holds only what differs on the phone layout. docs/android-support-plan.md, decisions 3 and 4.
ANDROID_RESOURCE_DIR = "resource_android"


def resource_layers(base: Path | None = None) -> tuple[Path, ...]:
    """The resource directories this run's game target loads, base first. Only resource/ (``base``) unless the game
    target is the Android backend."""
    base = Path(base) if base is not None else RESOURCE_ROOT / "resource"
    from host import game_backend
    if game_backend() == "android":
        return (base, base.parent / ANDROID_RESOURCE_DIR)
    return (base,)


def resolve_resource_image(relative, base: Path | None = None) -> Path | None:
    """The image file for ``relative`` (a path under image/) in the topmost layer that has it; with no layer holding
    it, the base layer's path, exactly as before (existing or not). None when the path leaves image/, so callers
    keep their own error messages."""
    chosen = base_path = None
    for layer in resource_layers(base):
        root = (layer / "image").resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            return None
        if base_path is None:
            base_path = path
        if path.is_file():
            chosen = path
    return chosen or base_path
