"""Assemble, sign and check the macOS (Apple silicon) test package. Never publishes anything.

    python3 -I scripts/macos_package.py client [--allow-tool-drift]
    python3 -I scripts/macos_package.py package --runtime DIR --unit-manifest FILE --commit SHA --output DIR
    python3 -I scripts/macos_package.py verify --dmg FILE --scratch DIR      (Apple silicon only)

client    builds the Tauri client for aarch64-apple-darwin as an .app (`tauri build --bundles app`).
          On an Intel Mac this cross-compiles. Node and pnpm must match .node-version and
          client/package.json unless --allow-tool-drift is given (local builds only).
package   copies that .app and fills Contents/Resources with what the Windows package has beside
          MaaNikki.exe: interface.json (macOS overlays, test-package overlay with the commit),
          agent/ ai/ resource/ tasks/ options/, documents and licenses, maafw/ (the runtime written by
          native/macos-control-unit/build.py --runtime-out: official MaaFramework with the patched
          control unit) and python/ (python-build-standalone with the locked arm64 wheels, from
          build/macos/runtime.lock.json). Bytecode is compiled ahead of time (unchecked-hash) so the
          embedded Python never writes into the signed bundle. Every Mach-O file must be arm64 only and
          load nothing outside the bundle and the system; each is signed ad hoc, then the bundle. The
          DMG (with an Applications link and the tester notes) and a SHA256SUMS file go to --output.
          Runs on any Mac: nothing here executes arm64 code unless the host is Apple silicon.
verify    Apple silicon only: copies the app out of the DMG like a tester would, runs the embedded
          Python (pinned versions, MaaFramework resource loading, agent import and registration)
          with a scratch data root, and checks afterwards that the bundle's signature is still intact,
          i.e. that nothing was written into it.

The user's data folder (~/Library/Application Support/MaaNikki) is never touched. Ad-hoc signing
gives no Developer ID: testers clear the quarantine or use "Open Anyway" (docs/macos-test-build.md).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
# Run with -I, which leaves this directory off sys.path.
sys.path.insert(0, str(ROOT / "scripts"))
import macos_interface  # noqa: E402
import release  # noqa: E402  (helpers only; nothing in release.py runs on import)

LOCK = ROOT / "build" / "macos" / "runtime.lock.json"
UPSTREAM_LOCK = ROOT / "native" / "macos-control-unit" / "upstream.lock.json"
TESTER_NOTES = ROOT / "docs" / "macos-test-build.md"
TARGET = "aarch64-apple-darwin"
CLIENT_APP = ROOT / "client" / "src-tauri" / "target" / TARGET / "release" / "bundle" / "macos" / "MaaNikki.app"
APP_NAME = "MaaNikki.app"
UNIT = "libMaaMacOSControlUnit.dylib"
RUNTIME_MARKER = ".maanikki-control-unit-runtime"
MINIMUM_MACOS = "14.0"
# Mach-O and universal headers as they appear in the first four bytes of a file.
MACHO_MAGICS = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}
ALLOWED_LINK_PREFIXES = ("@rpath/", "@loader_path/", "@executable_path/", "/System/Library/", "/usr/lib/")
# python-build-standalone extras the agent never runs: console scripts with build-machine shebangs,
# and pip (nothing is installed at run time).
PYTHON_REMOVED = ("bin/2to3", "bin/2to3-3.12", "bin/idle3", "bin/idle3.12", "bin/pip", "bin/pip3",
                  "bin/pip3.12", "bin/pydoc3", "bin/pydoc3.12", "bin/python3-config", "bin/python3.12-config")


def run(*command, cwd: Path | None = None, env: dict | None = None, capture: bool = False) -> str:
    shown = (str(part) if len(str(part)) <= 160 else str(part)[:157] + "..." for part in command)
    print("+", " ".join(shown), flush=True)
    result = subprocess.run([str(part) for part in command], cwd=cwd, env=env, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return (result.stdout or "").strip()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def is_apple_silicon() -> bool:
    return platform.machine() == "arm64"


# ---------------------------------------------------------------- client


def pinned_pnpm() -> Path:
    """The pnpm version of client/package.json, installed under .build/build-tools like build.ps1 does."""
    match = re.fullmatch(r"pnpm@(\d+\.\d+\.\d+)(?:\+sha512\.[0-9a-f]+)?",
                         read_json(ROOT / "client" / "package.json")["packageManager"])
    if not match:
        sys.exit("client/package.json must pin an exact pnpm version in packageManager")
    tools = ROOT / ".build" / "build-tools"
    pnpm = tools / "node_modules" / ".bin" / "pnpm"
    current = run(pnpm, "--version", capture=True) if pnpm.exists() else ""
    if current != match[1]:
        run("npm", "install", "--prefix", tools, "--cache", ROOT / ".build" / "npm-cache", "--no-save",
            "--no-package-lock", "--ignore-scripts", "--no-audit", "--no-fund", f"pnpm@{match[1]}")
        if run(pnpm, "--version", capture=True) != match[1]:
            sys.exit(f"could not install pnpm {match[1]} under {tools}")
    return pnpm


def build_client(allow_tool_drift: bool) -> Path:
    expected_node = "v" + (ROOT / ".node-version").read_text(encoding="utf-8").strip()
    actual_node = run("node", "--version", capture=True)
    if actual_node != expected_node:
        if not allow_tool_drift:
            sys.exit(f"use Node.js {expected_node} for this build (found {actual_node})")
        print(f"warning: building with Node.js {actual_node}; CI uses {expected_node}", flush=True)
    if TARGET not in run("rustup", "target", "list", "--installed", cwd=ROOT / "client", capture=True).split():
        sys.exit(f"the Rust target {TARGET} is not installed: rustup target add {TARGET}")
    pnpm = pinned_pnpm()
    # Tauri's beforeBuildCommand runs `pnpm build` and must find the same pnpm.
    env = dict(os.environ, PATH=f"{pnpm.parent}{os.pathsep}{os.environ['PATH']}")
    run(pnpm, "install", "--frozen-lockfile", cwd=ROOT / "client", env=env)
    # Bundle only the .app; signing, resources and the DMG are done by `package`.
    run(pnpm, "exec", "tauri", "build", "--target", TARGET, "--bundles", "app", "--", "--locked",
        cwd=ROOT / "client", env=env)
    if not (CLIENT_APP / "Contents" / "Info.plist").is_file():
        sys.exit(f"the client build did not produce {CLIENT_APP}")
    print(f"client app: {CLIENT_APP}", flush=True)
    return CLIENT_APP


# ---------------------------------------------------------------- package


def sha256(path: Path) -> str:
    return release.sha256(path)


def download(artifact: dict) -> Path:
    """Locked download into .build/downloads, verified by SHA-256 (release.py's helper)."""
    return release.download(artifact)


def safe_tar_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive) as tar:
        # "data" rejects absolute paths, links leaving the destination and device files.
        tar.extractall(destination, filter="data")


