"""Dry checks of the export entitlement gate against fabricated `codesign -d --entitlements :-` output.
Run: python3 -m unittest discover -s .github/scripts"""
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import unittest

import check_export_entitlements as gate

HEAD = b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" ' \
       b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0">\n<dict>\n'


def output(*entries: bytes) -> bytes:
    return HEAD + b"".join(entries) + b"</dict>\n</plist>\n"


def attest(value: bytes) -> bytes:
    return b"<key>com.apple.developer.devicecheck.appattest-environment</key><string>" + value + b"</string>"


OTHER = b"<key>application-identifier</key><string>TEAM.com.radioshake</string>" \
        b"<key>get-task-allow</key><false/>"


class ExportEntitlementGateTests(unittest.TestCase):
    def test_production_passes(self):
        self.assertEqual(gate.check(output(OTHER, attest(b"production"))), "")

    def test_text_around_the_plist_is_ignored(self):
        raw = b"Executable=/x/RadioShake.app/RadioShake\n" + output(attest(b"production")) + b"\ntrailing\n"
        self.assertEqual(gate.check(raw), "")

    def test_missing_key_fails(self):
        self.assertIn("entitlements missing at export", gate.check(output(OTHER)))

    def test_development_fails(self):
        message = gate.check(output(attest(b"development")))
        self.assertTrue(message.startswith("entitlements missing at export"), message)
        self.assertIn("not 'production'", message)

    def test_empty_or_garbage_output_fails(self):
        for raw in (b"", b"not a plist", b"<plist><dict>"):
            self.assertIn("entitlements missing at export", gate.check(raw))

    def test_command_line_exit_codes(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_export_entitlements.py")
        for raw, expected in ((output(attest(b"production")), 0), (output(OTHER), 1)):
            with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as fh:
                fh.write(raw)
            try:
                result = subprocess.run([sys.executable, script, fh.name], capture_output=True, text=True)
            finally:
                os.unlink(fh.name)
            self.assertEqual(result.returncode, expected, result.stdout)
        self.assertIn("entitlements missing at export", result.stdout)


class DeclaredEntitlementTests(unittest.TestCase):
    """The gate only applies when the released source declares the App Attest entitlement (a main sha from before
    Phase P1 has none, and must still release)."""

    SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_export_entitlements.py")

    def test_declared_in_project_yml(self):
        text = "targets:\n  RadioShake:\n    entitlements:\n      properties:\n" \
               "        com.apple.developer.devicecheck.appattest-environment: $(APP_ATTEST_ENV)\n"
        self.assertTrue(gate.declared(text))

    def test_absent_is_not_declared(self):
        self.assertFalse(gate.declared("targets:\n  RadioShake:\n    type: application\n"))

    def test_a_comment_does_not_count_as_declared(self):
        self.assertFalse(gate.declared("# com.apple.developer.devicecheck.appattest-environment: production\n"))

    def _run(self, text):
        with tempfile.NamedTemporaryFile(suffix=".yml", delete=False, mode="w") as fh:
            fh.write(text)
        try:
            return subprocess.run([sys.executable, self.SCRIPT, "--declared", fh.name], capture_output=True, text=True)
        finally:
            os.unlink(fh.name)

    def test_declared_command_line_exit_codes(self):
        self.assertEqual(self._run("a: com.apple.developer.devicecheck.appattest-environment\n").returncode, 0)
        result = self._run("targets: {}\n")
        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertIn("gate not applicable", result.stdout)

    def test_an_unreadable_project_yml_is_an_error_not_a_skip(self):
        result = subprocess.run([sys.executable, self.SCRIPT, "--declared", "/nonexistent/project.yml"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_the_workflow_gate_is_conditional_on_the_declaration(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "workflows", "ios-testflight.yml"), encoding="utf-8") as fh:
            text = fh.read()
        start = text.index("gate_entitlements() {")
        body = text[start:text.index("classify_export_failure() {")]
        self.assertIn("check_export_entitlements.py\" --declared", body)
        self.assertLess(body.index("--declared"), body.index("xcodebuild -exportArchive"))

    def test_the_archive_is_ad_hoc_signed_so_entitlements_reach_export(self):
        # Run 41579626789: a CODE_SIGNING_ALLOWED=NO archive embeds no entitlements, so export had nothing to carry
        # over. 5c decision 16 (c): the archive carries an ad-hoc signature (no identity, no team id, no profile).
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "workflows", "ios-testflight.yml"), encoding="utf-8") as fh:
            text = fh.read()
        start = text.index("do_archive() {")
        body = text[start:text.index("# Mach-O or dSYM bundle", start)]
        self.assertIn("xcodebuild archive", body)
        for setting in ("CODE_SIGNING_ALLOWED=YES", "CODE_SIGNING_REQUIRED=NO", "CODE_SIGN_IDENTITY=-",
                        "CODE_SIGN_STYLE=Manual", "AD_HOC_CODE_SIGNING_ALLOWED=YES"):
            self.assertRegex(body, r"(?<![\w=])" + re.escape(setting) + r"(?=\s|\\)", setting)
        self.assertNotIn("CODE_SIGNING_ALLOWED=NO", body)
        self.assertNotIn("CODE_SIGN_STYLE=Automatic", body)
        # The team id still comes from the secret through the step env (masked), never a literal.
        self.assertIn('DEVELOPMENT_TEAM="$TEAM"', body)
        self.assertNotIn("CODE_SIGNING_ALLOWED=NO", text)
        # The gate stays in front of the uploading export.
        self.assertLess(text.index("gate_entitlements\n"), text.index("if do_export; then"))


