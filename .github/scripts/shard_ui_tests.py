#!/usr/bin/env python3
"""Deterministically split the iOS XCUITest classes into balanced shards for ios-shared-validate.

Enumerates test classes from the cloned source (every `class X: XCTestCase` under the UI test
directory that has at least one `func testY()`), minus the excluded ones (the screenshot tour,
which must never run in the public job), sorts them and assigns them greedily, heaviest first, to
the shard with the smallest load relative to its capacity. Weight is the measured seconds per class
from ui_test_weights.json when present, otherwise the method count times a default per-method cost.

Every invocation recomputes the full partition and fails (exit 1, ::error::) if it is not an exact
partition of the discovered classes (a class missing or in two shards), or if the requested shard is
empty. Class names are validated as Swift identifiers before they reach a command line.

  shard_ui_tests.py --ui-dir src/ios/UITests/RadioShakeUITests --shards 3 --index 1 \\
      --capacities 0.25,1,1 --weights .github/scripts/ui_test_weights.json --out /tmp/ui-shard-args.txt

Run the unit tests with: python3 -m unittest discover -s .github/scripts
"""
import argparse
import json
import os
import re
import sys

DEFAULT_SECONDS_PER_METHOD = 25.0
DEFAULT_EXCLUDE = ("ScreenshotTourUITests",)
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A column-0 type declaration starts a new section; test methods belong to the section they are in.
DECL = re.compile(
    r"^(?:(?:@\w+(?:\([^)]*\))?|final|open|public|internal|private|fileprivate)\s+)*"
    r"(class|struct|enum|actor|extension|protocol)\s+([A-Za-z_][A-Za-z0-9_.]*)\s*(?::\s*([^{]+?))?\s*(?:where\b[^{]*)?\{?\s*$"
)
TEST_FUNC = re.compile(r"^[ \t]+(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:final|override|public|open|internal)\s+)*func\s+(test\w*)\s*\(\s*\)")
PRIVATE_FUNC = re.compile(r"^[ \t]+(?:@\w+(?:\([^)]*\))?\s+)*(?:private|fileprivate)\b")
LINE_COMMENT = re.compile(r"//.*$")


class ShardError(Exception):
    pass


def _strip_comments(text):
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    return "\n".join(LINE_COMMENT.sub("", line) for line in text.split("\n"))


def parse_swift(text):
    """Return ({class: superclass_name_or_None}, {class_or_extended_type: [test method names]})."""
    supers, tests = {}, {}
    current = None
    for line in _strip_comments(text).split("\n"):
        m = DECL.match(line)
        if m:
            kind, name, inherits = m.group(1), m.group(2), m.group(3)
            current = name if kind in ("class", "extension") else None
            if kind == "class":
                first = (inherits or "").split(",")[0].strip()
                supers[name] = first or None
            continue
        if current is None:
            continue
        if line and not line[0].isspace() and line.strip() not in ("}", ""):
            current = None
            continue
        f = TEST_FUNC.match(line)
        if f and not PRIVATE_FUNC.match(line):
            tests.setdefault(current, []).append(f.group(1))
    return supers, tests


def discover(ui_dir, exclude=DEFAULT_EXCLUDE):
    """Return {class name: test method count} for every concrete XCTestCase subclass under ui_dir."""
    supers, tests = {}, {}
    swift_files = []
    for root, _dirs, names in os.walk(ui_dir):
        swift_files += [os.path.join(root, n) for n in names if n.endswith(".swift")]
    if not swift_files:
        raise ShardError(f"no Swift files under {ui_dir}")
    for path in sorted(swift_files):
        with open(path, "r", encoding="utf-8") as fh:
            s, t = parse_swift(fh.read())
        supers.update(s)
        for name, methods in t.items():
            tests.setdefault(name, []).extend(methods)

    def is_test_case(cls, seen=()):
        sup = supers.get(cls)
        if sup is None or cls in seen:
            return False
        return sup in ("XCTestCase", "XCTest.XCTestCase") or is_test_case(sup, seen + (cls,))

    result = {}
    for cls in sorted(supers):
        if cls in exclude or not is_test_case(cls):
            continue
        count = len(tests.get(cls, []))
        if count == 0:
            continue
        if not IDENT.match(cls):
            raise ShardError(f"unsafe class name {cls!r}")
        result[cls] = count
    return result


def split(classes, capacities, weights=None):
    """Greedy largest-first assignment. classes: {name: method count}. Returns list of sorted name lists."""
    if not capacities or any(c <= 0 for c in capacities):
        raise ShardError("capacities must be positive numbers")
    weights = weights or {}
    cost = {c: float(weights.get(c, n * DEFAULT_SECONDS_PER_METHOD)) for c, n in classes.items()}
    shards = [[] for _ in capacities]
    load = [0.0] * len(capacities)
    for cls in sorted(classes, key=lambda c: (-cost[c], c)):
        best = min(range(len(capacities)), key=lambda i: ((load[i] + cost[cls]) / capacities[i], i))
        shards[best].append(cls)
        load[best] += cost[cls]
    return [sorted(s) for s in shards], load


def verify_partition(classes, shards):
    seen = {}
    for i, group in enumerate(shards):
        for cls in group:
            if cls in seen:
                raise ShardError(f"class {cls} is in shards {seen[cls]} and {i}")
            seen[cls] = i
    missing = sorted(set(classes) - set(seen))
    extra = sorted(set(seen) - set(classes))
    if missing or extra:
        raise ShardError(f"shards are not a partition (missing={missing}, extra={extra})")


def _parse_capacities(raw, shards):
    caps = [float(x) for x in raw.split(",")] if raw else [1.0] * shards
    if len(caps) != shards:
        raise ShardError(f"--capacities needs {shards} values, got {len(caps)}")
    return caps


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ui-dir", required=True)
    p.add_argument("--shards", type=int, required=True)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--capacities", default="")
    p.add_argument("--weights", default="")
    p.add_argument("--bundle", default="RadioShakeUITests")
    p.add_argument("--exclude", default=",".join(DEFAULT_EXCLUDE))
    p.add_argument("--out", required=True, help="file receiving one -only-testing:Bundle/Class line per class")
    a = p.parse_args(argv)
    try:
        if not 0 <= a.index < a.shards:
            raise ShardError(f"--index {a.index} is outside 0..{a.shards - 1}")
        weights = {}
        if a.weights and os.path.exists(a.weights):
            with open(a.weights, "r", encoding="utf-8") as fh:
                weights = json.load(fh)
        exclude = tuple(x for x in a.exclude.split(",") if x)
        classes = discover(a.ui_dir, exclude)
        shards, load = split(classes, _parse_capacities(a.capacities, a.shards), weights)
        verify_partition(classes, shards)
        mine = shards[a.index]
        if not mine:
            raise ShardError(f"shard {a.index} has no test classes")
    except (ShardError, OSError, ValueError) as e:
        print(f"::error::{e}")
        return 1
    total = sum(classes.values())
    print(f"Discovered {len(classes)} UI test classes, {total} test methods; excluded: {', '.join(exclude) or '(none)'}")
    for i, group in enumerate(shards):
        print(f"shard {i}: {len(group)} classes, {sum(classes[c] for c in group)} methods, est {load[i]:.0f}s")
    print(f"shard {a.index} classes: {', '.join(mine)}")
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write("".join(f"-only-testing:{a.bundle}/{c}\n" for c in mine))
    return 0


if __name__ == "__main__":
    sys.exit(main())
