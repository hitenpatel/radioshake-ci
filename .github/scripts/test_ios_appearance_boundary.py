import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ios_appearance_boundary as b

UDID = '12345678-1234-4234-8234-123456789ABC'

DEFAULT_KEYS=('theme_mode','AppleInterfaceStyle','UIUserInterfaceStyle','NSInterfaceStyle','AppleInterfaceStyleSwitchesAutomatically')
ENV_KEYS=('AppleInterfaceStyle','UIUserInterfaceStyle','NSInterfaceStyle','AppleInterfaceStyleSwitchesAutomatically')
def absent(): return {'state':'absent'}
def value(text): return {'state':'value','type':'string','value':text,'truncated':False}
def sample_context():
    effective={key:absent() for key in DEFAULT_KEYS}; effective['theme_mode']=value('LIGHT')
    domain={'state':'present','values':{key:absent() for key in DEFAULT_KEYS}}
    return dict(pid=88,storeMode='AUTO',
        bundle={'bundleIdentifier':value('com.radioshake.debug'),'CFBundleVersion':value('1'),
                'CFBundleShortVersionString':value('1.0'),'UIUserInterfaceStyle':absent()},
        arguments={'occurrences':[{'index':1,'option':'-RSKeepTheme','value':{'state':'flag'}}],
                   'total':1,'truncated':False,'fixtureEndpoints':True,'skipOnboarding':True},
        environment={key:absent() for key in ENV_KEYS+tuple('SIMCTL_CHILD_'+x for x in ENV_KEYS)},
        defaults={'effective':effective,'app':domain,'global':{'state':'absent'},'argument':domain,'registration':domain},
        start=dict(wall=1,uptime=1),end=dict(wall=2,uptime=2))

def make_attachments(root):
    directory=root/'attachments'; directory.mkdir(exist_ok=True)
    records=[]
    context=sample_context()
    encoded=base64.b64encode(json.dumps(context).encode()).decode()
    for index,phase in enumerate(('beforeLaunch','afterLaunch','attachedSettings','effectiveCheckpoint'),1):
        body=dict(phase=phase,testName='-[RadioShakeUITests.ThemeUITests test_system_coldUnforcedLaunch_onDarkDevice]',matcherSelfCheck=True,sequence=index,pid=44,
                  device=dict(raw=2,interpretation='dark',start=dict(wall=1,uptime=1),end=dict(wall=2,uptime=2),clockBackward=False,wallClockDivergence=False))
        if index > 2: body.update(effective='light',effectiveExists=True,effectiveTruncated=False,systemSelected=False,systemSelectionExists=True,boundary='attached=1;appearanceContext='+encoded,boundaryTruncated=False)
        if index==4: body['waitResult']=False
        name=f'{index}.txt'; (directory/name).write_text(json.dumps(body))
        records.append(dict(exportedFileName=name,deviceId=UDID,suggestedHumanReadableName='system-appearance-'+phase+'_0_random.txt'))
    (directory/'manifest.json').write_text(json.dumps([dict(testIdentifier='ThemeUITests/test_system_coldUnforcedLaunch_onDarkDevice()',testIdentifierURL='test://com.apple.xcode/RadioShake/RadioShakeUITests/ThemeUITests/test_system_coldUnforcedLaunch_onDarkDevice',attachments=records)]))

