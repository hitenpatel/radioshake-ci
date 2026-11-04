#!/usr/bin/env python3
"""Export entitlement gate for ios-testflight.yml (RadioShake Phase P1, spec decision 17).

Reads the output of `codesign -d --entitlements :- RadioShake.app` run on the EXPORTED (signed) app and fails unless it
carries `com.apple.developer.devicecheck.appattest-environment = production`. The archive is ad-hoc signed (so entitlements are
embedded) and re-signed at export, so this is where a dropped entitlement would show up, before anything is uploaded.

Usage: check_export_entitlements.py FILE   (use - for stdin)
Exit 0: present and production. Exit 1: "entitlements missing at export" (absent, another value, or unreadable output).

Applicability: check_export_entitlements.py --declared PROJECT_YML
Exit 0: the released source declares the entitlement (the gate applies). Exit 4: it does not (a sha from before Phase P1;
the gate is not applicable and the release continues). Exit 2: PROJECT_YML is unreadable (never a silent skip).

Phase 5c (spec decision 16 b), the widget extension:
  check_export_entitlements.py --widgets-declared PROJECT_YML
      Exit 0: the XcodeGen spec defines the RadioShakeWidgets target. Exit 4: it does not. Exit 2: unreadable.
  check_export_entitlements.py --widgets EXPORTED_APP
      Reads the exported app's Info.plist and runs `codesign -d --entitlements :-` (`$RS_CODESIGN` replaces codesign in
      dry checks) on the app and on PlugIns/RadioShakeWidgets.appex. A build from before 5c (no appex and no RSAppGroup in
      its Info.plist) passes with "pre-5c app, guard skipped". Otherwise the app and the appex must both carry
      com.apple.security.application-groups with the release group, else "entitlements missing at export". Prints entitlement
      KEYS only, never values, never a provisioning profile. Exit 0: pass or skipped. Exit 1: fail.
"""
import os
import plistlib
import re
import subprocess
import sys

KEY = "com.apple.developer.devicecheck.appattest-environment"
WANT = "production"
GROUPS_KEY = "com.apple.security.application-groups"
RELEASE_GROUP = "group.com.radioshake"
APPEX_DIR = "PlugIns/RadioShakeWidgets.appex"
APPEX_NAME = "RadioShakeWidgets.appex"


def parse(raw: bytes) -> dict:
    """The XML plist inside codesign's output (which may carry text before or after it)."""
    start = raw.find(b"<?xml")
    if start < 0:
        start = raw.find(b"<plist")
    end = raw.rfind(b"</plist>")
    if start < 0 or end < 0:
        raise ValueError("no entitlements plist in the codesign output")
    loaded = plistlib.loads(raw[start:end + len(b"</plist>")])
    if not isinstance(loaded, dict):
        raise ValueError("entitlements are not a dictionary")
    return loaded


def declared(project_yml: str) -> bool:
    """True when an uncommented line of the XcodeGen spec names the App Attest entitlement."""
    return any(KEY in line for line in project_yml.splitlines() if not line.lstrip().startswith("#"))


def widgets_declared(project_yml: str) -> bool:
    """True when an uncommented line of the XcodeGen spec defines the RadioShakeWidgets target (a mapping key, not the
    `- target: RadioShakeWidgets` dependency line of the app)."""
    return any(re.match(r"^\s+RadioShakeWidgets:\s*(#.*)?$", line) for line in project_yml.splitlines()
               if not line.lstrip().startswith("#"))


def codesign_entitlements(path: str) -> bytes:
    """`codesign -d --entitlements :- PATH` (stderr dropped: it names the executable and may mention the profile)."""
    tool = os.environ.get("RS_CODESIGN", "codesign")
    result = subprocess.run([tool, "-d", "--entitlements", ":-", path], capture_output=True)
    return result.stdout