def extract_wheel(archive: Path, site_packages: Path) -> None:
    with zipfile.ZipFile(archive) as wheel:
        for member in wheel.infolist():
            # The MaaFramework binaries come from maafw/ (MAAFW_BINARY_PATH), as on Windows.
            if member.filename.startswith("maa/bin/"):
                continue
            parts = PurePosixPath(member.filename).parts
            if not parts:
                continue
            if parts[0].endswith(".data"):
                if len(parts) < 3 or parts[1] not in ("purelib", "platlib"):
                    continue  # console entry points are not used
                relative = "/".join(parts[2:])
            else:
                relative = member.filename
            release.extract_member(wheel, member, site_packages, relative)


def check_runtime(runtime: Path, unit_manifest: dict) -> None:
    """The maafw runtime must be build.py's copy of the locked official runtime with this unit."""
    if not (runtime / RUNTIME_MARKER).is_file():
        sys.exit(f"{runtime} is not a runtime written by native/macos-control-unit/build.py --runtime-out")
    stock = read_json(UPSTREAM_LOCK)["stock_runtime"]
    for name, expected in stock.items():
        if name.endswith(".dylib") and name != UNIT and sha256(runtime / name) != expected:
            sys.exit(f"{runtime / name} does not match the official runtime in upstream.lock.json")
    patches = sorted((ROOT / "native" / "macos-control-unit" / "patches").glob("*.patch"))
    expected = {path.name: sha256(path) for path in patches}
    if unit_manifest.get("patches") != list(expected) or unit_manifest.get("patch_sha256") != expected:
        sys.exit(f"the unit was built from other patches than the tree's {list(expected)} (names or contents "
                 "differ, or the manifest predates patch hashes); rebuild it with native/macos-control-unit/build.py")
    if sha256(runtime / UNIT) != unit_manifest["sha256"]:
        sys.exit(f"{runtime / UNIT} is not the unit described by the manifest")
    if unit_manifest.get("architecture") != "arm64":
        sys.exit(f"the unit manifest says {unit_manifest.get('architecture')}, expected arm64")


