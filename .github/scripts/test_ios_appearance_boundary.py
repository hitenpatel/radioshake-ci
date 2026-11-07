import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ios_appearance_boundary as b

UDID = '12345678-1234-4234-8234-123456789ABC'

def make_attachments(root):
    directory=root/'attachments'; directory.mkdir(exist_ok=True)
    records=[]
    context=dict(pid=88,storeMode='auto',bundle={},arguments={},environment={},defaults={},start=dict(wall=1,uptime=1),end=dict(wall=2,uptime=2))
    encoded=base64.b64encode(json.dumps(context).encode()).decode()
    for index,phase in enumerate(('beforeLaunch','afterLaunch','attachedSettings','effectiveCheckpoint'),1):
        body=dict(phase=phase,testName='-[RadioShakeUITests.ThemeUITests test_system_coldUnforcedLaunch_onDarkDevice]',matcherSelfCheck=True,sequence=index,pid=44,
                  device=dict(raw=2,interpretation='dark',start=dict(wall=1,uptime=1),end=dict(wall=2,uptime=2)))
        if index > 2: body.update(effective='light',systemSelected=True,boundary='attached=1;appearanceContext='+encoded)
        if index==4: body['waitResult']=False
        name=f'{index}.txt'; (directory/name).write_text(json.dumps(body))
        records.append(dict(exportedFileName=name,deviceId=UDID,suggestedHumanReadableName='system-appearance-'+phase+'_0_random.txt'))
    (directory/'manifest.json').write_text(json.dumps([dict(testIdentifier='ThemeUITests/test_system_coldUnforcedLaunch_onDarkDevice()',testIdentifierURL='test://com.apple.xcode/RadioShake/RadioShakeUITests/ThemeUITests/test_system_coldUnforcedLaunch_onDarkDevice',attachments=records)]))

