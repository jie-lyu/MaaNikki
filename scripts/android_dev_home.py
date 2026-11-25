"""Assemble a development home for running the client from source against the Android emulator.

    python3 -I scripts/android_dev_home.py [--home DIR] [--python VENV]
    MAANIKKI_DEV_HOME=<DIR> pnpm --dir client tauri dev

Like scripts/macos_dev_home.py, a debug client reads interface.json, resource/, tasks/, maafw/ and the agent from
MAANIKKI_DEV_HOME. Here the repository's directories are linked (resource_android/ too), maafw/ is the repository's
stock MaaFramework runtime (it has the ADB control unit; the native Mac game is not a target of this home), python/
is a virtual environment with requirements.txt, maafw/MaaAgentBinary holds the Maatouch helper (from the runtime
or the pip package), and interface.json is written by scripts/android_interface.py
(the nikki_android controller and the read-only AndroidCheck task). The repository's interface.json is not
touched. User data still goes to ~/Library/Application Support/MaaNikki.

Until the client wiring of phase B5b, the client neither applies the controller's input and capture methods nor
admits Android tasks; this home is for that work and for the device acceptance in B6.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # run with -I, which leaves this directory off sys.path
from android_interface import android_interface, render  # noqa: E402

LINKED = ("agent", "ai", "resource", "resource_android", "tasks", "options", "docs", "licenses")
MARKER = ".maanikki-android-dev-home"
# The client hands MaaFramework <maafw>/MaaAgentBinary (client/src-tauri/src/commands/maa_core.rs); the stock
# runtime has no such directory, the pip package ships it beside maa/.
MAATOUCH = Path("maatouch/universal/maatouch")
RUNTIME_MARKER = ".maanikki-android-dev-home-runtime"


def link(target: Path, source: Path) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        sys.exit(f"{target} exists and is not a link; remove it first")
    target.symlink_to(source, target_is_directory=source.is_dir())


def runtime_entries(runtime: Path, home: Path) -> dict:
    """Every entry of ``runtime`` (but MaaAgentBinary and a generated home's marker) resolved to its final file,
    read before anything is written: linking to resolved targets means a home built from another home's maafw/
    never points back through links that a later rebuild replaces."""
    entries = {}
    for entry in runtime.iterdir():
        if entry.name in ("MaaAgentBinary", RUNTIME_MARKER):
            continue
        resolved = entry.resolve()
        if not resolved.exists():
            sys.exit(f"{entry} does not resolve to a file")
        if resolved == home or home in resolved.parents:
            sys.exit(f"{entry} resolves into the home {home} being written")
        entries[entry.name] = resolved
    return entries


def runtime_dir(target: Path, entries: dict, agent_binary: Path) -> None:
    """``target`` as a directory of links to the resolved runtime entries and to MaaAgentBinary."""
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists() and not (target / RUNTIME_MARKER).exists():
        sys.exit(f"{target} exists and was not written by this script")
    target.mkdir(exist_ok=True)
    for entry in target.iterdir():
        if entry.is_symlink():
            entry.unlink()
    (target / RUNTIME_MARKER).write_text("written by scripts/android_dev_home.py\n", encoding="utf-8")
    for name, resolved in entries.items():
        (target / name).symlink_to(resolved, target_is_directory=resolved.is_dir())
    (target / "MaaAgentBinary").symlink_to(agent_binary, target_is_directory=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", type=Path, default=ROOT / ".build" / "android-dev-home")
    parser.add_argument("--python", type=Path, default=ROOT / ".venv",
                        help="virtual environment with requirements.txt installed")
    parser.add_argument("--runtime", type=Path, default=ROOT / "maafw", help="MaaFramework runtime with the ADB unit")
    parser.add_argument("--agent-binary", type=Path, default=None,
                        help="MaaAgentBinary directory with maatouch (default: the runtime's, else the venv's)")
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        sys.exit("the Android development home is for macOS hosts for now")
    home, venv, runtime = args.home.resolve(), args.python.resolve(), args.runtime.resolve()
    if not (venv / "bin" / "python3").exists():
        sys.exit(f"{venv} is not a virtual environment; create it with Python 3.12 and requirements.txt")
    if not (runtime / "libMaaFramework.dylib").exists() or not (runtime / "libMaaAdbControlUnit.dylib").exists():
        sys.exit(f"{runtime} is not a MaaFramework runtime with the ADB control unit")
    # Never read from what is about to be rewritten: a runtime inside the home (its own maafw/, or the home itself)
    # would lose its links before they are read again.
    for source, what in ((runtime, "--runtime"), (venv, "--python")):
        if source == home or home in source.parents:
            sys.exit(f"{what} {source} is inside the home {home} being written")
    agent_binary = args.agent_binary or next(
        (candidate for candidate in (runtime / "MaaAgentBinary",
                                     *sorted(venv.glob("lib/python3*/site-packages/MaaAgentBinary")))
         if (candidate / MAATOUCH).is_file()), None)
    if agent_binary is None or not (agent_binary.resolve() / MAATOUCH).is_file():
        sys.exit(f"no MaaAgentBinary with {MAATOUCH} (the Maatouch helper pushed to the device); pass --agent-binary")
    agent_binary = agent_binary.resolve()
    if agent_binary == home or home in agent_binary.parents:
        sys.exit(f"MaaAgentBinary {agent_binary} resolves into the home {home} being written")
    entries = runtime_entries(runtime, home)  # all reading done before the first write
    if home.exists() and any(home.iterdir()) and not (home / MARKER).exists():
        sys.exit(f"{home} exists and was not written by this script")
    home.mkdir(parents=True, exist_ok=True)
    (home / MARKER).write_text("written by scripts/android_dev_home.py\n", encoding="utf-8")
    for name in LINKED:
        link(home / name, ROOT / name)
    runtime_dir(home / "maafw", entries, agent_binary)
    link(home / "python", venv)
    target = home / "interface.json"
    if target.is_symlink():
        target.unlink()
    target.write_text(render(android_interface()), encoding="utf-8")
    for name in ("README.md", "CHANGELOG.md", "LICENSE"):
        if (ROOT / name).exists():
            link(home / name, ROOT / name)
    print(f"Android development home: {home}")
    print(f"run the client with: MAANIKKI_DEV_HOME={home} pnpm --dir {ROOT / 'client'} tauri dev")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
