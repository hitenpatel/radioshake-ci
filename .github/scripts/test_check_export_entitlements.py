"""Dry checks of the export entitlement gate against fabricated `codesign -d --entitlements :-` output.
Run: python3 -m unittest discover -s .github/scripts"""
import os
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


if __name__ == "__main__":
    unittest.main()
