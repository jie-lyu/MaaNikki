"""Build libMaaMacOSControlUnit.dylib from the pinned MaaFramework tag plus patches/.

    python3 -I native/macos-control-unit/build.py [--stock] [--work DIR] [--runtime-out DIR]

Everything comes from upstream.lock.json: the MaaFramework tag and commit,
the MaaUtils submodule commit, and the MaaDeps prebuilt dependencies by
SHA-256. cmake and ninja are pip-installed into a venv under the work
directory, so nothing is installed system-wide. Needs the Xcode command line
tools (clang and the macOS SDK), git and network access on first run.

--stock builds the unpatched unit; comparing it with the official binary
proves the toolchain before the patches are judged. --runtime-out also
downloads the official runtime of the same tag, checks its hashes, and
writes a copy with only the control unit replaced, for probe.py and
development runs.

The result keeps the official factory signature and version string, so the
stock libMaaFramework.dylib loads it unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
LOCK = json.loads((HERE / "upstream.lock.json").read_text(encoding="utf-8"))
UNIT = "libMaaMacOSControlUnit.dylib"


def run(*command, cwd: Path | None = None) -> str:
    print("+", " ".join(str(part) for part in command), flush=True)
    return subprocess.run([str(part) for part in command], cwd=cwd, check=True,
                          text=True, stdout=subprocess.PIPE).stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(url: str, expected: str, target: Path) -> Path:
    if target.exists() and sha256(target) == expected:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    print(f"downloading {url}", flush=True)
    urllib.request.urlretrieve(url, partial)
    actual = sha256(partial)
    if actual != expected:
        partial.unlink()
        sys.exit(f"{target.name}: sha256 {actual}, expected {expected}")
    partial.replace(target)
    return target


def tools(work: Path) -> Path:
    venv = work / "tools-venv"
    cmake, ninja = LOCK["build"]["cmake"], LOCK["build"]["ninja"]
    marker = venv / f".tools-{cmake}-{ninja}"
    if not marker.exists():
        run(sys.executable, "-m", "venv", "--clear", venv)
        run(venv / "bin" / "python", "-m", "pip", "install", "--quiet", f"cmake=={cmake}", f"ninja=={ninja}")
        marker.touch()
    return venv / "bin"


def source(work: Path) -> Path:
    pin = LOCK["maafw"]
    src = work / "src"
    if not (src / ".git").exists():
        run("git", "clone", "--depth", "1", "--branch", pin["tag"], pin["repository"], src)
    head = run("git", "rev-parse", "HEAD", cwd=src)
    if head != pin["commit"]:
        sys.exit(f"{pin['tag']} resolves to {head}, expected {pin['commit']}; remove {src} if the clone is stale")
    # Drop earlier patch applications; MaaDeps under the submodule is untracked and kept.
    run("git", "reset", "--hard", "--quiet", pin["commit"], cwd=src)
    run("git", "clean", "-fdq", "--", "source/MaaMacOSControlUnit", "source/ControlUnitCommon", cwd=src)
    run("git", "submodule", "update", "--init", "--depth", "1", "source/MaaUtils", cwd=src)
    utils = run("git", "rev-parse", "HEAD", cwd=src / "source" / "MaaUtils")
    if utils != pin["maautils_commit"]:
        sys.exit(f"MaaUtils at {utils}, expected {pin['maautils_commit']}")
    return src


def maadeps(work: Path, src: Path) -> None:
    deps = LOCK["maadeps"]
    root = src / "source" / "MaaUtils" / "MaaDeps"
    marker = root / f".maanikki-{deps['sha256']}"
    if marker.exists():
        return
    archive = fetch(deps["url"], deps["sha256"], work / "downloads" / deps["name"])
    with tarfile.open(archive) as tar:
        tar.extractall(root, filter="data")
    if not (root / "vcpkg" / "installed" / LOCK["build"]["triplet"]).is_dir():
        sys.exit(f"{deps['name']} has no {LOCK['build']['triplet']} tree")
    marker.touch()


def patch(src: Path) -> list[str]:
    applied = []
    for patch_file in sorted((HERE / "patches").glob("*.patch")):
        run("git", "apply", "--check", patch_file, cwd=src)
        run("git", "apply", patch_file, cwd=src)
        applied.append(patch_file.name)
    return applied


def build(work: Path, src: Path, bin_dir: Path, variant: str) -> Path:
    build_dir = work / f"build-{variant}"
    pin = LOCK["build"]
    off = ["ADB", "IOS", "CUSTOM", "PLAYCOVER", "RECORD", "REPLAY", "GAMEPAD", "LINUX", "WIN32", "ANDROID_NATIVE"]
    run(bin_dir / "cmake", "-S", src, "-B", build_dir, "-G", "Ninja",
        f"-DCMAKE_MAKE_PROGRAM={bin_dir / 'ninja'}",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DMAADEPS_TRIPLET={pin['triplet']}",
        "-DCMAKE_OSX_SYSROOT=macosx",
        f"-DCMAKE_OSX_ARCHITECTURES={pin['architecture']}",
        f"-DCMAKE_OSX_DEPLOYMENT_TARGET={pin['deployment_target']}",
        # The core logs a warning when a unit reports another version; keep the tag.
        f"-DMAA_HASH_VERSION={LOCK['maafw']['tag']}",
        "-DWITH_MAA_AGENT=OFF", "-DENABLE_CCACHE=OFF",
        *[f"-DWITH_{name}_CONTROLLER=OFF" for name in off])
    run(bin_dir / "cmake", "--build", build_dir, "--target", "MaaMacOSControlUnit")
    return build_dir / "bin" / UNIT


def runtime_copy(work: Path, unit: Path, target: Path) -> None:
    stock = LOCK["stock_runtime"]
    archive = fetch(stock["url"], stock["sha256"], work / "downloads" / stock["name"])
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.namelist():
            if member.startswith("bin/") and not member.endswith("/"):
                destination = target / member.removeprefix("bin/")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(bundle.read(member))
                info = bundle.getinfo(member)
                if info.external_attr >> 16:
                    destination.chmod((info.external_attr >> 16) & 0o777)
    for name, expected in stock.items():
        if name.endswith(".dylib") and sha256(target / name) != expected:
            sys.exit(f"stock {name} does not match upstream.lock.json")
    shutil.copy2(unit, target / UNIT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stock", action="store_true", help="skip patches/ and build the upstream unit")
    parser.add_argument("--work", type=Path, default=REPO / ".build" / "macos-control-unit")
    parser.add_argument("--runtime-out", type=Path, help="write the stock runtime with this unit swapped in")
    args = parser.parse_args()
    if sys.platform != "darwin":
        sys.exit("the macOS control unit builds only on macOS")

    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    bin_dir = tools(work)
    src = source(work)
    maadeps(work, src)
    applied = [] if args.stock else patch(src)
    variant = "stock" if args.stock else "patched"
    unit = build(work, src, bin_dir, variant)

    out = work / "out" / variant
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(unit, out / UNIT)
    manifest = {"maafw": LOCK["maafw"]["tag"], "commit": LOCK["maafw"]["commit"],
                "patches": applied, "sha256": sha256(out / UNIT)}
    (out / "build.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if args.runtime_out:
        runtime_copy(work, out / UNIT, args.runtime_out.resolve())
        manifest["runtime"] = str(args.runtime_out.resolve())
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