def copy_tree(source: Path, target: Path) -> None:
    def ignored(directory: str, names: list[str]) -> list[str]:
        return [name for name in names if name == "__pycache__" or name.endswith((".pyc", ".pyo"))
                or name == ".DS_Store" or release.is_local_helper(Path(directory) / name)]
    shutil.copytree(source, target, ignore=ignored, symlinks=False)


def assemble_resources(resources: Path, runtime: Path, unit_manifest: dict, commit: str) -> None:
    lock = read_json(LOCK)
    resources.mkdir(parents=True, exist_ok=True)

    # python/: relocatable CPython with the locked wheels.
    safe_tar_extract(download(lock["python"]), resources)
    python = resources / "python"
    for relative in PYTHON_REMOVED:
        target = python / relative
        if target.is_symlink() or target.exists():
            target.unlink()
    py_tag = ".".join(lock["python_version"].split(".")[:2])
    site_packages = python / "lib" / f"python{py_tag}" / "site-packages"
    for entry in list(site_packages.iterdir()):
        if entry.name.lower().startswith("pip"):
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    for wheel in lock["wheels"]:
        extract_wheel(download(wheel), site_packages)

    # maafw/: the official runtime with the patched control unit.
    shutil.copytree(runtime, resources / "maafw", symlinks=True, ignore=shutil.ignore_patterns(RUNTIME_MARKER))
    (resources / "maafw" / "control-unit.json").write_text(json.dumps(unit_manifest, indent=2) + "\n",
                                                           encoding="utf-8")

    # The same program content as the Windows package, including agent/host/macos.
    for directory in release.RUNTIME_DIRS:
        copy_tree(ROOT / directory, resources / directory)
    copy_tree(ROOT / "docs" / "images", resources / "docs" / "images")
    if (ROOT / "licenses").is_dir():
        shutil.copytree(ROOT / "licenses", resources / "licenses", dirs_exist_ok=True)
    for name in release.PACKAGED_DOCS:
        if (ROOT / name).is_file():
            (resources / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, resources / name)
    if TESTER_NOTES.is_file():
        shutil.copy2(TESTER_NOTES, resources / "docs" / TESTER_NOTES.name)
    release.collect_dependency_licenses(resources)

    (resources / "interface.json").write_text(
        macos_interface.render(macos_interface.macos_interface(test_build_commit=commit)), encoding="utf-8")
    manifest = {"schema_version": 1, "kind": "macos-arm64-test", "commit": commit,
                "version": read_json(ROOT / "interface.json")["version"],
                "runtime": lock, "control_unit": unit_manifest}
    (resources / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                                                     encoding="utf-8")


def compile_bytecode(resources: Path) -> None:
    """Write .pyc next to every source so the embedded Python never writes into the bundle.

    unchecked-hash pycs are used without looking at the source's timestamp, which copying, DMG
    images and signing do not preserve reliably. The compiling interpreter must be the same
    minor version as the embedded one: the embedded Python itself on Apple silicon, otherwise
    this interpreter (CI runs it under Python 3.12).
    """
    lock = read_json(LOCK)
    minor = tuple(int(part) for part in lock["python_version"].split(".")[:2])
    if is_apple_silicon():
        interpreter = resources / "python" / "bin" / "python3"
    elif sys.version_info[:2] == minor:
        interpreter = Path(sys.executable)
    else:
        sys.exit(f"compiling bytecode for Python {minor[0]}.{minor[1]} needs that interpreter on this "
                 f"Intel host (running {sys.version_info[0]}.{sys.version_info[1]})")
    py_tag = f"{minor[0]}.{minor[1]}"
    targets = [resources / "python" / "lib" / f"python{py_tag}", *(resources / d for d in release.RUNTIME_DIRS)]
    # -I: ignore PYTHON* variables and the user's site-packages; -j0: all cores.
    # Some test and template sources in the standard library do not compile; compileall reports
    # them and they are never imported. Only files that end up without a .pyc are checked below.
    subprocess.run([str(interpreter), "-I", "-m", "compileall", "-q", "-j0", "--invalidation-mode",
                    "unchecked-hash", *map(str, targets)], check=False)
    tag = f"cpython-{minor[0]}{minor[1]}"
    missing = []
    for target in targets[1:]:
        for source in target.rglob("*.py"):
            if not (source.parent / "__pycache__" / f"{source.stem}.{tag}.pyc").is_file():
                missing.append(str(source.relative_to(resources)))
    if missing:
        sys.exit("program files without bytecode: " + ", ".join(missing[:20]))


