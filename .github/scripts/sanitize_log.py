#!/usr/bin/env python3
"""Print a log-safe digest of a build log for the public Actions log.

Usage: sanitize_log.py LOGFILE [PROJECT_ROOT]
       sanitize_log.py --export EXPORT_LOGFILE

--export is for `xcodebuild -exportArchive` output: Apple tool messages (see EXPORT_KEEP) are
shown, including the matching lines of the *.log files inside any IDEDistribution
`*.xcdistributionlogs` folder the output names. Lines mentioning key material are dropped,
absolute paths become <path>, runs of 20+ base64/hex-like characters are redacted (the listed
Apple keywords such as NSLocalizedDescription are preserved), 200 characters per line, 20 lines.

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

# --export mode
EXPORT_KEEP = re.compile(
    r"^\s*error:|exportArchive:|\*\* EXPORT FAILED \*\*|IDEDistribution|Provisioning profile|"
    r"signing certificate|No profiles|Error Domain=|NSLocalizedDescription|NSLocalizedRecoverySuggestion",
    re.I)
EXPORT_KEYWORDS = re.compile(r"(NSLocalizedDescription|NSLocalizedRecoverySuggestion|IDEDistribution|Domain=[A-Za-z]+)")
EXPORT_DROP = re.compile(r"asc-key|AuthKey|\.p8\b|PRIVATE KEY|-authentication(?:Key|Issuer)|eyJ[A-Za-z0-9_-]{10,}", re.I)
DIST_FOLDER = re.compile(r"(/[^\s'\"()]+\.xcdistributionlogs)")
EXPORT_MAX_LINES = 20
EXPORT_MAX_CHARS = 200
MAX_DIST_LOG_BYTES = 2_000_000


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


def _redact_keeping_keywords(text):
    parts = EXPORT_KEYWORDS.split(text)
    return "".join(p if EXPORT_KEYWORDS.fullmatch(p) else REDACT.sub("<redacted>", p) for p in parts)


def sanitize_export_lines(lines, limit=EXPORT_MAX_LINES, seen=None, out=None):
    out = [] if out is None else out
    seen = set() if seen is None else seen
    for raw in lines:
        line = raw.rstrip("\n")
        if len(out) >= limit:
            break
        if not EXPORT_KEEP.search(line) or EXPORT_DROP.search(line):
            continue
        line = _redact_keeping_keywords(PATH.sub("<path>", line)).strip()[:EXPORT_MAX_CHARS]
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return out


def export_digest(log_path):
    """Sanitised Apple tool messages from the export output and its IDEDistribution log folders."""
    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    out, seen = [], set()
    sanitize_export_lines(text.splitlines(), out=out, seen=seen)
    folders = []
    for m in DIST_FOLDER.finditer(text):
        folder = m.group(1)
        if folder not in folders and ".." not in folder.split("/") and os.path.isdir(folder):
            folders.append(folder)
    for folder in folders:
        try:
            names = sorted(n for n in os.listdir(folder) if n.endswith(".log"))
        except OSError:
            continue
        for name in names:
            try:
                with open(os.path.join(folder, name), "r", encoding="utf-8", errors="replace") as fh:
                    sanitize_export_lines(fh.read(MAX_DIST_LOG_BYTES).splitlines(), out=out, seen=seen)
            except OSError:
                continue
    return out


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--export":
        try:
            lines = export_digest(sys.argv[2])
        except OSError:
            print("(no export log captured)")
            return 0
        print("\n".join(lines) if lines else "(no Apple tool messages matched in the export output)")
        return 0
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