GROUPS_KEY = b"<key>com.apple.security.application-groups</key>"


def groups(*names: str) -> bytes:
    return GROUPS_KEY + b"<array>" + b"".join(b"<string>" + n.encode() + b"</string>" for n in names) + b"</array>"


APP_ID = "com.radioshake"
GROUP = "group.com.radioshake"


def make_app(root, *, app_group=GROUP, appex=True, appex_group=GROUP, appex_id=APP_ID + ".widgets"):
    """A fabricated exported app: Info.plist, and (when `appex`) PlugIns/RadioShakeWidgets.appex with its Info.plist."""
    app = os.path.join(root, "Payload", "RadioShake.app")
    os.makedirs(app)
    info = {"CFBundleIdentifier": APP_ID, "CFBundleShortVersionString": "1.0.0", "CFBundleVersion": "7"}
    if app_group is not None:
        info["RSAppGroup"] = app_group
    with open(os.path.join(app, "Info.plist"), "wb") as fh:
        plistlib.dump(info, fh)
    if appex:
        ext = os.path.join(app, "PlugIns", "RadioShakeWidgets.appex")
        os.makedirs(ext)
        ext_info = {"CFBundleIdentifier": appex_id, "CFBundleShortVersionString": "1.0.0", "CFBundleVersion": "7",
                    "NSExtension": {"NSExtensionPointIdentifier": "com.apple.widgetkit-extension"}}
        if appex_group is not None:
            ext_info["RSAppGroup"] = appex_group
        with open(os.path.join(ext, "Info.plist"), "wb") as fh:
            plistlib.dump(ext_info, fh)
    return app


def codesign_for(app_ents: bytes, appex_ents: bytes):
    """A fake `codesign -d --entitlements :- PATH`: the app's output for the .app, the appex's for the .appex."""
    def run(path: str) -> bytes:
        return appex_ents if path.rstrip("/").endswith(".appex") else app_ents
    return run


class WidgetsGuardTests(unittest.TestCase):
    """5c decision 16 (b): the export guard for the App Group on the app and the widget extension. A build from before the
    widget target (no appex, no RSAppGroup) passes with "pre-5c app, guard skipped"."""

    def check(self, **kwargs):
        ents_app = kwargs.pop("app_ents", output(attest(b"production"), groups(GROUP)))
        ents_appex = kwargs.pop("appex_ents", output(groups(GROUP)))
        with tempfile.TemporaryDirectory() as root:
            app = make_app(root, **kwargs)
            return gate.check_widgets(app, codesign_for(ents_app, ents_appex))

    def test_present_on_both_passes(self):
        status, message = self.check()
        self.assertEqual(status, "pass", message)
        self.assertIn("com.apple.security.application-groups", message)

    def test_the_message_names_entitlement_keys_only(self):
        _, message = self.check()
        self.assertNotIn(GROUP, message)
        self.assertNotIn("production", message)

    def test_a_pre_5c_app_is_skipped(self):
        status, message = self.check(app_group=None, appex=False)
        self.assertEqual(status, "skip")
        self.assertIn("pre-5c app, guard skipped", message)

    def test_missing_on_the_app_fails(self):
        status, message = self.check(app_ents=output(attest(b"production")))
        self.assertEqual(status, "fail")
        self.assertIn("entitlements missing at export", message)
        self.assertIn("app", message)

    def test_missing_on_the_appex_fails(self):
        status, message = self.check(appex_ents=output(OTHER))
        self.assertEqual(status, "fail")
        self.assertIn("entitlements missing at export", message)
        self.assertIn("RadioShakeWidgets.appex", message)

    def test_another_group_fails(self):
        status, message = self.check(app_ents=output(groups("group.com.other")))
        self.assertEqual(status, "fail")
        self.assertIn("entitlements missing at export", message)

    def test_the_group_may_be_one_of_several(self):
        status, _ = self.check(app_ents=output(groups("group.com.other", GROUP)))
        self.assertEqual(status, "pass")

    def test_unreadable_codesign_output_fails(self):
        status, message = self.check(appex_ents=b"codesign: not signed")
        self.assertEqual(status, "fail")
        self.assertIn("entitlements missing at export", message)

    def test_an_appex_without_the_app_group_key_in_its_plist_fails(self):
        status, message = self.check(appex_group=None)
        self.assertEqual(status, "fail")
        self.assertIn("RSAppGroup", message)

    def test_a_debug_group_in_a_release_export_fails(self):
        status, message = self.check(app_group="group.com.radioshake.debug", appex_group="group.com.radioshake.debug",
                                     app_ents=output(groups("group.com.radioshake.debug")),
                                     appex_ents=output(groups("group.com.radioshake.debug")))
        self.assertEqual(status, "fail")
        self.assertIn(GROUP, message)

    def test_an_app_group_without_the_appex_fails(self):
        status, message = self.check(appex=False)
        self.assertEqual(status, "fail")
        self.assertIn("RadioShakeWidgets.appex", message)

    def test_the_appex_without_an_app_group_plist_key_on_the_app_fails(self):
        status, message = self.check(app_group=None)
        self.assertEqual(status, "fail")
        self.assertIn("RSAppGroup", message)


