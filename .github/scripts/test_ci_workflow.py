"""Structure checks for ios-shared-validate.yml: sharding must keep the gate's contract.
Run: python3 -m unittest discover -s .github/scripts   (skipped when PyYAML is unavailable)"""
import os
import re
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


LISTING_WORKFLOW = os.path.join(ROOT, ".github", "workflows", "asc-listing.yml")
SECRET_NAMES = {"FORGEJO_HOST", "FORGEJO_CLONE_TOKEN", "APP_STORE_CONNECT_KEY_ID",
                "APP_STORE_CONNECT_ISSUER_ID", "APP_STORE_CONNECT_PRIVATE_KEY_B64"}


@unittest.skipIf(yaml is None, "PyYAML not installed")
class AscListingWorkflowTests(unittest.TestCase):
    """asc-listing.yml can reach App Store Connect: pin every safety property."""

    @classmethod
    def setUpClass(cls):
        with open(LISTING_WORKFLOW, encoding="utf-8") as fh:
            cls.text = fh.read()
        cls.wf = yaml.safe_load(cls.text)
        cls.job = cls.wf["jobs"]["listing"]
        cls.steps = cls.job["steps"]

    def step(self, name):
        for s in self.steps:
            if s.get("name") == name:
                return s
        self.fail(f"missing step {name!r}")

    def index(self, name):
        return [s.get("name") for s in self.steps].index(name)

    def test_dispatch_only_with_dry_run_default(self):
        on = self.wf.get("on", self.wf.get(True))
        self.assertEqual(list(on), ["workflow_dispatch"])
        inputs = on["workflow_dispatch"]["inputs"]
        self.assertEqual(sorted(inputs), ["build", "mode", "sha"])
        self.assertEqual(inputs["mode"]["options"], ["plan", "apply", "preflight"])
        self.assertEqual(inputs["mode"]["default"], "plan")
        self.assertTrue(inputs["sha"]["required"])
        self.assertFalse(inputs["build"].get("required", False))

    def test_permissions_environment_and_concurrency(self):
        self.assertEqual(self.wf["permissions"], {"contents": "read"})
        self.assertNotIn("permissions", self.job)
        self.assertEqual(self.job["environment"], "testflight")
        self.assertEqual(list(self.wf["jobs"]), ["listing"])
        self.assertEqual(self.wf["concurrency"], {"group": "asc-listing", "cancel-in-progress": False})
        self.assertIsInstance(self.job["timeout-minutes"], int)

    def test_every_action_is_pinned_to_a_sha_and_checkout_keeps_no_credentials(self):
        uses = [s["uses"] for s in self.steps if "uses" in s]
        self.assertTrue(uses)
        for u in uses:
            self.assertRegex(u, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$", u)
        co = [s for s in self.steps if s.get("uses", "").startswith("actions/checkout@")]
        self.assertEqual(len(co), 1)
        self.assertIs(co[0]["with"]["persist-credentials"], False)
        self.assertIn(".github/scripts", co[0]["with"]["sparse-checkout"])
        self.assertIn("requirements-asc.txt", co[0]["with"]["sparse-checkout"] + " " + self.text)

    def test_runs_only_from_main_and_validates_inputs(self):
        run = self.step("Require a dispatch from main and valid inputs")["run"]
        self.assertIn("refs/heads/main", run)
        self.assertIn("[0-9a-f]{40}", run)
        self.assertIn("plan|apply|preflight", run)
        self.assertIn("[1-9][0-9]{0,8}", run)
        self.assertLess(self.index("Require a dispatch from main and valid inputs"),
                        self.index("Clone source at the requested commit"))

    def test_branch_rule_plan_accepts_feature_branch_others_only_main(self):
        step = self.step("Clone source at the requested commit")
        run = step["run"]
        self.assertEqual(self.text.count("feat/ios-early-review"), 1)
        self.assertIn("merge-base --is-ancestor", run)
        self.assertIn("refs/remotes/origin/main", run)
        self.assertLess(run.index('"$MODE" = "plan"'), run.index("feat/ios-early-review"))
        self.assertIn("add-mask", run)
        self.assertIn("http.extraheader", run)
        self.assertIn("AUTHORIZATION", run)
        self.assertEqual(step["env"]["MODE"], "${{ inputs.mode }}")

    def test_checker_runs_before_the_key_exists_and_before_the_script(self):
        checker = self.index("Check listing files (fail closed)")
        key = self.index("Prepare App Store Connect key")
        script = self.index("Run listing script")
        self.assertLess(checker, key)
        self.assertLess(key, script)
        run = self.step("Check listing files (fail closed)")["run"]
        self.assertIn("check_appstore_listing.py", run)
        self.assertIn("--submission", run)
        self.assertIn("apply|preflight", run)
        self.assertIn("--no-screenshots", run)
        self.assertLess(run.index("apply|preflight"), run.index("--submission"))
        self.assertNotIn("secrets.", str(self.step("Check listing files (fail closed)")))

    def test_hash_locked_install_and_script_invocation(self):
        run = self.step("Install PyJWT and requests (hash-locked)")["run"]
        self.assertIn("--require-hashes", run)
        self.assertIn("--only-binary=:all:", run)
        self.assertIn("requirements-asc.txt", run)
        script = self.step("Run listing script")
        self.assertIn("asc_listing.py", script["run"])
        self.assertIn('"$MODE"', script["run"])
        self.assertEqual(script["env"]["ASC_KEY_PATH"], "${{ runner.temp }}/asc-key.p8")

    def test_secrets_only_in_clone_and_key_steps(self):
        found = {}
        for s in self.steps:
            for name in re.findall(r"secrets\.([A-Z0-9_]+)", str(s)):
                found.setdefault(s["name"], set()).add(name)
        self.assertEqual(set(found), {"Clone source at the requested commit", "Prepare App Store Connect key"})
        self.assertEqual(found["Clone source at the requested commit"], {"FORGEJO_HOST", "FORGEJO_CLONE_TOKEN"})
        self.assertEqual(found["Prepare App Store Connect key"],
                         SECRET_NAMES - {"FORGEJO_HOST", "FORGEJO_CLONE_TOKEN"})
        self.assertNotIn("secrets.", self.step("Run listing script")["run"])

    def test_key_is_mode_600_and_removed_in_always_step(self):
        run = self.step("Prepare App Store Connect key")["run"]
        self.assertIn("0o600", run)
        self.assertIn("chmod 600", run)
        cleanup = self.step("Remove the App Store Connect key")
        self.assertEqual(cleanup["if"], "${{ always() }}")
        self.assertIn('rm -f "$RUNNER_TEMP/asc-key.p8"', cleanup["run"])
        self.assertEqual(self.index("Remove the App Store Connect key"), len(self.steps) - 1)

    def test_no_job_output_or_summary_can_carry_listing_text(self):
        self.assertNotIn("GITHUB_STEP_SUMMARY", self.text)
        self.assertNotIn("upload-artifact", self.text)


if __name__ == "__main__":
    unittest.main()
