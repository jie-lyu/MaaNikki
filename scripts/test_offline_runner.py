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


class CompareTests(unittest.TestCase):
    expected = {"tests": 3, "known_failures": ["A.test_old"]}

    def test_new_failure_fails_the_run(self):
        problems, _ = runner.compare("s", {"import_error": None, "outcomes": {
            "A.test_old": "fail", "A.test_new": "error", "A.test_ok": "pass"}}, self.expected)
        self.assertEqual(problems, ["s: A.test_new errored"])

    def test_known_failure_passing_is_a_notice(self):
        problems, notices = runner.compare("s", {"import_error": None, "outcomes": {
            "A.test_old": "pass", "A.test_b": "pass", "A.test_c": "skip"}}, self.expected)
        self.assertEqual(problems, [])
        self.assertEqual(len(notices), 1)

    def test_lost_tests_and_import_failure_fail(self):
        problems, _ = runner.compare("s", {"import_error": None, "outcomes": {"A.test_ok": "pass"}}, self.expected)
        self.assertEqual(problems, ["s: ran 1 tests, baseline has 3"])
        problems, _ = runner.compare("s", {"import_error": "ImportError: x", "outcomes": {}}, self.expected)
        self.assertEqual(len(problems), 1)

    def test_expected_import_failure_and_unlisted_suite(self):
        problems, _ = runner.compare("s", {"import_error": "boom", "outcomes": {}},
                                     {"tests": 0, "known_failures": [runner.IMPORT_FAILURE]})
        self.assertEqual(problems, [])
        problems, _ = runner.compare("s", {"import_error": None, "outcomes": {}}, None)
        self.assertEqual(problems, ["s: not in the baseline; add it"])

    def test_baseline_round_trip(self):
        results = {"a.py": {"import_error": None, "outcomes": {"T.x": "pass", "T.y": "fail"}},
                   "b.py": {"import_error": "boom", "outcomes": {}}}
        baseline = runner.baseline_from(results, "here")["suites"]
        self.assertEqual(baseline["a.py"], {"tests": 2, "known_failures": ["T.y"]})
        for name, report in results.items():
            self.assertEqual(runner.compare(name, report, baseline[name])[0], [])


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

    def test_tap_top_level_only(self):
        text = "TAP version 13\n# Subtest: a\n    ok 1 - inner\nok 1 - a\nnot ok 2 - b\nok 3 - c # SKIP later\n"
        self.assertEqual(runner.parse_tap(text), {"a": "pass", "b": "fail", "c": "skip"})


if __name__ == "__main__":
    unittest.main()
