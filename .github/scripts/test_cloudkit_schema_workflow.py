"""Structure checks of cloudkit-schema.yml: development only, the token never on a command line or echoed.
Run: python3 -m unittest test_cloudkit_schema_workflow"""
import os
import re
import unittest

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "workflows", "cloudkit-schema.yml")


class CloudKitSchemaWorkflowTests(unittest.TestCase):
    def setUp(self):
        with open(PATH, encoding="utf-8") as fh:
            self.text = fh.read()
        self.code = "\n".join(l for l in self.text.splitlines() if not l.lstrip().startswith("#"))

    def test_only_the_development_environment_is_targeted(self):
        self.assertNotIn("production", self.code.lower())
        self.assertEqual(self.code.count("--environment development"), 2)

    def test_validate_precedes_import_and_flags_are_complete(self):
        self.assertLess(self.code.index("validate-schema"), self.code.index("import-schema"))
        for flag in ("--team-id", "--container-id", "--file ios/CloudKit/schema.ckdb"):
            self.assertEqual(self.code.count(flag), 2, flag)

    def test_the_token_comes_from_the_secret_via_env_and_is_not_echoed(self):
        self.assertIn("CLOUDKIT_MANAGEMENT_TOKEN: ${{ secrets.CLOUDKIT_MANAGEMENT_TOKEN }}", self.code)
        self.assertNotRegex(self.code, r"echo (?!\"::add-mask::)[^\n]*\$\{?CLOUDKIT_MANAGEMENT_TOKEN")
        self.assertNotRegex(self.code, r"cktool[^\n]*CLOUDKIT_MANAGEMENT_TOKEN")

    def test_runs_only_on_dispatch(self):
        self.assertRegex(self.code, r"(?m)^on:\n  workflow_dispatch:")
        self.assertNotIn("push:", self.code)


if __name__ == "__main__":
    unittest.main()