def macho_files(app: Path) -> list[Path]:
    found = []
    for path in sorted(app.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) in MACHO_MAGICS:
                found.append(path)
    return found


def macho_output(*command) -> list[str]:
    return subprocess.run([str(part) for part in command], check=True, text=True,
                          stdout=subprocess.PIPE).stdout.splitlines()


def install_names(binaries: list[Path]) -> set[str]:
    """LC_ID_DYLIB of every library in the bundle."""
    return {line.strip() for binary in binaries
            for line in macho_output("otool", "-arch", "arm64", "-D", binary)[1:]}


def check_macho(binary: Path, app: Path, universal: list[str], bundle_ids: set[str],
                by_install_name: list[str]) -> list[str]:
    """The arm64 slice must exist and load nothing outside the bundle and the system.

    Universal files (opencv-python ships one) are accepted and listed, not thinned: thinning would
    rewrite a file that already carries a valid signature.
    """
    problems = []
    relative = str(binary.relative_to(app))
    archs = macho_output("lipo", "-archs", binary)[0].split()
    if "arm64" not in archs:
        return [f"{relative}: architectures {archs}, no arm64"]
    if archs != ["arm64"]:
        universal.append(f"{relative} ({' '.join(archs)})")
    # otool -D prints the file's own install name (a dylib's LC_ID_DYLIB), which -L lists first.
    own_id = {line.strip() for line in macho_output("otool", "-arch", "arm64", "-D", binary)[1:]}
    dependencies = [line.split()[0] for line in macho_output("otool", "-arch", "arm64", "-L", binary)[1:]]
    dependencies = [dependency for dependency in dependencies if dependency not in own_id]
    resources = app / "Contents" / "Resources"
    # Who loads the file decides @executable_path: the embedded Python for python/, the client otherwise.
    executable_dir = (resources / "python" / "bin" if binary.is_relative_to(resources / "python")
                      else app / "Contents" / "MacOS")
    for dependency in dependencies:
        if not dependency.startswith(ALLOWED_LINK_PREFIXES):
            problems.append(f"{relative}: links {dependency}, which a tester's Mac will not have")
            continue
        for prefix, base in (("@loader_path/", binary.parent), ("@executable_path/", executable_dir)):
            if dependency.startswith(prefix):
                target = base / dependency.removeprefix(prefix)
                if not (target.is_file() and target.resolve().is_relative_to(app.resolve())):
                    problems.append(f"{relative}: {dependency} is missing from the bundle")
    load_commands = macho_output("otool", "-arch", "arm64", "-l", binary)
    rpaths = [load_commands[i + 2].split()[1] for i, line in enumerate(load_commands)
              if line.strip() == "cmd LC_RPATH"]
    for dependency in (d for d in dependencies if d.startswith("@rpath/")):
        # Some wheels keep their build machines' absolute rpaths. They are harmless only while
        # nothing is looked up through them: an @rpath lookup could pick a same-named library from,
        # say, a tester's /usr/local/lib before the bundled one.
        if any(not rpath.startswith("@") for rpath in rpaths):
            problems.append(f"{relative}: {dependency} is looked up through absolute rpaths {rpaths}")
            continue
        name = dependency.removeprefix("@rpath/")
        found = [binary.parent / rpath.removeprefix("@loader_path").lstrip("/") / name
                 for rpath in rpaths if rpath.startswith("@loader_path")]
        if any(path.is_file() and path.resolve().is_relative_to(app.resolve()) for path in found):
            continue
        if dependency in bundle_ids:
            # MaaFramework's plugins (maafw/plugins/, the official runtime's demo plugin, also in the
            # Windows package) are loaded by libMaaFramework after it and libMaaUtils, and dyld uses
            # the already-loaded image with that install name; every device run so far loaded it so.
            by_install_name.append(f"{relative} -> {dependency}")
            continue
        problems.append(f"{relative}: {dependency} does not resolve inside the bundle via {rpaths}")
    return problems