class BoundaryTests(unittest.TestCase):
    def test_read_only_appearance_command_uses_exact_device(self):
        with mock.patch.object(b, '_bounded_command', return_value={'returncode': 0, 'stdout': 'Dark\n', 'stderr': '', 'stdoutTruncated': False, 'stderrTruncated': False, 'timedOut': False}) as command:
            record = b.appearance_query(UDID, 2)
        self.assertEqual(command.call_args.args[0], ['xcrun', 'simctl', 'ui', UDID, 'appearance'])
        self.assertEqual(record['appearance'], 'dark')
        with self.assertRaises(ValueError): b.appearance_query('booted', 2)

    def test_plist_projection_excludes_unrelated_secrets_and_records_types(self):
        result = b.project_plist({'CFBundleIdentifier':'com.radioshake.debug', 'UIUserInterfaceStyle':'Light', 'MinimumOSVersion':['private-canary']})
        self.assertEqual(result['UIUserInterfaceStyle']['value'], 'Light')
        self.assertEqual(result['MinimumOSVersion']['state'], 'unsupported')
        self.assertEqual(result['CFBundleVersion']['state'], 'absent')
        self.assertNotIn('private-canary', json.dumps(result))

    def test_wrong_built_bundle_never_queries_installed_container(self):
        built={'state':'available','values':b.project_plist({'CFBundleIdentifier':'wrong.bundle'}),'sha256':'a'*64,'size':42}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(b,'_plist_record',return_value=built), mock.patch.object(b,'_bounded_command') as command:
            result=b._configuration(UDID,Path(tmp))
            self.assertEqual(result['installed'],'unavailable')
            self.assertEqual(json.loads((Path(tmp)/'installed-plist.json').read_text())['reason'],'unexpectedBuiltIdentity')
            command.assert_not_called()

    def test_real_plist_hash_projection_and_symlink_rejection(self):
        import hashlib
        import plistlib
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'Info.plist'; raw=plistlib.dumps({'CFBundleIdentifier':'com.radioshake.debug','UIUserInterfaceStyle':'Light','unrelated':'private-canary'})
            path.write_bytes(raw); record=b._plist_record(path)
            self.assertEqual(record['sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual(record['values']['UIUserInterfaceStyle']['value'],'Light')
            self.assertNotIn('private-canary',json.dumps(record))
            link=Path(tmp)/'link.plist'; link.symlink_to(path)
            self.assertEqual(b._plist_record(link)['state'],'unavailable')

    def test_missing_selected_profile_rejected_without_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(b.BoundaryValidationError) as error:
                b.validate_boundary_evidence(Path(tmp), {'selector':b.SELECTOR}, {'destination_udid':UDID}, 0)
            self.assertNotIn(tmp, str(error.exception))

class FakeClock:
    def __init__(self): self.now=0
    def monotonic(self): return self.now
    def wall(self): return 1_700_000_000 + self.now

class FakeNative:
    def __init__(self, clock, end, code): self.clock=clock; self.end=end; self.code=code; self.pid=4242; self.returncode=None
    def poll(self):
        if self.clock.now >= self.end: self.returncode=self.code
        return self.returncode
    def wait(self, timeout=None):
        if self.returncode is not None: return self.returncode
        if timeout is None: self.clock.now=max(self.clock.now,self.end); self.returncode=self.code; return self.code
        if self.clock.now + timeout < self.end:
            self.clock.now += timeout
            raise b.subprocess.TimeoutExpired('native',timeout)
        self.clock.now=max(self.clock.now,self.end); self.returncode=self.code; return self.code

class ObserverTests(unittest.TestCase):
    def exercise(self, *, duration=30, code=0, trigger=None, query_duration=0, cancel=False, record_cap=542, initial_kind='dark'):
        clock=FakeClock(); tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); root=Path(tmp.name)
        log=root/'test.log'; log.write_text('')
        command=['xcodebuild','test','-project','RadioShake.xcodeproj','-scheme','RadioShake','-configuration','Debug','-destination','id='+UDID,'-derivedDataPath','/tmp/ios-diagnostic-derived','-resultBundlePath',str(root/'tests.xcresult'),'-only-testing:'+b.SELECTOR,'CODE_SIGNING_ALLOWED=NO']
        native=FakeNative(clock,duration,code); seen=[]
        def query(udid, timeout):
            start=clock.monotonic(); seen.append((start,timeout))
            if cancel and len(seen)==2: raise b.Cancelled()
            clock.now += min(query_duration,timeout)
            first=len(seen)==1
            supported=first and initial_kind=='dark' and query_duration<=timeout
            returncode=-9 if query_duration>timeout else (1 if first and initial_kind=='unsupported' else 0)
            return dict(argv=['xcrun','simctl','ui',udid,'appearance'],udid=udid,hostPID=123,
                        start={'wall':1_700_000_000+start,'monotonic':start},end={'wall':clock.wall(),'monotonic':clock.now},
                        wallClockDivergence=False,appearance='dark' if supported else 'unknown',returncode=returncode,timedOut=query_duration>timeout,
                        stdout='Dark\n' if supported else ('private-canary' if first else ''),stderr='',stdoutTruncated=False,stderrTruncated=False)
        def trigger_read(path, offset):
            if trigger is not None and clock.now >= trigger:
                return {'line':"Test Suite 'Selected tests' started.\n",'offset':0,'detected':{'wall':clock.wall(),'monotonic':clock.now}}, 1
            return None,offset
        def config(udid, directory):
            for name in ('built-plist.json','installed-plist.json'):
                b._write(directory/name,{'state':'unavailable','reason':'fixture'})
            return {'built':'unavailable','installed':'unavailable'}
        env={'REF':'feature/test','SHA':'a'*40,'SELECTOR':b.SELECTOR,'REQUEST_ID':'12345678-1234-4234-8234-123456789abc','DEVICE':'iPhone 17'}
        help_output=dict(returncode=0,timedOut=False,stdout='appearance [light | dark]',stderr='',stdoutTruncated=False,stderrTruncated=False)
        with mock.patch.dict(b.os.environ,env), mock.patch.object(b.time,'monotonic',clock.monotonic), mock.patch.object(b.time,'time',clock.wall), mock.patch.object(b.subprocess,'Popen',return_value=native), mock.patch.object(b,'_bounded_command',return_value=help_output), mock.patch.object(b,'appearance_query',side_effect=query), mock.patch.object(b,'_trigger',side_effect=trigger_read), mock.patch.object(b,'_configuration',side_effect=config), mock.patch.object(b,'MAX_RECORDS',record_cap), mock.patch.object(b.os,'killpg') as kill:
            if cancel:
                def killed(pid, sig): native.returncode=-sig
                kill.side_effect=killed
            result=b.run(UDID,root,command)
        status=json.loads((root/'appearance-boundary/status.json').read_text())
        make_attachments(root)
        return root,env,native,seen,result,status

    def test_delayed_trigger_changes_once_from_coarse_to_fast_and_keeps_exit(self):
        root,env,native,seen,result,status=self.exercise(duration=50,code=65,trigger=25)
        self.assertEqual(result,65)
        self.assertEqual([x[0] for x in seen[:4]],[0,10,20,25])
        self.assertEqual(status['counts'],{'initial':1,'coarse':2,'fast':25,'invocationEnd':1})
        self.assertEqual(status['trigger']['detected']['monotonic'],25)
        inputs={k.lower():v for k,v in env.items()}
        b.validate_boundary_evidence(root,inputs,{'destination_udid':UDID},65)

    def test_deadline_and_budget_prevent_late_final_query(self):
        _,_,_,seen,code,status=self.exercise(duration=5000)
        self.assertEqual(code,0)
        self.assertEqual(status['finalRead'],'notAttemptedDeadline')
        self.assertLessEqual(len(seen),421)
        self.assertTrue(all(start < 4200 for start,_ in seen))
        _,_,_,seen,code,status=self.exercise(duration=500,trigger=0)
        self.assertEqual(status['termination'],'fastWindowEnded')
        self.assertEqual(status['counts']['fast'],120)
        self.assertEqual(status['counts']['invocationEnd'],1)
        self.assertEqual(len(seen),122)
        _,_,_,seen,_,status=self.exercise(duration=50,trigger=0,record_cap=3)
        self.assertEqual(len(seen),3)
        self.assertEqual(status['termination'],'recordBudget')
        self.assertEqual(status['finalRead'],'notAttemptedBudget')

    def test_slow_timeout_never_overlaps_or_replays_missed_ticks(self):
        _,_,_,seen,_,status=self.exercise(duration=15,trigger=0,query_duration=3)
        self.assertTrue(all(seen[i+1][0]-seen[i][0] >= 2 for i in range(len(seen)-1)))
        self.assertLessEqual(status['counts']['fast'],7)

    def test_missing_trigger_and_unproved_or_unsupported_initial_read(self):
        root,env,_,_,code,status=self.exercise(duration=30,trigger=None)
        self.assertEqual(status['counts']['fast'],0)
        self.assertIsNone(status['trigger'])
        b.validate_boundary_evidence(root,{k.lower():v for k,v in env.items()},{'destination_udid':UDID},code)
        for kind in ('unknown','unsupported'):
            root,env,_,_,code,status=self.exercise(duration=3,initial_kind=kind)
            self.assertEqual(status['capability'],kind)
            with self.assertRaises(b.BoundaryValidationError) as error:
                b.validate_boundary_evidence(root,{k.lower():v for k,v in env.items()},{'destination_udid':UDID},code)
            self.assertNotIn('private-canary',str(error.exception))

    def test_cancel_reaps_native_and_writes_partial_status(self):
        _,_,native,_,result,status=self.exercise(duration=100,trigger=0,cancel=True)
        self.assertIsNotNone(native.returncode)
        self.assertEqual(result,-15)
        self.assertEqual(status['termination'],'cancelled')
        self.assertEqual(status['observerExit'],'incomplete')

    def test_validator_rejects_tampering_wrong_identity_and_missing_file(self):
        root,env,_,_,_,status=self.exercise(duration=3)
        inputs={k.lower():v for k,v in env.items()}; runtime={'destination_udid':UDID}
        b.validate_boundary_evidence(root,inputs,runtime,0)
        for invalid in ({**inputs,'request_id':'22345678-1234-4234-8234-123456789abc'},):
            with self.assertRaises(b.BoundaryValidationError): b.validate_boundary_evidence(root,invalid,runtime,0)
        with self.assertRaises(b.BoundaryValidationError): b.validate_boundary_evidence(root,inputs,{'destination_udid':'22345678-1234-4234-8234-123456789ABC'},0)
        with self.assertRaises(b.BoundaryValidationError): b.validate_boundary_evidence(root,inputs,runtime,65)
        path=root/'appearance-boundary/host-samples.jsonl'; path.write_text(path.read_text()+'private-canary')
        with self.assertRaises(b.BoundaryValidationError) as exc: b.validate_boundary_evidence(root,inputs,runtime,0)
        self.assertNotIn('private-canary',str(exc.exception))
        path.unlink()
        with self.assertRaises(b.BoundaryValidationError): b.validate_boundary_evidence(root,inputs,runtime,0)

class BoundedProcessTests(unittest.TestCase):
    def test_drains_and_caps_both_streams(self):
        import sys
        result=b._bounded_command([sys.executable,'-c',"import sys; sys.stdout.write('x'*20000); sys.stderr.write('y'*20000)"],2)
        self.assertEqual(result['returncode'],0)
        self.assertEqual(len(result['stdout']),4096)
        self.assertEqual(len(result['stderr']),4096)
        self.assertTrue(result['stdoutTruncated'])
        self.assertTrue(result['stderrTruncated'])

    def test_timeout_reaps_child(self):
        import sys
        result=b._bounded_command([sys.executable,'-c','import time; time.sleep(10)'],0.1)
        self.assertTrue(result['timedOut'])
        self.assertIsNotNone(result['returncode'])

    def test_wrapper_runs_real_child_once_preserves_exit_and_private_output(self):
        import os
        import subprocess
        import sys
        for native_exit in (0,65):
            with self.subTest(native_exit=native_exit), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); bindir=root/'bin'; bindir.mkdir(); evidence=root/'evidence'; evidence.mkdir()
                xcrun=bindir/'xcrun'
                xcrun.write_text("#!/bin/sh\nif [ \"$2\" = help ]; then echo 'appearance operation'; elif [ \"$2\" = ui ]; then echo Dark; else exit 1; fi\n"); xcrun.chmod(0o755)
                native=bindir/'xcodebuild'
                native.write_text("#!/bin/sh\nprintf 'run\\n' >> \"$CALLS\"\nprintf 'private-native-canary\\n'\nexit \"$NATIVE_EXIT\"\n"); native.chmod(0o755)
                env={**os.environ,'PATH':str(bindir)+os.pathsep+os.environ['PATH'],'REF':'feature/test','SHA':'a'*40,'SELECTOR':b.SELECTOR,'REQUEST_ID':'12345678-1234-4234-8234-123456789abc','DEVICE':'iPhone 17','CALLS':str(root/'calls'),'NATIVE_EXIT':str(native_exit)}
                command=['xcodebuild','test','-project','RadioShake.xcodeproj','-scheme','RadioShake','-configuration','Debug','-destination','id='+UDID,'-derivedDataPath','/tmp/ios-diagnostic-derived','-resultBundlePath',str(evidence/'tests.xcresult'),'-only-testing:'+b.SELECTOR,'CODE_SIGNING_ALLOWED=NO']
                with (evidence/'test.log').open('w') as log:
                    result=subprocess.run([sys.executable,str(Path(b.__file__)),UDID,str(evidence),'--']+command,env=env,stdout=log,stderr=subprocess.STDOUT)
                self.assertEqual(result.returncode,native_exit)
                self.assertEqual((root/'calls').read_text(),'run\n')
                self.assertIn('private-native-canary',(evidence/'test.log').read_text())
                status=json.loads((evidence/'appearance-boundary/status.json').read_text())
                self.assertEqual(status['nativeExit'],native_exit)
                self.assertEqual(status['capability'],'supported')

