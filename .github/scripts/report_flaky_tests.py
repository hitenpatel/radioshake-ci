#!/usr/bin/env python3
"""List XCTest cases that failed and then passed on an xcodebuild retry, as GitHub warnings.

  report_flaky_tests.py --log /tmp/ios-logs/xctest.log

xcodebuild's -retry-tests-on-failure re-runs a failed test in the same invocation, so the log holds
`Test Case '-[Class test]' failed` and, later, `... passed` for the same test. Such a test leaves the
step green, but it is a flake: this script prints one `::warning::` annotation per flaky test (with the
first failure message) and appends a table to $GITHUB_STEP_SUMMARY so the flake stays visible.
Tests that never passed (real failures) are not reported here; xcodebuild already failed the step.
Always exits 0: reporting must never turn a run red or hide the real test-step result.
"""
import argparse
import os
import re
import sys

CASE = re.compile(r"Test [Cc]ase '-\[(?P<cls>[A-Za-z_][\w.]*) (?P<name>\w+)\]' (?P<res>passed|failed)\b")
ERROR = re.compile(r"error: -\[(?P<cls>[A-Za-z_][\w.]*) (?P<name>\w+)\] : (?P<msg>.*)")
MAX_MSG = 300


def find_flaky(log_text):
    """Return [{test, failures, first_error}] for tests with a `failed` followed by a later `passed`."""
    outcomes, first_error, order = {}, {}, []
    for line in log_text.splitlines():
        e = ERROR.search(line)
        if e:
            first_error.setdefault(f"{e['cls']}.{e['name']}", e["msg"].strip())
        m = CASE.search(line)
        if m:
            key = f"{m['cls']}.{m['name']}"
            if key not in outcomes:
                order.append(key)
            outcomes.setdefault(key, []).append(m["res"])
    flaky = []
    for key in order:
        res = outcomes[key]
        if "failed" in res and "passed" in res[res.index("failed"):]:
            flaky.append({"test": key, "failures": res.count("failed"), "first_error": first_error.get(key, "")})
    return flaky


def _escape(s):
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotation(f):
    msg = f"{f['test']} failed {f['failures']} time(s) then passed on retry"
    if f["first_error"]:
        msg += f" - first failure: {f['first_error'][:MAX_MSG]}"
    return f"::warning title=Flaky UI test (passed on retry)::{_escape(msg)}"


def summary_markdown(flaky, label):
    lines = [f"### Flaky UI tests ({label})", "", "| Test | Failures before pass | First failure |", "|---|---|---|"]
    for f in flaky:
        err = f["first_error"][:MAX_MSG].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{f['test']}` | {f['failures']} | {err} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", required=True)
    p.add_argument("--label", default=os.environ.get("GITHUB_JOB", "ui tests"))
    a = p.parse_args(argv)
    try:
        with open(a.log, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        print(f"::notice::flaky-test report skipped, cannot read log: {e}")
        return 0
    flaky = find_flaky(text)
    if not flaky:
        print("OK - no UI test needed a retry")
        return 0
    for f in flaky:
        print(annotation(f))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(summary_markdown(flaky, a.label))
        except OSError:
            pass
    print(f"{len(flaky)} UI test(s) needed a retry (green only because of -retry-tests-on-failure)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
