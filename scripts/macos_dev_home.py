"""Assemble a macOS development home for running the client from source against the real game.

    python3 -I scripts/macos_dev_home.py [--home DIR] [--python VENV] [--runtime DIR]
    MAANIKKI_DEV_HOME=<DIR> pnpm --dir client tauri dev

A debug build of the client reads interface.json, resource/, tasks/, maafw/ and the agent from
MAANIKKI_DEV_HOME (client/src-tauri/src/commands/utils.rs). This script fills that directory the
way a macOS package will: the repository's resource directories are linked, interface.json is
written with the macOS agent command, maafw/ is the official runtime with MaaNikki's patched
control unit (native/macos-control-unit/build.py --runtime-out), and python/ is a virtual
environment with requirements.txt. The repository's interface.json keeps the Windows command, so
nothing here touches what Windows users get. User data still goes to
~/Library/Application Support/MaaNikki.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Agent command inside a macOS home or package: the embedded interpreter's layout.
MAC_AGENT_CHILD_EXEC = "./python/bin/python3"
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
    if home.exists() and any(home.iterdir()) and not (home / MARKER).exists():
        sys.exit(f"{home} exists and was not written by this script")
    home.mkdir(parents=True, exist_ok=True)
    (home / MARKER).write_text("written by scripts/macos_dev_home.py\n", encoding="utf-8")

    for name in LINKED:
        link(home / name, ROOT / name)
    link(home / "maafw", runtime)
    link(home / "python", venv)

    interface = json.loads((ROOT / "interface.json").read_text(encoding="utf-8"))
    agents = interface["agent"] if isinstance(interface["agent"], list) else [interface["agent"]]
    for agent in agents:
        agent["child_exec"] = MAC_AGENT_CHILD_EXEC
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