class AttachmentTests(unittest.TestCase):
    def test_rejects_missing_or_bad_runner_proof_without_disclosing_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); directory=root/'attachments'; directory.mkdir()
            with self.assertRaises(b.BoundaryValidationError): b.validate_runner_attachments(root,UDID)
            make_attachments(root)
            b.validate_runner_attachments(root,UDID)
            body=json.loads((directory/'4.txt').read_text())
            body['testName']='private-canary'; (directory/'4.txt').write_text(json.dumps(body))
            with self.assertRaises(b.BoundaryValidationError) as error: b.validate_runner_attachments(root,UDID)
            self.assertNotIn('private-canary',str(error.exception))

    def test_rejects_neighbour_case_duplicate_wrong_device_and_invalid_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); make_attachments(root); directory=root/'attachments'
            manifest_path=directory/'manifest.json'; original=json.loads(manifest_path.read_text())
            for kind in ('wrong-case','duplicate','wrong-device'):
                manifest=json.loads(json.dumps(original))
                if kind=='wrong-case': manifest[0]['testIdentifier']='OtherTests/test_system_coldUnforcedLaunch_onDarkDevice()'
                if kind=='duplicate': manifest[0]['attachments'].append(manifest[0]['attachments'][0])
                if kind=='wrong-device': manifest[0]['attachments'][0]['deviceId']='22345678-1234-4234-8234-123456789ABC'
                manifest_path.write_text(json.dumps(manifest))
                with self.subTest(kind=kind), self.assertRaises(b.BoundaryValidationError): b.validate_runner_attachments(root,UDID)
            manifest_path.write_text(json.dumps(original))
            record=json.loads((directory/'4.txt').read_text()); record['boundary']='appearanceContext=private-canary'
            (directory/'4.txt').write_text(json.dumps(record))
            with self.assertRaises(b.BoundaryValidationError): b.validate_runner_attachments(root,UDID)
