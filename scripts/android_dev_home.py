"""Assemble a development home for running the client from source against the Android emulator.

    python3 -I scripts/android_dev_home.py [--home DIR] [--python VENV]
    MAANIKKI_DEV_HOME=<DIR> pnpm --dir client tauri dev

Like scripts/macos_dev_home.py, a debug client reads interface.json, resource/, tasks/, maafw/ and the agent from
MAANIKKI_DEV_HOME. Here the repository's directories are linked (resource_android/ too), maafw/ is the repository's
stock MaaFramework runtime (it has the ADB control unit; the native Mac game is not a target of this home), python/
is a virtual environment with requirements.txt, and interface.json is written by scripts/android_interface.py
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


def link(target: Path, source: Path) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        sys.exit(f"{target} exists and is not a link; remove it first")
    target.symlink_to(source, target_is_directory=source.is_dir())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", type=Path, default=ROOT / ".build" / "android-dev-home")
    parser.add_argument("--python", type=Path, default=ROOT / ".venv",
                        help="virtual environment with requirements.txt installed")
    parser.add_argument("--runtime", type=Path, default=ROOT / "maafw", help="MaaFramework runtime with the ADB unit")
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        sys.exit("the Android development home is for macOS hosts for now")
    home, venv, runtime = args.home.resolve(), args.python.resolve(), args.runtime.resolve()
    if not (venv / "bin" / "python3").exists():
        sys.exit(f"{venv} is not a virtual environment; create it with Python 3.12 and requirements.txt")
    if not (runtime / "libMaaFramework.dylib").exists() or not (runtime / "libMaaAdbControlUnit.dylib").exists():
        sys.exit(f"{runtime} is not a MaaFramework runtime with the ADB control unit")
    if home.exists() and any(home.iterdir()) and not (home / MARKER).exists():
        sys.exit(f"{home} exists and was not written by this script")
    home.mkdir(parents=True, exist_ok=True)
    (home / MARKER).write_text("written by scripts/android_dev_home.py\n", encoding="utf-8")
    for name in LINKED:
        link(home / name, ROOT / name)
    link(home / "maafw", runtime)
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