def _groups(raw: bytes) -> list:
    entitlements = parse(raw)
    value = entitlements.get(GROUPS_KEY)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _plist(path: str) -> dict:
    with open(path, "rb") as fh:
        loaded = plistlib.load(fh)
    if not isinstance(loaded, dict):
        raise ValueError("not a dictionary")
    return loaded


def check_widgets(app_dir: str, codesign=codesign_entitlements) -> tuple:
    """Returns (status, message): status is "pass", "skip" or "fail". `codesign(path)` returns the entitlements output."""
    fail = "entitlements missing at export"
    try:
        info = _plist(os.path.join(app_dir, "Info.plist"))
    except Exception:
        return "fail", f"{fail} (the exported app's Info.plist cannot be read)"
    appex = os.path.join(app_dir, APPEX_DIR)
    group = info.get("RSAppGroup")
    if group is None and not os.path.isdir(appex):
        return "skip", "pre-5c app, guard skipped"
    if group != RELEASE_GROUP:
        return "fail", f"{fail} (the app's RSAppGroup is not {RELEASE_GROUP})"
    if not os.path.isdir(appex):
        return "fail", f"{fail} ({APPEX_NAME} is missing from the exported app's PlugIns)"
    try:
        appex_group = _plist(os.path.join(appex, "Info.plist")).get("RSAppGroup")
    except Exception:
        return "fail", f"{fail} ({APPEX_NAME} Info.plist cannot be read)"
    if appex_group != RELEASE_GROUP:
        return "fail", f"{fail} ({APPEX_NAME} RSAppGroup is not {RELEASE_GROUP})"
    for label, path in (("RadioShake.app", app_dir), (APPEX_NAME, appex)):
        try:
            carried = _groups(codesign(path))
        except Exception:
            return "fail", f"{fail} (codesign printed no readable entitlements for {label})"
        if RELEASE_GROUP not in carried:
            return "fail", f"{fail} ({label} lacks {GROUPS_KEY} with the release group)"
    return "pass", f"export entitlements ok: RadioShake.app and {APPEX_NAME} carry {GROUPS_KEY}"


def check(raw: bytes) -> str:
    """Returns "" when the gate passes, else the failure message."""
    try:
        entitlements = parse(raw)
    except Exception:
        return "entitlements missing at export (codesign printed no readable entitlements)"
    if KEY not in entitlements:
        return f"entitlements missing at export ({KEY} is absent)"
    if entitlements[KEY] != WANT:
        return f"entitlements missing at export ({KEY} is {entitlements[KEY]!r}, not {WANT!r})"
    return ""


def main(argv) -> int:
    if len(argv) == 3 and argv[1] == "--declared":
        try:
            with open(argv[2], encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            print(f"::error::cannot read {argv[2]} to decide whether the entitlement gate applies: {exc}")
            return 2
        if declared(text):
            print(f"{KEY} is declared: the export gate applies")
            return 0
        print("no App Attest entitlement declared in the released source: gate not applicable")
        return 4
    if len(argv) == 3 and argv[1] == "--widgets-declared":
        try:
            with open(argv[2], encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            print(f"::error::cannot read {argv[2]} to decide whether the widget guard applies: {exc}")
            return 2
        if widgets_declared(text):
            print("RadioShakeWidgets is defined: the widget extension guard applies")
            return 0
        print("no widget extension in the released source: widget guard not applicable")
        return 4
    if len(argv) == 3 and argv[1] == "--widgets":
        status, message = check_widgets(argv[2])
        if status == "fail":
            print(f"::error::{message}")
            return 1
        print(message)
        return 0
    if len(argv) != 2:
        print("usage: check_export_entitlements.py FILE|-", file=sys.stderr)
        return 2
    raw = sys.stdin.buffer.read() if argv[1] == "-" else open(argv[1], "rb").read()
    message = check(raw)
    if message:
        print(f"::error::{message}")
        return 1
    print(f"export entitlement ok: {KEY} = {WANT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
