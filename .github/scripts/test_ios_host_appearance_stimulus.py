import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('host_stimulus', HERE / 'ios_host_appearance_stimulus.py')
try:
    host = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(host)
except FileNotFoundError:
    host = None

IDENTITY = {'requestId': 'request', 'sourceSHA': 'source', 'workflowSHA': 'workflow',
            'selector': 'selector', 'destinationUDID': 'device'}
SLOTS = ['originalQuery', 'darkSetter', 'beforeQuery', 'afterQuery', 'restoreSetter', 'restoreQuery']


def record(argv, value='dark', status='ok', duration=0.1, total=None):
    raw = value.encode()
    stream = {'prefixBase64': base64.b64encode(raw[:1024]).decode(),
              'totalBytes': len(raw) if total is None else total,
              'truncated': len(raw) > 1024 if total is None else total > 1024}
    return {'argv': argv, 'start': 1.0, 'end': 1.1, 'duration': duration, 'timeout': 10,
            'returnCode': 0 if status == 'ok' else None, 'status': status,
            'appearance': value if value in ('light', 'dark') else None,
            'stdout': stream, 'stderr': {'prefixBase64': '', 'totalBytes': 0, 'truncated': False}}


class HostTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(host, 'trusted host helper is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.calls = []

    def run_host(self, answers, code=0, child_error=False):
        answers = iter(answers)
        def command(argv):
            self.calls.append(argv)
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return record(argv, *answer)
        def child(argv):
            self.calls.append(argv)
            if child_error:
                raise OSError('child unavailable')
            return code
        result = host.run_host_stimulus('device', self.path, ['native', 'arg'], IDENTITY,
                                        run_command=command, run_child=child)
        return result, json.loads((self.path / 'host-appearance-stimulus.json').read_text())

    def test_exact_six_slot_lifecycle_and_argv(self):
        rc, data = self.run_host([('light',), ('',), ('dark',), ('dark',), ('',), ('light',)])
        q = ['xcrun', 'simctl', 'ui', 'device', 'appearance']
        self.assertEqual(self.calls, [q, q + ['dark'], q, ['native', 'arg'], q, q + ['light'], q])
        self.assertEqual(list(data['commands']), SLOTS)
        self.assertEqual(rc, 0)
        self.assertEqual(data['child']['returnCode'], 0)
        self.assertFalse(data['outputOmitted'])

    def test_preflight_failure_never_starts_child(self):
        rc, data = self.run_host([('unknown',)])
        self.assertEqual(rc, 74)
        self.assertIsNone(data['child'])
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(data['commands'][s]['status'] == 'notAttempted' for s in SLOTS[1:]))
        rc, data = self.run_host([('light',), ('', 'error'), ('',), ('light',)])
        self.assertEqual(rc, 74)
        self.assertIsNone(data['child'])
        self.assertEqual(data['commands']['restoreQuery']['appearance'], 'light')

    def test_restoration_attempted_after_child_65_or_exception(self):
        for is_error, expected in [(False, 65), (True, 74)]:
            with self.subTest(is_error=is_error):
                self.calls = []
                rc, data = self.run_host([('light',), ('',), ('dark',)] + ([] if is_error else [('dark',)]) + [('',), ('light',)], 65, is_error)
                self.assertEqual(rc, expected)
                self.assertEqual(data['commands']['restoreQuery']['appearance'], 'light')
                if is_error:
                    self.assertIsNone(data['child'])
                else:
                    self.assertEqual(data['child']['returnCode'], 65)

    def test_native_zero_preserved_despite_post_timeout(self):
        rc, data = self.run_host([('light',), ('',), ('dark',), ('', 'timeout'), ('',), ('light',)])
        self.assertEqual(rc, 0)
        self.assertEqual(data['commands']['afterQuery']['status'], 'timeout')
        self.assertEqual(data['child']['returnCode'], 0)

    def test_known_post_light_and_restore_mismatch_retained(self):
        rc, data = self.run_host([('light',), ('',), ('dark',), ('light',), ('',), ('dark',)])
        self.assertEqual(rc, 0)
        self.assertEqual(data['commands']['afterQuery']['appearance'], 'light')
        self.assertEqual(data['commands']['restoreQuery']['appearance'], 'dark')
        rc, data = self.run_host([('light',), ('',), ('dark',), ('light',), ('', 'error'), ('light',)])
        self.assertEqual(rc, 0)
        self.assertEqual(data['commands']['restoreSetter']['status'], 'error')
        self.assertEqual(data['commands']['restoreQuery']['appearance'], 'light')

    def test_timeout_overrun_and_stream_limits(self):
        with mock.patch.object(host.subprocess, 'run') as run:
            # capture uses private file-backed streams, not unbounded PIPEs.
            def fake(argv, **kwargs):
                kwargs['stdout'].write(b'x' * 1025)
                kwargs['stderr'].write(b'y' * 1024)
                return subprocess.CompletedProcess(argv, 0)
            run.side_effect = fake
            times = iter([1.0, 11.0001])
            result = host.capture_simctl(['xcrun'], wall_clock=iter([1., 2.]).__next__, monotonic=times.__next__)
        self.assertEqual(result['status'], 'overrun')
        self.assertEqual(result['timeout'], 10)
        self.assertEqual(result['stdout']['totalBytes'], 1025)
        self.assertTrue(result['stdout']['truncated'])
        self.assertEqual(len(base64.b64decode(result['stdout']['prefixBase64'])), 1024)
        self.assertFalse(result['stderr']['truncated'])
        with mock.patch.object(host.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['x'], 10)):
            result = host.capture_simctl(['x'], wall_clock=iter([1., 2.]).__next__, monotonic=iter([1., 11.]).__next__)
        self.assertEqual(result['status'], 'timeout')
        self.assertEqual(result['duration'], 10)

    def test_sidecar_overflow_fallback_is_incomplete(self):
        # A long identity forces the bounded same-schema fallback while fitting it.
        identity = dict(IDENTITY, requestId='r' * 7000)
        def command(argv):
            value = 'light' if len(self.calls) in (0, 5) else 'dark'
            self.calls.append(argv)
            return record(argv, value * 300)
        # Preserve parseable query values while retaining large actual streams.
        def bounded(argv):
            i = len(self.calls)
            result = command(argv)
            result['appearance'] = 'light' if i in (0, 5) else 'dark'
            return result
        rc = host.run_host_stimulus('device', self.path, ['native'], identity, run_command=bounded, run_child=lambda _: 65)
        data = json.loads((self.path / 'host-appearance-stimulus.json').read_text())
        self.assertEqual(rc, 65)
        self.assertTrue(data['outputOmitted'])
        self.assertLessEqual((self.path / 'host-appearance-stimulus.json').stat().st_size, 16384)
        self.assertEqual(list(data['commands']), SLOTS)
        self.assertEqual(data['child']['returnCode'], 65)
        for item in data['commands'].values():
            self.assertEqual(item['stdout']['prefixBase64'], '')
            self.assertTrue(item['stdout']['truncated'])
            self.assertGreater(item['stdout']['totalBytes'], 0)

    def test_command_exception_preserves_actual_elapsed_and_overrun(self):
        answers = iter([OSError('failed'), ('light',)])
        def command(argv):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return record(argv, *answer)
        with mock.patch.object(host.time, 'time', side_effect=[100.0, 112.0, 200.0, 201.0]), mock.patch.object(host.time, 'monotonic', side_effect=[50.0, 61.0, 100.0, 100.1]):
            host.run_host_stimulus('device', self.path, ['native'], IDENTITY, run_command=command, run_child=lambda _: 0)
        item = json.loads((self.path / 'host-appearance-stimulus.json').read_text())['commands']['originalQuery']
        self.assertEqual((item['start'], item['end'], item['duration'], item['status']), (100.0, 112.0, 11.0, 'overrun'))

    def test_fake_xcrun_capture_and_actual_child_codes(self):
        executable = self.path / 'xcrun'
        executable.write_text('#!/bin/sh\nprintf "  LiGhT  \n"\nprintf err >&2\n')
        executable.chmod(0o755)
        result = host.capture_simctl([str(executable)], wall_clock=iter([1., 2.]).__next__, monotonic=iter([1., 11.]).__next__)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['duration'], 10)
        self.assertEqual(result['appearance'], 'light')
        self.assertEqual(base64.b64decode(result['stderr']['prefixBase64']), b'err')
        for code in (-9, 17):
            with self.subTest(code=code):
                self.calls = []
                rc, data = self.run_host([('light',), ('',), ('dark',), ('dark',), ('',), ('light',)], code)
                self.assertEqual(rc, code)
                self.assertEqual(data['child']['returnCode'], code)

    def test_sidecar_inclusive_byte_boundary(self):
        data = {'commands': {slot: host._empty() for slot in SLOTS}, 'outputOmitted': False, 'padding': ''}
        encoded = json.dumps(data, separators=(',', ':'), ensure_ascii=True).encode()
        data['padding'] = 'x' * (16384 - len(encoded))
        path = self.path / 'boundary.json'
        host._write_sidecar(path, data)
        self.assertEqual(path.stat().st_size, 16384)
        self.assertFalse(json.loads(path.read_text())['outputOmitted'])
        data['padding'] += 'x'
        host._write_sidecar(path, data)
        self.assertEqual(path.stat().st_size, 16384)
        self.assertTrue(json.loads(path.read_text())['outputOmitted'])
        data['padding'] += 'x'
        with self.assertRaises(ValueError):
            host._write_sidecar(path, data)

    def test_wrapper_real_child_once_private_output(self):
        out = self.path / 'child'
        answers = iter(['light', '', 'dark', 'dark', '', 'light'])
        def command(argv): return record(argv, next(answers))
        child = [sys.executable, '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text("once"); print(chr(112)+chr(114)+chr(105)+chr(118)+chr(97)+chr(116)+chr(101)); sys.exit(65)', str(out)]
        with open(self.path / 'private.log', 'w') as log:
            def run_child(argv): return subprocess.run(argv, stdout=log, stderr=log).returncode
            rc = host.run_host_stimulus('device', self.path, child, IDENTITY, run_command=command, run_child=run_child)
        self.assertEqual(rc, 65)
        self.assertEqual(out.read_text(), 'once')
        self.assertEqual((self.path / 'private.log').read_text(), 'private\n')
        self.assertNotIn('private', (self.path / 'host-appearance-stimulus.json').read_text())

    def test_new_old_generic_workflow_routes_preserve_native_argv(self):
        workflow = (HERE.parent / 'workflows/ios-diagnostic-tests.yml').read_text()
        line = next(x.strip() for x in workflow.splitlines() if x.strip().startswith('test_one()'))
        root = self.path
        (root / 'src/ios').mkdir(parents=True)
        (root / 'bin').mkdir()
        script = root / 'bin/xcodebuild'
        script.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TRACE"\n')
        script.chmod(0o755)
        fake_python = root / 'bin/python3'
        fake_python.write_text('#!/bin/sh\nprintf "%s\\n" "$1" > "$ROUTE"\nshift 4\nexec "$@"\n')
        fake_python.chmod(0o755)
        old = 'RadioShakeUITests/ThemeUITests/test_system_coldUnforcedLaunch_onDarkDevice'
        new = 'RadioShakeUITests/HostAppearanceStimulusUITests/test_hostDark_beforeInvocation_noModifier_coldReference'
        for selector, helper in [(old, 'ios_appearance_boundary.py'), (new, 'ios_host_appearance_stimulus.py'), ('other', None)]:
            route = root / 'route'
            route.unlink(missing_ok=True)
            env = dict(os.environ, PATH=str(root / 'bin') + ':' + os.environ['PATH'], SELECTOR=selector, UDID='device', EVIDENCE=str(root), GITHUB_WORKSPACE=str(root), TRACE=str(root/'trace'), ROUTE=str(route))
            subprocess.run(['bash', '-c', line + '\ntest_one'], cwd=root, env=env, check=True)
            self.assertEqual(route.read_text().strip().split('/')[-1] if route.exists() else None, helper)
            self.assertEqual((root/'trace').read_text().splitlines(), ['test', '-project', 'RadioShake.xcodeproj', '-scheme', 'RadioShake', '-configuration', 'Debug', '-destination', 'id=device', '-derivedDataPath', '/tmp/ios-diagnostic-derived', '-resultBundlePath', str(root/'tests.xcresult'), '-only-testing:' + selector, 'CODE_SIGNING_ALLOWED=NO'])

if __name__ == '__main__': unittest.main()
