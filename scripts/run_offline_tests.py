"""Run the offline regression suites: python scripts/run_offline_tests.py [--python python/python.exe].

Every scripts/test_*.py and scripts/test_*.cjs must be listed below. Gating
suites fail the run. Known-failing suites still run so their state stays
visible, but only produce a warning; promote one to gating once it passes.
Python suites run under --python (the packaged runtime on Windows, so they
see the pinned dependencies and ./maafw); .cjs suites run under node and need
the client's installed dependencies.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]

# None: gating. A string: why the suite is not gating yet.
SUITES: dict[str, str | None] = {
    "test_windows_contract.py": None,
    "test_close_game.py": None,
    "test_agent_pause.py": "Not yet observed on Windows; on macOS it fails only on the Windows-only "
                           "CREATE_NO_WINDOW flag. Promote after the first Windows run passes.",
    "test_clicks.py": "Out of date since 2026-10-11: fakes lack attributes the code now uses "
                      "(post_screencap, entry_failure_frame, scroll(paged=...)).",
    "test_fishing.py": "Out of date since 2026-10-11: imports route_fishing, removed in a9ff717.",
    "test_focus_pause.py": "Out of date since 2026-10-11: fake controllers lack post_stop/node_name.",
    "test_gameplay.py": "Out of date since 2026-10-11: Mock collaborators no longer match the code.",
    "test_home.py": "Out of date since 2026-10-11: patches navigation.fishing.ROOT, which no longer exists.",
    "test_meteor.py": "Out of date since 2026-10-09: fake argv lacks node_name.",
    "test_navigation.py": "Out of date since 2026-10-10: patches names that moved out of navigation.insect.",
    "test_performance.py": "Out of date since 2026-10-11: fakes lack capture_requires_foreground/succeeded.",
    "test_route_test.py": "Out of date since 2026-10-09: patches names that moved out of daily.route_test.",
    "test_subtask_test.py": "Out of date since 2026-10-11: expected frame sizes and counts changed.",
    "test_task_restart.cjs": "Out of date since 8c6e9bf: maaService.ts imports @/services/updateService, "
                             "which the harness does not fake (16 of 17 tests fail).",
}

GITHUB = os.environ.get("GITHUB_ACTIONS") == "true"


def discovered() -> set[str]:
    return {path.name for pattern in ("test_*.py", "test_*.cjs") for path in (ROOT / "scripts").glob(pattern)}


def annotate(level: str, message: str) -> None:
    if GITHUB:
        print(f"::{level}::{message}", flush=True)


def run_suite(name: str, python: str, timeout: float) -> tuple[bool, float]:
    command = ["node", f"scripts/{name}"] if name.endswith(".cjs") else [python, "-B", f"scripts/{name}"]
    print(f"::group::{name}" if GITHUB else f"===== {name}", flush=True)
    started = time.monotonic()
    try:
        returncode = subprocess.run(command, cwd=ROOT, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        print(f"{name} timed out after {timeout:.0f}s", flush=True)
        returncode = -1
    except OSError as error:
        print(f"{name} could not start: {error}", flush=True)
        returncode = -1
    finally:
        if GITHUB:
            print("::endgroup::", flush=True)
    return returncode == 0, time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", default=sys.executable, help="interpreter for the Python suites")
    parser.add_argument("--only", nargs="*", help="run only these suites")
    parser.add_argument("--skip-node", action="store_true", help="skip the .cjs suites")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()

    unlisted = sorted(discovered() - set(SUITES))
    missing = sorted(set(SUITES) - discovered())
    if unlisted or missing:
        print(f"Suite list out of date. Unlisted: {unlisted}. Missing: {missing}.", file=sys.stderr)
        return 2

    names = [name for name in SUITES if not args.only or name in args.only]
    if args.skip_node:
        names = [name for name in names if not name.endswith(".cjs")]
    results = []
    for name in names:
        passed, seconds = run_suite(name, args.python, args.timeout)
        results.append((name, passed, seconds))

    print("\nSummary", flush=True)
    gate_failed = False
    for name, passed, seconds in results:
        reason = SUITES[name]
        kind = "gating" if reason is None else "known-failing"
        print(f"  {'PASS' if passed else 'FAIL'}  {kind:13} {seconds:6.1f}s  {name}", flush=True)
        if reason is None and not passed:
            gate_failed = True
            annotate("error", f"{name} failed")
        elif reason is not None and not passed:
            annotate("warning", f"{name} failed (not gating): {reason}")
        elif reason is not None and passed:
            annotate("notice", f"{name} passed; consider making it gating in scripts/run_offline_tests.py")
    return 1 if gate_failed else 0


if __name__ == "__main__":
    sys.exit(main())
