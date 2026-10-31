#!/usr/bin/env python3
"""Fail unless every UI test class selected for this job actually ran (passed or failed) in xctest.log.

  check_shard_ran.py --args /tmp/ui-shard-args.txt --log /tmp/ios-logs/xctest.log

--args holds one `-only-testing:<Bundle>/<Class>` line per class (written by shard_ui_tests.py).
A class that is selected but has no `Test Suite '<Class>' passed|failed` line (typo, class renamed or
deleted, build skipped it) means the shard silently tested less than planned, so the job goes red.
"""
import argparse
import re
import sys


def selected_classes(args_text):
    classes = []
    for line in args_text.splitlines():
        m = re.fullmatch(r"-only-testing:[A-Za-z0-9_]+/([A-Za-z_][A-Za-z0-9_]*)", line.strip())
        if line.strip() and not m:
            raise ValueError(f"unexpected line in args file: {line.strip()!r}")
        if m:
            classes.append(m.group(1))
    return classes


def missing_classes(classes, log_text):
    ran = set(re.findall(r"Test Suite '([A-Za-z_][A-Za-z0-9_]*)' (?:passed|failed) at", log_text))
    return [c for c in classes if c not in ran]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--args", required=True)
    p.add_argument("--log", required=True)
    a = p.parse_args(argv)
    try:
        with open(a.args, encoding="utf-8") as fh:
            classes = selected_classes(fh.read())
        with open(a.log, encoding="utf-8", errors="replace") as fh:
            log = fh.read()
    except (OSError, ValueError) as e:
        print(f"::error::{e}")
        return 1
    if not classes:
        print("::error::no UI test classes were selected for this job")
        return 1
    missing = missing_classes(classes, log)
    if missing:
        print(f"::error::selected UI test classes that did not run: {', '.join(missing)}")
        return 1
    print(f"OK - all {len(classes)} selected UI test classes ran")
    return 0


if __name__ == "__main__":
    sys.exit(main())
