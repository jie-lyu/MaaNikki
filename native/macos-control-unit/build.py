"""Build libMaaMacOSControlUnit.dylib from the pinned MaaFramework tag plus patches/.

    python3 -I native/macos-control-unit/build.py [--stock] [--work DIR] [--runtime-out DIR]
    python3 -I native/macos-control-unit/build.py --unit-only [--work DIR]
    python3 -I native/macos-control-unit/build.py --tests-only --test-arch x86_64 [--work DIR]

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

The shipped unit is always arm64. By default (Apple silicon) the patched build
also compiles and runs the fake-platform input tests natively. Intel CI runners
cannot execute arm64, so they split the work, and each step proves only what it
says:
  --unit-only        the arm64 unit cross-compiles, has the expected load commands
                     and architecture, and gets a manifest; no tests run.
  --tests-only --test-arch x86_64
                     the input logic tests pass on x86_64, compiled from the same
                     sources and patches in a separate source tree, build directory
                     and MaaDeps (maadeps_x64_tests in the lock). This is not the
                     shipped binary: its behaviour is only verified on Apple silicon.
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


def source(work: Path, name: str = "src") -> Path:
    pin = LOCK["maafw"]
    src = work / name
    if not (src / ".git").exists():
        run("git", "clone", "--depth", "1", "--branch", pin["tag"], pin["repository"], src)
    head = run("git", "rev-parse", "HEAD", cwd=src)
    if head != pin["commit"]:
        sys.exit(f"{pin['tag']} resolves to {head}, expected {pin['commit']}; remove {src} if the clone is stale")
    # Drop earlier patch applications; MaaDeps under the submodule is untracked and kept.
    run("git", "reset", "--hard", "--quiet", pin["commit"], cwd=src)
    run("git", "clean", "-fdq", "--", "source/MaaMacOSControlUnit", "source/ControlUnitCommon", "test/maanikki", cwd=src)
    run("git", "submodule", "update", "--init", "--depth", "1", "source/MaaUtils", cwd=src)
    utils = run("git", "rev-parse", "HEAD", cwd=src / "source" / "MaaUtils")
    if utils != pin["maautils_commit"]:
        sys.exit(f"MaaUtils at {utils}, expected {pin['maautils_commit']}")
    # A reused work directory must not carry edits to tracked upstream files.
    dirty = run("git", "status", "--porcelain", "--untracked-files=no", cwd=src / "source" / "MaaUtils")
    if dirty:
        sys.exit(f"MaaUtils has local changes; use a clean --work directory:\n{dirty}")
    return src


def maadeps(work: Path, src: Path, deps: dict | None = None, triplet: str | None = None) -> None:
    """Extract the locked MaaDeps archive once per source tree (MaaUtils expects it inside the tree).

    Reuse trusts the extracted tree; CI and release builds use a fresh --work directory.
    """
    deps = deps or LOCK["maadeps"]
    triplet = triplet or LOCK["build"]["triplet"]
    root = src / "source" / "MaaUtils" / "MaaDeps"
    marker = root / f".maanikki-{deps['sha256']}"
    if marker.exists():
        return
    archive = fetch(deps["url"], deps["sha256"], work / "downloads" / deps["name"])
    with tarfile.open(archive) as tar:
        tar.extractall(root, filter="data")
    if not (root / "vcpkg" / "installed" / triplet).is_dir():
        sys.exit(f"{deps['name']} has no {triplet} tree")
    marker.touch()


def patch(src: Path) -> list[str]:
    applied = []
    for patch_file in sorted((HERE / "patches").glob("*.patch")):
        run("git", "apply", "--check", patch_file, cwd=src)
        run("git", "apply", patch_file, cwd=src)
        applied.append(patch_file.name)
    return applied


def check_architecture(binary: Path, expected: str) -> None:
    """The binary must contain exactly the expected architecture (lipo -archs)."""
    archs = run("lipo", "-archs", binary).split()
    if archs != [expected]:
        sys.exit(f"{binary.name} is {archs}, expected [{expected!r}]")


def configure(src: Path, build_dir: Path, bin_dir: Path, *, triplet: str, architecture: str, tests: bool) -> None:
    pin = LOCK["build"]
    off = ["ADB", "IOS", "CUSTOM", "PLAYCOVER", "RECORD", "REPLAY", "GAMEPAD", "LINUX", "WIN32", "ANDROID_NATIVE"]
    run(bin_dir / "cmake", "-S", src, "-B", build_dir, "-G", "Ninja",
        f"-DCMAKE_MAKE_PROGRAM={bin_dir / 'ninja'}",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DMAADEPS_TRIPLET={triplet}",
        "-DCMAKE_OSX_SYSROOT=macosx",
        f"-DCMAKE_OSX_ARCHITECTURES={architecture}",
        f"-DCMAKE_OSX_DEPLOYMENT_TARGET={pin['deployment_target']}",
        # The core logs a warning when a unit reports another version; keep the tag.
        f"-DMAA_HASH_VERSION={LOCK['maafw']['tag']}",
        # Link with the install RPATHs (@loader_path, @executable_path) instead of absolute build-tree paths,
        # so a missing packaged dependency cannot resolve from this machine's build or MaaDeps directories.
        "-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON",
        "-DWITH_MAA_AGENT=OFF", "-DENABLE_CCACHE=OFF",
        # The patch's HybridInput tests: an extra executable, not part of the unit.
        f"-DMAANIKKI_UNIT_TESTS={'ON' if tests else 'OFF'}",
        *[f"-DWITH_{name}_CONTROLLER=OFF" for name in off])


def run_input_tests(bin_dir: Path, build_dir: Path, architecture: str) -> None:
    run(bin_dir / "cmake", "--build", build_dir, "--target", "MaaNikkiHybridInputTest")
    test = build_dir / "bin" / "MaaNikkiHybridInputTest"
    check_architecture(test, architecture)
    # Fake platform only: nothing is captured or sent. A failure stops the build.
    run(test)


def build(work: Path, src: Path, bin_dir: Path, variant: str, *, run_tests: bool = True) -> Path:
    """The shipped-architecture (arm64) unit; with run_tests, also the input tests, natively."""
    patched = variant == "patched"
    build_dir = work / f"build-{variant}"
    pin = LOCK["build"]
    configure(src, build_dir, bin_dir, triplet=pin["triplet"], architecture=pin["architecture"], tests=patched)
    run(bin_dir / "cmake", "--build", build_dir, "--target", "MaaMacOSControlUnit")
    if patched and run_tests:
        run_input_tests(bin_dir, build_dir, pin["architecture"])
    unit = build_dir / "bin" / UNIT
    check_load_commands(unit)
    check_architecture(unit, pin["architecture"])
    return unit


def build_tests_for(work: Path, bin_dir: Path, architecture: str) -> None:
    """The input tests for another architecture, from the same sources and patches, in their own
    source tree (with that architecture's MaaDeps) and build directory. Never produces a unit."""
    deps = LOCK["maadeps_x64_tests"]
    if architecture != deps["architecture"]:
        sys.exit(f"no MaaDeps pinned for {architecture} tests")
    src = source(work, f"src-tests-{architecture}")
    maadeps(work, src, deps, deps["triplet"])
    patch(src)
    build_dir = work / f"build-tests-{architecture}"
    configure(src, build_dir, bin_dir, triplet=deps["triplet"], architecture=architecture, tests=True)
    run_input_tests(bin_dir, build_dir, architecture)


def check_load_commands(unit: Path) -> None:
    """The unit must load like the official one: same RPATHs, dependencies only via @rpath or the system."""
    commands = run("otool", "-l", unit).splitlines()
    rpaths = [commands[i + 2].split()[1] for i, line in enumerate(commands) if line.strip() == "cmd LC_RPATH"]
    if rpaths != ["@loader_path", "@executable_path"]:
        sys.exit(f"unexpected LC_RPATH entries {rpaths}")
    for line in run("otool", "-L", unit).splitlines()[1:]:
        dependency = line.split()[0]
        if not dependency.startswith(("@rpath/", "/System/Library/", "/usr/lib/")):
            sys.exit(f"{UNIT} links {dependency}, which a tester's Mac will not have")


RUNTIME_MARKER = ".maanikki-control-unit-runtime"


def runtime_copy(work: Path, unit: Path, target: Path) -> None:
    stock = LOCK["stock_runtime"]
    archive = fetch(stock["url"], stock["sha256"], work / "downloads" / stock["name"])
    if target.exists():
        # Only ever replace a directory this tool wrote; never an arbitrary path such as the checkout.
        if any(target.iterdir()) and not (target / RUNTIME_MARKER).is_file():
            sys.exit(f"{target} exists and was not written by build.py; choose a new or empty directory")
        shutil.rmtree(target)
    target.mkdir(parents=True)
    (target / RUNTIME_MARKER).write_text("written by native/macos-control-unit/build.py\n", encoding="utf-8")
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
    parser.add_argument("--unit-only", action="store_true",
                        help="build and check the arm64 unit without running the input tests (Intel CI)")
    parser.add_argument("--tests-only", action="store_true",
                        help="build and run only the input tests, for --test-arch, from the same sources (Intel CI)")
    parser.add_argument("--test-arch", choices=["x86_64"], help="architecture of --tests-only")
    args = parser.parse_args()
    if sys.platform != "darwin":
        sys.exit("the macOS control unit builds only on macOS")
    if args.tests_only and (args.unit_only or args.stock or args.runtime_out or not args.test_arch):
        sys.exit("--tests-only takes --test-arch and nothing that builds a unit")
    if args.test_arch and not args.tests_only:
        sys.exit("--test-arch only applies to --tests-only")
    if args.unit_only and args.stock:
        sys.exit("--unit-only builds the patched unit")

    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    bin_dir = tools(work)
    if args.tests_only:
        build_tests_for(work, bin_dir, args.test_arch)
        print(f"input logic tests passed on {args.test_arch} from the same sources (not the shipped arm64 binary)")
        return 0
    src = source(work)
    maadeps(work, src)
    applied = [] if args.stock else patch(src)
    variant = "stock" if args.stock else "patched"
    unit = build(work, src, bin_dir, variant, run_tests=not args.unit_only)

    out = work / "out" / variant
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(unit, out / UNIT)
    manifest = {"maafw": LOCK["maafw"]["tag"], "commit": LOCK["maafw"]["commit"],
                "patches": applied, "sha256": sha256(out / UNIT), "architecture": LOCK["build"]["architecture"],
                # Whether this build also ran the input tests natively on the shipped architecture.
                "input_tests": ("skipped (--unit-only)" if args.unit_only
                                else "not built (stock)" if args.stock
                                else "passed on " + LOCK["build"]["architecture"])}
    (out / "build.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if args.runtime_out:
        runtime_copy(work, out / UNIT, args.runtime_out.resolve())
        manifest["runtime"] = str(args.runtime_out.resolve())
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
