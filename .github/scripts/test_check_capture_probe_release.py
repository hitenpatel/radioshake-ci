"""Synthetic controls only: these fixtures are not native evidence."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

MODULE = Path(__file__).with_name('check_capture_probe_release.py')
if MODULE.exists():
    spec = importlib.util.spec_from_file_location('capture_audit', MODULE)
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
else:
    audit = None


def sha(data):
    return hashlib.sha256(data).hexdigest()


class CaptureAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.markers = [f'SyntheticMarker{i:02d}'.encode() for i in range(1, 9)]
        self.source = b'#if DEBUG\n' + b''.join(b'struct ' + m + b' {}\n' for m in self.markers) + b'#endif\n'
        self.profile = {'source_path': 'Synthetic.swift', 'source_sha256': sha(self.source),
                        'slots': [(i + 2, 'struct', sha(m)) for i, m in enumerate(self.markers)],
                        'inventory_sha256': 'a' * 64, 'lines': 54, 'occurrences': 64}
        (self.root / 'Synthetic.swift').write_bytes(self.source)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', 'Synthetic.swift'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
        self.head = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], text=True).strip()
        self.debug = self.app('debug', 'com.radioshake.debug', b' '.join(self.markers))
        self.release = self.app('release', 'com.radioshake', b'clean release')

    def app(self, name, bundle, code):
        app = self.root / (name + '.app')
        app.mkdir()
        (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': bundle, 'CFBundleExecutable': 'RadioShake', 'DTPlatformName': 'iphonesimulator'}))
        header = lambda kind: struct.pack('<IIIIIIII', 0xfeedfacf, 0x0100000c, 0, kind, 0, 0, 0, 0)
        (app / 'RadioShake').write_bytes(header(2) + (b'launcher' if name == 'debug' else code))
        if name == 'debug':
            (app / 'RadioShake.debug.dylib').write_bytes(header(6) + code)
        return app

    def run_audit(self):
        self.assertIsNotNone(audit, 'production checker is missing')
        return audit._audit(self.root, self.head, 'b' * 64, 'c' * 40, self.debug, self.release, self.profile)

    def test_synthetic_positive_and_individual_release_canaries(self):
        result = self.run_audit()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['source_guard']['status'], 'MATCHED_REVIEWED')
        self.assertEqual(result['binaries']['debug_code']['counts']['M01'], 1)
        self.assertEqual(result['binaries']['release_main']['counts']['M01'], 0)
        for i, marker in enumerate(self.markers, 1):
            with self.subTest(i=i):
                p = self.release / 'RadioShake'
                p.write_bytes(p.read_bytes()[:32] + marker)
                result = self.run_audit()
                self.assertEqual(result['status'], 'FAIL')
                self.assertIn(f'M{i:02d}', result['reason'])

    def test_each_missing_debug_marker_fails(self):
        p = self.debug / 'RadioShake.debug.dylib'
        for i, marker in enumerate(self.markers, 1):
            with self.subTest(i=i):
                p.write_bytes(p.read_bytes()[:32] + b' '.join(m for m in self.markers if m != marker))
                self.assertIn(f'M{i:02d}', self.run_audit()['reason'])

    def test_chunk_boundary_is_scanned(self):
        p = self.release / 'RadioShake'
        p.write_bytes(p.read_bytes()[:32] + b'x' * (1024 * 1024 - 35) + self.markers[0])
        self.assertIn('M01', self.run_audit()['reason'])

    def test_source_drift_and_old_source(self):
        p = self.root / 'Synthetic.swift'
        for data in (self.source + b'// comment\n', self.source.replace(b'#if DEBUG\n', b''), self.source.replace(b'#if DEBUG\n', b'').replace(b'#endif\n', b'#if DEBUG\n#endif\n'), self.source.replace(self.markers[0], self.markers[0][:-1], 1), b'stats . probe\n'):
            p.write_bytes(data)
            subprocess.run(['git', '-C', str(self.root), 'add', 'Synthetic.swift'], check=True)
            subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'drift'], check=True)
            self.head = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], text=True).strip()
            self.assertEqual(self.run_audit()['reason'], 'SOURCE_DRIFT')
        p.write_bytes(b'// old synthetic source\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'Synthetic.swift'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'old'], check=True)
        self.head = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], text=True).strip()
        result = self.run_audit()
        self.assertEqual(result['status'], 'NOT_APPLICABLE')
        self.assertEqual(result['binary_result'], 'NOT_RUN')

    def test_empty_tracked_source_fails_closed(self):
        p = self.root / 'Synthetic.swift'
        p.write_bytes(b'')
        subprocess.run(['git', '-C', str(self.root), 'add', 'Synthetic.swift'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'empty'], check=True)
        self.head = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], text=True).strip()
        self.assertEqual(self.run_audit()['reason'], 'SOURCE_EMPTY')

    def test_slot_digest_rejected(self):
        self.profile['slots'][0] = (2, 'struct', '0' * 64)
        self.assertEqual(self.run_audit()['reason'], 'SOURCE_SLOT_INVALID')

    def test_invalid_binaries_fail_closed(self):
        p = self.release / 'RadioShake'
        for data in (b'', b'bad', bytes(32), struct.pack('<IIIIIIII', 0xfeedfacf, 7, 0, 2, 0, 0, 0, 0), struct.pack('<IIIIIIII', 0xfeedfacf, 0x0100000c, 0, 6, 0, 0, 0, 0)):
            with self.subTest(size=len(data)):
                p.write_bytes(data)
                self.assertEqual(self.run_audit()['status'], 'FAIL')

    def test_hashes_bind_fixture_bytes(self):
        result = self.run_audit()
        self.assertEqual(result['source_sha256'], sha(self.source))
        for role, p in [('debug_launcher', self.debug / 'RadioShake'), ('debug_code', self.debug / 'RadioShake.debug.dylib'), ('release_main', self.release / 'RadioShake')]:
            self.assertEqual(result['binaries'][role]['sha256'], sha(p.read_bytes()))
            self.assertEqual(result['binaries'][role]['size'], p.stat().st_size)
        self.assertEqual(result['checker_sha256'], sha(MODULE.read_bytes()))

    def test_missing_source_and_head_mismatch_fail(self):
        (self.root / 'Synthetic.swift').unlink()
        self.assertEqual(self.run_audit()['status'], 'FAIL')
        (self.root / 'Synthetic.swift').write_bytes(self.source)
        self.head = '0' * 40
        self.assertEqual(self.run_audit()['status'], 'FAIL')

    def test_bad_plists_and_companions(self):
        plist = self.release / 'Info.plist'
        good = plist.read_bytes()
        for bad in (b'private-synthetic-canary', plistlib.dumps({'CFBundleIdentifier': 'wrong'})):
            plist.write_bytes(bad)
            self.assertEqual(self.run_audit()['reason'], 'PLIST_INVALID')
        plist.write_bytes(good)
        companion = self.release / 'RadioShake.debug.dylib'
        companion.write_bytes(b'x')
        self.assertEqual(self.run_audit()['reason'], 'RELEASE_COMPANION_PRESENT')
        companion.unlink()
        (self.debug / 'RadioShake.debug.dylib').unlink()
        self.assertEqual(self.run_audit()['reason'], 'INPUT_ERROR')

    def test_symlink_and_directory_rejected(self):
        target = self.release / 'RadioShake'
        target.unlink()
        target.symlink_to(self.debug / 'RadioShake')
        self.assertEqual(self.run_audit()['reason'], 'UNSAFE_PATH')
        target.unlink()
        target.mkdir()
        self.assertEqual(self.run_audit()['reason'], 'INVALID_FILE')
        target.rmdir()
        alias = self.root / 'alias'
        alias.symlink_to(self.release, target_is_directory=True)
        self.release = alias
        self.assertEqual(self.run_audit()['reason'], 'UNSAFE_PATH')

    def test_git_timeout_nonzero_and_unreadable_are_fixed(self):
        with mock.patch.object(audit.subprocess, 'run', side_effect=subprocess.TimeoutExpired('private-canary', 1)):
            self.assertEqual(self.run_audit()['reason'], 'GIT_TIMEOUT')
        with mock.patch.object(audit.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', b'private-canary')):
            self.assertEqual(self.run_audit()['reason'], 'GIT_FAILED')
        with mock.patch.object(audit, 'read_bounded', side_effect=PermissionError('private-canary')):
            self.assertEqual(self.run_audit()['reason'], 'INPUT_ERROR')

    def test_receipt_replaces_prior_pass_and_discloses_no_canaries(self):
        receipt = self.root / 'receipt.json'
        receipt.write_text('{"status":"PASS"}')
        args = ['--source-root', str(self.root), '--source-sha', 'bad-private-canary', '--ref-sha256', 'b' * 64, '--ci-sha', 'c' * 40, '--debug-app', str(self.debug), '--release-app', str(self.release), '--receipt', str(receipt)]
        with mock.patch('sys.stdout', new_callable=__import__('io').StringIO) as out, mock.patch('sys.stderr', new_callable=__import__('io').StringIO) as err:
            self.assertEqual(audit.main(args), 1)
            output = out.getvalue() + err.getvalue() + receipt.read_text()
        self.assertEqual(json.loads(receipt.read_text())['status'], 'FAIL')
        self.assertNotIn('private-canary', output)
        self.assertNotIn(str(self.root), output)
        with mock.patch.object(audit, 'write_receipt', side_effect=OSError('private-canary')):
            with mock.patch('sys.stderr', new_callable=__import__('io').StringIO) as err:
                self.assertEqual(audit.main(args), 1)
                self.assertNotIn('private-canary', err.getvalue())

    def test_invalid_utf8_and_blob_mismatch(self):
        p = self.root / 'Synthetic.swift'
        p.write_bytes(b'\xff')
        self.assertEqual(self.run_audit()['reason'], 'GIT_BLOB_MISMATCH')
        subprocess.run(['git', '-C', str(self.root), 'add', 'Synthetic.swift'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'invalid'], check=True)
        self.head = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], text=True).strip()
        self.assertEqual(self.run_audit()['reason'], 'SOURCE_UTF8_INVALID')

    def test_oversized_fat_and_same_artifacts_rejected(self):
        path = self.release / 'RadioShake'
        path.write_bytes(b'\xca\xfe\xba\xbe' + bytes(28))
        self.assertEqual(self.run_audit()['reason'], 'BINARY_HEADER_INVALID')
        with path.open('wb') as stream:
            stream.truncate(1024 * 1024 * 1024 + 1)
        self.assertEqual(self.run_audit()['reason'], 'BINARY_SIZE_INVALID')
        self.release = self.debug
        self.assertEqual(self.run_audit()['reason'], 'PLIST_INVALID')

    def test_unsafe_receipt_paths_and_parser_are_silent(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(audit.AuditError):
            audit.write_receipt(alias / 'receipt', {'status': 'FAIL'})
        with mock.patch('sys.stderr', new_callable=__import__('io').StringIO) as err:
            self.assertEqual(audit.main(['--private-canary']), 2)
            self.assertNotIn('private-canary', err.getvalue())

    def test_oversized_source_fails_before_git_blob_read(self):
        p = self.root / 'Synthetic.swift'
        with p.open('wb') as stream:
            stream.truncate(2 * 1024 * 1024 + 1)
        self.assertEqual(self.run_audit()['reason'], 'FILE_SIZE_INVALID')

    def test_each_plist_field_is_checked_for_each_app(self):
        expected = {'CFBundleIdentifier': 'com.radioshake.debug', 'CFBundleExecutable': 'RadioShake', 'DTPlatformName': 'iphonesimulator'}
        for app, bundle in ((self.debug, 'com.radioshake.debug'), (self.release, 'com.radioshake')):
            expected['CFBundleIdentifier'] = bundle
            p = app / 'Info.plist'
            original = p.read_bytes()
            for field in expected:
                with self.subTest(app=bundle, field=field):
                    data = dict(expected)
                    data[field] = 'wrong'
                    p.write_bytes(plistlib.dumps(data))
                    self.assertEqual(self.run_audit()['reason'], 'PLIST_INVALID')
            p.write_bytes(original)

    def test_swapped_artifacts_rejected(self):
        self.debug, self.release = self.release, self.debug
        self.assertEqual(self.run_audit()['reason'], 'PLIST_INVALID')

    def test_equal_hash_guard_rejects_even_if_scan_reports_equal_hashes(self):
        original = audit.scan
        def scan_with_equal_hash(path, kind, markers):
            result = original(path, kind, markers)
            result['sha256'] = 'a' * 64
            return result
        with mock.patch.object(audit, 'scan', side_effect=scan_with_equal_hash):
            self.assertEqual(self.run_audit()['reason'], 'IDENTICAL_BINARIES')

    def test_receipt_leaf_symlink_rejected_without_touching_target(self):
        target = self.root / 'target'
        target.write_text('sentinel')
        link = self.root / 'receipt'
        link.symlink_to(target)
        with self.assertRaises(audit.AuditError):
            audit.write_receipt(link, {'status': 'FAIL'})
        self.assertEqual(target.read_text(), 'sentinel')

    def test_actual_source_binary_plist_and_exception_canaries_stay_private(self):
        import io
        receipt = self.root / 'receipt.json'
        args = ['--source-root', str(self.root), '--source-sha', self.head, '--ref-sha256', 'b' * 64, '--ci-sha', 'c' * 40, '--debug-app', str(self.debug), '--release-app', str(self.release), '--receipt', str(receipt)]
        def run():
            with mock.patch.object(audit, 'PROFILE', self.profile), mock.patch('sys.stdout', new_callable=io.StringIO) as out, mock.patch('sys.stderr', new_callable=io.StringIO) as err:
                status = audit.main(args)
                return status, out.getvalue() + err.getvalue() + receipt.read_text()
        # Source and binary marker are synthetic private canaries observed by real audit logic.
        binary = self.release / 'RadioShake'
        binary.write_bytes(binary.read_bytes() + self.markers[0])
        status, output = run()
        self.assertEqual(status, 1)
        self.assertNotIn(self.markers[0].decode(), output)
        self.assertNotIn(self.source.decode(), output)
        self.assertNotIn(str(self.root), output)
        binary.write_bytes(binary.read_bytes()[:-len(self.markers[0])])
        plist = self.release / 'Info.plist'
        data = plistlib.loads(plist.read_bytes())
        data['CFBundleIdentifier'] = 'private-plist-canary'
        plist.write_bytes(plistlib.dumps(data))
        status, output = run()
        self.assertEqual(status, 1)
        self.assertNotIn('private-plist-canary', output)
        with mock.patch.object(audit, 'audit', side_effect=RuntimeError('private-exception-canary')):
            status, output = run()
        self.assertEqual(status, 1)
        self.assertNotIn('private-exception-canary', output)


if __name__ == '__main__':
    unittest.main()
