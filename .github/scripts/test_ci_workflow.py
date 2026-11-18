"""Structure checks for ios-shared-validate.yml: sharding must keep the gate's contract.
Run: python3 -m unittest discover -s .github/scripts   (skipped when PyYAML is unavailable)"""
import json
import os
import re
import subprocess
import tempfile
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ios-shared-validate.yml")
VALIDATE_UNIT_STEP = "Run XCTest unit tests (no retry)"
VALIDATE_UI_STEP = "Run XCTest UI tests (shard 0, retry once)"
RETRY_FLAGS = "-retry-tests-on-failure -test-iterations 2"
SHARED_STEPS = [
    "Clone source from private host",
    "Set up JDK 21",
    "macOS preflight",
    "Install XcodeGen (pinned)",
    "Generate Xcode project",
    "Screenshot tour did not run in the public job",
    "Decompress evidence for the secret scan",
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
        validate_run = self.steps(self.jobs["validate"])[VALIDATE_UI_STEP]["run"]
        self.assertNotIn("-only-testing:RadioShakeTests", validate_run)
        self.assertIn("-skip-testing:RadioShakeUITests/ScreenshotTourUITests", validate_run)
        unit_run = self.steps(self.jobs["validate"])[VALIDATE_UNIT_STEP]["run"]
        self.assertIn("-only-testing:RadioShakeTests", unit_run)
        self.assertNotIn("-only-testing:RadioShakeUITests", unit_run)

    GUARD = "Screenshot tour did not run in the public job"

    def test_only_ui_test_invocations_retry(self):
        ui = [self.steps(self.jobs["validate"])[VALIDATE_UI_STEP]["run"],
              self.steps(self.jobs["ui-shard"])["Run XCTest UI tests (shard)"]["run"]]
        for run in ui:
            self.assertIn(RETRY_FLAGS, run)
        unit = self.steps(self.jobs["validate"])[VALIDATE_UNIT_STEP]["run"]
        self.assertNotIn("-retry-tests-on-failure", unit)
        self.assertNotIn("-test-iterations", unit)
        # No other xcodebuild test invocation may carry retry flags.
        for name, step in self.steps(self.jobs["validate"]).items():
            if name != VALIDATE_UI_STEP:
                self.assertNotIn("-retry-tests-on-failure", step.get("run", ""), name)

    def test_unit_log_and_bundle_are_kept_and_storefront_reads_unit_log(self):
        unit = self.steps(self.jobs["validate"])[VALIDATE_UNIT_STEP]["run"]
        self.assertIn("tee /tmp/ios-logs/xctest-unit.log", unit)
        self.assertIn("/tmp/ios-app-unit-test.xcresult", self.wf["jobs"]["validate"]["env"]["EVIDENCE_PATHS"])
        sk = self.steps(self.jobs["validate"])["StoreKit storefront test ran and was not skipped"]["run"]
        self.assertIn("LOG=/tmp/ios-logs/xctest-unit.log", sk)

    def test_guards_scan_every_xctest_log(self):
        for job in ("validate", "ui-shard"):
            self.assertIn("/tmp/ios-logs/xctest*.log", self.steps(self.jobs[job])[self.GUARD]["run"], job)

    def test_both_jobs_report_retried_tests_without_gating_on_success(self):
        for job in ("validate", "ui-shard"):
            step = self.steps(self.jobs[job])["Report UI tests that needed a retry"]
            self.assertEqual(step["if"], "always()")
            self.assertIn("report_flaky_tests.py --log /tmp/ios-logs/xctest.log", step["run"])
            self.assertNotIn("continue-on-error", step)

    def test_both_jobs_skip_the_store_screenshot_class(self):
        shard = self.steps(self.jobs["ui-shard"])["Run XCTest UI tests (shard)"]["run"]
        validate = self.steps(self.jobs["validate"])[VALIDATE_UI_STEP]["run"]
        for run in (shard, validate):
            self.assertIn("-skip-testing:RadioShakeUITests/ScreenshotTourUITests", run)
            self.assertIn("-skip-testing:RadioShakeUITests/StoreScreenshotsUITests", run)

    def test_both_guards_fail_if_the_store_class_ran(self):
        for job in ("validate", "ui-shard"):
            run = self.steps(self.jobs[job])[self.GUARD]["run"]
            self.assertIn("ScreenshotTourUITests", run, job)
            self.assertRegex(run, r"grep -qE .*StoreScreenshotsUITests", job)
            self.assertIn("StoreScreenshotsUITests ran in the public job", run, job)
            self.assertIn("exit 1", run, job)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class A11yGateWorkflowTests(unittest.TestCase):
    """A11y-1: per-screen accessibility audit gate wiring."""

    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as fh:
            cls.wf = yaml.safe_load(fh)
        cls.jobs = cls.wf["jobs"]
        cls.steps = {s["name"]: s for s in cls.jobs["validate"]["steps"] if "name" in s}
        cls.order = [s.get("name") for s in cls.jobs["validate"]["steps"]]
        cls.step = cls.steps.get("iOS accessibility source checks (A11y)")

    def test_a11y_step_follows_privacy_step_and_is_guarded(self):
        self.assertIsNotNone(self.step)
        self.assertEqual(self.order.index("iOS accessibility source checks (A11y)"),
                         self.order.index("iOS privacy source checks (TASK-14)") + 1)
        self.assertEqual(self.step["working-directory"], "src")
        run = self.step["run"]
        self.assertIn('if [ ! -f scripts/check_ios_a11y_inventory.py ]; then', run)
        self.assertLess(run.index("scripts/check_ios_a11y_inventory.py ]"), run.index("python3 "))
        self.assertIn("scripts/check_ios_a11y_sources.py ios/App ios/a11y/sources-allow.json ios/a11y/inventory.json", run)
        self.assertIn('"${BASE_ARGS[@]}"', run)

    def test_base_args_are_only_added_conditionally_and_empty_array_safe(self):
        run = self.step["run"]
        self.assertIn("BASE_ARGS=()", run)
        for flag, f in (("--base-inventory", "inventory.json"), ("--base-exclusions", "exclusions.swift")):
            self.assertEqual(run.count(flag), 1, flag)
            line = [l for l in run.splitlines() if flag in l][0]
            self.assertIn("BASE_ARGS+=(", line)
            self.assertTrue(line.strip().startswith("if [ -f"), flag)
            self.assertIn(f, line)
        self.assertIn('${BASE_ARGS[@]+"${BASE_ARGS[@]}"}', run)
        self.assertNotIn('"${BASE_ARGS[@]}"', run.replace('${BASE_ARGS[@]+"${BASE_ARGS[@]}"}', ""))

    def test_clone_token_never_reaches_a_step_that_runs_branch_code(self):
        allowed = {"Clone source from private host", "Fetch A11y base files from main",
                   "Secret scan of evidence artifacts"}
        for st in self.jobs["validate"]["steps"]:
            if "secrets.FORGEJO_CLONE_TOKEN" in yaml.safe_dump(st):
                self.assertIn(st.get("name"), allowed, st.get("name"))
        self.assertNotIn("env", self.step)

    def test_base_fetch_step_runs_early_and_runs_only_git(self):
        name = "Fetch A11y base files from main"
        self.assertIn(name, self.steps)
        idx = self.order.index(name)
        self.assertEqual(idx, self.order.index("Clone source from private host") + 1)
        for i, st in enumerate(self.jobs["validate"]["steps"]):
            if str(st.get("working-directory", "")).startswith("src") or "python3" in st.get("run", "") \
                    or "scripts/" in st.get("run", ""):
                self.assertGreater(i, idx, st.get("name"))
        run = self.steps[name]["run"]
        self.assertNotIn("scripts/", run)
        self.assertNotIn("python3", run)
        self.assertIn("GIT_CONFIG_GLOBAL=/dev/null", run)
        self.assertIn("GIT_CONFIG_NOSYSTEM=1", run)
        self.assertIn("a11y-base", run)
        self.assertEqual(set(self.steps[name]["env"]), {"HOST", "TOKEN"})

    def test_release_deny_list_has_the_new_flags(self):
        run = self.steps["Audit Release binary for debug-only symbols (TASK-136 + TASK-137)"]["run"]
        deny = re.search(r"DENY='([^']*)'", run).group(1)
        for flag in ("RSForceIncreaseContrast", "RSForceBoldText"):
            self.assertIn(flag, deny.split("|"))

    def test_validate_no_longer_carries_the_audit_matrix(self):
        on = self.wf.get("on", self.wf.get(True))
        self.assertNotIn("a11y_matrix", on["workflow_dispatch"]["inputs"])
        self.assertNotIn("a11y_matrix", self.jobs["resolve"]["outputs"])
        self.assertNotIn("a11y-full", self.jobs)
        text = open(WORKFLOW, encoding="utf-8").read()
        for gone in ("a11y_matrix", "A11Y_MATRIX", "a11y-full", "TEST_RUNNER_RS_A11Y_MATRIX"):
            self.assertNotIn(gone, text)


AUDIT_WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ios-a11y-audit.yml")


@unittest.skipIf(yaml is None, "PyYAML not installed")
class A11yAuditWorkflowTests(unittest.TestCase):
    """Nightly / weekly / manual per-screen accessibility audit workflow."""

    @classmethod
    def setUpClass(cls):
        with open(AUDIT_WORKFLOW, encoding="utf-8") as fh:
            cls.text = fh.read()
        cls.wf = yaml.safe_load(cls.text)
        cls.on = cls.wf.get("on", cls.wf.get(True))
        cls.jobs = cls.wf["jobs"]
        cls.resolve = {s.get("id") or s.get("name"): s for s in cls.jobs["resolve"]["steps"]}
        cls.audit = cls.jobs["audit"]
        cls.asteps = {s["name"]: s for s in cls.audit["steps"] if "name" in s}
        cls.aorder = [s.get("name") for s in cls.audit["steps"]]

    def test_schedules_and_dispatch_inputs(self):
        crons = [c["cron"] for c in self.on["schedule"]]
        self.assertEqual(sorted(crons), ["17 5 * * 0", "41 1 * * *"])
        inp = self.on["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inp), {"ref", "sha", "matrix"})
        self.assertEqual(inp["ref"]["default"], "main")
        self.assertEqual(inp["matrix"]["type"], "choice")
        self.assertEqual(inp["matrix"]["options"], ["gate", "full"])
        self.assertEqual(inp["matrix"]["default"], "gate")

    def test_concurrency_keyed_on_the_resolved_ref_without_cancelling(self):
        c = self.wf["concurrency"]
        self.assertTrue(c["group"].startswith("ios-a11y-audit-"))
        self.assertIs(c["cancel-in-progress"], False)

    def test_resolve_reads_inputs_through_env_and_validates_them(self):
        st = [s for s in self.jobs["resolve"]["steps"] if s.get("id") == "resolve"][0]
        run = st["run"]
        self.assertNotIn("${{", run)
        self.assertIn("github.event.schedule", yaml.safe_dump(st["env"]))
        self.assertIn("'^(gate|full)$'", run)
        self.assertRegex(run, r"ref must match")
        self.assertIn("17 5 * * 0", yaml.safe_dump(st["env"]) + run)
        for out in ("ref", "sha", "matrix", "passes", "device", "context"):
            self.assertIn(out, self.jobs["resolve"]["outputs"], out)

    def test_pass_lists_and_devices_per_matrix(self):
        run = [s for s in self.jobs["resolve"]["steps"] if s.get("id") == "resolve"][0]["run"]
        self.assertIn('["defaultLight","ax5DarkBold"]', run)
        self.assertIn('["defaultDark","ax5Light","contrastLight","contrastDark","xSmallLight"]', run)
        self.assertIn("iPhone SE (3rd generation)", run)
        self.assertIn("iPhone 17", run)
        for ctx in ("ios-a11y-nightly", "ios-a11y-weekly", "ios-a11y"):
            self.assertIn(ctx, run)

    def test_audit_job_shape(self):
        self.assertEqual(self.audit["runs-on"], "macos-26")
        self.assertEqual(self.audit["timeout-minutes"], 240)
        st = self.audit["strategy"]
        self.assertEqual(st["max-parallel"], 2)
        self.assertIs(st["fail-fast"], False)
        self.assertIn("needs.resolve.outputs.passes", st["matrix"]["pass"])
        self.assertIn("fromJSON", st["matrix"]["pass"])

    def test_audit_classes_are_found_right_after_the_clone_and_gate_the_build(self):
        """A source ref without audit classes (main before A11y-1 merges) must not spend a macOS build."""
        find = "Find audit classes in the source"
        self.assertIn(find, self.asteps)
        self.assertEqual(self.aorder.index(find), self.aorder.index("Clone source from private host") + 1)
        step = self.asteps[find]
        self.assertEqual(step.get("id"), "classes")
        self.assertNotIn("env", step, "the discovery step holds no secrets")
        self.assertIn("^final class +A11yAudit[A-Za-z0-9_]*UITests", step["run"])
        self.assertIn('present=', step["run"])
        gated = ["Set up JDK 21", "macOS preflight", "Install XcodeGen (pinned)",
                 "Assemble shared Debug XCFramework and point symlink at it", "Generate Xcode project",
                 "Boot simulator", "Run accessibility audit passes", "Verify every selected audit test ran"]
        for name in gated:
            self.assertIn("steps.classes.outputs.present == 'true'", str(self.asteps[name].get("if", "")), name)

    def test_test_step_selects_methods_per_audit_class_without_retries(self):
        run = self.asteps["Run accessibility audit passes"]["run"]
        self.assertIn("^final class +A11yAudit[A-Za-z0-9_]*UITests", run)
        self.assertIn("testGate", run)
        self.assertIn("testFull", run)
        self.assertNotIn("-retry-tests-on-failure", self.text)
        self.assertNotIn("-test-iterations", self.text)
        self.assertEqual(run.count("TEST_RUNNER_RS_A11Y_MATRIX=full"), 1)
        self.assertRegex(run, r'if \[ "\$MATRIX" = "full" \]; then\s+export TEST_RUNNER_RS_A11Y_MATRIX=full')
        self.assertIn("skipping", run)
        self.assertIn("A11yAudit", run)

    def test_every_selected_audit_test_must_have_run_and_not_been_skipped(self):
        step = self.asteps["Verify every selected audit test ran"]
        run = self.asteps["Run accessibility audit passes"]["run"]
        self.assertIn("/tmp/a11y-only-args.txt", run)
        self.assertEqual(self.aorder.index("Verify every selected audit test ran"),
                         self.aorder.index("Run accessibility audit passes") + 1)
        self.assertEqual(step["if"], "success()")
        self.assertNotIn("working-directory", step)
        self.assertIn("python3 ci/.github/scripts/check_shard_ran.py", step["run"])
        self.assertIn("--args /tmp/a11y-only-args.txt", step["run"])
        self.assertLess(self.aorder.index("Check out CI scripts (this repo, scripts only)"),
                        self.aorder.index("Run accessibility audit passes"))
        self.assertNotIn("src/.github", step["run"])

    def test_unknown_schedule_fails_loudly(self):
        run = [s for s in self.jobs["resolve"]["steps"] if s.get("id") == "resolve"][0]["run"]
        self.assertIn("unknown schedule", run)
        self.assertIn("17 5 * * 0", run)
        self.assertIn("41 1 * * *", run)

    def test_report_job_has_a_timeout(self):
        self.assertEqual(self.jobs["report"]["timeout-minutes"], 15)

    def test_issue_summary_is_extracted_and_uploaded(self):
        text = "\n".join(s.get("run", "") for s in self.audit["steps"])
        self.assertIn("error: -[RadioShakeUITests.A11yAudit", text)
        up = [s for s in self.audit["steps"] if s.get("uses", "").startswith("actions/upload-artifact")]
        self.assertTrue(up)
        self.assertIn("a11y-issues", yaml.safe_dump(up))

    def test_clone_token_only_in_clone_fetch_and_secret_scan(self):
        allowed = {"Clone source from private host", "Secret scan of evidence artifacts"}
        for jname, job in self.jobs.items():
            for st in job["steps"]:
                if "FORGEJO_CLONE_TOKEN" in yaml.safe_dump(st):
                    ok = (jname == "resolve" and st.get("id") == "resolve") or \
                        (jname == "audit" and st.get("name") in allowed)
                    self.assertTrue(ok, f"{jname}/{st.get('name')}")
        for st in self.audit["steps"]:
            if st.get("name") in ("Clone source from private host", "Secret scan of evidence artifacts"):
                continue
            self.assertNotIn("TOKEN", yaml.safe_dump(st.get("env", {})), st.get("name"))
        self.assertNotIn("FORGEJO_CLONE_TOKEN", yaml.safe_dump(self.wf.get("env", {})))
        self.assertNotIn("FORGEJO_CLONE_TOKEN", yaml.safe_dump(self.audit.get("env", {})))
        clone = self.asteps["Clone source from private host"]
        self.assertEqual(self.aorder.index("Clone source from private host"), 0)
        self.assertIn("auth header was persisted", clone["run"])

    def test_clone_and_scan_steps_match_validate(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            v = {s["name"]: s for s in yaml.safe_load(fh)["jobs"]["ui-shard"]["steps"] if "name" in s}
        for name in ("Clone source from private host", "Set up JDK 21", "Install XcodeGen (pinned)",
                     "Generate Xcode project", "Decompress evidence for the secret scan",
                     "Secret scan of evidence artifacts", "Check out CI scripts (this repo, scripts only)"):
            keys = ("run", "uses", "with", "shell", "if", "id") if name.startswith("Clone") \
                else ("run", "uses", "with", "env", "shell", "if", "id")
            for key in keys:
                self.assertEqual(v[name].get(key), self.asteps[name].get(key), f"{name}: {key}")

    def test_full_matrix_creates_the_se_simulator_from_the_resolved_device(self):
        st = self.asteps["Boot simulator"]
        self.assertIn("needs.resolve.outputs.device", st["env"]["DEVICE"])
        self.assertIn("simctl create", st["run"])

    def test_report_job_posts_the_right_contexts_and_never_fails(self):
        rep = self.jobs["report"]
        self.assertEqual(rep["if"], "always()")
        self.assertIn("audit", rep["needs"])
        self.assertIn("resolve", rep["needs"])
        run = rep["steps"][0]["run"]
        env = yaml.safe_dump(rep["steps"][0]["env"])
        self.assertIn("needs.resolve.outputs.context", env)
        self.assertIn("needs.audit.result", env)
        self.assertIn('"success"', run)
        self.assertNotIn("exit 1", run)
        self.assertIn("::warning::", run)
        self.assertIn("STATUS_TOKEN", env)
        self.assertNotIn("FORGEJO_CLONE_TOKEN", yaml.safe_dump(rep))


HARD_SHELL = "/bin/bash --noprofile --norc -p -eo pipefail {0}"
TOKEN_RE = re.compile(r"secrets\.FORGEJO_(CLONE|STATUS)_TOKEN")


@unittest.skipIf(yaml is None, "PyYAML not installed")
class TokenStepHardeningTests(unittest.TestCase):
    """A token-holding step that runs after branch code must not be hijackable via BASH_ENV,
    GITHUB_PATH or Homebrew binaries."""

    def jobs_under_test(self):
        out = []
        for path, names in ((WORKFLOW, ("validate", "ui-shard")), (AUDIT_WORKFLOW, ("audit",))):
            with open(path, encoding="utf-8") as fh:
                wf = yaml.safe_load(fh)
            for n in names:
                out.append((os.path.basename(path), n, wf["jobs"][n]["steps"]))
        return out

    def test_token_steps_after_branch_code_are_hardened(self):
        for wf, job, steps in self.jobs_under_test():
            names = [s.get("name") for s in steps]
            ci = names.index("Check out CI scripts (this repo, scripts only)")
            checked = 0
            for i, st in enumerate(steps):
                if i <= ci or not TOKEN_RE.search(yaml.safe_dump(st)):
                    continue
                checked += 1
                where = f"{wf}/{job}/{st.get('name')}"
                self.assertEqual(st.get("shell"), HARD_SHELL, where)
                lines = [l.strip() for l in st["run"].splitlines() if l.strip() and not l.strip().startswith("#")]
                self.assertEqual(lines[0], "PATH=/usr/bin:/bin:/usr/sbin:/sbin; export PATH", where)
                self.assertIn("unset DEVELOPER_DIR", st["run"], where)
                self.assertNotIn("brew", st["run"], where)
                self.assertNotRegex(st["run"], r"(?m)^\s*python3\b", where)
                self.assertIn("/usr/bin/python3 -I", st["run"], where)
                self.assertNotIn("zstd", st["run"].replace("zstd-decompressed", ""), where)
            self.assertGreaterEqual(checked, 1, f"{wf}/{job}")

    def test_decompress_step_holds_no_tokens_and_precedes_the_scan(self):
        for wf, job, steps in self.jobs_under_test():
            names = [s.get("name") for s in steps]
            d = names.index("Decompress evidence for the secret scan")
            self.assertEqual(names.index("Secret scan of evidence artifacts"), d + 1)
            self.assertNotIn("TOKEN", yaml.safe_dump(steps[d]))
            self.assertEqual(steps[d]["if"], "always()")

    def run_hard(self, script_text, env=None):
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
            fh.write(script_text)
        self.addCleanup(os.unlink, fh.name)
        cmd = [a.replace("{0}", fh.name) for a in HARD_SHELL.split()]
        full = {"PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir()}
        full.update(env or {})
        return subprocess.run(cmd, env=full, capture_output=True, text=True)

    @unittest.skipUnless(os.path.exists("/bin/bash"), "no /bin/bash")
    def test_hard_shell_string_is_valid_bash_privileged_and_ignores_bash_env(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as be:
            be.write("echo BASH_ENV_SOURCED\n")
        self.addCleanup(os.unlink, be.name)
        r = self.run_hard('[[ $- == *p* ]] && echo privileged-on\n', {"BASH_ENV": be.name})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("privileged-on", r.stdout)
        self.assertNotIn("BASH_ENV_SOURCED", r.stdout)

    def scan_run(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            steps = yaml.safe_load(fh)["jobs"]["validate"]["steps"]
        return [s for s in steps if s.get("name") == "Secret scan of evidence artifacts"][0]["run"]

    def test_scan_interpreter_gets_an_allowlisted_environment(self):
        for wf, job, steps in self.jobs_under_test():
            scan = [s for s in steps if s.get("name") == "Secret scan of evidence artifacts"][0]
            run = scan["run"]
            self.assertIn("/usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin", run, wf)
            self.assertIn("/usr/bin/python3 -I -", run, wf)
            for var in ("HOME=", "EVIDENCE_PATHS=", "FORGEJO_CLONE_TOKEN=", "FORGEJO_STATUS_TOKEN="):
                self.assertIn(var, run, wf)
            self.assertNotIn("TOOLCHAINS=", run)
            self.assertIn("trap '/bin/rm -rf /tmp/scan-decompressed' EXIT", run, wf)
            self.assertLess(run.index("trap "), run.index("/usr/bin/env -i"))

    def run_scan(self, files, manifest=None):
        """Run the real scan step script (hardened shell) against a temp evidence dir."""
        d = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", d, "/tmp/scan-decompressed"])
        for name, data in files.items():
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(data)
        subprocess.run(["rm", "-rf", "/tmp/scan-decompressed"])
        if manifest is not None:
            os.makedirs("/tmp/scan-decompressed")
            items = []
            for name, extra in manifest.items():
                dest = os.path.join("/tmp/scan-decompressed", name)
                with open(dest, "wb") as fh:
                    fh.write(extra)
                items.append({"label": os.path.join(d, name), "file": dest, "source": os.path.join(d, name)})
            with open("/tmp/scan-decompressed/manifest.json", "w") as fh:
                json.dump({"items": items, "failed": []}, fh)
        env = {"EVIDENCE_PATHS": d + "/\n", "FORGEJO_CLONE_TOKEN": "tok-clone-123",
               "FORGEJO_STATUS_TOKEN": "tok-status-456"}
        return self.run_hard(self.scan_run(), env)

    @unittest.skipUnless(os.path.exists("/usr/bin/python3") and os.path.exists("/bin/bash"), "no system python/bash")
    def test_scan_step_script_executes_and_detects_leaks_and_unlisted_zstd_blobs(self):
        magic = b"\x28\xb5\x2f\xfd"
        r = self.run_scan({"a.log": b"clean"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK - no secret", r.stdout)
        self.assertFalse(os.path.exists("/tmp/scan-decompressed"), "trap must remove the decompressed copies")
        r = self.run_scan({"a.log": b"x tok-status-456 y"})
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("tok-status-456", r.stdout + r.stderr)
        # a zstd-magic file with no manifest entry fails even with no xcresult bundle present
        r = self.run_scan({"b.bin": magic + b"junk"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("not in decompression manifest", r.stdout)
        # listed in the manifest: scanned (clean) and accepted; a leak in the decompressed copy is caught
        r = self.run_scan({"b.bin": magic + b"junk"}, manifest={"b.bin": b"decoded clean"})
        self.assertEqual(r.returncode, 0, r.stdout)
        r = self.run_scan({"b.bin": magic + b"junk"}, manifest={"b.bin": b"decoded tok-clone-123"})
        self.assertEqual(r.returncode, 1)

    def test_scan_step_reads_decompressed_manifest_not_zstd(self):
        for wf, job, steps in self.jobs_under_test():
            scan = [s for s in steps if s.get("name") == "Secret scan of evidence artifacts"][0]
            self.assertIn("manifest.json", scan["run"])


SCREENSHOTS_WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ios-screenshots.yml")


@unittest.skipIf(yaml is None, "PyYAML not installed")
class ScreenshotsWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SCREENSHOTS_WORKFLOW, encoding="utf-8") as fh:
            cls.wf = yaml.safe_load(fh)
        cls.on = cls.wf.get("on", cls.wf.get(True))
        cls.steps = {s["name"]: s for s in cls.wf["jobs"]["tour"]["steps"] if "name" in s}

    def test_tour_job_allows_an_ipad_tour_to_finish(self):
        # A full iPad (A16) tour ran past 60 minutes and was cancelled (run 41353769395).
        self.assertGreaterEqual(self.wf["jobs"]["tour"]["timeout-minutes"], 120)

    def test_suite_input_is_a_tour_store_choice_defaulting_to_tour(self):
        suite = self.on["workflow_dispatch"]["inputs"]["suite"]
        self.assertEqual(suite["type"], "choice")
        self.assertEqual(suite["options"], ["tour", "store"])
        self.assertEqual(suite["default"], "tour")
        self.assertFalse(suite.get("required", False))

    def test_run_step_selects_class_by_suite(self):
        step = self.steps["Run screenshot tour"]
        self.assertIn("inputs.suite", step["env"]["SUITE"])
        run = step["run"]
        self.assertIn("-only-testing:RadioShakeUITests/ScreenshotTourUITests", run)
        self.assertIn("-only-testing:RadioShakeUITests/StoreScreenshotsUITests", run)
        self.assertIn("store", run)

    def test_store_shots_become_srgb_jpeg_before_encryption(self):
        step = self.steps["Export and encrypt screenshots, delete plaintext"]
        self.assertIn("SUITE", step["env"])
        run = step["run"]
        self.assertIn("store-", run)
        self.assertIn("region-notice", run)
        self.assertIn("sips -m '/System/Library/ColorSync/Profiles/sRGB Profile.icc' "
                      "-s format jpeg -s formatOptions 95", run)
        export = run.index("xcresulttool export attachments")
        sips = run.index("sips -m")
        tar = run.index("tar -czf")
        enc = run.index("enc -aes-256-cbc")
        self.assertLess(export, sips)
        self.assertLess(sips, tar)
        self.assertLess(sips, enc)

    def test_screenshots_only_leave_through_the_encrypted_upload(self):
        uploads = [(n, s) for n, s in self.steps.items() if "upload-artifact" in s.get("uses", "")]
        self.assertEqual(len(uploads), 1)
        paths = uploads[0][1]["with"]["path"].split()
        self.assertEqual(sorted(paths), ["/tmp/rs-enc/key.enc", "/tmp/rs-enc/shots.tgz.enc"])
        for step in self.wf["jobs"]["tour"]["steps"]:
            self.assertNotIn("upload-artifact", step.get("uses", "") if step is not uploads[0][1] else "")
        # converted JPEGs are written inside the directory that is tarred and then deleted
        run = self.steps["Export and encrypt screenshots, delete plaintext"]["run"]
        self.assertIn("rm -rf /tmp/rs-key.hex /tmp/rs-shots.tgz /tmp/rs-shots", run)


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
        self.assertEqual(self.text.count("feat/ios-early-review"), run.count("feat/ios-early-review"))
        self.assertEqual(self.text.count("refs/remotes/origin/main"), run.count("refs/remotes/origin/main"))
        self.assertIn("merge-base --is-ancestor", run)
        self.assertIn("refs/remotes/origin/main", run)
        self.assertLess(run.index('"$MODE" = "plan"'), run.index("feat/ios-early-review"))
        self.assertLess(run.index("refs/remotes/origin/main"), run.index('"$MODE" = "plan"'))
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

    def test_secrets_only_in_clone_key_and_run_env(self):
        found = {}
        for s in self.steps:
            for name in re.findall(r"secrets\.([A-Z0-9_]+)", str(s)):
                found.setdefault(s["name"], set()).add(name)
        self.assertEqual(set(found), {"Clone source at the requested commit", "Prepare App Store Connect key",
                                      "Run listing script"})
        self.assertEqual(found["Clone source at the requested commit"], {"FORGEJO_HOST", "FORGEJO_CLONE_TOKEN"})
        self.assertEqual(found["Prepare App Store Connect key"], {"APP_STORE_CONNECT_PRIVATE_KEY_B64"})
        self.assertEqual(found["Run listing script"], {"APP_STORE_CONNECT_KEY_ID", "APP_STORE_CONNECT_ISSUER_ID"})
        self.assertTrue(set().union(*found.values()) <= SECRET_NAMES)
        for name in found:
            self.assertNotIn("secrets.", self.step(name)["run"], name)

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
