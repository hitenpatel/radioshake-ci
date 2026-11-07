#!/usr/bin/env python3
"""Private single-class diagnostic evidence helpers. Never print native output."""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

SELECTOR = re.compile(r'(RadioShakeTests|RadioShakeUITests)/([A-Za-z_][A-Za-z0-9_]*)')
CASE = re.compile(r"^Test Case '-\[(?:(RadioShakeTests|RadioShakeUITests)\.)?([A-Za-z_][A-Za-z0-9_]*) ([A-Za-z_][A-Za-z0-9_]*)\]' (passed|failed|skipped) \([0-9.]+ seconds\)\.$")
SUMMARY = re.compile(r'^\s*Executed ([0-9]+) tests?, with ([0-9]+) failures? \([0-9]+ unexpected\) in [0-9.]+ \([0-9.]+\) seconds$')
SUITE = re.compile(r"^Test Suite '([A-Za-z_][A-Za-z0-9_]*)' (passed|failed) at .+\.$")


def validate_inputs(ref, sha, selector, request_id, device):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', ref):
        raise ValueError('invalid ref')
    if subprocess.run(['git', 'check-ref-format', '--branch', ref], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        raise ValueError('invalid ref')
    if not re.fullmatch(r'[0-9a-f]{40}', sha) or not SELECTOR.fullmatch(selector):
        raise ValueError('invalid selection')
    try:
        valid_uuid = str(uuid.UUID(request_id, version=4)) == request_id
    except (ValueError, AttributeError):
        valid_uuid = False
    if not valid_uuid or device not in ('iPhone 17', 'iPhone SE (3rd generation)'):
        raise ValueError('invalid request')
    return dict(ref=ref, sha=sha, selector=selector, request_id=request_id, device=device)


def summarize_execution(selector, xcode_exit, log_text, xcresult_present):
    target, selected = SELECTOR.fullmatch(selector).groups()
    counts = dict(passed=0, failed=0, skipped=0)
    seen = set(); invalid = False; suites = []; pending = False; summaries = []
    for line in log_text.splitlines():
        if pending:
            summary = SUMMARY.fullmatch(line)
            if summary:
                summaries.append(tuple(map(int, summary.groups())))
            pending = False
        m = CASE.fullmatch(line)
        if m:
            module, cls, name, outcome = m.groups()
            key = (module, cls, name)
            if cls != selected or (module and module != target) or key in seen:
                invalid = True
            else:
                seen.add(key); counts[outcome] += 1
        elif line.startswith('Test Case ') and not line.endswith(' started.'):
            invalid = True
        m = SUITE.fullmatch(line)
        if m and m.group(1) == selected:
            suites.append(m.group(2)); pending = True
    complete = bool(xcresult_present and len(suites) == 1 and seen and counts['passed'] + counts['failed'] > 0 and not invalid and summaries == [(sum(counts.values()), counts['failed'])])
    outcome = 'passed' if complete and xcode_exit == 0 and not counts['failed'] and suites == ['passed'] else ('failed' if complete else 'incomplete')
    return dict(xcode_exit=xcode_exit, xcresult_present=bool(xcresult_present), **counts, outcome=outcome)


def make_manifest(inputs, runtime, stages, execution):
    return dict(schema_version=1, inputs=inputs, runtime=runtime, stages=stages, execution=execution)


def print_summary(execution):
    print('DIAGNOSTIC_' + execution['outcome'].upper())
    print('passed={} failed={} skipped={}'.format(execution['passed'], execution['failed'], execution['skipped']))


def scan(directory, token):
    if not token or not shutil.which('zstd'):
        return False
    needles = [token.encode(), base64.b64encode(token.encode()), base64.b64encode(b'oauth2:' + token.encode())]
    for path in Path(directory).rglob('*'):
        if path.is_symlink():
            return False
        if not path.is_file():
            continue
        data = path.read_bytes()
        if any(n in data for n in needles):
            return False
        if data.startswith(b'\x28\xb5\x2f\xfd'):
            p = subprocess.run(['zstd', '-dcq'], input=data, capture_output=True)
            if p.returncode or any(n in p.stdout for n in needles):
                return False
    return True


def main():
    p = argparse.ArgumentParser(); p.add_argument('command', choices=['validate','manifest','scan'])
    a = p.parse_args()
    try:
        if a.command == 'validate':
            validate_inputs(*(os.environ[x] for x in ('REF','SHA','SELECTOR','REQUEST_ID','DEVICE')))
            print('INPUT_OK'); return 0
        if a.command == 'scan':
            ok = scan(os.environ['EVIDENCE'], os.environ.get('FORGEJO_CLONE_TOKEN',''))
            print('SCAN_OK' if ok else 'SCAN_FAILED'); return 0 if ok else 1
        root = Path(os.environ['EVIDENCE']); inputs = validate_inputs(*(os.environ[x] for x in ('REF','SHA','SELECTOR','REQUEST_ID','DEVICE')))
        stages = json.loads((root/'stages.json').read_text())
        runtime = json.loads((root/'runtime.json').read_text())
        log = (root/'test.log').read_text(errors='replace') if (root/'test.log').exists() else ''
        execution = summarize_execution(inputs['selector'], stages.get('test'), log, (root/'tests.xcresult').is_dir())
        manifest = make_manifest(inputs,runtime,stages,execution)
        manifest['file_hashes'] = {str(f.relative_to(root)): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(root.rglob('*')) if f.is_file() and not f.is_symlink() and f.name != 'manifest.json'}
        (root/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        print_summary(execution)
        return 0 if execution['outcome'] == 'passed' and all(v == 0 for v in stages.values()) else 1
    except Exception:
        print('DIAGNOSTIC_ERROR'); return 1

if __name__ == '__main__':
    sys.exit(main())
