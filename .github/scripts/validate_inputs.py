#!/usr/bin/env python3
"""Validate ios-testflight workflow_dispatch inputs (anchored regexes, from env only).

Reads SHA, VALIDATED_RUN, MODE, RESUME_BUILD, NOTES from the environment and
exits non-zero with a GitHub ::error:: annotation on the first violation.
Nothing from the inputs is echoed back except the validated, constrained values.
"""
import os
import re
import sys

NOTES_MAX_CHARS = 4000
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def check(env):
    errors = []
    sha = env.get("SHA", "")
    run = env.get("VALIDATED_RUN", "")
    mode = env.get("MODE", "")
    resume = env.get("RESUME_BUILD", "")
    notes = env.get("NOTES", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        errors.append("sha must be a full 40-character lowercase hex commit id")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", run):
        errors.append("validated_run must be a numeric workflow run id")
    if mode not in ("release", "archive_only", "resume"):
        errors.append("mode must be release, archive_only or resume")
    if mode == "resume":
        if not re.fullmatch(r"[1-9][0-9]{0,8}", resume):
            errors.append("resume_build must be a build number (integer) when mode is resume")
    elif resume:
        errors.append("resume_build is only allowed with mode=resume")
    if len(notes) > NOTES_MAX_CHARS:
        errors.append(f"notes must be at most {NOTES_MAX_CHARS} characters")
    if CONTROL.search(notes):
        errors.append("notes must not contain control characters")
    return errors


def main():
    errors = check(os.environ)
    for message in errors:
        print(f"::error::{message}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
