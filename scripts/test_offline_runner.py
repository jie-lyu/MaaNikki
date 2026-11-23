"""Offline suite runner checks: python -B scripts/test_offline_runner.py. Standard library only."""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("maanikki_offline_runner", ROOT / "scripts/run_offline_tests.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


def report(outcomes=None, import_error=None, run_error=None):
    return {"import_error": import_error, "run_error": run_error, "outcomes": outcomes or {}}


class CompareTests(unittest.TestCase):
    expected = {"tests": ["A.test_old", "A.test_ok", "A.test_skip"], "known_failures": ["A.test_old"],
                "skips": ["A.test_skip"]}

    def test_matching_run_passes(self):
        problems, _ = runner.compare("s", report({"A.test_old": "fail", "A.test_ok": "pass", "A.test_skip": "skip"}),
                                     self.expected)
        self.assertEqual(problems, [])

    def test_new_failure_fails_the_run(self):
        problems, _ = runner.compare("s", report({"A.test_old": "fail", "A.test_ok": "error", "A.test_skip": "skip"}),
                                     self.expected)
        self.assertEqual(problems, ["s: A.test_ok errored"])

    def test_newly_skipped_test_fails(self):
        problems, _ = runner.compare("s", report({"A.test_old": "fail", "A.test_ok": "skip", "A.test_skip": "skip"}),
                                     self.expected)
        self.assertEqual(problems, ["s: A.test_ok was skipped"])

    def test_replaced_test_fails_even_with_same_count(self):
        problems, _ = runner.compare("s", report({"A.test_old": "fail", "A.test_new": "pass", "A.test_skip": "skip"}),
                                     self.expected)
        self.assertEqual(problems, ["s: A.test_ok did not run", "s: A.test_new is not in the baseline; add it"])

    def test_known_failure_passing_is_a_notice(self):
        problems, notices = runner.compare("s", report({"A.test_old": "pass", "A.test_ok": "pass",
                                                        "A.test_skip": "skip"}), self.expected)
        self.assertEqual(problems, [])
        self.assertEqual(len(notices), 1)

    def test_import_failure_and_run_errors(self):
        problems, _ = runner.compare("s", report(import_error="ImportError: x"), self.expected)
        self.assertEqual(len(problems), 1)
        quarantined = {"tests": [], "known_failures": [runner.IMPORT_FAILURE], "skips": []}
        self.assertEqual(runner.compare("s", report(import_error="boom"), quarantined)[0], [])
        # A quarantined import failure that turns into a timeout or crash still fails.
        self.assertEqual(runner.compare("s", report(run_error="timed out after 9s"), quarantined)[0],
                         ["s: timed out after 9s"])
        self.assertEqual(runner.compare("s", report(), None)[0], ["s: not in the baseline; add it"])

    def test_baseline_round_trip(self):
        results = {"a.py": report({"T.x": "pass", "T.y": "fail", "T.z": "skip"}),
                   "b.py": report(import_error="boom"),
                   "c.py": report(run_error="crashed")}
        baseline = runner.baseline_from(results, "here")["suites"]
        self.assertEqual(baseline["a.py"], {"tests": ["T.x", "T.y", "T.z"], "known_failures": ["T.y"],
                                            "skips": ["T.z"]})
        self.assertNotIn("c.py", baseline)
        for name in ("a.py", "b.py"):
            self.assertEqual(runner.compare(name, results[name], baseline[name])[0], [])


class RecordingTests(unittest.TestCase):
    def test_subtests_fold_into_their_test(self):
        class Sample(unittest.TestCase):
            def test_sub(self):
                for value in (1, 2):
                    with self.subTest(value=value):
                        self.assertEqual(value, 1)

            def test_ok(self):
                pass

            def test_error(self):
                raise RuntimeError("x")

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Sample)
        result = unittest.TextTestRunner(resultclass=runner._RecordingResult, stream=io.StringIO()).run(suite)
        outcomes = {key.rsplit(".", 1)[-1]: value for key, value in result.outcomes.items()}
        self.assertEqual(outcomes, {"test_sub": "fail", "test_ok": "pass", "test_error": "error"})

    def test_tap_exit_codes(self):
        self.assertIsNone(runner.tap_run_error(0, {"a": "pass"}))
        self.assertIsNone(runner.tap_run_error(1, {"a": "fail"}))
        self.assertIsNotNone(runner.tap_run_error(1, {"a": "pass"}))
        self.assertIsNotNone(runner.tap_run_error(0xC0000005, {"a": "fail"}))  # crash after reported failures
        self.assertIsNotNone(runner.tap_run_error(1, {}))

    def test_tap_top_level_only(self):
        text = "TAP version 13\n# Subtest: a\n    ok 1 - inner\nok 1 - a\nnot ok 2 - b\nok 3 - c # SKIP later\n"
        self.assertEqual(runner.parse_tap(text), {"a": "pass", "b": "fail", "c": "skip"})


if __name__ == "__main__":
    unittest.main()
