"""Dry checks of the widget archive probe (5c decision 16 a) against a fabricated archived app and fake codesign output.
Run: python3 -m unittest discover -s .github/scripts"""
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest

import check_widget_archive as probe

HEAD = b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" ' \
       b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0">\n<dict>\n'
GROUP = "group.com.radioshake"


def ents(**values) -> bytes:
    body = b""
    for key, value in values.items():
        body += b"<key>" + key.encode() + b"</key><array>" + b"".join(
            b"<string>" + v.encode() + b"</string>" for v in value) + b"</array>"
    return HEAD + body + b"</dict>\n</plist>\n"


GROUPS = "com.apple.security.application-groups"
DOMAINS = "com.apple.developer.associated-domains"


def make_app(root, *, app_extra=None, appex_extra=None, appex_id="com.radioshake.widgets", appex_version="7",
             appex_group=GROUP, app_group=GROUP, point="com.apple.widgetkit-extension"):
    app = os.path.join(root, "RadioShake.app")
    os.makedirs(os.path.join(app, "PlugIns", "RadioShakeWidgets.appex"))
    info = {"CFBundleIdentifier": "com.radioshake", "CFBundleShortVersionString": "1.0.0", "CFBundleVersion": "7",
            "RSAppGroup": app_group}
    info.update(app_extra or {})
    with open(os.path.join(app, "Info.plist"), "wb") as fh:
        plistlib.dump(info, fh)
    ext = {"CFBundleIdentifier": appex_id, "CFBundleShortVersionString": "1.0.0", "CFBundleVersion": appex_version,
           "RSAppGroup": appex_group, "NSExtension": {"NSExtensionPointIdentifier": point}}
    ext.update(appex_extra or {})
    with open(os.path.join(app, "PlugIns", "RadioShakeWidgets.appex", "Info.plist"), "wb") as fh:
        plistlib.dump(ext, fh)
    return app


def codesign(app_ents, appex_ents):
    return lambda path: appex_ents if path.rstrip("/").endswith(".appex") else app_ents


GOOD = ents(**{GROUPS: [GROUP]})
YML_5C = "targets:\n  RadioShakeWidgets:\n    type: app-extension\n"
YML_ALL = YML_5C + "        CFBundleURLSchemes: [radioshake]\n        NSSupportsLiveActivities: true\n" \
          "        NSAlarmKitUsageDescription: x\n          - applinks:radioshake.media\n"


class ProbeTests(unittest.TestCase):
    def run_probe(self, yml=YML_5C, app_ents=GOOD, appex_ents=GOOD, **kwargs):
        with tempfile.TemporaryDirectory() as root:
            app = make_app(root, **kwargs)
            return probe.check(app, yml, codesign(app_ents, appex_ents))

    def test_a_correct_archive_passes(self):
        self.assertEqual(self.run_probe(), [])

    def test_the_app_group_must_be_on_the_app_and_the_appex(self):
        errors = self.run_probe(app_ents=ents(**{GROUPS: ["group.com.other"]}))
        self.assertEqual(len(errors), 1)
        self.assertIn("RadioShake.app", errors[0])
        errors = self.run_probe(appex_ents=ents())
        self.assertEqual(len(errors), 1)
        self.assertIn("RadioShakeWidgets.appex", errors[0])

    def test_the_release_group_is_required_in_both_plists(self):
        self.assertTrue(self.run_probe(app_group="group.com.radioshake.debug"))
        self.assertTrue(self.run_probe(appex_group="group.com.radioshake.debug"))

    def test_the_appex_is_a_widgetkit_extension_with_the_widgets_bundle_id_and_the_apps_versions(self):
        self.assertTrue(self.run_probe(point="com.apple.share-services"))
        self.assertTrue(self.run_probe(appex_id="com.radioshake.other"))
        self.assertTrue(self.run_probe(appex_version="8"))

    def test_a_missing_appex_fails(self):
        with tempfile.TemporaryDirectory() as root:
            app = make_app(root)
            import shutil
            shutil.rmtree(os.path.join(app, "PlugIns"))
            errors = probe.check(app, YML_5C, codesign(GOOD, GOOD))
        self.assertTrue(any("RadioShakeWidgets.appex" in e for e in errors))

    def test_plist_keys_are_checked_only_when_the_project_declares_them(self):
        # Declared but absent from the built plist: each is an error.
        errors = self.run_probe(yml=YML_ALL)
        joined = " ".join(errors)
        for needle in ("CFBundleURLSchemes", "NSSupportsLiveActivities", "NSAlarmKitUsageDescription",
                       "com.apple.developer.associated-domains"):
            self.assertIn(needle, joined, needle)
        # Declared and present: passes.
        extra = {"CFBundleURLTypes": [{"CFBundleURLSchemes": ["radioshake"]}], "NSSupportsLiveActivities": True,
                 "NSAlarmKitUsageDescription": "x"}
        self.assertEqual(self.run_probe(yml=YML_ALL, app_extra=extra,
                                        app_ents=ents(**{GROUPS: [GROUP], DOMAINS: ["applinks:radioshake.media"]})), [])

    def test_a_comment_does_not_declare_a_key(self):
        yml = YML_5C + "        # CFBundleURLSchemes: [radioshake]\n"
        self.assertEqual(self.run_probe(yml=yml), [])

    def test_command_line(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_widget_archive.py")
        with tempfile.TemporaryDirectory() as root:
            app = make_app(root)
            yml = os.path.join(root, "project.yml")
            with open(yml, "w") as fh:
                fh.write(YML_5C)
            app_file, appex_file = os.path.join(root, "a.txt"), os.path.join(root, "x.txt")
            for path, data in ((app_file, GOOD), (appex_file, GOOD)):
                with open(path, "wb") as fh:
                    fh.write(data)
            fake = os.path.join(root, "fake-codesign")
            with open(fake, "w") as fh:
                fh.write('#!/bin/sh\ncase "$4" in *.appex) cat "%s";; *) cat "%s";; esac\n' % (appex_file, app_file))
            os.chmod(fake, 0o755)
            result = subprocess.run([sys.executable, script, "--project-yml", yml, app], capture_output=True, text=True,
                                    env=dict(os.environ, RS_CODESIGN=fake))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with open(appex_file, "wb") as fh:
                fh.write(ents())
            result = subprocess.run([sys.executable, script, "--project-yml", yml, app], capture_output=True, text=True,
                                    env=dict(os.environ, RS_CODESIGN=fake))
            self.assertEqual(result.returncode, 1)
            self.assertIn("RadioShakeWidgets.appex", result.stdout)


if __name__ == "__main__":
    unittest.main()
