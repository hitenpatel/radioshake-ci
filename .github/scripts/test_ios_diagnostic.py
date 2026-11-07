import base64
import contextlib
import hashlib
import tempfile
from unittest import mock
import io
import json
import subprocess
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import ios_diagnostic as d

REF = 'feat/example'
SHA = 'a' * 40
UUID = '12345678-1234-4234-8234-123456789abc'
SELECTOR = 'RadioShakeTests/ExampleTests'

def case(name='testOne', cls='ExampleTests', result='passed', module='RadioShakeTests'):
    return f"Test Case '-[{module}.{cls} {name}]' {result} (0.001 seconds).\n"

def suite(cls='ExampleTests', result='passed', count=1, failures=0):
    return (f"Test Suite '{cls}' {result} at 2026-11-07 07:54:30.810.\n"
            f"\t Executed {count} tests, with {failures} failures (0 unexpected) in 0.001 (0.002) seconds\n")

class DiagnosticTests(unittest.TestCase):
    def inputs(self, **kw):
        args = dict(ref=REF, sha=SHA, selector=SELECTOR, request_id=UUID, device='iPhone 17')
        args.update(kw)
        return d.validate_inputs(**args)

    def test_strict_inputs(self):
        self.assertEqual(self.inputs()['sha'], SHA)
        self.assertEqual(self.inputs(selector='RadioShakeUITests/ExampleTests')['selector'], 'RadioShakeUITests/ExampleTests')
        for field, values in {'ref': ['', '-x', 'a..b', 'a\nb', 'a;ls', 'a.lock', 'a'*201], 'sha': ['', 'a'*39, 'A'*40], 'selector': ['', 'RadioShakeTests/A/test', 'RadioShakeTests/A*', 'RadioShakeTests/A\n', 'RadioShakeTests/A,Other/B', '--help'], 'request_id': ['', UUID.upper(), '12345678-1234-1234-8234-123456789abc'], 'device': ['', 'iPhone 18']}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.inputs(**{field:value})

    def test_result_requires_complete_matching_non_skipped_cases(self):
        for log, rc, present in [('',0,True), (suite(),0,True), (case(result='skipped')+suite(),0,True), (case(cls='OtherTests')+suite('OtherTests'),0,True), (case()+suite(),1,True), (case()+suite(),0,False), (case()+case(cls='OtherTests')+suite(),0,True)]:
            self.assertNotEqual(d.summarize_execution(SELECTOR,rc,log,present)['outcome'],'passed')
        result=d.summarize_execution(SELECTOR,0,case()+suite(),True)
        self.assertEqual((result['outcome'],result['passed'],result['failed'],result['skipped']),('passed',1,0,0))

    def test_requires_matching_suite_execution_summary(self):
        log = case()+suite()
        self.assertEqual(d.summarize_execution(SELECTOR,0,log,True)['outcome'], 'passed')
        mismatch = log.replace('Executed 1 tests', 'Executed 2 tests')
        self.assertNotEqual(d.summarize_execution(SELECTOR,0,mismatch,True)['outcome'], 'passed')

    def test_native_skipped_summary_matches_terminal_counts(self):
        summary = "\t Executed 2 tests, with 1 test skipped and 0 failures (0 unexpected) in 0.001 (0.002) seconds\n"
        suite_line = "Test Suite 'ExampleTests' passed at 2026-11-07 07:54:30.810.\n"
        mixed = case() + case('testTwo', result='skipped') + suite_line + summary
        result = d.summarize_execution(SELECTOR, 0, mixed, True)
        self.assertEqual((result['outcome'], result['passed'], result['skipped']), ('passed', 1, 1))
        all_skipped = case(result='skipped') + suite_line + summary.replace('2 tests', '1 test')
        self.assertNotEqual(d.summarize_execution(SELECTOR, 0, all_skipped, True)['outcome'], 'passed')
        mismatch = mixed.replace('1 test skipped', '2 tests skipped')
        self.assertNotEqual(d.summarize_execution(SELECTOR, 0, mismatch, True)['outcome'], 'passed')

    def test_manifest_hashes_nested_manifest_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); nested=root/'tests.xcresult'/'Data'/'manifest.json'; nested.parent.mkdir(parents=True)
            nested.write_text('nested evidence')
            (root/'stages.json').write_text(json.dumps({'test': 0}))
            (root/'runtime.json').write_text('{}')
            (root/'test.log').write_text(case()+suite())
            env={'EVIDENCE':tmp, 'REF':REF, 'SHA':SHA, 'SELECTOR':SELECTOR, 'REQUEST_ID':UUID, 'DEVICE':'iPhone 17'}
            with mock.patch.dict(os.environ,env), mock.patch.object(sys,'argv',['ios_diagnostic.py','manifest']), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(d.main(),0)
            hashes=json.loads((root/'manifest.json').read_text())['file_hashes']
            self.assertEqual(hashes['tests.xcresult/Data/manifest.json'], hashlib.sha256(b'nested evidence').hexdigest())
            self.assertNotIn('manifest.json',hashes)

    def test_terminal_counts_and_duplicates(self):
        log="Test Case '-[RadioShakeTests.ExampleTests testOne]' started.\n"+case()+case('testTwo',result='failed')+suite(result='failed', count=2, failures=1)
        r=d.summarize_execution(SELECTOR,1,log,True)
        self.assertEqual((r['passed'],r['failed'],r['outcome']),(1,1,'failed'))
        self.assertNotEqual(d.summarize_execution(SELECTOR,0,case()+case()+suite(),True)['outcome'],'passed')
        self.assertEqual(d.summarize_execution('RadioShakeUITests/ExampleTests',0,case(module='RadioShakeUITests')+suite(),True)['outcome'],'passed')

    def test_manifest_contains_association_and_counts(self):
        execution=d.summarize_execution(SELECTOR,0,case()+suite(),True)
        m=d.make_manifest(self.inputs(),{'source_sha':SHA}, {'test':0},execution)
        self.assertEqual(m['schema_version'],1)
        self.assertEqual(m['inputs']['request_id'],UUID)
        self.assertEqual(m['execution'],execution)

    def test_scan_rejects_raw_and_base64_token_and_missing_decompressor(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence=Path(tmp)/'file'
            with mock.patch.object(d.shutil, 'which', return_value='/fake/zstd'):
                evidence.write_bytes(base64.b64encode(b'secret-token'))
                self.assertFalse(d.scan(tmp,'secret-token'))
                evidence.write_bytes(b'safe')
                self.assertTrue(d.scan(tmp,'secret-token'))
            with mock.patch.object(d.shutil, 'which', return_value=None):
                self.assertFalse(d.scan(tmp,'secret-token'))

    def test_scan_checks_decompressed_bundle_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'blob').write_bytes(b'\x28\xb5\x2f\xfdcompressed')
            with mock.patch.object(d.shutil, 'which', return_value='/fake/zstd'), mock.patch.object(d.subprocess, 'run') as run:
                run.return_value.returncode = 0
                run.return_value.stdout = b'contains secret-token'
                self.assertFalse(d.scan(tmp, 'secret-token'))
                run.return_value.stdout = b'safe'
                self.assertTrue(d.scan(tmp, 'secret-token'))
                run.return_value.returncode = 1
                self.assertFalse(d.scan(tmp, 'secret-token'))

    def test_public_output_never_contains_tool_output(self):
        canary='private/Filename.swift\nerror: secret TOKEN_ABC https://private.example source text'
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            d.print_summary(d.summarize_execution(SELECTOR,1,canary,False))
        self.assertNotIn(canary,output.getvalue())
        for s in ('Filename.swift','TOKEN_ABC','private.example','source text'):
            self.assertNotIn(s,output.getvalue())

