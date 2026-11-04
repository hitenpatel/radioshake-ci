#!/usr/bin/env python3
"""Widget extension archive probe (RadioShake Phase 5c, spec decision 16 a).

Run on the app inside an ad-hoc signed Release device archive made on a branch (ios-shared-validate), where nothing can
be exported or uploaded yet. It proves the archive carries what the export will need:

  * `codesign -d --entitlements :-` on RadioShake.app and PlugIns/RadioShakeWidgets.appex shows the release App Group;
  * the app's built Info.plist has RSAppGroup = group.com.radioshake, and (each only when the XcodeGen spec declares it)
    a URL scheme `radioshake`, NSSupportsLiveActivities true, NSAlarmKitUsageDescription, and the entitlement
    com.apple.developer.associated-domains with applinks:radioshake.media;
  * the appex has RSAppGroup, the widgetkit extension point, bundle id com.radioshake.widgets and both versions equal to
    the app's.

Usage: check_widget_archive.py [--project-yml PROJECT_YML] APP     (APP: .../RadioShake.xcarchive/Products/Applications/RadioShake.app)
`$RS_CODESIGN` replaces codesign in dry checks. Prints entitlement keys only, never values from a profile.
Exit 0: all checks pass. Exit 1: any failure (each printed as ::error::).
"""
import os
import plistlib
import sys

import check_export_entitlements as ce

APPEX_ID = "com.radioshake.widgets"
POINT = "com.apple.widgetkit-extension"
DOMAINS_KEY = "com.apple.developer.associated-domains"
APPLINKS = "applinks:radioshake.media"


def declared(project_yml: str, word: str) -> bool:
    """True when an uncommented line of the XcodeGen spec mentions `word`."""
    return any(word in line for line in project_yml.splitlines() if not line.lstrip().startswith("#"))


def _load(path):
    with open(path, "rb") as fh:
        loaded = plistlib.load(fh)
    return loaded if isinstance(loaded, dict) else {}


def _entitlement_list(raw: bytes, key: str) -> list:
    value = ce.parse(raw).get(key)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def check(app: str, project_yml: str, codesign=ce.codesign_entitlements) -> list:
    errors = []
    try:
        info = _load(os.path.join(app, "Info.plist"))
    except Exception:
        return [f"the archived app's Info.plist cannot be read ({app})"]
    appex = os.path.join(app, ce.APPEX_DIR)
    if info.get("RSAppGroup") != ce.RELEASE_GROUP:
        errors.append(f"RadioShake.app Info.plist: RSAppGroup is not {ce.RELEASE_GROUP}")
    if declared(project_yml, "CFBundleURLSchemes"):
        schemes = [s for t in info.get("CFBundleURLTypes", []) if isinstance(t, dict)
                   for s in t.get("CFBundleURLSchemes", [])]
        if "radioshake" not in schemes:
            errors.append("RadioShake.app Info.plist: CFBundleURLSchemes does not contain radioshake")
    if declared(project_yml, "NSSupportsLiveActivities") and info.get("NSSupportsLiveActivities") is not True:
        errors.append("RadioShake.app Info.plist: NSSupportsLiveActivities is not true")
    if declared(project_yml, "NSAlarmKitUsageDescription") and not info.get("NSAlarmKitUsageDescription"):
        errors.append("RadioShake.app Info.plist: NSAlarmKitUsageDescription is missing")

    if not os.path.isdir(appex):
        errors.append(f"{ce.APPEX_NAME} is missing from the archived app's PlugIns")
    else:
        try:
            ext = _load(os.path.join(appex, "Info.plist"))
        except Exception:
            ext = None
            errors.append(f"{ce.APPEX_NAME} Info.plist cannot be read")
        if ext is not None:
            if ext.get("RSAppGroup") != ce.RELEASE_GROUP:
                errors.append(f"{ce.APPEX_NAME} Info.plist: RSAppGroup is not {ce.RELEASE_GROUP}")
            if (ext.get("NSExtension") or {}).get("NSExtensionPointIdentifier") != POINT:
                errors.append(f"{ce.APPEX_NAME} Info.plist: the extension point is not {POINT}")
            if ext.get("CFBundleIdentifier") != APPEX_ID:
                errors.append(f"{ce.APPEX_NAME} Info.plist: the bundle id is not {APPEX_ID}")
            for key in ("CFBundleShortVersionString", "CFBundleVersion"):
                if ext.get(key) != info.get(key):
                    errors.append(f"{ce.APPEX_NAME} Info.plist: {key} differs from the app's")

    targets = [("RadioShake.app", app)] + ([(ce.APPEX_NAME, appex)] if os.path.isdir(appex) else [])
    for label, path in targets:
        try:
            raw = codesign(path)
            groups = _entitlement_list(raw, ce.GROUPS_KEY)
            domains = _entitlement_list(raw, DOMAINS_KEY)
        except Exception:
            errors.append(f"{label}: codesign printed no readable entitlements")
            continue
        if ce.RELEASE_GROUP not in groups:
            errors.append(f"{label}: {ce.GROUPS_KEY} lacks the release group")
        if label == "RadioShake.app" and declared(project_yml, "applinks:") and APPLINKS not in domains:
            errors.append(f"{label}: {DOMAINS_KEY} lacks {APPLINKS}")
    return errors


def main(argv) -> int:
    args = list(argv[1:])
    project_yml = ""
    if len(args) >= 2 and args[0] == "--project-yml":
        try:
            with open(args[1], encoding="utf-8") as fh:
                project_yml = fh.read()
        except OSError as exc:
            print(f"::error::cannot read {args[1]}: {exc}")
            return 1
        args = args[2:]
    if len(args) != 1:
        print("usage: check_widget_archive.py [--project-yml PROJECT_YML] APP", file=sys.stderr)
        return 2
    errors = check(args[0], project_yml)
    for e in errors:
        print(f"::error::{e}")
    if errors:
        return 1
    print("widget archive probe ok: App Group on the app and the extension, plist keys as declared")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