class WidgetsCommandLineTests(unittest.TestCase):
    SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_export_entitlements.py")

    def _fake_codesign(self, root, app_ents: bytes, appex_ents: bytes) -> str:
        app_file, appex_file = os.path.join(root, "app.txt"), os.path.join(root, "appex.txt")
        with open(app_file, "wb") as fh:
            fh.write(app_ents)
        with open(appex_file, "wb") as fh:
            fh.write(appex_ents)
        script = os.path.join(root, "fake-codesign")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\n# usage: codesign -d --entitlements :- PATH\n"
                     'case "$4" in *.appex) cat "%s";; *) cat "%s";; esac\n' % (appex_file, app_file))
        os.chmod(script, 0o755)
        return script

    def _run(self, app, codesign):
        env = dict(os.environ, RS_CODESIGN=codesign)
        return subprocess.run([sys.executable, self.SCRIPT, "--widgets", app], capture_output=True, text=True, env=env)

    def test_exit_codes_and_messages(self):
        good = output(groups(GROUP))
        with tempfile.TemporaryDirectory() as root:
            app = make_app(root)
            result = self._run(app, self._fake_codesign(root, good, good))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self._run(app, self._fake_codesign(root, good, output(OTHER)))
            self.assertEqual(result.returncode, 1)
            self.assertIn("entitlements missing at export", result.stdout)
        with tempfile.TemporaryDirectory() as root:
            app = make_app(root, app_group=None, appex=False)
            result = self._run(app, self._fake_codesign(root, output(OTHER), output(OTHER)))
            self.assertEqual(result.returncode, 0)
            self.assertIn("pre-5c app, guard skipped", result.stdout)

    def test_an_unreadable_app_is_an_error_not_a_skip(self):
        result = subprocess.run([sys.executable, self.SCRIPT, "--widgets", "/nonexistent/RadioShake.app"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("entitlements missing at export", result.stdout)

    def _declared(self, text):
        with tempfile.NamedTemporaryFile(suffix=".yml", delete=False, mode="w") as fh:
            fh.write(text)
        try:
            return subprocess.run([sys.executable, self.SCRIPT, "--widgets-declared", fh.name],
                                  capture_output=True, text=True)
        finally:
            os.unlink(fh.name)

    def test_widgets_declared_exit_codes(self):
        self.assertEqual(self._declared("targets:\n  RadioShakeWidgets:\n    type: app-extension\n").returncode, 0)
        result = self._declared("targets:\n  RadioShake:\n    type: application\n")
        self.assertEqual(result.returncode, 4)
        self.assertIn("no widget extension", result.stdout)
        self.assertEqual(self._declared("# RadioShakeWidgets:\n").returncode, 4)
        self.assertEqual(self._declared("  - target: RadioShakeWidgets\n").returncode, 4,
                         "a dependency line is not the target definition")

    def test_an_unreadable_project_yml_is_an_error(self):
        result = subprocess.run([sys.executable, self.SCRIPT, "--widgets-declared", "/nonexistent/project.yml"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


class WidgetsWorkflowTests(unittest.TestCase):
    """The testflight workflow wires the guard and the appex archive checks, and stays safe for builds before 5c."""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "workflows", "ios-testflight.yml"), encoding="utf-8") as fh:
            cls.text = fh.read()

    def gate_body(self):
        start = self.text.index("gate_entitlements() {")
        return self.text[start:self.text.index("classify_export_failure() {")]

    def verify_body(self):
        start = self.text.index("verify_archive() {")
        return self.text[start:self.text.index("do_export() {")]

    def test_the_gate_applies_for_either_declaration_and_skips_only_when_neither_is_declared(self):
        body = self.gate_body()
        self.assertIn('check_export_entitlements.py" --declared', body)
        self.assertIn('check_export_entitlements.py" --widgets-declared', body)
        self.assertRegex(body, r'\[ "\$attest" = 4 \] && \[ "\$widgets" = 4 \]')
        self.assertIn("gate not applicable", body)

    def test_the_gate_runs_the_widgets_guard_on_the_exported_app_before_any_upload(self):
        body = self.gate_body()
        self.assertIn('check_export_entitlements.py" --widgets "$app"', body)
        self.assertLess(body.index("codesign -d --entitlements"), body.index("--widgets \"$app\""))
        self.assertLess(self.text.index("gate_entitlements\n"), self.text.index("if do_export; then"))

    def test_the_app_attest_check_still_runs_when_declared(self):
        body = self.gate_body()
        self.assertIn('check_export_entitlements.py" "$ents"', body)
        self.assertIn('"$attest" = 0', body)

    def test_verify_archive_checks_the_appex_only_when_the_archive_has_one(self):
        body = self.verify_body()
        self.assertIn("PlugIns/RadioShakeWidgets.appex", body)
        self.assertRegex(body, r'if \[ -d "\$appex" \]')
        for needle in ("CFBundleIdentifier", "$BUNDLE_ID.widgets", "CFBundleShortVersionString", "CFBundleVersion",
                       "RSAppGroup", "RadioShakeWidgets.appex.dSYM", "dSYM UUID does not match the widget extension"):
            self.assertIn(needle, body, needle)

    def test_nothing_in_the_guard_prints_a_profile(self):
        body = self.gate_body()
        self.assertNotIn("embedded.mobileprovision", body)
        self.assertNotIn("security cms", body)


VALIDATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workflows", "ios-shared-validate.yml")


class WidgetsProbeWorkflowTests(unittest.TestCase):
    """5c decision 16 (a): ios-shared-validate archives the device build ad hoc and inspects it, only when the project
    defines RadioShakeWidgets (main before 5c skips the probe)."""

    @classmethod
    def setUpClass(cls):
        with open(VALIDATE, encoding="utf-8") as fh:
            cls.text = fh.read()
        start = cls.text.index("- name: Widget extension archive probe")
        end = cls.text.index("\n      - name:", start + 10)
        cls.step = cls.text[start:end]

    def test_the_probe_is_skipped_without_the_widget_target(self):
        self.assertIn("RadioShakeWidgets:", self.step)
        self.assertIn("probe skipped", self.step)
        self.assertLess(self.step.index("probe skipped"), self.step.index("xcodebuild archive"))

    def test_the_probe_archives_ad_hoc_signed_with_entitlements(self):
        for setting in ("CODE_SIGNING_ALLOWED=YES", "CODE_SIGNING_REQUIRED=NO", "CODE_SIGN_IDENTITY=-",
                        "CODE_SIGN_STYLE=Manual", "AD_HOC_CODE_SIGNING_ALLOWED=YES"):
            self.assertRegex(self.step, r"(?<![\w=])" + re.escape(setting) + r"(?=\s|\\)", setting)
        self.assertNotIn("CODE_SIGNING_ALLOWED=NO", self.step)

    def test_the_probe_runs_the_archive_checks(self):
        self.assertIn("check_widget_archive.py", self.step)
        self.assertIn("--project-yml ios/project.yml", self.step)
        self.assertLess(self.step.index("xcodebuild archive"), self.step.index("check_widget_archive.py"))

    def test_the_probe_restores_the_debug_framework_link(self):
        self.assertIn("XCFrameworks/debug/RadioShakeShared.xcframework", self.step)

    def test_the_probe_runs_after_the_device_build(self):
        self.assertLess(self.text.index("- name: Build iOS app (Release, device, unsigned) (TASK-14)"),
                        self.text.index("- name: Widget extension archive probe"))

    def test_the_widget_catalogue_is_checked_when_it_exists(self):
        self.assertIn("ios/Widgets/Localizable.xcstrings", self.text)
        self.assertRegex(self.text, r"check_xcstrings.py Widgets/Localizable.xcstrings --xliff")


if __name__ == "__main__":
    unittest.main()
