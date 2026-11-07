#!/usr/bin/env python3
"""Private, exact-method system appearance observation; no public payload output."""
import base64
import hashlib
import json
import math
import os
import plistlib
import re
import selectors
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

SELECTOR = 'RadioShakeUITests/ThemeUITests/test_system_coldUnforcedLaunch_onDarkDevice'
PROFILE = 'system-appearance-boundary-v1'
KEYS = ('CFBundleIdentifier', 'CFBundleVersion', 'CFBundleShortVersionString',
        'MinimumOSVersion', 'DTSDKName', 'UIUserInterfaceStyle')
MAX_SECONDS = 4200
MAX_RECORDS = 542

class BoundaryValidationError(Exception):
    pass

class Cancelled(Exception):
    pass

def _udid(value):
    try:
        if str(uuid.UUID(value)).upper() != value.upper() or not re.fullmatch(r'[0-9A-Fa-f-]{36}', value):
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError('invalid destination') from None
    return value

def _bounded_command(argv, timeout):
    """Drain both pipes without retaining more than 4096 bytes per stream."""
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    selector = selectors.DefaultSelector()
    data = {'stdout': bytearray(), 'stderr': bytearray()}
    truncated = {'stdout': False, 'stderr': False}
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        for name in data:
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                name = key.data
                space = 4096 - len(data[name])
                data[name].extend(chunk[:space])
                truncated[name] |= len(chunk) > space
        if not timed_out:
            try: process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired: timed_out = True
    finally:
        if timed_out or process.poll() is None:
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
        process.wait()
        selector.close()
        process.stdout.close(); process.stderr.close()
    decoded={}
    for name in data:
        encoded=bytes(data[name]).decode('utf-8','replace').encode('utf-8')
        truncated[name] |= len(encoded) > 4096
        decoded[name]=encoded[:4096].decode('utf-8','ignore')
    return dict(returncode=process.returncode, timedOut=timed_out, **decoded,
                stdoutTruncated=truncated['stdout'], stderrTruncated=truncated['stderr'])

def appearance_query(udid, timeout):
    _udid(udid)
    argv = ['xcrun', 'simctl', 'ui', udid, 'appearance']
    start = dict(wall=time.time(), monotonic=time.monotonic())
    try:
        result = _bounded_command(argv, timeout)
    except Cancelled:
        raise
    except Exception as exc:
        result = dict(returncode=None, timedOut=False, stdout='', stderr='', stdoutTruncated=False,
                      stderrTruncated=False, errorType=type(exc).__name__)
    end = dict(wall=time.time(), monotonic=time.monotonic())
    value = result['stdout'].strip().lower()
    parsed = value if result['returncode'] == 0 and not result['timedOut'] and value in ('light', 'dark') else 'unknown'
    jump = abs((end['wall']-start['wall'])-(end['monotonic']-start['monotonic'])) > 0.25
    return dict(argv=argv, udid=udid, hostPID=os.getpid(), start=start, end=end, wallClockDivergence=jump,
                appearance=parsed, **result)

def scalar(value, present=True):
    if not present: return {'state':'absent'}
    if isinstance(value, str):
        raw=value.encode('utf-8'); clipped=raw[:256].decode('utf-8', 'ignore')
        return dict(state='value', type='string', value=clipped, truncated=len(raw)>256)
    if isinstance(value, bool): return dict(state='value', type='bool', value=value)
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return dict(state='value', type=type(value).__name__, value=value)
    return dict(state='unsupported', type=type(value).__name__[:256])

def project_plist(plist):
    return {k: scalar(plist.get(k), k in plist) for k in KEYS}

