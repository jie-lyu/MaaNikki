"""The macOS interface.json: the repository's interface.json with the macOS overlays applied.

    build/macos/interface-overlay.json   the macOS agent command and the nikki_macos controller
                                         (development home and package)
    build/macos/test-build-overlay.json  test packages only: switches off what reports to or
                                         updates from the project's production services, and
                                         marks the window title with the build's commit

The repository's interface.json is only read; the Windows package copies it unchanged. Both the
development home (scripts/macos_dev_home.py) and the macOS package (scripts/macos_package.py) write
the result with render(), so they produce the same bytes for the same inputs.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTERFACE = ROOT / "interface.json"
OVERLAY = ROOT / "build" / "macos" / "interface-overlay.json"
TEST_BUILD_OVERLAY = ROOT / "build" / "macos" / "test-build-overlay.json"
COMMIT_PLACEHOLDER = "{commit}"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def apply_overlay(interface: dict, overlay: dict) -> dict:
    """The macOS overlay: agent fields replace the repository's, controllers are appended."""
    result = copy.deepcopy(interface)
    agents = result["agent"] if isinstance(result["agent"], list) else [result["agent"]]
    for agent in agents:
        agent.update(overlay["agent"])
    result["controller"] = [*result["controller"], *overlay["controller"]]
    return result


def apply_test_build_overlay(interface: dict, overlay: dict, commit: str) -> dict:
    """Remove the listed top-level keys, update the agent entries with "agent", and set the
    listed top-level keys ({commit} is replaced)."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        raise ValueError(f"commit must be a hexadecimal Git revision, got {commit!r}")
    result = copy.deepcopy(interface)
    for key in overlay.get("remove", []):
        result.pop(key, None)
    if "agent" in overlay:
        agents = result["agent"] if isinstance(result["agent"], list) else [result["agent"]]
        for agent in agents:
            agent.update(overlay["agent"])
    for key, value in overlay.get("set", {}).items():
        result[key] = value.replace(COMMIT_PLACEHOLDER, commit) if isinstance(value, str) else value
    return result


# What a test package must never carry, whatever the overlay file says: Sentry telemetry and
# failure uploads, statistics, and both update sources (the client checks for updates while either
# of the last two is present).
REQUIRED_REMOVED = ("telemetry", "statistics", "mirrorchyan_rid", "github")


def test_build_violations(packaged: dict, repository: dict, overlay: dict) -> list[str]:
    """What a test package's interface still carries that must be gone.

    Every key in REQUIRED_REMOVED and in the overlay's remove list must be absent, and no URL that
    those keys hold in the repository's interface.json (the Sentry DSN, the failure-upload, statistics
    and repository URLs) may appear anywhere in the packaged document, so a copy of one of them under
    another key is caught too. Only URLs are compared: the other values (the mirror id is the
    project name, the Sentry environment is "production") also occur legitimately elsewhere, and
    the key check covers them.
    """
    removed = [*REQUIRED_REMOVED, *(key for key in overlay.get("remove", []) if key not in REQUIRED_REMOVED)]
    problems = [f"key {key!r} is still present" for key in removed if key in packaged]
    text = json.dumps(packaged, ensure_ascii=False)
    for key in removed:
        for value in _strings(repository.get(key)):
            if value.startswith(("https://", "http://")) and value in text:
                problems.append(f"URL from removed key {key!r} still appears: {value[:40]}")
    return problems


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def macos_interface(*, test_build_commit: str | None = None) -> dict:
    interface = apply_overlay(read_json(INTERFACE), read_json(OVERLAY))
    if test_build_commit is None:
        return interface
    overlay = read_json(TEST_BUILD_OVERLAY)
    packaged = apply_test_build_overlay(interface, overlay, test_build_commit)
    problems = test_build_violations(packaged, read_json(INTERFACE), overlay)
    if problems:
        raise ValueError("test package interface.json: " + "; ".join(problems))
    return packaged


def render(interface: dict) -> str:
    return json.dumps(interface, ensure_ascii=False, indent=2) + "\n"
