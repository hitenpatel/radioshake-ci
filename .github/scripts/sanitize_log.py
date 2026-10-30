#!/usr/bin/env python3
"""Print a log-safe digest of a build log for the public Actions log.

Usage: sanitize_log.py LOGFILE [PROJECT_ROOT]

Keeps only error-looking lines, rewrites absolute paths (project paths become
relative, everything else becomes <path>), drops any line resembling a key or
token, truncates lines and caps the output.
"""
import re
import sys

KEEP = re.compile(r"error:|^e: |\*\* (ARCHIVE|EXPORT|BUILD) FAILED|FAILURE: |What went wrong|^error", re.I)
# Checked on the raw line (secret-looking markers) and on the path-rewritten line (long opaque runs,
# so that ordinary long paths are not mistaken for tokens).
DROP_RAW = re.compile(r"PRIVATE KEY|eyJ[A-Za-z0-9_-]{10,}|AuthKey|\.p8\b|-authentication(Key|Issuer)", re.I)
DROP_REWRITTEN = re.compile(r"[A-Za-z0-9+/=_-]{60,}")
PATH = re.compile(r"(?<![\w.:])(?:file://)?(/[^\s:'\"()\[\],;]+)")
MAX_LINES = 40
MAX_CHARS = 300


def sanitize(lines, root=""):
    root = root.rstrip("/")
    out, seen = [], set()

    def repl(match):
        path = match.group(1)
        if root and path.startswith(root + "/"):
            return path[len(root) + 1:]
        return "<path>"

    for line in lines:
        line = line.rstrip("\n")
        if not KEEP.search(line) or DROP_RAW.search(line):
            continue
        line = PATH.sub(repl, line)
        if DROP_REWRITTEN.search(line):
            continue
        line = line[:MAX_CHARS]
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
    print("\n".join(lines) if lines else "(no error lines found in the log)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
