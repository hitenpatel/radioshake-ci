#!/usr/bin/env python3
"""Record one bounded host appearance stimulus around the native command."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

SLOTS = ('originalQuery', 'darkSetter', 'beforeQuery', 'afterQuery', 'restoreSetter', 'restoreQuery')
STREAM_LIMIT = 1024
SIDECAR_LIMIT = 16384


def _stream(file):
    file.flush()
    total = file.tell()
    file.seek(0)
    return {'prefixBase64': base64.b64encode(file.read(STREAM_LIMIT)).decode('ascii'),
            'totalBytes': total, 'truncated': total > STREAM_LIMIT}


def capture_simctl(argv, *, timeout=10, wall_clock=time.time, monotonic=time.monotonic):
    start = wall_clock()
    tick = monotonic()
    status, code = 'unknown', None
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            completed = subprocess.run(argv, stdout=stdout, stderr=stderr, timeout=timeout, check=False)
            code = completed.returncode
            status = 'ok' if code == 0 else 'error'
        except subprocess.TimeoutExpired:
            status = 'timeout'
        except (OSError, subprocess.SubprocessError):
            status = 'error'
        duration = monotonic() - tick
        end = wall_clock()
        out, err = _stream(stdout), _stream(stderr)
    if duration > timeout:
        status = 'overrun'
    appearance = None
    if not out['truncated']:
        try:
            value = base64.b64decode(out['prefixBase64']).decode('utf-8').strip().lower()
            if value in ('light', 'dark'):
                appearance = value
        except UnicodeDecodeError:
            pass
    return {'argv': list(argv), 'start': start, 'end': end, 'duration': duration,
            'timeout': timeout, 'returnCode': code, 'status': status, 'appearance': appearance,
            'stdout': out, 'stderr': err}


def _empty():
    return dict.fromkeys(('argv', 'start', 'end', 'duration', 'timeout', 'returnCode', 'appearance', 'stdout', 'stderr')) | {'status': 'notAttempted'}


def _write_sidecar(path, data):
    def encode():
        return json.dumps(data, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
    encoded = encode()
    if len(encoded) > SIDECAR_LIMIT:
        data['outputOmitted'] = True
        for item in data['commands'].values():
            for name in ('stdout', 'stderr'):
                stream = item[name]
                if stream is not None:
                    stream['prefixBase64'] = ''
                    stream['truncated'] = stream['totalBytes'] > 0
        encoded = encode()
    if len(encoded) > SIDECAR_LIMIT:
        raise ValueError('sidecar metadata exceeds limit')
    path.write_bytes(encoded)


def run_host_stimulus(udid: str, evidence: Path, command: list[str], identity: dict, *,
                      run_command=capture_simctl, run_child=lambda argv: subprocess.run(argv, check=False).returncode) -> int:
    commands = {slot: _empty() for slot in SLOTS}
    data = {'schema': 1, 'experiment': 'host-before-invocation-dark-v1',
            **identity, 'commands': commands, 'child': None, 'outputOmitted': False}
    base = ['xcrun', 'simctl', 'ui', udid, 'appearance']
    original = None
    result = 74

    def run(slot, argv):
        start = time.time()
        tick = time.monotonic()
        try:
            commands[slot] = run_command(argv)
        except Exception:
            elapsed = time.monotonic() - tick
            end = time.time()
            commands[slot] = {'argv': list(argv), 'start': start, 'end': end, 'duration': elapsed,
                              'timeout': 10, 'returnCode': None, 'status': 'overrun' if elapsed > 10 else 'error',
                              'appearance': None, 'stdout': None, 'stderr': None}
        return commands[slot]

    def successful(item):
        return item['status'] == 'ok' and item['returnCode'] == 0 and item['duration'] <= 10

    def eligible(item):
        return successful(item) and all(
            item[name] is not None and item[name]['totalBytes'] <= STREAM_LIMIT
            and not item[name]['truncated'] for name in ('stdout', 'stderr'))

    try:
        query = run('originalQuery', base)
        if successful(query) and query['appearance'] in ('light', 'dark'):
            original = query['appearance']
            setter = run('darkSetter', base + ['dark']) if eligible(query) else None
            if setter is not None and eligible(setter):
                before = run('beforeQuery', base)
                if eligible(before) and before['appearance'] == 'dark':
                    child = {'argv': list(command), 'start': time.time(), 'end': None, 'returnCode': None}
                    data['child'] = child
                    try:
                        result = run_child(command)
                        child['returnCode'] = result
                        child['end'] = time.time()
                    except OSError:
                        data['child'] = None
                    except Exception:
                        pass
                    if data['child'] is not None:
                        run('afterQuery', base)
    finally:
        if original is not None:
            run('restoreSetter', base + [original])
            run('restoreQuery', base)
        _write_sidecar(Path(evidence) / 'host-appearance-stimulus.json', data)
    return result


def main(argv):
    if len(argv) < 5 or argv[3] != '--':
        return 74
    udid, evidence = argv[1:3]
    identity = {'requestId': os.environ.get('REQUEST_ID', ''), 'sourceSHA': os.environ.get('SHA', ''),
                'workflowSHA': os.environ.get('GITHUB_SHA', ''), 'selector': os.environ.get('SELECTOR', ''),
                'destinationUDID': udid}
    return run_host_stimulus(udid, Path(evidence), argv[4:], identity)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
