"""Static safety contract for the macOS iOS framework validation workflow."""

from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest

import yaml


WORKFLOW_PATH = Path(__file__).parents[1] / ".github/workflows/ios-shared-validate.yml"


class IosSharedValidateWorkflowTest(unittest.TestCase):
    mandatory_gate_ids = (
        "clone",
        "toolchain_runtime",
        "setup_java",
        "gradle_jdk_config",
        "gradle_evidence",
        "link_frameworks",
        "archive_validation",
        "header_audit",
        "swift_typecheck",
        "ios_debug_tests",
        "ios_release_tests",
        "android_sdk",
        "regression_tests",
        "reports_copy",
        "evidence_bundle",
        "upload_evidence",
    )

    @classmethod
    def setUpClass(cls):
        cls.workflow_text = WORKFLOW_PATH.read_text()
        cls.workflow = yaml.safe_load(cls.workflow_text)
        cls.on = cls.workflow.get("on", cls.workflow.get(True))
        cls.job = cls.workflow["jobs"]["ios-validate"]
        cls.steps = cls.job["steps"]
        cls.upload = next(
            step for step in cls.steps
            if step.get("uses", "").startswith("actions/upload-artifact@")
        )
        cls.clone = next(step for step in cls.steps if step.get("id") == "clone")
        cls.steps_by_id = {
            step["id"]: step for step in cls.steps if "id" in step
        }
        cls.step_index = {
            step["id"]: index for index, step in enumerate(cls.steps) if "id" in step
        }
        cls.upload_path = cls.upload["with"]["path"]

    @classmethod
    def run_for(cls, step_id):
        return "\n".join(
            line
            for line in cls.steps_by_id[step_id]["run"].splitlines()
            if not line.lstrip().startswith("#")
        )

    def assert_block(self, body, pattern):
        """Match a live, line-anchored shell block rather than disconnected text."""
        self.assertRegex(body, re.compile(pattern, re.MULTILINE))

    @classmethod
    def archive_member_validation_result(cls, member):
        """Run the workflow's live member-rejection block against one archive member."""
        archive_run = cls.run_for("archive_validation")
        validation_block = re.search(
            r'''^\s*if unsafe_members=\$\(grep -vE .*?^\s*fi$''',
            archive_run,
            re.MULTILINE | re.DOTALL,
        )
        if validation_block is None:
            raise AssertionError("archive member validation block was not found")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            members_file = temp_path / "members.txt"
            archive_report = temp_path / "archive-report.txt"
            members_file.write_text(member + "\n", encoding="utf-8")
            return subprocess.run(
                [
                    "bash",
                    "-ceu",
                    'members_file="$1"\narchive_report="$2"\n' + validation_block.group(),
                    "archive-member-validation",
                    str(members_file),
                    str(archive_report),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_dispatches_require_nonempty_ref_and_exact_sha(self):
        """A dispatch without an exact ref/SHA must not clone or report a different commit."""
        manual = self.on["workflow_dispatch"]["inputs"]
        self.assertTrue(manual["ref"]["required"])
        self.assertTrue(manual["sha"]["required"])
        self.assertNotIn("default", manual["ref"])
        self.assertNotIn("default", manual["sha"])
        self.assertIn("github.event.client_payload.ref", self.clone["env"]["REF"])
        self.assertIn("github.event.client_payload.sha", self.clone["env"]["WANTED_SHA"])
        clone_run = self.run_for("clone")
        self.assert_block(
            clone_run,
            r'''^\s*if \[ -z "\$REF" \] \|\| \[ -z "\$WANTED_SHA" \]; then$
\s*echo .*Both ref and sha are required.*$
\s*exit 1$
\s*fi$
\s*if ! git check-ref-format --branch "\$REF" > /dev/null; then$
\s*echo .*Requested ref is not a valid branch or tag name.*$
\s*exit 1$
\s*fi$
\s*if ! printf '%s' "\$WANTED_SHA" \| grep -Eq '\^\[0-9a-f\]\{40\}\$'; then$''',
        )
        self.assert_block(
            clone_run,
            r'''^\s*AUTH=\$\(printf 'oauth2:%s' "\$TOKEN" \| base64\)$
\s*echo "::add-mask::\$AUTH"$
\s*git -c http\.extraheader="AUTHORIZATION: basic \$AUTH" \\
\s*clone --depth 1 --branch "\$REF" "https://\$\{HOST\}/hiten/RadioShake\.git" src$
\s*ACTUAL_SHA=\$\(git -C src rev-parse HEAD\)$
\s*echo "commit_sha=\$ACTUAL_SHA" >> "\$GITHUB_OUTPUT"$
\s*echo "Cloned requested ref at \$ACTUAL_SHA"$
\s*if \[ "\$ACTUAL_SHA" != "\$WANTED_SHA" \]; then$
\s*echo .*Cloned SHA does not equal the requested SHA.*$
\s*exit 1$
\s*fi$
\s*! grep -rq "AUTHORIZATION" src/\.git/config$''',
        )

    def test_sha_keyed_non_cancelling_concurrency(self):
        """Independent requested revisions must retain their evidence rather than canceling."""
        concurrency = self.workflow["concurrency"]
        self.assertIn("ios-shared-validate-${{", concurrency["group"])
        self.assertIn("github.event.client_payload.sha", concurrency["group"])
        self.assertIn("inputs.sha", concurrency["group"])
        self.assertFalse(concurrency["cancel-in-progress"])
        self.assertIn("cancel-in-progress: false", self.workflow_text)

    def test_four_static_archives_are_safely_inspected_for_arm64_objects(self):
        """Static framework archives cannot pass merely because an ar container exists."""
        archive_run = self.run_for("archive_validation")
        self.assert_block(
            archive_run,
            r'''^\s*find shared/build/bin -path '\*RadioShakeShared\.framework/RadioShakeShared' -type f -print \| sort > "\$archives_file"$
\s*COUNT=\$\(wc -l < "\$archives_file" \| tr -d ' '\)$
\s*if \[ "\$COUNT" -ne 4 \]; then$
\s*echo .*Expected exactly four static framework archives.*$
\s*exit 1$
\s*fi$''',
        )
        self.assert_block(
            archive_run,
            r'''^\s*file_info=\$\(file "\$archive"\)$
\s*printf '%s\\n' "\$file_info" \| tee -a "\$archive_report"$
\s*if ! printf '%s\\n' "\$file_info" \| grep -q 'current ar archive'; then$
\s*echo .*Framework binary is not an ar archive.*$
\s*exit 1$
\s*fi$
\s*shasum -a 256 "\$archive" >> "\$checksums_file"$
\s*extract_dir=\$\(mktemp -d "\$\{TMPDIR:-/tmp\}/task7-framework\.XXXXXX"\)$
\s*case "\$extract_dir" in
[\s\S]*?^\s*esac$
\s*cleanup_extract_dir\(\) \{$
\s*rm -rf -- "\$extract_dir"$
\s*\}$
\s*trap 'cleanup_extract_dir' EXIT HUP INT TERM$
\s*members_file="\$extract_dir/members\.txt"$''',
        )
        self.assert_block(
            archive_run,
            r'''^\s*if unsafe_members=\$\(grep -vE '\^\(__\\\.SYMDEF\|__\.SYMDEF SORTED\)\$' "\$members_file" \| grep -E '\(\^\|/\)\\\.\\\.\?\(\\/\|\$\)\|\[\^A-Za-z0-9\._: -\]'\); then$
\s*printf '%s\\n' "\$unsafe_members" \| tee -a "\$archive_report"$
\s*echo .*Static archive contains an unsafe member name.*$
\s*exit 1$
\s*fi$
\s*\(cd "\$extract_dir" && ar -x "\$archive"\)$
\s*object_count=0$''',
        )
        self.assert_block(
            archive_run,
            r'''^\s*object_info=\$\(file "\$object"\)$
\s*printf '%s\\n' "\$object_info" \| tee -a "\$archive_report"$
\s*if ! printf '%s\\n' "\$object_info" \| grep -q 'Mach-O 64-bit\.\*arm64'; then$
\s*object_archs=\$\(lipo -archs "\$object" 2>&1 \|\| true\)$
[\s\S]*?^\s*if ! printf '%s\\n' "\$object_archs" \| grep -qw 'arm64'; then$
\s*echo .*Static archive object is not arm64.*$
\s*exit 1$
\s*fi$
\s*fi$''',
        )
        self.assert_block(
            archive_run,
            r'''^\s*if \[ "\$object_count" -eq 0 \]; then$
\s*echo .*Static archive contains only symbol-table members.*$
\s*exit 1$
\s*fi$
\s*rm -rf -- "\$extract_dir"$
\s*trap - EXIT HUP INT TERM$''',
        )

    def test_export_contract_checksum_and_swift_evidence_are_complete(self):
        """The checked framework, its public API, and committed Swift sample are reproducible evidence."""
        header_run = self.run_for("header_audit")
        swift_run = self.run_for("swift_typecheck")
        toolchain_run = self.run_for("toolchain_runtime")
        self.assert_block(
            header_run,
            r'''^\s*HEADER=\$\(find shared/build/bin -path '\*iosSimulatorArm64/debugFramework/RadioShakeShared\.framework/Headers/RadioShakeShared\.h' -type f -print -quit\)$
\s*MODULEMAP=\$\(find shared/build/bin -path '\*iosSimulatorArm64/debugFramework/RadioShakeShared\.framework/Modules/module\.modulemap' -type f -print -quit\)$
\s*if \[ -z "\$HEADER" \] \|\| \[ -z "\$MODULEMAP" \]; then$
[\s\S]*?^\s*fi$
\s*cp "\$HEADER" "\$EVIDENCE_DIR/RadioShakeShared-header\.h"$
\s*cp "\$MODULEMAP" "\$EVIDENCE_DIR/RadioShakeShared-module\.modulemap"$
\s*shasum -a 256 "\$HEADER" "\$MODULEMAP" >> "\$EVIDENCE_DIR/task7-archive-checksums\.txt"$
\s*intended="\$EVIDENCE_DIR/task7-intended-signatures\.txt"$''',
        )
        self.assert_block(
            header_run,
            r'''^\s*awk '$
\s*/swift_name\\\("\(IosSharedFacade\|IosStation\|IosCancellation\|IosErrorCode\)/ \{ capture = 1 \}$
\s*capture \{ print \}$
\s*capture && /\^@end/ \{ capture = 0 \}$
\s*' "\$HEADER" > "\$intended"$''',
        )
        self.assert_block(
            header_run,
            r'''^\s*for symbol in IosSharedFacade IosStation IosCancellation IosErrorCode; do$
[\s\S]*?^\s*done$
\s*if \[ "\$\(grep -c 'swift_name\("Ios' "\$intended"\)" -ne 4 \]; then$
[\s\S]*?^\s*fi$
\s*for symbol in RadioBrowserApiService ShazamApiService StationDto DatabaseDriverFactory FavouriteRepository StationRepository SongLogRepository; do$
[\s\S]*?^\s*done$
\s*grep -oE 'RadioShakeDatabase\|FavouriteQueries\|StationQueries\|SongLogQueries' "\$HEADER" \| sort -u > "\$EVIDENCE_DIR/task7-generated-sqldelight-symbols\.txt" \|\| true$
\s*if grep -Eq 'RadioShakeDatabase\|FavouriteQueries\|StationQueries\|SongLogQueries' "\$intended"; then$''',
        )
        self.assert_block(
            header_run,
            r'''^\s*manifest_actual=\$\(mktemp "\$\{TMPDIR:-/tmp\}/task7-source-manifest\.XXXXXX"\)$
\s*rg -l '\^\(public \)\?\(data class\|class\|interface\|object\|fun\|val\|expect \(class\|fun\|val\)\) ' \\
[\s\S]*?shared/src/commonMain/kotlin/com/radioshake/shared/repository \| sort > "\$manifest_actual"$
\s*if \[ "\$\(wc -l < "\$manifest_actual" \| tr -d ' '\)" -ne 29 \]; then$
[\s\S]*?^\s*fi$
\s*if ! diff -u docs/ios/swift-export-source-manifest\.txt "\$manifest_actual"; then$''',
        )
        self.assert_block(
            swift_run,
            r'''^\s*contract=docs/ios/shared-framework-contract\.md$
\s*swift_source="\$EVIDENCE_DIR/Task7FacadeSmoke\.swift"$
\s*git show "HEAD:\$contract" > "\$EVIDENCE_DIR/task7-contract\.md"$
\s*if grep -q 'TASK7_SWIFT_START' "\$EVIDENCE_DIR/task7-contract\.md" && grep -q 'TASK7_SWIFT_END' "\$EVIDENCE_DIR/task7-contract\.md"; then$
\s*awk '/TASK7_SWIFT_START/ \{ capture = 1; next \} /TASK7_SWIFT_END/ \{ exit \} capture \{ print \}' "\$EVIDENCE_DIR/task7-contract\.md" > "\$swift_source"$
\s*else$
\s*awk '/\^```swift\[\[:space:\]\]\*\$/ \{ capture = 1; next \} capture && /\^```\[\[:space:\]\]\*\$/ \{ exit \} capture \{ print \}' "\$EVIDENCE_DIR/task7-contract\.md" > "\$swift_source"$
\s*fi$
\s*rm -f -- "\$EVIDENCE_DIR/task7-contract\.md"$
\s*if \[ ! -s "\$swift_source" \]; then$''',
        )
        self.assert_block(
            swift_run,
            r'''^\s*SDK_PATH=\$\(xcrun --sdk iphonesimulator --show-sdk-path\)$
\s*FRAMEWORK_PATH=shared/build/bin/iosSimulatorArm64/debugFramework$
\s*xcrun swiftc -typecheck "\$swift_source" \\
\s*-sdk "\$SDK_PATH" \\
\s*-target arm64-apple-ios17\.0-simulator \\
\s*-F "\$FRAMEWORK_PATH" \\
\s*-framework RadioShakeShared > "\$EVIDENCE_DIR/Task7FacadeSmoke\.typecheck\.txt" 2>&1$''',
        )
        self.assertIn("xcrun simctl list runtimes available", toolchain_run)
        self.assertIn("iosSimulatorArm64ReleasePolicyTest", self.run_for("gradle_evidence"))

    def test_release_followups_are_always_gated_by_prior_simulator_outcomes(self):
        """Release, SDK, and regression steps must retain their explicit outcome dependencies."""
        self.assertEqual(
            self.steps_by_id["ios_release_tests"]["if"],
            "always() && steps.ios_debug_tests.outcome != 'cancelled'",
        )
        for step_id in ("android_sdk", "regression_tests"):
            self.assertEqual(
                self.steps_by_id[step_id]["if"],
                "always() && steps.ios_release_tests.outcome != 'cancelled'",
            )

    def test_private_clone_and_evidence_artifact_stay_scoped(self):
        """No credentials, checkout, configuration, or cache can be exposed by the artifact."""
        self.assertIn("http.extraheader", self.run_for("clone"))
        self.assertNotIn("cat gradle.properties", self.run_for("gradle_jdk_config"))
        self.assertIn(
            "actions/setup-java@cf277c60eb25467037889841efdb72551f06f6c3",
            self.workflow_text,
        )
        self.assertIn(
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
            self.workflow_text,
        )
        for forbidden in ("src/", "gradle.properties", "local.properties", ".git", ".gradle", "cache"):
            self.assertNotIn(forbidden, self.upload_path)
        for required in (
            "task7-evidence",
            "RadioShakeShared-header.h",
            "RadioShakeShared-module.modulemap",
            "Task7FacadeSmoke.swift",
            "Task7FacadeSmoke.typecheck.txt",
            "task7-archive-checksums.txt",
            "task7-runtimes.txt",
            "task7-toolchain.txt",
            "task7-shared-tasks.txt",
            "gradle-reports",
        ):
            self.assertIn(required, self.upload_path)

    def test_mandatory_gates_have_stable_ids_and_fail_closed_status(self):
        """A skipped or failed required gate must make the external status fail."""
        self.assertTrue(set(self.mandatory_gate_ids).issubset(self.steps_by_id))
        self.assertIn("forgejo_status", self.steps_by_id)
        self.assertEqual(self.steps_by_id["evidence_bundle"]["if"], "always()")
        self.assertEqual(self.steps_by_id["upload_evidence"]["if"], "always()")
        self.assertEqual(self.steps_by_id["forgejo_status"]["if"], "always()")
        self.assertLess(self.step_index["reports_copy"], self.step_index["evidence_bundle"])
        self.assertLess(self.step_index["evidence_bundle"], self.step_index["upload_evidence"])
        self.assertLess(self.step_index["upload_evidence"], self.step_index["forgejo_status"])

        status = self.steps_by_id["forgejo_status"]
        for gate_id in self.mandatory_gate_ids:
            outcome_name = gate_id.upper()
            self.assertEqual(
                status["env"][outcome_name],
                "${{ steps." + gate_id + ".outcome }}",
            )
        self.assert_block(
            self.run_for("forgejo_status"),
            r'''^\s*for outcome in "\$CLONE" "\$TOOLCHAIN_RUNTIME" "\$SETUP_JAVA" "\$GRADLE_JDK_CONFIG" "\$GRADLE_EVIDENCE" "\$LINK_FRAMEWORKS" "\$ARCHIVE_VALIDATION" "\$HEADER_AUDIT" "\$SWIFT_TYPECHECK" "\$IOS_DEBUG_TESTS" "\$IOS_RELEASE_TESTS" "\$ANDROID_SDK" "\$REGRESSION_TESTS" "\$REPORTS_COPY" "\$EVIDENCE_BUNDLE" "\$UPLOAD_EVIDENCE"; do$
\s*if \[ "\$outcome" != success \]; then$
\s*OVERALL=failure$
\s*fi$
\s*done$
\s*DESCRIPTION="iOS shared framework: \$OVERALL"$
\s*curl -fsS -X POST \\$
[\s\S]*?^\s*-d "\{\\"context\\":\\"github/ios-validate\\",\\"state\\":\\"\$\{OVERALL\}\\",\\"target_url\\":\\"\$\{RUN_URL\}\\",\\"description\\":\\"\$\{DESCRIPTION\}\\"\}"$''',
        )

    def run_status_with_outcomes(self, overrides=None):
        """Run the parsed status body against controlled outcomes and a fake curl."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            curl_args = temp_path / "curl-args.txt"
            fake_curl = temp_path / "curl"
            fake_curl.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TASK7_CURL_ARGS"\n')
            fake_curl.chmod(0o755)
            env = os.environ | {
                "PATH": str(temp_path) + os.pathsep + os.environ["PATH"],
                "TASK7_CURL_ARGS": str(curl_args),
                "HOST": "forgejo.example.test",
                "STATUS_TOKEN": "test-token",
                "COMMIT_SHA": "a" * 40,
                "RUN_URL": "https://github.example.test/run/1",
            }
            for gate_id in self.mandatory_gate_ids:
                env[gate_id.upper()] = "success"
            env.update(overrides or {})
            result = subprocess.run(
                ["bash", "-c", self.run_for("forgejo_status")],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            return result, curl_args.read_text()

    def test_forgejo_status_executes_the_parsed_fail_closed_loop(self):
        """The parsed status body posts success only when every supplied outcome succeeds."""
        success_result, success_payload = self.run_status_with_outcomes()
        self.assertEqual(success_result.returncode, 0, success_result.stderr)
        self.assertIn('"state":"success"', success_payload)
        self.assertIn("Posted commit status: success", success_result.stdout)

        skipped_result, skipped_payload = self.run_status_with_outcomes(
            {"ARCHIVE_VALIDATION": "skipped"}
        )
        self.assertEqual(skipped_result.returncode, 0, skipped_result.stderr)
        self.assertIn('"state":"failure"', skipped_payload)
        self.assertIn("Posted commit status: failure", skipped_result.stdout)

        failed_result, failed_payload = self.run_status_with_outcomes(
            {"EVIDENCE_BUNDLE": "failure"}
        )
        self.assertEqual(failed_result.returncode, 0, failed_result.stderr)
        self.assertIn('"state":"failure"', failed_payload)
        self.assertIn("Posted commit status: failure", failed_result.stdout)

    def test_evidence_preflight_and_upload_require_complete_bundle(self):
        """Upload remains diagnostic-friendly, but only after checking every evidence item."""
        preflight = self.steps_by_id["evidence_bundle"]
        self.assertIn("for evidence_file in", preflight["run"])
        self.assertIn(
            'if [ ! -f "$EVIDENCE_DIR/$evidence_file" ]; then', preflight["run"]
        )
        for filename in (
            "RadioShakeShared-header.h",
            "RadioShakeShared-module.modulemap",
            "Task7FacadeSmoke.swift",
            "Task7FacadeSmoke.typecheck.txt",
            "task7-archive-checksums.txt",
            "task7-archive-inspection.txt",
            "task7-generated-sqldelight-symbols.txt",
            "task7-jdk-gradle.txt",
            "task7-runtimes.txt",
            "task7-shared-tasks.txt",
            "task7-toolchain.txt",
        ):
            self.assertIn(filename, preflight["run"])
        self.assertIn('if [ ! -d "$EVIDENCE_DIR/gradle-reports" ]; then', preflight["run"])
        self.assertEqual(self.upload["id"], "upload_evidence")
        self.assertEqual(self.upload["with"]["if-no-files-found"], "error")

    def test_archive_cleanup_trap_precedes_archive_operations(self):
        """Extraction cleanup must run even when a command exits under set -e."""
        archive_run = self.run_for("archive_validation")
        allocation = archive_run.index('extract_dir=$(mktemp -d')
        validation_end = archive_run.index("esac", allocation)
        trap = archive_run.index("trap 'cleanup_extract_dir' EXIT HUP INT TERM")
        list_members = archive_run.index('ar -t "$archive"')
        extract = archive_run.index('(cd "$extract_dir" && ar -x "$archive")')
        cleanup = archive_run.index('rm -rf -- "$extract_dir"', extract)
        clear_trap = archive_run.index('trap - EXIT HUP INT TERM', cleanup)
        self.assertLess(allocation, trap)
        self.assertLess(validation_end, trap)
        self.assertLess(trap, list_members)
        self.assertLess(trap, extract)
        self.assertLess(extract, cleanup)
        self.assertLess(cleanup, clear_trap)

    def test_archive_member_diagnostic_is_persisted_before_rejection(self):
        """A rejected archive member remains available in uploaded inspection evidence."""
        archive_run = self.run_for("archive_validation")
        self.assert_block(
            archive_run,
            r'''^\s*printf '%s\\n' "Archive members for \$archive:" \| tee -a "\$archive_report"$
\s*ar -t "\$archive" \| tee -a "\$archive_report" > "\$members_file"$
\s*if \[ ! -s "\$members_file" \]; then$''',
        )
        self.assert_block(
            archive_run,
            r'''^\s*if unsafe_members=\$\(grep -vE '\^\(__\\\.SYMDEF\|__\.SYMDEF SORTED\)\$' "\$members_file" \| grep -E '\(\^\|/\)\\\.\\\.\?\(\\/\|\$\)\|\[\^A-Za-z0-9\._: -\]'\); then$
\s*printf '%s\\n' "\$unsafe_members" \| tee -a "\$archive_report"$
\s*echo .*Static archive contains an unsafe member name.*$
\s*exit 1$
\s*fi$''',
        )

    def test_archive_member_validation_permits_kotlin_native_colons(self):
        """Rejecting ':' breaks legitimate Kotlin/Native dependency archive members."""
        result = self.archive_member_validation_result(
            "libio.ktor:ktor-client-core-cache.a.o"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_archive_member_validation_rejects_traversal(self):
        """Permitting ':' must not allow an extracted object to escape its temporary directory."""
        result = self.archive_member_validation_result("../escape.o")
        self.assertNotEqual(result.returncode, 0)

    def test_archive_member_validation_rejects_shell_control_character(self):
        """Permitting ':' must not allow a member name with shell-significant control input."""
        result = self.archive_member_validation_result("unsafe;member.o")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
