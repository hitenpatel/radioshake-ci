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
        args = ['--source-root', str(self.root), '--source-sha', 'bad-private-canary', '--ref-sha256', 'b' * 64, '--ci-sha', 'c' * 40, '--debug-app', str(self.debug), '--release-app', str(self.release), '--receipt', str(receipt), '--canary-receipt', str(self.root / 'canary.json')]
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

    def test_input_symlink_then_parent_rejected(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        self.debug = alias / '..' / self.debug.name
        self.assertEqual(self.run_audit()['reason'], 'UNSAFE_PATH')

    def test_receipt_symlink_then_parent_rejected_without_touching_target(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        target = self.root / 'receipt'
        target.write_text('sentinel')
        with self.assertRaises(audit.AuditError):
            audit.write_receipt(alias / '..' / 'receipt', {'status': 'FAIL'})
        self.assertEqual(target.read_text(), 'sentinel')

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
        args = ['--source-root', str(self.root), '--source-sha', self.head, '--ref-sha256', 'b' * 64, '--ci-sha', 'c' * 40, '--debug-app', str(self.debug), '--release-app', str(self.release), '--receipt', str(receipt), '--canary-receipt', str(self.root / 'canary.json')]
        def run():
            with mock.patch.object(audit, 'PROFILE', self.profile), mock.patch('sys.stdout', new_callable=io.StringIO) as out, mock.patch('sys.stderr', new_callable=io.StringIO) as err:
                status = audit.main(args)
                return status, out.getvalue() + err.getvalue() + receipt.read_text() + (self.root / 'canary.json').read_text()
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


    def test_canary_actual_synthetic_copy_and_cleanup(self):
        baseline = self.run_audit()
        original = (self.release / 'RadioShake').read_bytes()
        original_factory = tempfile.TemporaryDirectory
        created = []
        def make_directory(*args, **kwargs):
            directory = original_factory(*args, **kwargs)
            created.append(Path(directory.name))
            return directory
        with mock.patch.object(audit.tempfile, 'TemporaryDirectory', side_effect=make_directory):
            result = audit._canary(self.root, self.head, 'b' * 64, 'c' * 40, self.debug, self.release, baseline, 'd' * 64, self.profile)
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists())
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['observed_reason'], 'RELEASE_PRESENT_M01')
        self.assertEqual(result['counts'], {f'M{i:02d}': int(i == 1) for i in range(1, 9)})
        self.assertEqual(result['mutated_sha256'], sha(original + self.markers[0]))
        self.assertEqual(result['mutated_size'], len(original) + len(self.markers[0]))
        self.assertEqual(result['original_integrity'], 'PASS')
        self.assertEqual(result['cleanup'], 'PASS')
        self.assertEqual((self.release / 'RadioShake').read_bytes(), original)

    def test_canary_baseline_fail_and_not_applicable_without_copy(self):
        for status, reason in [('FAIL', 'BASELINE_NOT_PASS'), ('NOT_APPLICABLE', 'KNOWN_FEATURE_ABSENT')]:
            baseline = {'status': status}
            result = audit._canary(self.root, self.head, 'b'*64, 'c'*40, self.debug, self.release, baseline, 'd'*64, self.profile)
            self.assertEqual(result['status'], status)
            self.assertEqual(result['reason'], reason)
            self.assertEqual(result['control'], 'NOT_RUN')

    def test_canary_result_rejects_mismatched_fields(self):
        import copy
        baseline = self.run_audit()
        path = self.release / 'RadioShake'
        old = path.read_bytes()
        path.write_bytes(old + self.markers[0])
        observed = self.run_audit()
        path.write_bytes(old)
        expected_hash, expected_size = sha(old + self.markers[0]), len(old) + len(self.markers[0])
        audit._check_canary_result(baseline, observed, expected_hash, expected_size)
        for keys, value in [(('reason',), 'OTHER'), (('binary_result',), 'PASS'), (('source_sha',), '0'*40), (('source_guard',), {}), (('binaries','debug_code','sha256'), '0'*64), (('binaries','debug_launcher','counts','M01'), 999), (('binaries','release_main','counts','M01'), 2), (('binaries','release_main','counts','M02'), 1), (('binaries','release_main','sha256'), '0'*64), (('binaries','release_main','size'), 0), (('binaries','release_main','outcome'), 'PASS')]:
            with self.subTest(keys=keys):
                bad = copy.deepcopy(observed)
                node = bad
                for key in keys[:-1]: node = node[key]
                node[keys[-1]] = value
                with self.assertRaises(audit.AuditError):
                    audit._check_canary_result(baseline, bad, expected_hash, expected_size)

    def test_canary_original_changed_and_io_failures_are_closed(self):
        baseline = self.run_audit()
        p = self.release / 'RadioShake'
        p.write_bytes(p.read_bytes() + b'changed')
        result = audit._canary(self.root, self.head, 'b'*64, 'c'*40, self.debug, self.release, baseline, 'd'*64, self.profile)
        self.assertEqual(result['status'], 'FAIL')
        for name in ('read_bounded', '_copy_release', '_append_marker', '_cleanup_copy'):
            with self.subTest(name=name):
                # Fresh regular baseline; inject only I/O failures, never audit decisions.
                current = self.run_audit()
                original = getattr(audit, name)
                def fail_io(*args):
                    if name == '_cleanup_copy':
                        original(*args)
                    raise OSError('private-exception-canary')
                with mock.patch.object(audit, name, side_effect=fail_io):
                    result = audit._canary(self.root, self.head, 'b'*64, 'c'*40, self.debug, self.release, current, 'd'*64, self.profile)
                self.assertEqual(result['status'], 'FAIL')
                self.assertNotIn('private-exception-canary', json.dumps(result))

    def test_canary_cli_requires_distinct_safe_outputs(self):
        receipt = self.root / 'receipt.json'
        second = self.root / 'second.json'
        args = ['--source-root',str(self.root),'--source-sha',self.head,'--ref-sha256','b'*64,'--ci-sha','c'*40,'--debug-app',str(self.debug),'--release-app',str(self.release),'--receipt',str(receipt)]
        with mock.patch('sys.stderr', new_callable=__import__('io').StringIO):
            self.assertEqual(audit.main(args), 2)
            self.assertNotEqual(audit.main(args + ['--canary-receipt', str(receipt)]), 0)
            alias = self.root / 'link'
            alias.symlink_to(self.root, target_is_directory=True)
            self.assertNotEqual(audit.main(args + ['--canary-receipt', str(alias / 'second.json')]), 0)
            self.assertNotEqual(audit.main(args + ['--canary-receipt',str(second),'--skip-canary']), 0)

    def test_canary_detects_changed_original_plist_and_binary(self):
        for path in (self.release / 'Info.plist', self.release / 'RadioShake', self.debug / 'Info.plist'):
            with self.subTest(file=path.name, app=path.parent.name):
                baseline = self.run_audit()
                before = path.read_bytes()
                append = audit._append_marker
                def change_original(copy_path, marker):
                    append(copy_path, marker)
                    if path.name == 'Info.plist':
                        info = plistlib.loads(before)
                        info['SyntheticChange'] = 'changed'
                        path.write_bytes(plistlib.dumps(info))
                    else:
                        path.write_bytes(before + b'changed')
                with mock.patch.object(audit, '_append_marker', side_effect=change_original):
                    result = audit._canary(self.root, self.head, 'b'*64, 'c'*40, self.debug, self.release, baseline, 'd'*64, self.profile)
                path.write_bytes(before)
                self.assertEqual(result['status'], 'FAIL')
                self.assertEqual(result['reason'], 'ORIGINAL_CHANGED')
                self.assertEqual(result['cleanup'], 'PASS')

    def test_canary_copy_size_capacity_and_symlink_inputs(self):
        source = self.root / 'big'
        with source.open('wb') as stream:
            stream.truncate(1024 * 1024 * 1024)
        with self.assertRaises(audit.AuditError):
            audit._copy_release(source, self.root / 'copy', {'size': source.stat().st_size, 'sha256': 'a'*64}, self.markers[0])
        alias = self.root / 'binary-link'
        alias.symlink_to(self.release / 'RadioShake')
        with self.assertRaises(audit.AuditError):
            audit._copy_release(alias, self.root / 'copy', {'size': 0, 'sha256': 'a'*64}, self.markers[0])

    def test_both_prior_receipts_replaced_with_fail_on_regular_failure(self):
        receipt, second = self.root / 'receipt.json', self.root / 'second.json'
        receipt.write_text('{"status":"PASS"}')
        second.write_text('{"status":"PASS"}')
        args = ['--source-root',str(self.root),'--source-sha','0'*40,'--ref-sha256','b'*64,'--ci-sha','c'*40,'--debug-app',str(self.debug),'--release-app',str(self.release),'--receipt',str(receipt),'--canary-receipt',str(second)]
        with mock.patch('sys.stdout', new_callable=__import__('io').StringIO) as out:
            self.assertEqual(audit.main(args), 1)
        self.assertEqual(json.loads(receipt.read_text())['status'], 'FAIL')
        self.assertEqual(json.loads(second.read_text())['reason'], 'BASELINE_NOT_PASS')
        self.assertNotIn(str(self.root), out.getvalue() + receipt.read_text() + second.read_text())

    def test_receipt_write_failure_leaves_both_initial_failures(self):
        first, second = self.root / 'one.json', self.root / 'two.json'
        first.write_text('{"status":"PASS"}')
        second.write_text('{"status":"PASS"}')
        args = ['--source-root',str(self.root),'--source-sha','0'*40,'--ref-sha256','b'*64,'--ci-sha','c'*40,'--debug-app',str(self.debug),'--release-app',str(self.release),'--receipt',str(first),'--canary-receipt',str(second)]
        write = audit.write_receipt
        calls = []
        def fail_third(*values):
            calls.append(1)
            if len(calls) == 3:
                raise OSError('private-write-canary')
            return write(*values)
        with mock.patch.object(audit, 'write_receipt', side_effect=fail_third), mock.patch('sys.stderr', new_callable=__import__('io').StringIO) as err:
            self.assertEqual(audit.main(args), 1)
        self.assertEqual(json.loads(first.read_text())['reason'], 'INCOMPLETE')
        self.assertEqual(json.loads(second.read_text())['reason'], 'INCOMPLETE')
        self.assertNotIn('private-write-canary', err.getvalue())

    def test_regular_audit_body_unchanged(self):
        import inspect
        self.assertEqual(sha(inspect.getsource(audit._audit).encode()), '72cb84e25bbc0f9b5080bd9c941d15bb5c047f1e0d660edfc16d92b302c12ee9')


class WorkflowIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = Path(__file__).parents[1] / 'workflows' / 'ios-shared-validate.yml'
        if cls.workflow.exists():
            cls.text = cls.workflow.read_text()
        else:
            cls.text = subprocess.check_output(['git', '-C', str(Path(__file__).parent), 'show', 'HEAD:.github/workflows/ios-shared-validate.yml'], text=True)
        cls.validate = cls.text.split('  # UI test shards 1..N-1.')[0]

    def step(self, name):
        start = self.validate.index('      - name: ' + name)
        end = self.validate.find('\n      - name:', start + 1)
        return self.validate[start:end if end >= 0 else None]

    def test_controls_run_after_checkout(self):
        checkout = self.validate.index('      - name: Check out CI scripts (this repo, scripts only)')
        controls = self.validate.index('      - name: Capture probe audit controls')
        self.assertLess(checkout, controls)
        self.assertIn("python3 -m unittest discover -s ci/.github/scripts -p test_check_capture_probe_release.py", self.step('Capture probe audit controls'))

    def test_audit_follows_release_checksum_and_binds_inputs(self):
        self.assertLess(self.validate.index('      - name: Record Release app bundle checksum'), self.validate.index('      - name: Audit capture probe in Release binary'))
        step = self.step('Audit capture probe in Release binary')
        for fragment in ("${{ inputs.ref || github.event.client_payload.ref || 'feat/task-7-ios-shared-framework' }}", '${{ steps.clone.outputs.head_sha }}', '${{ github.sha }}', 'ci/.github/scripts/check_capture_probe_release.py', '--source-root', '--source-sha', '--ref-sha256', '--ci-sha', '--debug-app', '--release-app', '--receipt', 'derived-data/Build/Products/Debug-iphonesimulator/RadioShake.app', 'derived-data-release/Build/Products/Release-iphonesimulator/RadioShake.app'):
            self.assertIn(fragment, step)
        self.assertIn("os.path.realpath('/tmp')", step)
        self.assertEqual(step.count('realpath('), 1)
        self.assertIn("hashlib.sha256(os.environ['REQUESTED_REF'].encode('utf-8')).hexdigest()", step)
        self.assertNotIn('${{ inputs.ref', step.split('        run: |', 1)[1])
        self.assertIn('CANARY_RECEIPT="$TMP_ROOT/capture-probe-release-native-canary.json"', step)
        self.assertIn('--canary-receipt "$CANARY_RECEIPT"', step)
        self.assertIn('rm -f "$RECEIPT" "$CANARY_RECEIPT"', step)
        self.assertIn('set -euo pipefail', step)
        self.assertNotIn('dispatch', step.lower())
        self.assertNotIn('strings ', step)
        self.assertNotIn('cat ', step)

    def test_receipt_is_only_new_evidence_and_fixed_deny_is_unchanged(self):
        evidence = self.validate.split('      EVIDENCE_PATHS: |', 1)[1].split('    outputs:', 1)[0]
        self.assertEqual(evidence.count('/tmp/capture-probe-release-audit.json'), 1)
        self.assertEqual(evidence.count('/tmp/capture-probe-release-native-canary.json'), 1)
        self.assertEqual(len([line for line in evidence.splitlines() if 'capture-probe-' in line]), 2)
        self.assertNotIn('RadioShake.app', evidence)
        self.assertNotIn('src/ios/', evidence)
        self.assertNotIn('derived-data/', evidence)
        self.assertNotIn('derived-data-release/', evidence)
        deny = self.step('Audit Release binary for debug-only symbols (TASK-136 + TASK-137)')
        self.assertEqual(hashlib.sha256((deny + '\n').encode()).hexdigest(), '3278c77942411f243aad26d18528f58ddb70ddadc6df8387f7cf55fd80348b84')  # c3b5308: + RSDataSaver, RSDataUsageSeed, RSNetworkScript (Phase 5e)
        self.assertEqual(self.validate.count('      - name: Audit capture probe in Release binary'), 1)



