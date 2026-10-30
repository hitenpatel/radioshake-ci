#!/usr/bin/env python3
"""Pure checks for the ios-testflight gate (the shell step fetches the JSON/log files with gh).

validate  Prove that a validated_run is a trustworthy ios-shared-validate run of <sha>:
          - it belongs to this repository and to .github/workflows/ios-shared-validate.yml
          - completed/success, event workflow_dispatch, head_branch main, and its head_sha is
            an ancestor of (or equal to) the current main of this repository (compare status
            ahead/identical), so the workflow revision that ran is reviewed main code
          - exactly one `validate` job, successful, whose FIRST line matching
            "Cloned <ref> at <40-hex>" is exactly "Cloned main at <sha>". The clone step is the
            first step of that job, so this line is printed before any source code runs.
release   Prove that a release_run is an ios-testflight `release` run of <sha> that uploaded
          <build>: workflow path, workflow_dispatch from main (head_sha on main), completed, a
          display_title (rendered by GitHub from the run-name and the dispatch inputs, not by
          source code) of "ios-testflight release <sha> (validated <id>)", exactly one
          `testflight` job, and the exact log line "Uploaded build <build> for sha <sha>".

Exit code 1 and ::error:: annotations on any failure. Only constrained values are echoed.
"""
import argparse
import json
import re
import sys

VALIDATE_PATH = ".github/workflows/ios-shared-validate.yml"
TESTFLIGHT_PATH = ".github/workflows/ios-testflight.yml"
TS = r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z )?"
CLONE_LINE = re.compile(r"^" + TS + r"(Cloned [^ ]+ at [0-9a-f]{40})$")
SHA_RE = re.compile(r"[0-9a-f]{40}")
BUILD_RE = re.compile(r"[1-9][0-9]{0,8}")


def first_clone_line(log):
    """First line of the form 'Cloned <ref> at <40-hex>' (timestamp prefix optional), or None."""
    for line in log.replace("\r", "").split("\n"):
        m = CLONE_LINE.match(line)
        if m:
            return m.group(1)
    return None


def has_upload_line(log, build, sha):
    want = re.compile(r"^" + TS + re.escape(f"Uploaded build {build} for sha {sha}") + r"$")
    return any(want.match(line) for line in log.replace("\r", "").split("\n"))


def _common(run, workflow, compare, repo, expected_path, errors):
    if (run.get("repository") or {}).get("full_name") != repo:
        errors.append("the run belongs to another repository")
    if workflow.get("path") != expected_path:
        errors.append(f"the run is not a {expected_path.rsplit('/', 1)[-1]} run")
    if run.get("status") != "completed":
        errors.append(f"the run is not completed (status={run.get('status')})")
    if run.get("event") != "workflow_dispatch":
        errors.append(f"the run was not started by workflow_dispatch (event={run.get('event')})")
    if run.get("head_branch") != "main":
        errors.append(f"the run did not run from main (head_branch={run.get('head_branch')})")
    if compare.get("status") not in ("ahead", "identical"):
        errors.append("the run's workflow revision is not an ancestor of the current main")


def _single_job(jobs, name, errors):
    matching = [j for j in jobs if j.get("name") == name]
    if len(matching) != 1:
        errors.append(f"expected exactly one {name} job in the run, found {len(matching)}")
        return None
    return matching[0]


def check_validate(run, workflow, compare, jobs, log, repo, sha):
    errors = []
    if not SHA_RE.fullmatch(sha):
        return ["sha must be 40 lowercase hex characters"]
    _common(run, workflow, compare, repo, VALIDATE_PATH, errors)
    if run.get("conclusion") != "success":
        errors.append(f"the run did not succeed (conclusion={run.get('conclusion')})")
    job = _single_job(jobs, "validate", errors)
    if job is not None and job.get("conclusion") != "success":
        errors.append(f"the validate job did not succeed (conclusion={job.get('conclusion')})")
    first = first_clone_line(log)
    if first != f"Cloned main at {sha}":
        errors.append(f"the first clone line of the validate job is not exactly 'Cloned main at {sha}'")
    return errors


def check_release(run, workflow, compare, jobs, log, repo, sha, build):
    errors = []
    if not SHA_RE.fullmatch(sha) or not BUILD_RE.fullmatch(build):
        return ["sha or resume_build is malformed"]
    _common(run, workflow, compare, repo, TESTFLIGHT_PATH, errors)
    title = run.get("display_title") or ""
    if not re.fullmatch(r"ios-testflight release " + sha + r" \(validated [0-9]+\)", title):
        errors.append(f"the release run was not an ios-testflight release run of {sha}")
    job = _single_job(jobs, "testflight", errors)
    if job is not None and not has_upload_line(log, build, sha):
        errors.append(f"the release run log has no line 'Uploaded build {build} for sha {sha}'")
    return errors


def _load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", choices=["validate", "release"])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--build", default="")
    for name in ("run", "workflow", "compare", "jobs", "log"):
        parser.add_argument(f"--{name}", required=True)
    a = parser.parse_args(argv)
    try:
        run, workflow, compare, jobs = _load(a.run), _load(a.workflow), _load(a.compare), _load(a.jobs)
        with open(a.log, "r", encoding="utf-8", errors="replace") as fh:
            log = fh.read()
    except (OSError, ValueError):
        print("::error::could not read the run evidence")
        return 1
    if a.kind == "validate":
        errors = check_validate(run, workflow, compare, jobs, log, a.repo, a.sha)
    else:
        errors = check_release(run, workflow, compare, jobs, log, a.repo, a.sha, a.build)
    for message in errors:
        print(f"::error::{message}")
    if not errors:
        print(f"{a.kind} run verified for {a.sha}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