def _safe_file(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError('unavailable file')
    return path

def _plist_record(path):
    try:
        data = _safe_file(path).read_bytes()
        plist = plistlib.loads(data)
        if not isinstance(plist, dict): raise ValueError()
        return dict(state='available', sha256=hashlib.sha256(data).hexdigest(), size=len(data), values=project_plist(plist))
    except Exception as exc:
        return dict(state='unavailable', errorType=type(exc).__name__)

def _write(path, value):
    path.write_text(json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n')

def _configuration(udid, directory):
    # macOS /tmp is a platform symlink to /private/tmp; inspect the fixed physical
    # root and refuse symlinks inside the derived-data path.
    built = _plist_record(Path('/tmp').resolve()/'ios-diagnostic-derived/Build/Products/Debug-iphonesimulator/RadioShake.app/Info.plist')
    installed = {'state':'unavailable', 'reason':'builtIdentityUnavailable'}
    identity = built.get('values', {}).get('CFBundleIdentifier', {})
    if identity.get('state') == 'value' and identity.get('type') == 'string' and identity.get('value') == 'com.radioshake.debug' and not identity.get('truncated'):
        try:
            result = _bounded_command(['xcrun', 'simctl', 'get_app_container', udid, identity['value'], 'app'], 2)
            result['argv']=['xcrun', 'simctl', 'get_app_container', udid, identity['value'], 'app']
            if result['returncode'] != 0 or result['timedOut'] or result['stdoutTruncated']:
                installed = {'state':'unavailable', 'reason':'containerQueryFailed', 'query':result}
            else:
                container = Path(result['stdout'].strip())
                if not container.is_absolute() or any(p.is_symlink() for p in (container, *container.parents)) or not container.is_dir():
                    raise ValueError()
                installed = _plist_record(container/'Info.plist')
                installed['containerQuery'] = result
        except Exception as exc:
            installed = {'state':'unavailable', 'errorType':type(exc).__name__}
    elif built['state'] == 'available':
        installed = {'state':'unavailable', 'reason':'unexpectedBuiltIdentity'}
    _write(directory/'built-plist.json', built)
    _write(directory/'installed-plist.json', installed)
    comparison={'built':built['state'], 'installed':installed['state'], 'hashMatch':None, 'bundleIdentityMatch':None}
    if built['state'] == installed['state'] == 'available':
        comparison['hashMatch'] = built['sha256'] == installed['sha256']
        comparison['bundleIdentityMatch'] = built['values']['CFBundleIdentifier'] == installed['values']['CFBundleIdentifier']
    return comparison

def _trigger(path, offset):
    try:
        with path.open('rb') as log:
            log.seek(offset)
            while True:
                position = log.tell(); line = log.readline(4097)
                if not line: return None, position
                if not line.endswith(b'\n'):
                    return None, position
                if (line.startswith(b"Test Suite 'Selected tests' started") or
                    line.startswith(b"Test Suite 'ThemeUITests' started")):
                    return {'line':line[:4096].decode('utf-8','replace'), 'offset':position,
                            'detected':{'wall':time.time(), 'monotonic':time.monotonic()}}, log.tell()
    except FileNotFoundError:
        return None, offset

def _check_command(udid, evidence, command):
    expected = ['xcodebuild', 'test', '-project', 'RadioShake.xcodeproj', '-scheme', 'RadioShake',
                '-configuration', 'Debug', '-destination', 'id='+udid, '-derivedDataPath',
                '/tmp/ios-diagnostic-derived', '-resultBundlePath', str(evidence/'tests.xcresult'),
                '-only-testing:'+SELECTOR, 'CODE_SIGNING_ALLOWED=NO']
    if command != expected or os.environ.get('SELECTOR') != SELECTOR: raise ValueError('invalid command')

def run(udid, evidence, command):
    _udid(udid); evidence = Path(evidence); _check_command(udid, evidence, command)
    directory=evidence/'appearance-boundary'; directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    started=time.monotonic(); deadline=started+MAX_SECONDS
    requests={k.lower():os.environ[k] for k in ('REF','SHA','SELECTOR','REQUEST_ID','DEVICE')}
    status=dict(profile=PROFILE, schema=1, inputs=requests, udid=udid, nativeExit=None,
                termination='initializing', observerExit='incomplete', startedMonotonic=started,
                deadlineMonotonic=deadline, trigger=None, counts={'initial':0,'coarse':0,'fast':0,'invocationEnd':0},
                finalRead='notAttempted', configuration={}, cadenceTransitions=[], files={})
    samples=directory/'host-samples.jsonl'; native=None; rc=None; cancelled=False
    previous_sample=None
    old_handlers={}
    def cancel(signum, frame): raise Cancelled()
    for sig in (signal.SIGTERM, signal.SIGINT):
        old_handlers[sig]=signal.signal(sig,cancel)
    def sample(kind):
        nonlocal previous_sample
        remaining=deadline-time.monotonic()
        if remaining <= 0: return None
        result=appearance_query(udid,min(2,remaining)); result['phase']=kind
        result['wallClockDivergenceSincePrevious'] = bool(previous_sample and abs(
            (result['start']['wall']-previous_sample['end']['wall']) -
            (result['start']['monotonic']-previous_sample['end']['monotonic'])) > 0.25)
        previous_sample=result
        with samples.open('a') as stream: stream.write(json.dumps(result, separators=(',',':'))+'\n')
        status['counts'][kind]+=1
        return result
    try:
        samples.touch(mode=0o600)
        try:
            help_result = _bounded_command(['xcrun','simctl','help','ui'], 2)
        except Cancelled:
            raise
        except Exception as exc:
            help_result = dict(returncode=None,timedOut=False,stdout='',stderr='',stdoutTruncated=False,
                               stderrTruncated=False,errorType=type(exc).__name__)
        help_result['argv']=['xcrun','simctl','help','ui']
        _write(directory/'simctl-help.json',help_result)
        help_text=help_result['stdout']+'\n'+help_result['stderr']
        status['helpSupported'] = (help_result['returncode'] == 0 and not help_result['timedOut'] and
                                   re.search(r'\bappearance\b',help_text,re.IGNORECASE) is not None)
        started=time.monotonic(); deadline=started+MAX_SECONDS
        status['startedMonotonic']=started; status['deadlineMonotonic']=deadline
        initial=sample('initial')
        if initial and initial['returncode'] == 0 and initial['appearance'] in ('light','dark') and status['helpSupported']:
            status['capability']='supported'
        elif initial and initial['returncode'] not in (None,0) and not initial['timedOut']:
            status['capability']='unsupported'
        else:
            status['capability']='unknown'
        native=subprocess.Popen(command, start_new_session=True)
        next_query=time.monotonic()+10; fast_end=None; offset=0
        status['cadenceTransitions'].append({'kind':'coarse','monotonic':time.monotonic()})
        while native.poll() is None:
            now=time.monotonic()
            if now >= deadline: status['termination']='deadline'; break
            if fast_end is not None and now >= fast_end: status['termination']='fastWindowEnded'; break
            if status['trigger'] is None:
                trigger,offset=_trigger(evidence/'test.log',offset)
                if trigger:
                    detected=trigger['detected']['monotonic']
                    status['trigger']=trigger; fast_end=detected+120; next_query=detected
                    status['cadenceTransitions'].append({'kind':'fast','monotonic':detected})
            if now >= next_query:
                kind='fast' if fast_end is not None else 'coarse'
                if sum(status['counts'].values()) >= MAX_RECORDS:
                    status['termination']='recordBudget'; break
                if status['counts'][kind] >= (120 if kind=='fast' else 420):
                    status['termination']=kind+'Budget'; break
                record=sample(kind)
                if record is None:
                    status['termination']='deadline'; break
                next_query=max(record['start']['monotonic']+(1 if kind=='fast' else 10), time.monotonic())
            else:
                try: native.wait(timeout=min(1, max(0, next_query-now), max(0,deadline-now)))
                except subprocess.TimeoutExpired: pass
        if status['termination'] == 'initializing': status['termination']='invocationEnded'
        rc=native.wait()
        if time.monotonic() >= deadline: status['finalRead']='notAttemptedDeadline'
        elif sum(status['counts'].values()) >= MAX_RECORDS: status['finalRead']='notAttemptedBudget'
        elif sample('invocationEnd'): status['finalRead']='attemptedAfterInvocationAndTeardown'
        status['observerExit']='complete'
    except Cancelled:
        cancelled=True; status['termination']='cancelled'
    except Exception as exc:
        status['termination']='observerError'; status['errorType']=type(exc).__name__
        if native is not None:
            rc=native.wait()
    finally:
        for sig in old_handlers: signal.signal(sig,signal.SIG_IGN)
        if native is not None and native.poll() is None:
            try: os.killpg(native.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: native.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try: os.killpg(native.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                native.wait()
        if rc is None and native is not None: rc=native.returncode
        status['nativeExit']=rc
        try: status['configuration']=_configuration(udid,directory)
        except Exception as exc: status['configuration']={'state':'error','errorType':type(exc).__name__}
        status['endedMonotonic']=time.monotonic()
        for path in directory.iterdir():
            if path.is_file() and path.name != 'status.json':
                status['files'][path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        _write(directory/'status.json',status)
        for sig,handler in old_handlers.items(): signal.signal(sig,handler)
    return rc if rc is not None else (128+signal.SIGTERM if cancelled else 127)

def validate_boundary_evidence(root, inputs, runtime, native_exit):
    try:
        if inputs['selector'] != SELECTOR: raise ValueError()
        udid=_udid(runtime['destination_udid']); directory=Path(root)/'appearance-boundary'
        if directory.is_symlink() or (directory/'status.json').is_symlink(): raise ValueError()
        status=json.loads((directory/'status.json').read_text())
        expected={k:inputs[k] for k in ('ref','sha','selector','request_id','device')}
        if (status['schema'] != 1 or status['profile'] != PROFILE or status['inputs'] != expected or
            status['udid'] != udid or type(native_exit) is not int or status['nativeExit'] != native_exit or
            status['observerExit'] != 'complete'): raise ValueError()
        if status['termination'] not in ('invocationEnded','deadline','fastWindowEnded','coarseBudget','fastBudget','recordBudget'): raise ValueError()
        counts=status['counts']
        if set(counts) != {'initial','coarse','fast','invocationEnd'} or any(type(v) is not int or v < 0 for v in counts.values()): raise ValueError()
        if counts['initial'] != 1 or counts['coarse'] > 420 or counts['fast'] > 120 or counts['invocationEnd'] > 1 or sum(counts.values()) > MAX_RECORDS: raise ValueError()
        if any(type(status[k]) not in (int,float) or not math.isfinite(status[k]) for k in ('startedMonotonic','deadlineMonotonic','endedMonotonic')): raise ValueError()
        if status['deadlineMonotonic']-status['startedMonotonic'] != MAX_SECONDS: raise ValueError()
        if status['finalRead'] not in ('attemptedAfterInvocationAndTeardown','notAttemptedDeadline','notAttemptedBudget'): raise ValueError()
        if (status['finalRead'] == 'attemptedAfterInvocationAndTeardown') != (counts['invocationEnd'] == 1): raise ValueError()
        if status['helpSupported'] is not True or status['capability'] != 'supported': raise ValueError()
        names={'host-samples.jsonl','built-plist.json','installed-plist.json','simctl-help.json'}
        if set(status['files']) != names: raise ValueError()
        for name in names:
            path=directory/name
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != status['files'][name]: raise ValueError()
        help_record=json.loads((directory/'simctl-help.json').read_text())
        if (help_record['argv'] != ['xcrun','simctl','help','ui'] or help_record['returncode'] != 0 or help_record['timedOut'] is not False or
            not re.search(r'\bappearance\b',help_record['stdout']+'\n'+help_record['stderr'],re.IGNORECASE)): raise ValueError()
        if any(type(help_record[k]) is not bool for k in ('stdoutTruncated','stderrTruncated')) or any(len(help_record[k].encode()) > 4096 for k in ('stdout','stderr')): raise ValueError()
        observed={k:0 for k in counts}
        previous_end=None; previous_record=None; first=None; phases=[]; previous_starts={}; phase_starts={'coarse':[],'fast':[]}
        for line in (directory/'host-samples.jsonl').read_text().splitlines():
            entry=json.loads(line); phase=entry['phase']; observed[phase]+=1
            if entry['udid'] != udid or entry['argv'] != ['xcrun','simctl','ui',udid,'appearance'] or entry['appearance'] not in ('light','dark','unknown'): raise ValueError()
            if len(entry['stdout'].encode()) > 4096 or len(entry['stderr'].encode()) > 4096: raise ValueError()
            if any(type(entry[k]) is not bool for k in ('timedOut','stdoutTruncated','stderrTruncated','wallClockDivergence','wallClockDivergenceSincePrevious')): raise ValueError()
            if entry['returncode'] is not None and type(entry['returncode']) is not int: raise ValueError()
            if type(entry['hostPID']) is not int or entry['hostPID'] <= 0: raise ValueError()
            for bracket in ('start','end'):
                if any(type(entry[bracket][key]) not in (int,float) or not math.isfinite(entry[bracket][key]) for key in ('wall','monotonic')): raise ValueError()
            start=entry['start']['monotonic']; end=entry['end']['monotonic']
            if end < start or (previous_end is not None and start < previous_end) or start >= status['deadlineMonotonic']: raise ValueError()
            if start < status['startedMonotonic']: raise ValueError()
            if phase in ('coarse','fast'):
                cadence=10 if phase=='coarse' else 1
                if phase in previous_starts and start-previous_starts[phase] < cadence: raise ValueError()
                previous_starts[phase]=start
                phase_starts[phase].append(start)
            actual=entry['stdout'].strip().lower()
            parsed=actual if entry['returncode'] == 0 and not entry['timedOut'] and actual in ('light','dark') else 'unknown'
            if parsed != entry['appearance']: raise ValueError()
            jumped=abs((entry['end']['wall']-entry['start']['wall'])-(end-start))>0.25
            if entry['wallClockDivergence'] != jumped: raise ValueError()
            between=bool(previous_record and abs((entry['start']['wall']-previous_record['end']['wall'])-(start-previous_record['end']['monotonic']))>0.25)
            if entry['wallClockDivergenceSincePrevious'] != between: raise ValueError()
            previous_record=entry
            previous_end=end; phases.append(phase)
            if first is None: first=entry
        if observed != counts: raise ValueError()
        if not phases or phases[0] != 'initial' or (counts['invocationEnd'] and phases[-1] != 'invocationEnd'): raise ValueError()
        if phases != sorted(phases,key={'initial':0,'coarse':1,'fast':2,'invocationEnd':3}.get): raise ValueError()
        if counts['fast'] and status['trigger'] is None: raise ValueError()
        if status['trigger'] is not None:
            trigger=status['trigger']
            if type(trigger['offset']) is not int or trigger['offset'] < 0 or not isinstance(trigger['line'],str) or len(trigger['line'].encode()) > 4096: raise ValueError()
            if not (trigger['line'].startswith("Test Suite 'Selected tests' started") or trigger['line'].startswith("Test Suite 'ThemeUITests' started")): raise ValueError()
            if any(type(trigger['detected'][k]) not in (int,float) or not math.isfinite(trigger['detected'][k]) for k in ('wall','monotonic')): raise ValueError()
            moment=trigger['detected']['monotonic']
            if any(t > moment for t in phase_starts['coarse']) or any(t < moment or t >= moment+120 for t in phase_starts['fast']): raise ValueError()
        transitions=status['cadenceTransitions']
        expected_kinds=['coarse']+(['fast'] if status['trigger'] is not None else [])
        if not isinstance(transitions,list) or [x.get('kind') for x in transitions] != expected_kinds: raise ValueError()
        if any(type(x.get('monotonic')) not in (int,float) or not math.isfinite(x['monotonic']) for x in transitions): raise ValueError()
        if status['trigger'] is not None and transitions[1]['monotonic'] != status['trigger']['detected']['monotonic']: raise ValueError()
        if (first['returncode'] != 0 or first['timedOut'] or first['appearance'] not in ('light','dark') or
            first['stdout'].strip().lower() != first['appearance']): raise ValueError()
        for name in ('built-plist.json','installed-plist.json'):
            record=json.loads((directory/name).read_text())
            if record.get('state') not in ('available','unavailable'): raise ValueError()
            if record['state']=='available':
                if set(record['values']) != set(KEYS) or not re.fullmatch('[0-9a-f]{64}',record['sha256']) or type(record['size']) is not int or record['size'] < 0: raise ValueError()
                for value in record['values'].values():
                    if value.get('state') == 'absent':
                        if set(value) != {'state'}: raise ValueError()
                    elif value.get('state') == 'unsupported':
                        if set(value) != {'state','type'} or not isinstance(value['type'],str) or len(value['type'].encode()) > 256: raise ValueError()
                    elif value.get('state') == 'value':
                        kind=value.get('type'); raw=value.get('value')
                        if kind == 'string':
                            if set(value) != {'state','type','value','truncated'} or not isinstance(raw,str) or len(raw.encode()) > 256 or type(value['truncated']) is not bool: raise ValueError()
                        elif kind == 'bool':
                            if set(value) != {'state','type','value'} or type(raw) is not bool: raise ValueError()
                        elif kind in ('int','float'):
                            if set(value) != {'state','type','value'} or type(raw) is not (int if kind == 'int' else float) or not math.isfinite(raw): raise ValueError()
                        else: raise ValueError()
                    else: raise ValueError()
        validate_runner_attachments(root,udid)
    except Exception:
        raise BoundaryValidationError() from None

def validate_runner_attachments(root,udid):
    try:
        directory=Path(root)/'attachments'
        if directory.is_symlink() or (directory/'manifest.json').is_symlink(): raise ValueError()
        manifest=json.loads((directory/'manifest.json').read_text())
        cases=[case for case in manifest if case.get('testIdentifier') == 'ThemeUITests/test_system_coldUnforcedLaunch_onDarkDevice()']
        if len(cases) != 1: raise ValueError()
        if cases[0].get('testIdentifierURL') != 'test://com.apple.xcode/RadioShake/RadioShakeUITests/ThemeUITests/test_system_coldUnforcedLaunch_onDarkDevice': raise ValueError()
        phases=('beforeLaunch','afterLaunch','attachedSettings','effectiveCheckpoint')
        selected={}
        for attachment in cases[0]['attachments']:
            human=attachment.get('suggestedHumanReadableName','')
            for phase in phases:
                if human.startswith('system-appearance-'+phase+'_'):
                    if phase in selected: raise ValueError()
                    if attachment.get('deviceId') != udid: raise ValueError()
                    filename=attachment['exportedFileName']
                    if not isinstance(filename,str) or Path(filename).name != filename or filename in ('.','..'): raise ValueError()
                    path=directory/filename
                    if path.is_symlink() or not path.is_file() or path.stat().st_size > 24*1024: raise ValueError()
                    selected[phase]=json.loads(path.read_text())
        if set(selected) != set(phases): raise ValueError()
        accepted={
            '-[RadioShakeUITests.ThemeUITests test_system_coldUnforcedLaunch_onDarkDevice]',
            '-[ThemeUITests test_system_coldUnforcedLaunch_onDarkDevice]',
            'RadioShakeUITests.ThemeUITests.test_system_coldUnforcedLaunch_onDarkDevice',
            'ThemeUITests.test_system_coldUnforcedLaunch_onDarkDevice',
            'test_system_coldUnforcedLaunch_onDarkDevice'}
        for sequence,phase in enumerate(phases,1):
            record=selected[phase]
            if (record.get('phase') != phase or record.get('testName') not in accepted or
                record.get('matcherSelfCheck') is not True or record.get('sequence') != sequence or
                type(record.get('pid')) is not int or record['pid'] <= 0): raise ValueError()
            device=record['device']
            if type(device['raw']) is not int or not isinstance(device['interpretation'],str): raise ValueError()
            for bracket in ('start','end'):
                if any(type(device[bracket][key]) not in (int,float) or not math.isfinite(device[bracket][key]) for key in ('wall','uptime')): raise ValueError()
            if phase in ('attachedSettings','effectiveCheckpoint'):
                if not isinstance(record['effective'],str) or len(record['effective'].encode()) > 256 or type(record['systemSelected']) is not bool or not isinstance(record['boundary'],str): raise ValueError()
                match=re.search(r'(?:^|;)appearanceContext=([^;]+)',record['boundary'])
                if not match: raise ValueError()
                context=base64.b64decode(match.group(1),validate=True)
                if len(context)>12*1024: raise ValueError()
                context=json.loads(context)
                if set(context) != {'pid','storeMode','bundle','arguments','environment','defaults','start','end'}: raise ValueError()
                if type(context['pid']) is not int or context['pid'] <= 0 or not isinstance(context['storeMode'],str) or len(context['storeMode'].encode())>256: raise ValueError()
                if any(not isinstance(context[key],dict) for key in ('bundle','arguments','environment','defaults')): raise ValueError()
                def check_scalar_caps(value):
                    if isinstance(value,str):
                        if len(value.encode()) > 256: raise ValueError()
                    elif isinstance(value,dict):
                        for k,v in value.items(): check_scalar_caps(k); check_scalar_caps(v)
                    elif isinstance(value,list):
                        for item in value: check_scalar_caps(item)
                    elif isinstance(value,float) and not math.isfinite(value): raise ValueError()
                    elif value is not None and type(value) not in (bool,int,float): raise ValueError()
                check_scalar_caps(context)
                for bracket in ('start','end'):
                    if any(type(context[bracket][key]) not in (int,float) or not math.isfinite(context[bracket][key]) for key in ('wall','uptime')): raise ValueError()
            if phase == 'effectiveCheckpoint' and type(record.get('waitResult')) is not bool: raise ValueError()
    except Exception:
        raise BoundaryValidationError() from None

def main():
    try:
        if len(sys.argv) < 5 or sys.argv[3] != '--': raise ValueError()
        return run(sys.argv[1],Path(sys.argv[2]),sys.argv[4:])
    except Exception:
        return 2

if __name__ == '__main__': sys.exit(main())
