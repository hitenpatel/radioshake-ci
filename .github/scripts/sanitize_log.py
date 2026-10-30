#!/usr/bin/env python3
"""Print a log-safe digest of a build log for the public Actions log.

Usage: sanitize_log.py LOGFILE [PROJECT_ROOT]

Only compiler diagnostics of the form `path:line:col: error: message` are shown, and:
  - the path must be an existing file under PROJECT_ROOT (shown relative); anything else
    becomes <path>, so build output cannot smuggle text through the path position
  - absolute paths inside the message are rewritten (project-relative or <path>)
  - every run of 20 or more base64/hex-like characters ([A-Za-z0-9+/=_-]) is redacted
  - each line is capped at 160 characters, at most 40 lines, duplicates dropped
All other log text (including Gradle, Xcode and export chatter) is deliberately not
printed: callers print their own fixed messages.
"""
import os
import re
import sys

ERROR = re.compile(r"^(?P<path>[^\s:]+):(?P<line>[0-9]+):(?P<col>[0-9]+): error: (?P<msg>.*)$")
PATH = re.compile(r"(?<![\w.:])(?:file://)?(/[^\s:'\"()\[\],;]+)")
REDACT = re.compile(r"[A-Za-z0-9+/=_-]{20,}")
MAX_LINES = 40
MAX_CHARS = 160


def sanitize(lines, root=""):
    root = root.rstrip("/")
    out, seen = [], set()

    def rel_if_project_file(path):
        if root and path.startswith(root + "/"):
            rel = path[len(root) + 1:]
            if ".." not in rel.split("/") and os.path.isfile(os.path.join(root, rel)):
                return rel
        return None

    def repl(match):
        return rel_if_project_file(match.group(1)) or "<path>"

    for raw in lines:
        m = ERROR.match(raw.rstrip("\n"))
        if not m:
            continue
        shown = rel_if_project_file(m.group("path")) or "<path>"
        msg = PATH.sub(repl, m.group("msg"))
        line = f"{shown}:{m.group('line')}:{m.group('col')}: error: {REDACT.sub('<redacted>', msg)}"[:MAX_CHARS]
        if line not in seen:
            seen.add(line)
            out.append(line)
        if len(out) >= MAX_LINES:
            break
    return out


def main():
    if len(sys.argv) < 2:
        print("usage: sanitize_log.py LOGFILE [ROOT]", file=sys.stderr)
        return 2
    try:
        with open(sys.argv[1], "r", encoding="utf-8", errors="replace") as fh:
            lines = sanitize(fh, sys.argv[2] if len(sys.argv) > 2 else "")
    except OSError:
        print("(no log captured)")
        return 0
    print("\n".join(lines) if lines else "(no compiler error lines in the log; see the failing step name above)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
