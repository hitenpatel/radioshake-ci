"""Structure checks for ios-shared-validate.yml: sharding must keep the gate's contract.
Run: python3 -m unittest discover -s .github/scripts   (skipped when PyYAML is unavailable)"""
import os
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ios-shared-validate.yml")
SHARED_STEPS = [
    "Clone source from private host",
    "Set up JDK 21",
    "macOS preflight",
    "Install XcodeGen (pinned)",
    "Generate Xcode project",
    "Screenshot tour did not run in the public job",
    "Secret scan of evidence artifacts",
    "Check out CI scripts (this repo, scripts only)",
]


@unittest.skipIf(yaml is None, "PyYAML not installed")
class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as fh:
            cls.wf = yaml.safe_load(fh)
        cls.jobs = cls.wf["jobs"]

    @staticmethod
    def steps(job):
        return {s["name"]: s for s in job["steps"] if "name" in s}

    def test_exactly_one_job_named_validate(self):
        names = [j.get("name", key) for key, j in self.jobs.items()]
        self.assertEqual(names.count("validate"), 1)
        self.assertIn("validate", self.jobs)
        self.assertNotIn("name", self.jobs["validate"])  # name == job id; matrix names are expressions elsewhere

    def test_validate_keeps_clone_first_and_release_checks(self):
        steps = self.jobs["validate"]["steps"]
        self.assertEqual(steps[0]["name"], "Clone source from private host")
        names = self.steps(self.jobs["validate"])
        for required in ["Audit Release binary for debug-only symbols (TASK-136 + TASK-137)",
                         "Build iOS app (Release, simulator, unsigned)",
                         "Build iOS app (Release, device, unsigned) (TASK-14)",
                         "Run JVM + Android + Wear regression tests",
                         "Screenshot tour did not run in the public job",
                         "StoreKit storefront test ran and was not skipped",
                         "Secret scan of evidence artifacts", "Upload artifacts"]:
            self.assertIn(required, names)
        self.assertIn("OK — no debug-only", self.jobs["validate"]["steps"][
            [s.get("name") for s in steps].index("Audit Release binary for debug-only symbols (TASK-136 + TASK-137)")]["run"])

    def test_shared_steps_are_identical_in_validate_and_shards(self):
        v, s = self.steps(self.jobs["validate"]), self.steps(self.jobs["ui-shard"])
        for name in SHARED_STEPS:
            self.assertIn(name, v, name)
            self.assertIn(name, s, name)
            self.assertEqual(v[name], s[name], f"step {name!r} drifted between validate and ui-shard")

    def test_matrix_covers_shards_1_to_n_minus_1_and_capacities(self):
        n = int(self.wf["env"]["UI_SHARDS"])
        caps = self.wf["env"]["UI_SHARD_CAPACITIES"].split(",")
        self.assertEqual(len(caps), n)
        self.assertEqual(self.jobs["ui-shard"]["strategy"]["matrix"]["shard"], list(range(1, n)))
        self.assertFalse(self.jobs["ui-shard"]["strategy"]["fail-fast"])

    def test_report_status_needs_every_test_job_and_requires_all_success(self):
        rs = self.jobs["report-status"]
        self.assertEqual(sorted(rs["needs"]), ["ui-shard", "validate"])
        self.assertEqual(rs["if"], "always()")
        run = self.steps(rs)["Post status to Forgejo"]["run"]
        self.assertIn('[ "$VALIDATE_RESULT" = "success" ] && [ "$SHARDS_RESULT" = "success" ]', run)

    def test_every_job_clones_the_pinned_commit(self):
        for job in ("validate", "ui-shard"):
            env = self.steps(self.jobs[job])["Clone source from private host"]["env"]
            self.assertTrue(env["EXPECTED_SHA"].startswith("${{ needs.resolve.outputs.sha"), job)
            self.assertIn("resolve", self.jobs[job]["needs"])

    def test_shard_logs_and_uploads_are_scoped(self):
        shard = self.jobs["ui-shard"]
        paths = shard["env"]["EVIDENCE_PATHS"].splitlines()
        self.assertEqual(paths, ["/tmp/ios-logs/", "/tmp/ios-app-test-shard-${{ matrix.shard }}.xcresult"])
        run = self.steps(shard)["Run XCTest UI tests (shard)"]["run"]
        self.assertIn("-skip-testing:RadioShakeUITests/ScreenshotTourUITests", run)
        self.assertIn("tee /tmp/ios-logs/xctest.log", run)
        validate_run = self.steps(self.jobs["validate"])["Run XCTest unit + UI tests"]["run"]
        self.assertIn("-only-testing:RadioShakeTests", validate_run)
        self.assertIn("-skip-testing:RadioShakeUITests/ScreenshotTourUITests", validate_run)


if __name__ == "__main__":
    unittest.main()