class TriggerTests(unittest.TestCase):
    def poll(self,path,offset,state):
        return b._trigger(path,offset,state)

    def test_long_complete_line_then_trigger_retains_actual_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'test.log'; prefix=b'x'*5000+b'\n'; line=b"Test Suite 'Selected tests' started at now.\n"
            path.write_bytes(prefix+line); state={}
            trigger,offset=self.poll(path,0,state)
            self.assertIsNotNone(trigger)
            self.assertEqual(trigger['line'],line.decode())
            self.assertEqual(trigger['offset'],len(prefix))
            self.assertEqual(offset,len(prefix)+len(line))

    def test_partial_long_line_continuation_is_not_a_new_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'test.log'; path.write_bytes(b'x'*5000); state={}
            trigger,offset=self.poll(path,0,state); self.assertIsNone(trigger)
            with path.open('ab') as f: f.write(b"Test Suite 'Selected tests' started fake\nordinary\nTest Suite 'ThemeUITests' started real\n")
            trigger,offset=self.poll(path,offset,state)
            self.assertIsNotNone(trigger)
            self.assertEqual(trigger['line'],"Test Suite 'ThemeUITests' started real\n")
            self.assertEqual(trigger['offset'],path.read_bytes().index(b"Test Suite 'ThemeUITests'"))
            self.assertEqual(offset,path.stat().st_size)

    def test_large_log_returns_bounded_progress_per_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'test.log'; path.write_bytes(b'x'*2_000_000); state={}
            trigger,offset=self.poll(path,0,state)
            self.assertIsNone(trigger)
            self.assertGreater(offset,0)
            self.assertLessEqual(offset,65536)

    def test_partial_trigger_is_retained_and_scan_respects_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'test.log'; path.write_bytes(b"Test Suite 'Selected tests' started"); state={}
            trigger,offset=self.poll(path,0,state); self.assertIsNone(trigger)
            self.assertEqual(offset,path.stat().st_size)
            with path.open('ab') as stream: stream.write(b' at now.\n')
            with mock.patch.object(b.time,'monotonic',return_value=10):
                trigger,held=b._trigger(path,offset,state,deadline=10)
            self.assertIsNone(trigger); self.assertEqual(held,offset)
            trigger,offset=self.poll(path,held,state)
            self.assertEqual(trigger['line'],"Test Suite 'Selected tests' started at now.\n")
            self.assertEqual(trigger['offset'],0)