class StructureTests(unittest.TestCase):
    def test_simulator_state_and_failures(self):
        import yaml
        p=Path(__file__).resolve().parents[1]/'workflows'/'ios-diagnostic-tests.yml'
        wf=yaml.safe_load(p.read_text())
        run=next(x['run'] for x in wf['jobs']['diagnostic']['steps'] if x.get('name')=='Build and test one class')
        function=run[run.index('simulator() {'):run.index('test_one()')]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); stub=root/'xcrun'
            function=function.replace('/tmp/ios-diagnostic-devices.json',str(root/'devices.json')).replace('/tmp/ios-diagnostic-types.json',str(root/'types.json')).replace('/tmp/ios-diagnostic-udid',str(root/'udid'))
            stub.write_text("""#!/usr/bin/env python3
import json, os, sys
args=sys.argv[1:]
if args[:3]==['simctl','list','devices']:
 print(json.dumps({'devices':{'com.apple.CoreSimulator.SimRuntime.iOS-26-5':[{'name':'iPhone 17','udid':'AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA','state':os.environ['SIM_STATE']}]}}))
elif args[:2]==['simctl','boot']:
 open(os.environ['CALLS'],'a').write('boot\\n'); sys.exit(int(os.environ['BOOT_RC']))
elif args[:2]==['simctl','bootstatus']:
 open(os.environ['CALLS'],'a').write('bootstatus\\n'); sys.exit(int(os.environ['READY_RC']))
else: sys.exit(3)
""")
            stub.chmod(0o755)
            for state, boot_rc, ready_rc, expected, calls in [('Shutdown',0,0,0,['boot','bootstatus']),('Booted',0,0,0,['bootstatus']),('Shutdown',1,0,1,['boot']),('Booted',0,1,1,['bootstatus'])]:
                calls_file=root/'calls'; calls_file.write_text('')
                env={**os.environ,'PATH':tmp+os.pathsep+os.environ['PATH'],'EVIDENCE':tmp,'DEVICE':'iPhone 17','SIM_STATE':state,'BOOT_RC':str(boot_rc),'READY_RC':str(ready_rc),'CALLS':str(calls_file)}
                p=subprocess.run(['bash','-c',function+'\nsimulator'],env=env,capture_output=True,text=True)
                with self.subTest(state=state,boot_rc=boot_rc,ready_rc=ready_rc):
                    self.assertEqual(p.returncode,expected,p.stderr)
                    self.assertEqual(calls_file.read_text().splitlines(),calls)

    def test_workflow_envelope(self):
        import yaml
        p=Path(__file__).resolve().parents[1]/'workflows'/'ios-diagnostic-tests.yml'
        text=p.read_text(); wf=yaml.safe_load(text); on=wf.get('on',wf.get(True))
        self.assertEqual(list(on),['workflow_dispatch'])
        self.assertEqual(list(wf['jobs']),['diagnostic'])
        self.assertEqual(wf['permissions'],{'contents':'read'})
        self.assertEqual(wf['jobs']['diagnostic']['runs-on'],'macos-26')
        self.assertEqual(wf['jobs']['diagnostic']['timeout-minutes'],90)
        self.assertIn('persist-credentials: false',text)
        self.assertIn('ref: ${{ github.sha }}',text)
        self.assertNotIn('tee ',text)
        self.assertNotIn('-retry-tests-on-failure',text)
        self.assertNotIn('FORGEJO_STATUS_TOKEN',text)
        self.assertNotIn('APP_STORE_CONNECT',text)
        self.assertIn('bundle.tgz.enc',text)
        self.assertIn('key.enc',text)
        self.assertEqual(wf['concurrency']['cancel-in-progress'], False)
        self.assertEqual(on['workflow_dispatch']['inputs']['device']['options'], ['iPhone 17','iPhone SE (3rd generation)'])
        steps=wf['jobs']['diagnostic']['steps']
        uploads=[s for s in steps if 'upload-artifact@' in s.get('uses','')]
        self.assertEqual(len(uploads),1)
        self.assertEqual(uploads[0]['with']['retention-days'],1)
        self.assertEqual(set(uploads[0]['with']['path'].split()), {'/tmp/ios-diagnostic-encrypted/bundle.tgz.enc','/tmp/ios-diagnostic-encrypted/key.enc'})
        self.assertIn('if: always()',text)
        self.assertIn('trap ',text)
        self.assertEqual(text.count('xcodebuild test -project'),1)
        self.assertIn('-configuration Debug',text)
        self.assertNotIn('-configuration Release',text)
        self.assertIn('gradle() ( cd src',text)
        self.assertIn('generate() ( cd src/ios',text)
        self.assertIn('test_one() ( cd src/ios',text)
        self.assertIn('merge-base --is-ancestor',text)
        self.assertIn('unset AUTH TOKEN HOST',text)
        for step in wf['jobs']['diagnostic']['steps']:
            self.assertNotIn('${{ inputs.',step.get('run',''))