if __name__ == '__main__':
    unittest.main()


@unittest.skipIf(audit is None, 'checker missing')
class ProfileSelectionTests(unittest.TestCase):
    """Phase 5e: two reviewed StreamAudioCapture.swift sources are accepted; anything else keeps the drift check."""

    def root_with(self, data):
        # Resolved: macOS temp folders sit behind /var -> /private/var, and the checker refuses symlinked paths.
        directory = tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir()))
        self.addCleanup(directory.cleanup)
        root = os.path.realpath(directory.name)
        path = Path(root) / audit.SOURCE_PATH
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        return root

    def test_the_profile_whose_digest_matches_is_chosen(self):
        a, b = b'struct A {}\n', b'struct B {}\n'
        first = dict(audit.PROFILE, source_sha256=sha(a))
        second = dict(audit.PROFILE, source_sha256=sha(b))
        with mock.patch.object(audit, 'PROFILES', (first, second)), mock.patch.object(audit, 'PROFILE', first):
            self.assertIs(audit.profile_for(self.root_with(b)), second)
            self.assertIs(audit.profile_for(self.root_with(a)), first)

    def test_an_unreviewed_source_falls_back_to_the_first_profile(self):
        self.assertIs(audit.profile_for(self.root_with(b'struct Other {}\n')), audit.PROFILE)

    def test_a_missing_source_falls_back_to_the_first_profile(self):
        directory = tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir()))
        self.addCleanup(directory.cleanup)
        self.assertIs(audit.profile_for(os.path.realpath(directory.name)), audit.PROFILE)

    def test_the_5e_profile_moves_only_the_func_slot(self):
        self.assertEqual(audit.PROFILE_5E['slots'][:7], audit.PROFILE['slots'][:7])
        self.assertEqual(audit.PROFILE_5E['slots'][7], (612, 'func', audit.PROFILE['slots'][7][2]))
        self.assertNotEqual(audit.PROFILE_5E['source_sha256'], audit.PROFILE['source_sha256'])
