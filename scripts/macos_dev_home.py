"""Assemble a macOS development home for running the client from source against the real game.

    python3 -I scripts/macos_dev_home.py [--home DIR] [--python VENV] [--runtime DIR]
    MAANIKKI_DEV_HOME=<DIR> pnpm --dir client tauri dev

A debug build of the client reads interface.json, resource/, tasks/, maafw/ and the agent from
MAANIKKI_DEV_HOME (client/src-tauri/src/commands/utils.rs). This script fills that directory the
way a macOS package will: the repository's resource directories are linked, interface.json is the
repository's with build/macos/interface-overlay.json applied (the macOS agent command and the
nikki_macos controller), maafw/ is the official runtime with MaaNikki's patched control unit
(native/macos-control-unit/build.py --runtime-out), and python/ is a virtual environment with
requirements.txt. The repository's interface.json, which the Windows package copies unchanged,
is not touched. User data still goes to ~/Library/Application Support/MaaNikki.

Supervised device tests of tasks not yet on the verified allowlist can set
MAANIKKI_MACOS_EXTRA_TASKS=<entry>,<entry> before starting the client; only a debug client honours it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "build" / "macos" / "interface-overlay.json"
PATCHED_BUILD = ROOT / ".build" / "macos-control-unit" / "out" / "patched" / "build.json"
LINKED = ("agent", "ai", "resource", "tasks", "options", "docs", "licenses")
MARKER = ".maanikki-macos-dev-home"


def link(target: Path, source: Path) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        sys.exit(f"{target} exists and is not a link; remove it first")
    target.symlink_to(source, target_is_directory=source.is_dir())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", type=Path, default=ROOT / ".build" / "macos-dev-home")
    parser.add_argument("--python", type=Path, default=ROOT / ".venv",
                        help="virtual environment with requirements.txt installed")
    parser.add_argument("--runtime", type=Path, default=ROOT / ".build" / "macos-control-unit" / "runtime-patched",
                        help="runtime directory written by native/macos-control-unit/build.py --runtime-out")
    args = parser.parse_args()
    if sys.platform != "darwin":
        sys.exit("the macOS development home is for macOS")

    home, venv, runtime = args.home.resolve(), args.python.resolve(), args.runtime.resolve()
    if not (venv / "bin" / "python3").exists():
        sys.exit(f"{venv} is not a virtual environment; create it with Python 3.12 and requirements.txt")
    if not (runtime / "libMaaFramework.dylib").exists() or not (runtime / ".maanikki-control-unit-runtime").exists():
        sys.exit(f"{runtime} is not a runtime from native/macos-control-unit/build.py --runtime-out")
    # A stock runtime also carries the build.py marker; only the patched unit can drive the game.
    if not PATCHED_BUILD.exists():
        sys.exit(f"{PATCHED_BUILD} is missing; run native/macos-control-unit/build.py first")
    expected = json.loads(PATCHED_BUILD.read_text(encoding="utf-8"))["sha256"]
    actual = hashlib.sha256((runtime / "libMaaMacOSControlUnit.dylib").read_bytes()).hexdigest()
    if actual != expected:
        sys.exit(f"{runtime} does not hold the patched control unit ({actual[:12]} != {expected[:12]})")
    if home.exists() and any(home.iterdir()) and not (home / MARKER).exists():
        sys.exit(f"{home} exists and was not written by this script")
    home.mkdir(parents=True, exist_ok=True)
    (home / MARKER).write_text("written by scripts/macos_dev_home.py\n", encoding="utf-8")

    for name in LINKED:
        link(home / name, ROOT / name)
    link(home / "maafw", runtime)
    link(home / "python", venv)

    interface = json.loads((ROOT / "interface.json").read_text(encoding="utf-8"))
    overlay = json.loads(OVERLAY.read_text(encoding="utf-8"))
    agents = interface["agent"] if isinstance(interface["agent"], list) else [interface["agent"]]
    for agent in agents:
        agent.update(overlay["agent"])
    interface["controller"] = [*interface["controller"], *overlay["controller"]]
    target = home / "interface.json"
    if target.is_symlink():
        target.unlink()
    target.write_text(json.dumps(interface, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name in ("README.md", "CHANGELOG.md", "LICENSE"):
        if (ROOT / name).exists():
            link(home / name, ROOT / name)

    print(f"macOS development home: {home}")
    print(f"run the client with: MAANIKKI_DEV_HOME={home} pnpm --dir {ROOT / 'client'} tauri dev")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
