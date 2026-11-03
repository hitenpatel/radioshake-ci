import os
import tempfile
import unittest

import check_shard_ran
import report_flaky_tests as r

FLAKE_THEN_PASS = """\
Test Case '-[RadioShakeUITests.AddStreamUITests testToast]' started.
/src/AddStreamUITests.swift:88: error: -[RadioShakeUITests.AddStreamUITests testToast] : XCTAssertTrue failed - toast missing
Test Case '-[RadioShakeUITests.AddStreamUITests testToast]' failed (31.2 seconds).
Test Case '-[RadioShakeUITests.AddStreamUITests testOther]' passed (4.0 seconds).
Test Suite 'AddStreamUITests' failed at 2026-11-03 10:00:00.000.
Test Case '-[RadioShakeUITests.AddStreamUITests testToast]' started.
Test Case '-[RadioShakeUITests.AddStreamUITests testToast]' passed (9.9 seconds).
Test Suite 'AddStreamUITests' passed at 2026-11-03 10:01:00.000.
"""

HARD_FAIL = """\
Test Case '-[RadioShakeUITests.MapUITests testTap]' failed (30.0 seconds).
Test Case '-[RadioShakeUITests.MapUITests testTap]' failed (30.0 seconds).
Test Suite 'MapUITests' failed at 2026-11-03 10:00:00.000.
"""


class FindFlakyTests(unittest.TestCase):
    def test_fail_then_pass_is_flaky_with_first_error(self):
        f = r.find_flaky(FLAKE_THEN_PASS)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["test"], "RadioShakeUITests.AddStreamUITests.testToast")
        self.assertEqual(f[0]["failures"], 1)
        self.assertIn("toast missing", f[0]["first_error"])

    def test_plain_pass_and_hard_failure_are_not_reported(self):
        self.assertEqual(r.find_flaky("Test Case '-[A.B testX]' passed (1.0 seconds).\n"), [])
        self.assertEqual(r.find_flaky(HARD_FAIL), [])

    def test_pass_then_fail_is_not_flaky(self):
        self.assertEqual(r.find_flaky("Test Case '-[A.B testX]' passed (1 seconds).\nTest Case '-[A.B testX]' failed (1 seconds).\n"), [])

    def test_parallel_destination_line_format(self):
        log = ("Test case '-[A.B testX]' failed on 'Clone 1 of iPhone 17 - RadioShake (1)' (3.0 seconds)\n"
               "Test case '-[A.B testX]' passed on 'iPhone 17 - RadioShake (2)' (3.0 seconds)\n")
        self.assertEqual(len(r.find_flaky(log)), 1)

    def test_annotation_is_a_single_escaped_warning_line(self):
        a = r.annotation({"test": "A.B.t", "failures": 1, "first_error": "50% bad\nsecond"})
        self.assertTrue(a.startswith("::warning title=Flaky UI test (passed on retry)::"))
        self.assertNotIn("\n", a)
        self.assertIn("50%25 bad%0Asecond", a)

    def test_main_exits_zero_and_writes_summary(self):
        with tempfile.TemporaryDirectory() as d:
            log, summ = os.path.join(d, "x.log"), os.path.join(d, "s.md")
            open(log, "w").write(FLAKE_THEN_PASS)
            os.environ["GITHUB_STEP_SUMMARY"] = summ
            try:
                self.assertEqual(r.main(["--log", log]), 0)
            finally:
                del os.environ["GITHUB_STEP_SUMMARY"]
            self.assertIn("testToast", open(summ).read())

    def test_missing_log_exits_zero(self):
        self.assertEqual(r.main(["--log", "/nonexistent/x.log"]), 0)


class ShardCheckSurvivesRetries(unittest.TestCase):
    def test_class_that_failed_then_passed_still_counts_as_run(self):
        self.assertEqual(check_shard_ran.missing_classes(["AddStreamUITests"], FLAKE_THEN_PASS), [])

    def test_class_never_run_still_missing(self):
        self.assertEqual(check_shard_ran.missing_classes(["GhostUITests"], FLAKE_THEN_PASS), ["GhostUITests"])


if __name__ == "__main__":
    unittest.main()
