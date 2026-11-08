#!/usr/bin/env python3
"""Bounded, literal capture-probe artifact audit; not native build attestation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import struct
import subprocess
import sys
import tempfile

SOURCE_PATH = 'ios/App/SongId/StreamAudioCapture.swift'
PROFILE = {
    'source_path': SOURCE_PATH,
    'source_sha256': 'acb3030f4b5d06f220280e8a145599382af04e49168cb5a0328f9ba61643483d',
    'inventory_sha256': 'cc4e955ebef2bc13a47d0112a1dd55cd79bd1706accd8fdeee8f9f887dcb9b0d',
    'lines': 54, 'occurrences': 64,
    'slots': [
        (114, 'struct', '8984ee4a86b539291273a0d17220dd682b82d0aa7f5d43fcea5d559c4c20738f'),
        (135, 'struct', '26d1b700d6526f78957f15f0e40527a3aa5557557828ed472c1313d2b4675d18'),
        (146, 'struct', 'f79386bea1abaa604943fb34bd2b34298be023b75b33b857bf1b9886424c17bb'),
        (356, 'var', 'ae07e2682d6c7d6540e542690beb57f2f053bcdfe41ac1fdee557673f816d2e9'),
        (357, 'var', 'aa1ffe768eec501e57df8000420428f1346efc2760a60d33737960b027acc4d2'),
        (358, 'var', '429521fd9953f2ef10ea9a9bf9f81632ce95aff94d8d688b678fa444e3bfa5b6'),
        (359, 'var', 'd71fcaca67fcdb90962d07b2dce2d3404035cdc5ff6550df294a88095932d601'),
        (607, 'func', '4d95db79ba687fedbb48abe58973a98010cb7eaa554af7dd5dc04d3d469f4d29'),
    ],
}
CHUNK = 1024 * 1024


class AuditError(Exception):
    pass


def fail(code):
    raise AuditError(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(path, directory=False):
    if '..' in Path(path).parts:
        fail('UNSAFE_PATH')
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail('UNSAFE_PATH')
    if directory and not stat.S_ISDIR(info.st_mode):
        fail('INVALID_DIRECTORY')
    if not directory and not stat.S_ISREG(info.st_mode):
        fail('INVALID_FILE')
    return path


def read_bounded(path, maximum):
    path = safe_path(path)
    if path.stat().st_size > maximum:
        fail('FILE_SIZE_INVALID')
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        fail('FILE_SIZE_INVALID')
    return data


def git(root, *args):
    try:
        result = subprocess.run(['git', '-C', str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=False)
        if result.returncode:
            fail('GIT_FAILED')
        return result.stdout
    except subprocess.TimeoutExpired:
        fail('GIT_TIMEOUT')


def markers_for_source(data, profile):
    try:
        data.decode('utf-8')
    except UnicodeDecodeError:
        fail('SOURCE_UTF8_INVALID')
    known = {slot[2] for slot in profile['slots']}
    if digest(data) != profile['source_sha256']:
        identifiers = re.findall(rb'[A-Za-z_][A-Za-z_0-9]*', data)
        if any(digest(name) in known for name in identifiers) or re.search(rb'\bstats\s*\.\s*probe\b', data):
            fail('SOURCE_DRIFT')
        return None
    lines = data.splitlines()
    markers = []
    for line, kind, expected in profile['slots']:
        if line > len(lines):
            fail('SOURCE_SLOT_INVALID')
        match = re.search(rb'\b' + kind.encode('ascii') + rb'\s+([A-Za-z_][A-Za-z_0-9]*)', lines[line - 1])
        if not match or digest(match.group(1)) != expected:
            fail('SOURCE_SLOT_INVALID')
        markers.append(match.group(1))
    if len(markers) != 8 or len(set(markers)) != 8:
        fail('SOURCE_SLOT_INVALID')
    return markers


def scan(path, kind, markers):
    path = safe_path(path)
    size = path.stat().st_size
    if size < 32 or size > 1024 * 1024 * 1024:
        fail('BINARY_SIZE_INVALID')
    counts = [0] * len(markers or [])
    h = hashlib.sha256()
    with path.open('rb') as stream:
        header = stream.read(32)
        if len(header) != 32:
            fail('BINARY_HEADER_INVALID')
        magic, cpu, _, filetype = struct.unpack('<IIII', header[:16])
        if (magic, cpu, filetype) != (0xfeedfacf, 0x0100000c, kind):
            fail('BINARY_HEADER_INVALID')
        stream.seek(0)
        tail = b''
        overlap = max((len(m) for m in markers or []), default=1) - 1
        observed = 0
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            observed += len(chunk)
            if observed > 1024 * 1024 * 1024:
                fail('BINARY_SIZE_INVALID')
            h.update(chunk)
            block = tail + chunk
            for i, marker in enumerate(markers or []):
                start = 0
                while True:
                    pos = block.find(marker, start)
                    if pos < 0:
                        break
                    if pos + len(marker) > len(tail):
                        counts[i] += 1
                    start = pos + 1
            tail = block[-overlap:] if overlap else b''
    if observed != size:
        fail('BINARY_SIZE_INVALID')
    return {'size': observed, 'sha256': h.hexdigest(), 'counts': {f'M{i:02d}': n for i, n in enumerate(counts, 1)}, 'outcome': 'NOT_RUN' if markers is None else 'OBSERVED'}


def app_info(path, expected):
    safe_path(path, directory=True)
    try:
        info = plistlib.loads(read_bounded(path / 'Info.plist', 1024 * 1024))
    except (plistlib.InvalidFileException, ValueError, TypeError, OverflowError):
        fail('PLIST_INVALID')
    if not isinstance(info, dict) or any(info.get(k) != v for k, v in {'CFBundleIdentifier': expected, 'CFBundleExecutable': 'RadioShake', 'DTPlatformName': 'iphonesimulator'}.items()):
        fail('PLIST_INVALID')


def _audit(source_root, source_sha, ref_sha256, ci_sha, debug_app, release_app, profile):
    receipt = {'schema': 1, 'profile': 'capture-probe-release-v1', 'status': 'FAIL', 'reason': 'INCOMPLETE', 'binary_result': 'NOT_RUN'}
    try:
        if not all(re.fullmatch(r'[0-9a-f]{%d}' % length, value) for value, length in ((source_sha, 40), (ref_sha256, 64), (ci_sha, 40))):
            fail('ARGUMENT_INVALID')
        receipt.update(source_sha=source_sha, ref_sha256=ref_sha256, ci_sha=ci_sha)
        receipt['checker_sha256'] = digest(read_bounded(Path(__file__), 2 * 1024 * 1024))
        root = safe_path(source_root, directory=True)
        if git(root, 'rev-parse', 'HEAD').strip() != source_sha.encode('ascii'):
            fail('GIT_HEAD_MISMATCH')
        source = read_bounded(root / profile['source_path'], 2 * 1024 * 1024)
        if not source:
            fail('SOURCE_EMPTY')
        blob = source_sha + ':' + profile['source_path']
        blob_size = git(root, 'cat-file', '-s', blob).strip()
        if not blob_size.isdigit() or int(blob_size) > 2 * 1024 * 1024:
            fail('GIT_BLOB_INVALID')
        if git(root, 'show', blob) != source:
            fail('GIT_BLOB_MISMATCH')
        receipt['source_sha256'] = digest(source)
        markers = markers_for_source(source, profile)
        receipt['source_guard'] = {'status': 'MATCHED_REVIEWED' if markers else 'NOT_APPLICABLE', 'lines': profile['lines'] if markers else 0, 'occurrences': profile['occurrences'] if markers else 0, 'inventory_sha256': profile['inventory_sha256'] if markers else None}
        debug_app, release_app = Path(debug_app), Path(release_app)
        app_info(debug_app, 'com.radioshake.debug')
        app_info(release_app, 'com.radioshake')
        if os.path.lexists(release_app / 'RadioShake.debug.dylib'):
            fail('RELEASE_COMPANION_PRESENT')
        bins = {}
        receipt['binaries'] = bins
        bins['debug_launcher'] = scan(debug_app / 'RadioShake', 2, markers)
        bins['debug_code'] = scan(debug_app / 'RadioShake.debug.dylib', 6, markers)
        bins['release_main'] = scan(release_app / 'RadioShake', 2, markers)
        if bins['debug_code']['sha256'] == bins['release_main']['sha256']:
            fail('IDENTICAL_BINARIES')
        if markers is None:
            receipt.update(status='NOT_APPLICABLE', reason='KNOWN_FEATURE_ABSENT')
            return receipt
        receipt['binary_result'] = 'FAIL'
        for ident, count in bins['debug_code']['counts'].items():
            if count == 0:
                bins['debug_code']['outcome'] = 'FAIL'
                fail('DEBUG_MISSING_' + ident)
        bins['debug_code']['outcome'] = 'PASS'
        for ident, count in bins['release_main']['counts'].items():
            if count:
                bins['release_main']['outcome'] = 'FAIL'
                fail('RELEASE_PRESENT_' + ident)
        bins['release_main']['outcome'] = 'PASS'
        bins['debug_launcher']['outcome'] = 'REPORTED'
        receipt.update(status='PASS', reason='OK', binary_result='PASS')
    except AuditError as exc:
        receipt['reason'] = str(exc)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        receipt['reason'] = 'INPUT_ERROR'
    return receipt


def audit(source_root, source_sha, ref_sha256, ci_sha, debug_app, release_app):
    return _audit(source_root, source_sha, ref_sha256, ci_sha, debug_app, release_app, PROFILE)



_IDENTITY_FIELDS = ('source_sha', 'source_sha256', 'ref_sha256', 'ci_sha', 'checker_sha256', 'source_guard')
_BINARY_FIELDS = ('size', 'sha256', 'counts')


def _check_canary_result(baseline, observed, expected_sha256, expected_size):
    try:
        if baseline['status'] != 'PASS' or baseline['binary_result'] != 'PASS':
            fail('CANARY_RESULT_MISMATCH')
        if (observed['status'], observed['reason'], observed['binary_result']) != ('FAIL', 'RELEASE_PRESENT_M01', 'FAIL'):
            fail('CANARY_RESULT_MISMATCH')
        for key in _IDENTITY_FIELDS:
            if baseline[key] != observed[key]:
                fail('CANARY_RESULT_MISMATCH')
        for role in ('debug_launcher', 'debug_code'):
            for key in _BINARY_FIELDS:
                if baseline['binaries'][role][key] != observed['binaries'][role][key]:
                    fail('CANARY_RESULT_MISMATCH')
        if observed['binaries']['debug_launcher']['outcome'] != 'OBSERVED' or observed['binaries']['debug_code']['outcome'] != 'PASS':
            fail('CANARY_RESULT_MISMATCH')
        release = observed['binaries']['release_main']
        counts = {f'M{i:02d}': int(i == 1) for i in range(1, 9)}
        if release['outcome'] != 'FAIL' or release['counts'] != counts or release['sha256'] != expected_sha256 or release['size'] != expected_size:
            fail('CANARY_RESULT_MISMATCH')
    except (KeyError, TypeError):
        fail('CANARY_RESULT_MISMATCH')


def _copy_release(source, dest, baseline, marker):
    source = safe_path(source)
    size = source.stat().st_size
    if size != baseline['size'] or size + len(marker) > 1024 * 1024 * 1024:
        fail('COPY_SIZE_INVALID')
    original_hash, mutated_hash = hashlib.sha256(), hashlib.sha256()
    total = 0
    with source.open('rb') as inp, dest.open('xb') as out:
        os.chmod(dest, 0o600)
        while True:
            chunk = inp.read(CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > size:
                fail('COPY_SIZE_INVALID')
            original_hash.update(chunk)
            mutated_hash.update(chunk)
            out.write(chunk)
    if total != size or original_hash.hexdigest() != baseline['sha256']:
        fail('COPY_MISMATCH')
    mutated_hash.update(marker)
    return mutated_hash.hexdigest(), size + len(marker)


def _append_marker(path, marker):
    with path.open('ab') as stream:
        stream.write(marker)


def _cleanup_copy(directory):
    directory.cleanup()


def _canary(source_root, source_sha, ref_sha256, ci_sha, debug_app, release_app, baseline, baseline_receipt_sha256, profile):
    result = {'schema': 1, 'profile': 'capture-probe-native-canary-v1', 'status': 'FAIL', 'reason': 'BASELINE_NOT_PASS', 'control': 'NOT_RUN'}
    if baseline.get('status') == 'NOT_APPLICABLE':
        result.update(status='NOT_APPLICABLE', reason='KNOWN_FEATURE_ABSENT')
        return result
    if baseline.get('status') != 'PASS':
        return result
    directory = None
    try:
        if not re.fullmatch(r'[0-9a-f]{64}', baseline_receipt_sha256):
            fail('CANARY_INPUT_INVALID')
        for key, value in (('source_sha', source_sha), ('ref_sha256', ref_sha256), ('ci_sha', ci_sha)):
            if baseline[key] != value:
                fail('CANARY_INPUT_INVALID')
        source = read_bounded(Path(source_root) / profile['source_path'], 2 * 1024 * 1024)
        markers = markers_for_source(source, profile)
        if markers is None or digest(source) != baseline['source_sha256']:
            fail('CANARY_INPUT_INVALID')
        marker = markers[0]
        plist_hashes = {role: digest(read_bounded(Path(app) / 'Info.plist', 1024 * 1024)) for role, app in (('debug', debug_app), ('release', release_app))}
        directory = tempfile.TemporaryDirectory(prefix='capture-canary-', dir=os.path.realpath('/tmp'))
        bundle = Path(directory.name) / 'Release.app'
        bundle.mkdir(mode=0o700)
        plist_data = read_bounded(Path(release_app) / 'Info.plist', 1024 * 1024)
        if digest(plist_data) != plist_hashes['release']:
            fail('ORIGINAL_CHANGED')
        with (bundle / 'Info.plist').open('xb') as stream:
            os.chmod(bundle / 'Info.plist', 0o600)
            stream.write(plist_data)
        expected_hash, expected_size = _copy_release(Path(release_app) / 'RadioShake', bundle / 'RadioShake', baseline['binaries']['release_main'], marker)
        _append_marker(bundle / 'RadioShake', marker)
        observed = _audit(source_root, source_sha, ref_sha256, ci_sha, debug_app, bundle, profile)
        _check_canary_result(baseline, observed, expected_hash, expected_size)
        original = _audit(source_root, source_sha, ref_sha256, ci_sha, debug_app, release_app, profile)
        if original.get('status') != 'PASS' or original.get('reason') != 'OK' or original.get('binary_result') != 'PASS':
            fail('ORIGINAL_CHANGED')
        for key in _IDENTITY_FIELDS:
            if original[key] != baseline[key]:
                fail('ORIGINAL_CHANGED')
        for role in ('debug_launcher', 'debug_code', 'release_main'):
            if original['binaries'][role] != baseline['binaries'][role]:
                fail('ORIGINAL_CHANGED')
        for role, app in (('debug', debug_app), ('release', release_app)):
            if digest(read_bounded(Path(app) / 'Info.plist', 1024 * 1024)) != plist_hashes[role]:
                fail('ORIGINAL_CHANGED')
        result.update(reason='INCOMPLETE', control='PASS', marker='M01', baseline_receipt_sha256=baseline_receipt_sha256,
                      provenance={key: baseline[key] for key in _IDENTITY_FIELDS},
                      binaries={role: baseline['binaries'][role] for role in ('debug_launcher', 'debug_code', 'release_main')},
                      plist_sha256=plist_hashes, mutated_sha256=expected_hash, mutated_size=expected_size,
                      counts=observed['binaries']['release_main']['counts'], observed_reason=observed['reason'], original_integrity='PASS')
    except AuditError as exc:
        result['reason'] = str(exc)
    except Exception:
        result['reason'] = 'CANARY_IO_ERROR'
    finally:
        if directory is not None:
            try:
                _cleanup_copy(directory)
                result['cleanup'] = 'PASS'
            except Exception:
                result.update(status='FAIL', reason='CLEANUP_FAILED', cleanup='FAIL')
    if result['reason'] == 'INCOMPLETE' and result.get('cleanup') == 'PASS':
        result.update(status='PASS', reason='OK')
    return result


def canary(source_root, source_sha, ref_sha256, ci_sha, debug_app, release_app, baseline, baseline_receipt_sha256):
    return _canary(source_root, source_sha, ref_sha256, ci_sha, debug_app, release_app, baseline, baseline_receipt_sha256, PROFILE)


def write_receipt(path, receipt):
    if '..' in Path(path).parts:
        fail('UNSAFE_PATH')
    path = Path(os.path.abspath(path))
    parent = safe_path(path.parent, directory=True)
    if os.path.lexists(path):
        safe_path(path)
    fd, temporary = tempfile.mkstemp(prefix='.capture-audit-', dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(receipt, stream, sort_keys=True, separators=(',', ':'))
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv):
    class QuietParser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError('invalid arguments')
    parser = QuietParser(add_help=False, allow_abbrev=False)
    for arg in ('source-root', 'source-sha', 'ref-sha256', 'ci-sha', 'debug-app', 'release-app', 'receipt', 'canary-receipt'):
        parser.add_argument('--' + arg, required=True)
    try:
        args = parser.parse_args(argv)
    except (SystemExit, ValueError):
        print('FAIL ARGUMENT_INVALID', file=sys.stderr)
        return 2
    try:
        paths = [Path(args.receipt), Path(args.canary_receipt)]
        for path in paths:
            if '..' in path.parts:
                fail('UNSAFE_PATH')
            safe_path(path.parent, directory=True)
            if os.path.lexists(path):
                safe_path(path)
        if os.path.abspath(paths[0]) == os.path.abspath(paths[1]) or (paths[0].exists() and paths[1].exists() and os.path.samefile(*paths)):
            fail('UNSAFE_PATH')
        write_receipt(paths[0], {'schema': 1, 'profile': 'capture-probe-release-v1', 'status': 'FAIL', 'reason': 'INCOMPLETE'})
        write_receipt(paths[1], {'schema': 1, 'profile': 'capture-probe-native-canary-v1', 'status': 'FAIL', 'reason': 'INCOMPLETE', 'control': 'NOT_RUN'})
        result = audit(Path(args.source_root), args.source_sha, args.ref_sha256, args.ci_sha, Path(args.debug_app), Path(args.release_app))
        write_receipt(paths[0], result)
        receipt_hash = digest(read_bounded(paths[0], 2 * 1024 * 1024))
        supplement = canary(Path(args.source_root), args.source_sha, args.ref_sha256, args.ci_sha, Path(args.debug_app), Path(args.release_app), result, receipt_hash)
        write_receipt(paths[1], supplement)
    except Exception:
        print('FAIL RECEIPT_ERROR', file=sys.stderr)
        return 1
    print(result['status'], result['reason'], supplement['status'], supplement['reason'])
    return 0 if (result['status'], supplement['status']) in (('PASS', 'PASS'), ('NOT_APPLICABLE', 'NOT_APPLICABLE')) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