def set_info_plist(app: Path, commit: str) -> None:
    path = app / "Contents" / "Info.plist"
    with path.open("rb") as stream:
        info = plistlib.load(stream)
    info["LSMinimumSystemVersion"] = MINIMUM_MACOS
    # Visible in Finder's Get Info / `defaults read`; the window title also names the commit.
    info["MaaNikkiBuild"] = f"macOS arm64 test build {commit}"
    with path.open("wb") as stream:
        plistlib.dump(info, stream)


def validly_signed(binary: Path) -> bool:
    return subprocess.run(["codesign", "--verify", "--strict", str(binary)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def sign(app: Path, binaries: list[Path]) -> list[Path]:
    """Sign ad hoc what has no valid signature yet, then the bundle; returns the files signed here.

    Files that already carry a valid signature (arm64 linkers sign ad hoc, so the official
    MaaFramework libraries, the patched control unit, python-build-standalone and the wheels
    normally do) are left byte for byte as they are: their SHA-256 stays the one in the locks,
    which the control unit's lifecycle probe checks. The bundle signature seals them as they are.
    """
    # Extended attributes (quarantine, Finder info) make codesign refuse to sign.
    run("xattr", "-cr", app)
    # Archives (the official runtime zip among them) carry group- and world-writable modes; an
    # installed app must not let other users replace its libraries.
    run("chmod", "-R", "go-w", app)
    main = app / "Contents" / "MacOS"
    unsigned = [path for path in binaries if path.parent != main and not validly_signed(path)]
    for start in range(0, len(unsigned), 200):
        run("codesign", "--force", "--sign", "-", "--timestamp=none", *unsigned[start:start + 200])
    # No hardened runtime: its library validation would refuse the other ad-hoc signed libraries.
    run("codesign", "--force", "--sign", "-", "--timestamp=none", app)
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", app)
    return unsigned


def check_shipped_runtime(resources: Path, unit_manifest: dict) -> None:
    """After signing: the shipped MaaFramework files are still the locked ones, byte for byte."""
    stock = read_json(UPSTREAM_LOCK)["stock_runtime"]
    for name, expected in stock.items():
        if name.endswith(".dylib") and name != UNIT and sha256(resources / "maafw" / name) != expected:
            sys.exit(f"maafw/{name} changed while packaging")
    if sha256(resources / "maafw" / UNIT) != unit_manifest["sha256"]:
        sys.exit(f"maafw/{UNIT} changed while packaging; the lifecycle probe would test another file")


def make_dmg(stage: Path, app: Path, dmg: Path, volume: str) -> None:
    content = stage / "dmg"
    content.mkdir()
    run("ditto", app, content / APP_NAME)
    (content / "Applications").symlink_to("/Applications")
    if TESTER_NOTES.is_file():
        shutil.copy2(TESTER_NOTES, content / "使用说明.md")
    run("hdiutil", "create", "-volname", volume, "-srcfolder", content, "-fs", "HFS+", "-format", "UDZO",
        "-ov", dmg)
    run("hdiutil", "verify", dmg)


def check_dmg(dmg: Path, commit: str) -> None:
    """Mount read-only and check the app inside: signature, resources root, test interface."""
    with tempfile.TemporaryDirectory(prefix="maanikki-dmg-") as mount:
        run("hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", mount, dmg)
        try:
            app = Path(mount) / APP_NAME
            run("codesign", "--verify", "--deep", "--strict", "--verbose=2", app)
            interface = read_json(app / "Contents" / "Resources" / "interface.json")
            if commit not in interface.get("title", ""):
                sys.exit("the packaged interface.json does not name the commit")
        finally:
            run("hdiutil", "detach", mount)


def package(runtime: Path, unit_manifest_path: Path, commit: str, output: Path, client_app: Path) -> None:
    if sys.platform != "darwin":
        sys.exit("the macOS package is assembled on macOS")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        sys.exit("--commit must be the full 40-character commit hash")
    if not (client_app / "Contents" / "Info.plist").is_file():
        sys.exit(f"{client_app} is not an app; run `{Path(__file__).name} client` first")
    output = release.build_output(output)
    unit_manifest = read_json(unit_manifest_path)
    runtime = runtime.resolve()
    check_runtime(runtime, unit_manifest)
    short = commit[:7]
    version = read_json(ROOT / "interface.json")["version"]

    stage = Path(tempfile.mkdtemp(prefix="maanikki-macos-", dir=ROOT / ".build"))
    app = stage / APP_NAME
    run("ditto", client_app, app)
    executables = list((app / "Contents" / "MacOS").iterdir())
    if len(executables) != 1:
        sys.exit(f"expected one executable in Contents/MacOS, found {[p.name for p in executables]}")
    set_info_plist(app, commit)
    resources = app / "Contents" / "Resources"
    assemble_resources(resources, runtime, unit_manifest, short)
    compile_bytecode(resources)

    binaries = macho_files(app)
    universal: list[str] = []
    by_install_name: list[str] = []
    bundle_ids = install_names(binaries)
    problems = [problem for binary in binaries
                for problem in check_macho(binary, app, universal, bundle_ids, by_install_name)]
    if problems:
        sys.exit("Mach-O checks failed:\n" + "\n".join(problems))
    stray = [path for path in (app / "Contents" / "MacOS").iterdir() if path not in binaries]
    if stray:
        sys.exit(f"non-code files in Contents/MacOS: {stray}")
    signed_here = sign(app, binaries)
    check_shipped_runtime(resources, unit_manifest)

    base = f"MaaNikki-v{version}-macos-arm64-test-{short}"
    dmg = output / f"{base}.dmg"
    make_dmg(stage, app, dmg, f"MaaNikki Mac Test {short}")
    check_dmg(dmg, short)
    unit_sha = unit_manifest["sha256"]
    sums = output / f"{base}-SHA256SUMS.txt"
    sums.write_text(f"{sha256(dmg)}  {dmg.name}\n", encoding="utf-8")
    shutil.rmtree(stage)
    print(f"test package: {dmg}\nchecksums: {sums}\ncontrol unit sha256 (for the lifecycle probe): {unit_sha}\n"
          f"universal (arm64 slice used): {universal}\n"
          f"@rpath resolved by an already loaded library: {by_install_name}\n"
          f"Mach-O files: {len(binaries)}, signed here because they had no valid signature: "
          f"{[str(path.relative_to(app)) for path in signed_here]}", flush=True)


# ---------------------------------------------------------------- verify


VERIFY_RUNTIME = """
import importlib.metadata, json, os, pathlib, sys
import cv2, numpy, scipy, strenum
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker
root = pathlib.Path.cwd().resolve()
expected = json.loads(sys.argv[1])
assert '.'.join(map(str, sys.version_info[:3])) == expected['python_version'], sys.version
assert pathlib.Path(sys.prefix).resolve().is_relative_to(root), sys.prefix
for entry in sys.path:
    if entry and pathlib.Path(entry).exists():
        assert pathlib.Path(entry).resolve().is_relative_to(root), ('sys.path leaves the bundle', entry)
for package in expected['wheels']:
    actual = importlib.metadata.version(package['package']).lstrip('v')
    assert actual == package['version'], (package['package'], actual, package['version'])
assert Library.version().lstrip('v') == expected['maafw_version'], Library.version()
Tasker.set_log_dir(os.environ['MAANIKKI_DATA_ROOT'] + '/debug')
resource = Resource()
resource.use_cpu()
job = resource.post_bundle(root / 'resource').wait()
assert job.succeeded and resource.loaded, 'MaaFramework resource loading failed'
print('embedded runtime verified: pinned versions, sys.path inside the bundle, resource loading')
"""


def verify(dmg: Path, scratch: Path) -> None:
    if sys.platform != "darwin" or not is_apple_silicon():
        sys.exit("verify runs the arm64 package and needs Apple silicon")
    scratch = scratch.resolve()
    if scratch.exists() and any(scratch.iterdir()):
        sys.exit(f"{scratch} is not empty; choose a new scratch directory")
    scratch.mkdir(parents=True, exist_ok=True)
    data_root = scratch / "data"
    data_root.mkdir()
    with tempfile.TemporaryDirectory(prefix="maanikki-dmg-") as mount:
        run("hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", mount, dmg)
        try:
            run("ditto", Path(mount) / APP_NAME, scratch / APP_NAME)
        finally:
            run("hdiutil", "detach", mount)
    app = scratch / APP_NAME
    resources = app / "Contents" / "Resources"
    python = resources / "python" / "bin" / "python3"
    lock = read_json(LOCK)
    # What the client gives the agent (maa_agent.rs, utils.rs apply_agent_root_env); HOME points at
    # the scratch folder so nothing can reach the user's Application Support folder.
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(scratch), "PYTHONUTF8": "1",
           "PYTHONIOENCODING": "utf-8", "MAAFW_BINARY_PATH": str(resources / "maafw"),
           "MAANIKKI_RESOURCE_ROOT": str(resources), "MAANIKKI_DATA_ROOT": str(data_root)}
    # No -B: if anything tried to write bytecode into the bundle, the signature check below fails.
    # The client's -B (child_args) is a second line of defence, not what this check relies on.
    # -I and UTF-8 mode as the client runs it (packaged interface.json child_args); no -B here.
    run(python, "-I", "-X", "utf8", "-c", VERIFY_RUNTIME, json.dumps(lock), cwd=resources, env=env)
    run(python, "-I", "-X", "utf8", "-c", "import runpy; runpy.run_path('agent/main.py', run_name='maanikki_import_check'); "
        "print('agent imports and action/recognition registrations verified')", cwd=resources, env=env)
    # Once more the way the client starts the agent (the packaged child_args flags), in a hostile
    # environment: PYTHONOPTIMIZE would make Python write new .opt pycs into the bundle, PYTHONPATH and
    # a user site would shadow the locked wheels. The flags must neutralise all three.
    agent = read_json(resources / "interface.json")["agent"]
    flags = agent["child_args"][:agent["child_args"].index("./agent/main.py")]
    shadow = scratch / "shadow"
    (shadow / "numpy").mkdir(parents=True)
    (shadow / "numpy" / "__init__.py").write_text("raise ImportError('shadowed numpy was imported')\n", encoding="utf-8")
    hostile = dict(env, PYTHONOPTIMIZE="1", PYTHONPATH=str(shadow), PYTHONUSERBASE=str(shadow),
                   PYTHONDONTWRITEBYTECODE="")
    run(python, *flags, "-c", "import runpy, numpy; runpy.run_path('agent/main.py', run_name='maanikki_import_check'); "
        "print('agent imports with the client flags in a hostile environment')", cwd=resources, env=hostile)
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", app)
    print(f"verified {dmg.name}: the bundle is unchanged after running the embedded Python", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    client = sub.add_parser("client", help="build the Tauri client .app for aarch64-apple-darwin")
    client.add_argument("--allow-tool-drift", action="store_true",
                        help="accept node/pnpm versions other than the pins (local builds only)")
    pack = sub.add_parser("package", help="assemble, sign and check the test package")
    pack.add_argument("--runtime", type=Path, required=True,
                      help="directory written by native/macos-control-unit/build.py --runtime-out")
    pack.add_argument("--unit-manifest", type=Path, required=True,
                      help="build.json of the patched unit (build.py's out/patched/build.json)")
    pack.add_argument("--commit", required=True, help="full commit hash the package is built from")
    pack.add_argument("--output", type=Path, required=True, help="new directory under .build/ or artifacts/")
    pack.add_argument("--client-app", type=Path, default=CLIENT_APP)
    check = sub.add_parser("verify", help="run the packaged runtime from a copy of the DMG (Apple silicon)")
    check.add_argument("--dmg", type=Path, required=True)
    check.add_argument("--scratch", type=Path, required=True, help="new empty directory")
    args = parser.parse_args()
    if args.command == "client":
        build_client(args.allow_tool_drift)
    elif args.command == "package":
        package(args.runtime, args.unit_manifest, args.commit, args.output, args.client_app.resolve())
    else:
        verify(args.dmg.resolve(), args.scratch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
