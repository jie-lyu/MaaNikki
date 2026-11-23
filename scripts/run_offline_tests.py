"""Run the offline regression suites: python scripts/run_offline_tests.py [--python python/python.exe].

Every scripts/test_*.py and scripts/test_*.cjs runs, and each test's outcome
is compared with scripts/fixtures/offline-test-baseline.json. The run fails
when a test fails that the baseline does not list as a known failure, when a
suite stops importing, or when a suite runs fewer tests than the baseline
records. Known failures that start passing are reported so the baseline can
be tightened. New suites must be added to the baseline.

Python suites run under --python (the packaged runtime on Windows, so they
see the pinned dependencies and ./maafw), one subprocess per suite. Their
tests are loaded from the module without running its __main__ block. .cjs
suites run under node with the TAP reporter and need the client's installed
dependencies.

    --results PATH         write every observed outcome as JSON
    --write-baseline PATH  write a baseline from this run (review before committing)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts/fixtures/offline-test-baseline.json"
GITHUB = os.environ.get("GITHUB_ACTIONS") == "true"
IMPORT_FAILURE = "<import>"


def discovered() -> list[str]:
    return sorted(path.name for pattern in ("test_*.py", "test_*.cjs") for path in (ROOT / "scripts").glob(pattern))


def annotate(level: str, message: str) -> None:
    if GITHUB:
        print(f"::{level}::{message}", flush=True)


class _RecordingResult(unittest.TextTestResult):
    """Text output as usual, plus one outcome per test id (subtests fold into their test)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes: dict[str, str] = {}

    @staticmethod
    def _key(test) -> str:
        test = getattr(test, "test_case", test)  # _SubTest -> its parent test
        identifier = test.id()
        return identifier.split(".", 1)[1] if "." in identifier else identifier

    def _mark(self, test, outcome):
        key = self._key(test)
        if outcome == "pass" and key in self.outcomes:
            return  # a failed subtest already decided this test
        self.outcomes[key] = outcome

    def addSuccess(self, test):
        super().addSuccess(test)
        self._mark(test, "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._mark(test, "fail")

    def addError(self, test, err):
        super().addError(test, err)
        self._mark(test, "error")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._mark(test, "skip")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._mark(test, "pass")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._mark(test, "fail")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._mark(subtest, "fail" if issubclass(err[0], test.failureException) else "error")


def child(suite: Path, output: Path) -> int:
    """Run one Python suite in this process and write its outcomes."""
    report = {"import_error": None, "outcomes": {}}
    try:
        spec = importlib.util.spec_from_file_location("offline_suite", suite)
        module = importlib.util.module_from_spec(spec)
        sys.modules["offline_suite"] = module
        spec.loader.exec_module(module)
        tests = unittest.defaultTestLoader.loadTestsFromModule(module)
    except BaseException as error:  # noqa: BLE001 - any import failure is an outcome
        import traceback
        traceback.print_exc()
        report["import_error"] = f"{type(error).__name__}: {error}"[:500]
    else:
        runner = unittest.TextTestRunner(resultclass=_RecordingResult, verbosity=2, stream=sys.stderr)
        report["outcomes"] = runner.run(tests).outcomes
    output.write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
    return 0


_TAP = re.compile(r"^(not ok|ok) \d+ - (.*?)(\s+# (SKIP|TODO).*)?$")


def parse_tap(text: str) -> dict[str, str]:
    outcomes = {}
    for line in text.splitlines():
        match = _TAP.match(line)  # top-level tests only; subtests are indented
        if match:
            status, name, directive = match.group(1), match.group(2), match.group(4)
            outcomes[name] = "skip" if directive else ("pass" if status == "ok" else "fail")
    return outcomes


def run_suite(name: str, python: str, timeout: float) -> dict:
    print(f"::group::{name}" if GITHUB else f"===== {name}", flush=True)
    report = {"import_error": None, "outcomes": {}}
    try:
        if name.endswith(".cjs"):
            completed = subprocess.run(["node", "--test-reporter=tap", f"scripts/{name}"], cwd=ROOT,
                                       timeout=timeout, stdout=subprocess.PIPE, text=True, encoding="utf-8")
            print(completed.stdout, flush=True)
            report["outcomes"] = parse_tap(completed.stdout)
            if not report["outcomes"]:
                report["import_error"] = f"no TAP results (exit {completed.returncode})"
        else:
            output = ROOT / ".build" / f"offline-{name}.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.unlink(missing_ok=True)
            subprocess.run([python, "-B", str(Path(__file__).resolve()), "--child", f"scripts/{name}", str(output)],
                           cwd=ROOT, timeout=timeout)
            if output.is_file():
                report = json.loads(output.read_text(encoding="utf-8"))
            else:
                report["import_error"] = "suite process ended without a report"
    except subprocess.TimeoutExpired:
        report["import_error"] = f"timed out after {timeout:.0f}s"
    except OSError as error:
        report["import_error"] = f"could not start: {error}"
    finally:
        if GITHUB:
            print("::endgroup::", flush=True)
    return report


def compare(name: str, report: dict, expected: dict | None) -> tuple[list[str], list[str]]:
    """Problems that fail the run, and notices about known failures that now pass."""
    if expected is None:
        return [f"{name}: not in the baseline; add it"], []
    problems, notices = [], []
    known = set(expected.get("known_failures", []))
    if report["import_error"]:
        if IMPORT_FAILURE not in known:
            problems.append(f"{name}: suite did not run ({report['import_error']})")
        return problems, notices
    if IMPORT_FAILURE in known:
        notices.append(f"{name}: suite imports again; record its tests in the baseline")
    outcomes = report["outcomes"]
    for test, outcome in sorted(outcomes.items()):
        if outcome in ("fail", "error") and test not in known:
            problems.append(f"{name}: {test} {outcome}ed")
        elif outcome == "pass" and test in known:
            notices.append(f"{name}: {test} now passes; remove it from known_failures")
    minimum = expected.get("tests", 0)
    if len(outcomes) < minimum:
        problems.append(f"{name}: ran {len(outcomes)} tests, baseline has {minimum}")
    return problems, notices


def baseline_from(results: dict, source: str) -> dict:
    suites = {}
    for name, report in sorted(results.items()):
        if report["import_error"]:
            suites[name] = {"tests": 0, "known_failures": [IMPORT_FAILURE]}
        else:
            outcomes = report["outcomes"]
            suites[name] = {"tests": len(outcomes),
                            "known_failures": sorted(test for test, outcome in outcomes.items()
                                                     if outcome in ("fail", "error"))}
    return {"source": source, "suites": suites}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", default=sys.executable, help="interpreter for the Python suites")
    parser.add_argument("--only", nargs="*", help="run only these suites")
    parser.add_argument("--skip-node", action="store_true", help="skip the .cjs suites")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--write-baseline", type=Path)
    parser.add_argument("--baseline-source", default="")
    parser.add_argument("--child", nargs=2, metavar=("SUITE", "OUTPUT"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return child(ROOT / args.child[0], Path(args.child[1]))

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))["suites"] if args.baseline.is_file() else {}
    names = [name for name in discovered() if not args.only or name in args.only]
    if args.skip_node:
        names = [name for name in names if not name.endswith(".cjs")]

    results, timings = {}, {}
    for name in names:
        started = time.monotonic()
        results[name] = run_suite(name, args.python, args.timeout)
        timings[name] = time.monotonic() - started

    if args.results:
        args.results.parent.mkdir(parents=True, exist_ok=True)
        args.results.write_text(json.dumps(results, indent=1, sort_keys=True), encoding="utf-8")
    if args.write_baseline:
        source = args.baseline_source or f"{sys.platform}, {args.python}"
        args.write_baseline.write_text(json.dumps(baseline_from(results, source), indent=1) + "\n",
                                       encoding="utf-8")

    print("\nSummary", flush=True)
    all_problems = []
    if not args.only:
        all_problems += [f"{name}: listed in the baseline but missing from scripts/"
                         for name in sorted(set(baseline) - set(discovered()))]
    for name in names:
        report = results[name]
        problems, notices = compare(name, report, baseline.get(name))
        all_problems += problems
        counts = {}
        for outcome in report["outcomes"].values():
            counts[outcome] = counts.get(outcome, 0) + 1
        detail = report["import_error"] or ", ".join(f"{key} {value}" for key, value in sorted(counts.items()))
        print(f"  {'FAIL' if problems else 'ok':4} {timings[name]:6.1f}s  {name}: {detail}", flush=True)
        for notice in notices:
            print(f"        note: {notice}", flush=True)
            annotate("notice", notice)
    for problem in all_problems:
        print(f"  problem: {problem}", flush=True)
        annotate("error", problem)
    return 1 if all_problems else 0


if __name__ == "__main__":
    sys.exit(main())
