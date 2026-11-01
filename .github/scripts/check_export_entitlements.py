#!/usr/bin/env python3
"""Export entitlement gate for ios-testflight.yml (RadioShake Phase P1, spec decision 17).

Reads the output of `codesign -d --entitlements :- RadioShake.app` run on the EXPORTED (signed) app and fails unless it
carries `com.apple.developer.devicecheck.appattest-environment = production`. The archive is unsigned and re-signed at
export, so this is where a dropped entitlement would show up, before anything is uploaded.

Usage: check_export_entitlements.py FILE   (use - for stdin)
Exit 0: present and production. Exit 1: "entitlements missing at export" (absent, another value, or unreadable output).

Applicability: check_export_entitlements.py --declared PROJECT_YML
Exit 0: the released source declares the entitlement (the gate applies). Exit 4: it does not (a sha from before Phase P1;
the gate is not applicable and the release continues). Exit 2: PROJECT_YML is unreadable (never a silent skip).
"""
import plistlib
import sys

KEY = "com.apple.developer.devicecheck.appattest-environment"
WANT = "production"


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