class BoundaryTests(unittest.TestCase):
    def test_read_only_appearance_command_uses_exact_device(self):
        with mock.patch.object(b, '_bounded_command', return_value={'returncode': 0, 'stdout': 'Dark\n', 'stderr': '', 'stdoutTruncated': False, 'stderrTruncated': False, 'timedOut': False}) as command:
            record = b.appearance_query(UDID, 2)
        self.assertEqual(command.call_args.args[0], ['xcrun', 'simctl', 'ui', UDID, 'appearance'])
        self.assertEqual(record['appearance'], 'dark')
        with self.assertRaises(ValueError): b.appearance_query('booted', 2)

    def test_query_timeout_is_clipped_to_remaining_deadline(self):
        response={'returncode':0,'stdout':'Dark\n','stderr':'','stdoutTruncated':False,'stderrTruncated':False,'timedOut':False}
        with mock.patch.object(b.time,'monotonic',side_effect=[4199.5,4199.75]), mock.patch.object(b.time,'time',return_value=100), mock.patch.object(b,'_bounded_command',return_value=response) as command:
            record=b.appearance_query(UDID,2,4200)
        self.assertEqual(record['requestedTimeout'],0.5)
        self.assertEqual(command.call_args.args[1],0.5)
        with mock.patch.object(b.time,'monotonic',return_value=4200), mock.patch.object(b,'_bounded_command') as command:
            self.assertIsNone(b.appearance_query(UDID,2,4200))
            command.assert_not_called()

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
    def exercise(self, *, duration=30, code=0, trigger=None, query_duration=0, cancel=False, record_cap=542, initial_kind='dark', overrun_at=None, timeout_at=None):
        clock=FakeClock(); tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); root=Path(tmp.name)
        log=root/'test.log'; log.write_text('')
        command=['xcodebuild','test','-project','RadioShake.xcodeproj','-scheme','RadioShake','-configuration','Debug','-destination','id='+UDID,'-derivedDataPath','/tmp/ios-diagnostic-derived','-resultBundlePath',str(root/'tests.xcresult'),'-only-testing:'+b.SELECTOR,'CODE_SIGNING_ALLOWED=NO']
        native=FakeNative(clock,duration,code); seen=[]
        def query(udid, timeout, deadline):
            start=clock.monotonic(); seen.append((start,timeout))
            timeout=min(timeout,deadline-start)
            if cancel and len(seen)==2: raise b.Cancelled()
            clock.now += min(query_duration,timeout)
            if len(seen)==overrun_at: clock.now+=2.5
            first=len(seen)==1
            timed_out=query_duration>timeout or len(seen)==timeout_at
            supported=first and initial_kind=='dark' and not timed_out
            returncode=-9 if timed_out else (1 if first and initial_kind=='unsupported' else 0)
            return dict(argv=['xcrun','simctl','ui',udid,'appearance'],udid=udid,hostPID=123,requestedTimeout=timeout,
                        start={'wall':1_700_000_000+start,'monotonic':start},end={'wall':clock.wall(),'monotonic':clock.now},
                        wallClockDivergence=False,appearance='dark' if supported else 'unknown',returncode=returncode,timedOut=timed_out,
                        stdout='Dark\n' if supported else ('private-canary' if first else ''),stderr='',stdoutTruncated=False,stderrTruncated=False)
        def trigger_read(path, offset, state, deadline):
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

    def test_query_cleanup_overrun_is_retained_and_native_exit_unchanged(self):
        root,env,_,_,result,status=self.exercise(duration=10,code=65,trigger=0,overrun_at=2)
        self.assertEqual(result,65)
        self.assertEqual(status['nativeExit'],65)
        records=[json.loads(line) for line in (root/'appearance-boundary/host-samples.jsonl').read_text().splitlines()]
        self.assertTrue(records[1]['durationOverrun'])
        self.assertEqual(records[1]['requestedTimeout'],2)
        with self.assertRaises(b.BoundaryValidationError):
            b.validate_boundary_evidence(root,{k.lower():v for k,v in env.items()},{'destination_udid':UDID},65)
        self.assertTrue((root/'appearance-boundary/status.json').exists())

    def test_exact_two_second_queries_are_within_the_cap(self):
        root,env,_,_,result,_=self.exercise(duration=10,query_duration=2)
        b.validate_boundary_evidence(root,{k.lower():v for k,v in env.items()},{'destination_udid':UDID},result)

    def test_later_timeout_is_explicit_and_valid_within_bounds(self):
        root,env,_,_,result,_=self.exercise(duration=8,trigger=0,timeout_at=2)
        records=[json.loads(line) for line in (root/'appearance-boundary/host-samples.jsonl').read_text().splitlines()]
        self.assertTrue(records[1]['timedOut']); self.assertEqual(records[1]['appearance'],'unknown')
        b.validate_boundary_evidence(root,{k.lower():v for k,v in env.items()},{'destination_udid':UDID},result)

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

    def test_validator_rejects_each_phase_over_duration_or_deadline(self):
        import hashlib
        for phase in ('initial','coarse','fast','invocationEnd'):
            for violation in ('duration','deadline'):
                with self.subTest(phase=phase,violation=violation):
                    root,env,_,_,_,status=self.exercise(duration=50,trigger=25)
                    inputs={k.lower():v for k,v in env.items()}; runtime={'destination_udid':UDID}
                    b.validate_boundary_evidence(root,inputs,runtime,0)
                    directory=root/'appearance-boundary'; path=directory/'host-samples.jsonl'
                    records=[json.loads(line) for line in path.read_text().splitlines()]
                    index=next(i for i,r in enumerate(records) if r['phase']==phase)
                    if violation=='duration':
                        records[index]['end']['monotonic']+=3; records[index]['end']['wall']+=3
                        records[index]['durationOverrun']=True
                        for entry in records[index+1:]:
                            for end in ('start','end'): entry[end]['monotonic']+=3; entry[end]['wall']+=3
                        if phase in ('initial','coarse'):
                            status['trigger']['detected']['monotonic']+=3; status['trigger']['detected']['wall']+=3
                            status['cadenceTransitions'][1]['monotonic']+=3
                    else:
                        records=records[:index+1]
                        entry=records[-1]; shift=4199-entry['start']['monotonic']
                        entry['start']['monotonic']+=shift; entry['start']['wall']+=shift
                        entry['end']['monotonic']=4200.5; entry['end']['wall']=1_700_004_200.5
                        entry['deadlineOverrun']=True
                        entry['requestedTimeout']=1
                        if phase=='fast':
                            status['trigger']['detected']={'wall':1_700_004_199,'monotonic':4199}
                            status['cadenceTransitions'][1]['monotonic']=4199
                        elif phase in ('initial','coarse'):
                            status['trigger']=None; status['cadenceTransitions']=status['cadenceTransitions'][:1]
                        status['counts']={k:sum(r['phase']==k for r in records) for k in status['counts']}
                        if phase!='invocationEnd': status['finalRead']='notAttemptedDeadline'
                    path.write_text(''.join(json.dumps(r)+'\n' for r in records))
                    status['files'][path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
                    (directory/'status.json').write_text(json.dumps(status))
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
    def test_context_schema_rejects_missing_and_unrelated_fields_and_runner_flags(self):
        for mutation in ('missing-domain','missing-state','unknown-bundle','unknown-default','unknown-env','env-type','unknown-option','bad-count','bad-scalar','missing-clock','missing-exists','missing-truncated'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); make_attachments(root); path=root/'attachments/4.txt'; record=json.loads(path.read_text())
                context=sample_context()
                if mutation=='missing-domain': del context['defaults']['argument']
                if mutation=='missing-state': context['defaults']['app']={}
                if mutation=='unknown-bundle': context['bundle']['secret-canary']=value('x')
                if mutation=='unknown-default': context['defaults']['unrelated']='secret-canary'
                if mutation=='unknown-env': context['environment']['unrelated']=value('secret-canary')
                if mutation=='env-type': context['environment']['AppleInterfaceStyle']={'state':'value','type':'bool','value':True}
                if mutation=='unknown-option': context['arguments']['occurrences'][0]['option']='--unrelated'
                if mutation=='bad-count': context['arguments']['total']=2
                if mutation=='bad-scalar': context['environment']['AppleInterfaceStyle']={'state':'value','value':'x'}
                if mutation=='missing-clock': del record['device']['clockBackward']
                if mutation=='missing-exists': del record['systemSelectionExists']
                if mutation=='missing-truncated': del record['boundaryTruncated']
                record['boundary']='appearanceContext='+base64.b64encode(json.dumps(context).encode()).decode()
                path.write_text(json.dumps(record))
                with self.assertRaises(b.BoundaryValidationError) as error: b.validate_runner_attachments(root,UDID)
                self.assertNotIn('secret-canary',str(error.exception))

    def test_valid_explicit_absence_unsupported_truncation_and_false_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); make_attachments(root); path=root/'attachments/4.txt'; record=json.loads(path.read_text())
            context=sample_context()
            context['bundle']['UIUserInterfaceStyle']={'state':'unsupported','type':'Array<String>'}
            context['defaults']['global']={'state':'absent'}
            context['defaults']['argument']['values']['theme_mode']={'state':'value','type':'string','value':'x'*256,'truncated':True}
            record['device']['interpretation']='unknown'; record['effective']='light'; record['systemSelected']=False; record['waitResult']=False
            record['boundary']='appearanceContext='+base64.b64encode(json.dumps(context).encode()).decode(); path.write_text(json.dumps(record))
            b.validate_runner_attachments(root,UDID)
            context.update({k:{'state':'truncated'} for k in ('bundle','arguments','environment','defaults')})
            record['boundary']='appearanceContext='+base64.b64encode(json.dumps(context).encode()).decode(); path.write_text(json.dumps(record))
            b.validate_runner_attachments(root,UDID)

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
