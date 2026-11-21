"""Offline tests for shard_ui_tests.py and check_shard_ran.py. Run: python3 -m unittest discover -s .github/scripts"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_shard_ran as ran  # noqa: E402
import shard_ui_tests as sh  # noqa: E402


def swift_class(name, n, base="XCTestCase", extra=""):
    methods = "\n".join(f"    func test{i}() throws {{ XCTAssertTrue(true) }}" for i in range(n))
    return f"import XCTest\n\nfinal class {name}: {base} {{\n{methods}\n{extra}}}\n"


class FakeTree:
    def __init__(self, files):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        for name, text in files.items():
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
                fh.write(text)

    def cleanup(self):
        self.tmp.cleanup()


class DiscoverTests(unittest.TestCase):
    def discover(self, files, **kw):
        t = FakeTree(files)
        self.addCleanup(t.cleanup)
        return sh.discover(t.dir, **kw)

    def test_counts_test_methods_per_xctestcase_class(self):
        got = self.discover({"A.swift": swift_class("AUITests", 3), "B.swift": swift_class("BUITests", 1)})
        self.assertEqual(got, {"AUITests": 3, "BUITests": 1})

    def test_excludes_screenshot_tour_and_non_test_files(self):
        got = self.discover({
            "Tour.swift": swift_class("ScreenshotTourUITests", 5),
            "Support.swift": "import XCTest\nfinal class Helper {\n    func testLooksLikeATest() {}\n}\n",
            "A.swift": swift_class("AUITests", 2),
        })
        self.assertEqual(got, {"AUITests": 2})

    def test_default_exclude_covers_tour_and_store_screenshot_classes(self):
        self.assertIn("ScreenshotTourUITests", sh.DEFAULT_EXCLUDE)
        self.assertIn("StoreScreenshotsUITests", sh.DEFAULT_EXCLUDE)
        self.assertIn("IPadLayoutUITests", sh.DEFAULT_EXCLUDE)
        got = self.discover({
            "Store.swift": swift_class("StoreScreenshotsUITests", 9),
            "IPad.swift": swift_class("IPadLayoutUITests", 12),
            "A.swift": swift_class("AUITests", 2),
        })
        self.assertEqual(got, {"AUITests": 2})

    def test_excludes_per_screen_a11y_audit_classes_but_keeps_other_a11y_classes(self):
        got = self.discover({
            "A1.swift": swift_class("A11yAuditHomeUITests", 4),
            "A2.swift": swift_class("A11yAudit_Settings2UITests", 4),
            "T.swift": swift_class("A11yTraitOverrideUITests", 2),
            "E.swift": swift_class("A11yExclusionMatchTests", 3),
            "A.swift": swift_class("AUITests", 2),
        })
        self.assertEqual(got, {"A11yTraitOverrideUITests": 2, "A11yExclusionMatchTests": 3, "AUITests": 2})

    def test_audit_exclusion_applies_even_when_exclude_is_overridden(self):
        got = self.discover({"A1.swift": swift_class("A11yAuditHomeUITests", 4),
                             "A.swift": swift_class("AUITests", 2)}, exclude=())
        self.assertEqual(got, {"AUITests": 2})

    def test_ignores_private_commented_and_parameterised_tests(self):
        extra = ("    private func testHidden() {}\n    func testHelper(_ x: Int) {}\n"
                 "    // func testCommented() {}\n    /* func testBlock() {} */\n"
                 "    func helper() {}\n")
        got = self.discover({"A.swift": swift_class("AUITests", 2, extra=extra)})
        self.assertEqual(got, {"AUITests": 2})

    def test_counts_tests_in_extensions_and_follows_inheritance(self):
        files = {
            "Base.swift": "import XCTest\nclass BaseUITestCase: XCTestCase {\n    func setUpHelper() {}\n}\n",
            "A.swift": swift_class("AUITests", 1, base="BaseUITestCase")
            + "\nextension AUITests {\n    func testFromExtension() {}\n}\n",
        }
        self.assertEqual(self.discover(files), {"AUITests": 2})

    def test_helper_types_before_the_class_do_not_steal_methods(self):
        text = ("import XCTest\n\nstruct Fixture {\n    func testNotReal() {}\n}\n\n"
                + swift_class("AUITests", 2))
        self.assertEqual(self.discover({"A.swift": text}), {"AUITests": 2})

    def test_empty_directory_is_an_error(self):
        t = FakeTree({})
        self.addCleanup(t.cleanup)
        with self.assertRaises(sh.ShardError):
            sh.discover(t.dir)


class SplitTests(unittest.TestCase):
    CLASSES = {"A": 10, "B": 9, "C": 8, "D": 3, "E": 2, "F": 1, "G": 1}

    def test_exact_partition_and_deterministic(self):
        one, _ = sh.split(self.CLASSES, [1, 1, 1])
        two, _ = sh.split(dict(reversed(list(self.CLASSES.items()))), [1, 1, 1])
        self.assertEqual(one, two)
        sh.verify_partition(self.CLASSES, one)
        self.assertEqual(sorted(c for g in one for c in g), sorted(self.CLASSES))

    def test_balances_by_method_count_when_no_weights(self):
        shards, load = sh.split(self.CLASSES, [1, 1, 1])
        counts = [sum(self.CLASSES[c] for c in g) for g in shards]
        self.assertLessEqual(max(counts) - min(counts), 2, counts)
        self.assertAlmostEqual(sum(load), 34 * sh.DEFAULT_SECONDS_PER_METHOD)

    def test_weights_override_method_counts(self):
        classes = {"Few": 1, "Many": 10, "Mid": 5}
        shards, _ = sh.split(classes, [1, 1], {"Few": 1000, "Many": 10, "Mid": 10})
        self.assertEqual(shards[0], ["Few"])
        self.assertEqual(shards[1], ["Many", "Mid"])

    def test_small_capacity_gets_less_work(self):
        weights = {c: n for c, n in self.CLASSES.items()}
        shards, load = sh.split(self.CLASSES, [0.25, 1, 1], weights)
        self.assertLess(load[0], load[1])
        self.assertLess(load[0], load[2])
        sh.verify_partition(self.CLASSES, shards)

    def test_bad_capacities_rejected(self):
        for caps in ([], [1, 0], [1, -1]):
            with self.assertRaises(sh.ShardError):
                sh.split(self.CLASSES, caps)

    def test_verify_partition_detects_duplicates_and_gaps(self):
        with self.assertRaises(sh.ShardError):
            sh.verify_partition({"A": 1, "B": 1}, [["A"], ["A", "B"]])
        with self.assertRaises(sh.ShardError):
            sh.verify_partition({"A": 1, "B": 1}, [["A"], []])
        with self.assertRaises(sh.ShardError):
            sh.verify_partition({"A": 1}, [["A", "Ghost"]])


class CliTests(unittest.TestCase):
    def setUp(self):
        files = {f"C{i}.swift": swift_class(f"C{i}UITests", n) for i, n in enumerate([5, 4, 3, 2, 1])}
        files["Tour.swift"] = swift_class("ScreenshotTourUITests", 9)
        files["Store.swift"] = swift_class("StoreScreenshotsUITests", 8)
        self.tree = FakeTree(files)
        self.addCleanup(self.tree.cleanup)
        self.out = os.path.join(self.tree.dir, "..", f"args-{os.getpid()}.txt")

    def run_cli(self, *extra):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = sh.main(["--ui-dir", self.tree.dir, "--out", self.out, *extra])
        return code, buf.getvalue()

    def test_shards_union_to_all_classes_without_overlap_and_log_the_list(self):
        seen = []
        for i in range(3):
            code, out = self.run_cli("--shards", "3", "--index", str(i))
            self.assertEqual(code, 0, out)
            self.assertIn(f"shard {i} classes:", out)
            with open(self.out) as fh:
                lines = fh.read().splitlines()
            self.assertTrue(lines)
            self.assertTrue(all(l.startswith("-only-testing:RadioShakeUITests/") for l in lines))
            seen += lines
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), 5)
        self.assertFalse(any("ScreenshotTour" in l for l in seen))
        self.assertFalse(any("StoreScreenshots" in l for l in seen))

    def test_empty_shard_fails(self):
        code, out = self.run_cli("--shards", "9", "--index", "8")
        self.assertEqual(code, 1)
        self.assertIn("::error::", out)

    def test_index_out_of_range_and_capacity_mismatch_fail(self):
        self.assertEqual(self.run_cli("--shards", "3", "--index", "3")[0], 1)
        self.assertEqual(self.run_cli("--shards", "3", "--index", "0", "--capacities", "1,1")[0], 1)

    def test_weights_file_is_used_and_missing_file_ignored(self):
        w = os.path.join(self.tree.dir, "w.json")
        with open(w, "w") as fh:
            json.dump({"C0UITests": 10000}, fh)
        code, out = self.run_cli("--shards", "2", "--index", "0", "--weights", w)
        self.assertEqual(code, 0, out)
        with open(self.out) as fh:
            self.assertEqual(fh.read().splitlines(), ["-only-testing:RadioShakeUITests/C0UITests"])
        self.assertEqual(self.run_cli("--shards", "2", "--index", "0", "--weights", w + ".missing")[0], 0)


class CheckShardRanTests(unittest.TestCase):
    ARGS = "-only-testing:RadioShakeUITests/AUITests\n-only-testing:RadioShakeUITests/BUITests\n"

    def test_selected_classes_parsing(self):
        self.assertEqual(ran.selected_classes(self.ARGS), ["AUITests", "BUITests"])
        with self.assertRaises(ValueError):
            ran.selected_classes("-only-testing:X/Y; rm -rf /\n")

    def test_missing_classes(self):
        log = "Test Suite 'AUITests' passed at 2026-10-30 23:12:28.742.\nTest Suite 'BUITests' failed at 2026\n"
        self.assertEqual(ran.missing_classes(["AUITests", "BUITests"], log), [])
        self.assertEqual(ran.missing_classes(["AUITests", "CUITests"], log), ["CUITests"])
        # a suite that merely started, or the command line echo, is not a run
        self.assertEqual(ran.missing_classes(["AUITests"], "Test Suite 'AUITests' started at x\n-only-testing:RadioShakeUITests/AUITests"), ["AUITests"])

    METHOD_ARGS = ("-only-testing:RadioShakeUITests/A11yAuditHomeUITests/testGateDefaultLight\n"
                   "-only-testing:RadioShakeUITests/A11yAuditHomeUITests/testGateAx5DarkBold\n")

    def test_selected_entries_parse_class_and_class_method(self):
        self.assertEqual(ran.selected_entries(self.ARGS + self.METHOD_ARGS),
                         [("AUITests", None), ("BUITests", None),
                          ("A11yAuditHomeUITests", "testGateDefaultLight"),
                          ("A11yAuditHomeUITests", "testGateAx5DarkBold")])
        with self.assertRaises(ValueError):
            ran.selected_entries("-only-testing:X/Y/z/extra\n")
        with self.assertRaises(ValueError):
            ran.selected_entries("-only-testing:X/Y/z w\n")

    def test_missing_entries_require_a_passed_or_failed_test_case_line(self):
        entries = ran.selected_entries(self.METHOD_ARGS)
        passed = "Test Case '-[RadioShakeUITests.A11yAuditHomeUITests testGateDefaultLight]' passed (3.1 seconds).\n"
        failed = "Test Case '-[RadioShakeUITests.A11yAuditHomeUITests testGateAx5DarkBold]' failed (4.0 seconds).\n"
        skipped = "Test Case '-[RadioShakeUITests.A11yAuditHomeUITests testGateAx5DarkBold]' skipped (0.0 seconds).\n"
        started = "Test Case '-[RadioShakeUITests.A11yAuditHomeUITests testGateAx5DarkBold]' started.\n"
        self.assertEqual(ran.missing_entries(entries, passed + failed), [])
        self.assertEqual(ran.missing_entries(entries, passed + skipped), ["A11yAuditHomeUITests/testGateAx5DarkBold"])
        self.assertEqual(ran.missing_entries(entries, passed + started), ["A11yAuditHomeUITests/testGateAx5DarkBold"])
        self.assertEqual(ran.missing_entries(entries, ""), [
            "A11yAuditHomeUITests/testGateDefaultLight", "A11yAuditHomeUITests/testGateAx5DarkBold"])
        # a different class with the same method does not count
        other = "Test Case '-[RadioShakeUITests.A11yAuditOtherUITests testGateDefaultLight]' passed (1 seconds).\n"
        self.assertEqual(ran.missing_entries(entries[:1], other), ["A11yAuditHomeUITests/testGateDefaultLight"])

    def test_strict_mode_rejects_all_skipped_and_partly_skipped_classes(self):
        entries = ran.selected_entries("-only-testing:RadioShakeUITests/IPadUITests\n")
        suite = "Test Suite 'IPadUITests' passed at x\n"
        case = lambda m, s: f"Test Case '-[RadioShakeUITests.IPadUITests {m}]' {s} (0.1 seconds).\n"
        all_skipped = case("testA", "skipped") + case("testB", "skipped") + suite
        mixed = case("testA", "passed") + case("testB", "skipped") + suite
        good = case("testA", "passed") + case("testB", "failed") + suite
        # lenient (ui-shard) behaviour is unchanged
        self.assertEqual(ran.missing_entries(entries, all_skipped), [])
        self.assertEqual(ran.missing_entries(entries, mixed), [])
        self.assertEqual(len(ran.missing_entries(entries, all_skipped, strict=True)), 1)
        self.assertEqual(len(ran.missing_entries(entries, mixed, strict=True)), 1)
        self.assertEqual(ran.missing_entries(entries, good, strict=True), [])
        # another class's executed tests do not satisfy the selection
        other = "Test Case '-[RadioShakeUITests.OtherUITests testA]' passed (1 seconds).\n" + all_skipped
        self.assertEqual(len(ran.missing_entries(entries, other, strict=True)), 1)

    def test_cli_require_executed_flag(self):
        with tempfile.TemporaryDirectory() as d:
            a, l = os.path.join(d, "a"), os.path.join(d, "l")
            open(a, "w").write("-only-testing:RadioShakeUITests/IPadUITests\n")
            open(l, "w").write("Test Case '-[RadioShakeUITests.IPadUITests testA]' skipped (0.0 seconds).\n"
                               "Test Suite 'IPadUITests' passed at x\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ran.main(["--args", a, "--log", l]), 0)
                self.assertEqual(ran.main(["--args", a, "--log", l, "--require-executed"]), 1)

    def test_cli_method_entries(self):
        with tempfile.TemporaryDirectory() as d:
            a, l = os.path.join(d, "a"), os.path.join(d, "l")
            open(a, "w").write(self.METHOD_ARGS)
            open(l, "w").write("Test Case '-[RadioShakeUITests.A11yAuditHomeUITests testGateDefaultLight]' passed (1 seconds).\n"
                                "Test Case '-[RadioShakeUITests.A11yAuditHomeUITests testGateAx5DarkBold]' skipped (0 seconds).\n")
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(ran.main(["--args", a, "--log", l]), 1)
            self.assertIn("testGateAx5DarkBold", buf.getvalue())
            open(l, "a").write("Test Case '-[RadioShakeUITests.A11yAuditHomeUITests testGateAx5DarkBold]' passed (1 seconds).\n")
            open(l, "w").write("Test Case '-[RadioShakeUITests.A11yAuditHomeUITests testGateDefaultLight]' passed (1 seconds).\n"
                               "Test Case '-[RadioShakeUITests.A11yAuditHomeUITests testGateAx5DarkBold]' failed (1 seconds).\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ran.main(["--args", a, "--log", l]), 0)

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            a, l = os.path.join(d, "a"), os.path.join(d, "l")
            open(a, "w").write(self.ARGS)
            open(l, "w").write("Test Suite 'AUITests' passed at x\nTest Suite 'BUITests' passed at x\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(ran.main(["--args", a, "--log", l]), 0)
                open(l, "w").write("Test Suite 'AUITests' passed at x\n")
                self.assertEqual(ran.main(["--args", a, "--log", l]), 1)
                open(a, "w").write("")
                self.assertEqual(ran.main(["--args", a, "--log", l]), 1)
                self.assertEqual(ran.main(["--args", a + ".nope", "--log", l]), 1)


if __name__ == "__main__":
    unittest.main()
