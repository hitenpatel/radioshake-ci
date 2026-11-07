#!/usr/bin/env python3
"""Request-bound private iOS diagnostic evidence retrieval."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from ios_diagnostic import validate_inputs
from ios_appearance_boundary import SELECTOR as BOUNDARY_SELECTOR, validate_boundary_evidence

REPO = 'hitenpatel/radioshake-ci'
WORKFLOW = '.github/workflows/ios-diagnostic-tests.yml'
API = 'https://api.github.com/repos/' + REPO
SOURCE = '/home/ubuntu/dev/RadioShake'
ROOT = Path('/tmp/rs-ios-diagnostics')
KEY = Path.home()/'.config/radioshake-shots/key.pem'

class DiagnosticError(Exception):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class Transport:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, url, data=None, authenticated=True):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or (authenticated and parsed.netloc != 'api.github.com'):
            raise DiagnosticError()
        headers = {'Accept': 'application/vnd.github+json'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + self.token
        if data is not None:
            data = json.dumps(data).encode(); headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                return exc.code, dict(exc.headers), b''
            raise DiagnosticError() from None
        except Exception:
            raise DiagnosticError() from None

    def json(self, method, url, data=None):
        status, _, body = self.request(method, url, data)
        if status not in (200, 201, 204): raise DiagnosticError()
        try: return json.loads(body) if body else {}
        except Exception: raise DiagnosticError() from None

    def archive(self, artifact_id):
        status, headers, _ = self.request('GET', API + '/actions/artifacts/%s/zip' % artifact_id)
        location = next((v for k,v in headers.items() if k.lower() == 'location'), None)
        if status not in (301,302,303,307,308) or not location: raise DiagnosticError()
        status, _, body = self.request('GET', location, authenticated=False)
        if status != 200: raise DiagnosticError()
        return body

def select_run(runs, req):
    matches = [r for r in runs if r.get('display_title') == 'ios-diagnostic ' + req['request_id']]
    if len(matches) != 1: raise DiagnosticError()
    r = matches[0]
    if (r.get('event') != 'workflow_dispatch' or r.get('path') != WORKFLOW or
        r.get('head_sha') != req['workflow_sha'] or not isinstance(r.get('id'), int) or
        not isinstance(r.get('run_attempt'), int) or r['run_attempt'] != 1 or
        r.get('html_url') != 'https://github.com/%s/actions/runs/%s' % (REPO,r['id'])):
        raise DiagnosticError()
    return r

def select_artifact(items, request_id):
    matches = [a for a in items if a.get('name') == 'ios-diagnostic-encrypted-' + request_id]
    if len(matches) != 1 or matches[0].get('expired') is not False or type(matches[0].get('id')) is not int: raise DiagnosticError()
    return matches[0]

def unpack_zip(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) != 2 or {x.filename for x in infos} != {'key.enc','bundle.tgz.enc'}: raise DiagnosticError()
            if any(x.is_dir() or (x.external_attr >> 16 and stat.S_IFMT(x.external_attr >> 16) not in (0,stat.S_IFREG)) for x in infos): raise DiagnosticError()
            if any(x.file_size > 2_000_000_000 for x in infos): raise DiagnosticError()
            return {x.filename:z.read(x) for x in infos}
    except Exception: raise DiagnosticError() from None

def extract_tar(data, root):
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as t:
            seen=set()
            for m in t:
                path=PurePosixPath(m.name)
                if path.is_absolute() or '..' in path.parts or not (m.isfile() or m.isdir()): raise DiagnosticError()
                parts=[p for p in path.parts if p != '.']
                if not parts:
                    if not m.isdir(): raise DiagnosticError()
                    continue
                name='/'.join(parts)
                if name in seen: raise DiagnosticError()
                seen.add(name); out=root.joinpath(*parts)
                if m.isdir(): out.mkdir(parents=True,exist_ok=True)
                else:
                    out.parent.mkdir(parents=True,exist_ok=True)
                    with out.open('xb') as f:
                        source=t.extractfile(m)
                        if source is None: raise DiagnosticError()
                        while chunk := source.read(1024*1024): f.write(chunk)
    except Exception: raise DiagnosticError() from None

def verify_manifest(root, req, run):
    try:
        m=json.loads((root/'manifest.json').read_text()); rt=m['runtime']; ex=m['execution']; stages=m['stages']
        expected={k:req[k] for k in ('ref','sha','selector','request_id','device')}
        fields=dict(source_sha=req['sha'],workflow_repository=REPO,workflow_path=WORKFLOW,workflow_commit=req['workflow_sha'],run_id=str(run['id']),run_attempt=str(run['run_attempt']),job='diagnostic',configuration='Debug',optimization='native Debug -Onone')
        if m['schema_version'] != 1 or m['inputs'] != expected or any(rt.get(k)!=v for k,v in fields.items()): raise DiagnosticError()
        files={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and p != root/'manifest.json'}
        if files != m['file_hashes']: raise DiagnosticError()
        if req['selector'] == BOUNDARY_SELECTOR:
            validate_boundary_evidence(root, expected, rt, ex['xcode_exit'])
        base_stages = {'clone','toolchain','gradle','generate','simulator','test'}
        legacy = req['workflow_sha'] == '602eda30c1330ee9c89ace87e4772c3d9b8fbf1c' and set(stages) == base_stages
        if not legacy and set(stages) != base_stages | {'attachments'}: raise DiagnosticError()
        if any(v is not None and (type(v) is not int or v < 0) for v in stages.values()): raise DiagnosticError()
        if not legacy:
            present = ex['xcresult_present']
            if type(present) is not bool: raise DiagnosticError()
            if present:
                if (not (root/'tests.xcresult').is_dir() or not (root/'attachments').is_dir() or
                    'attachments-export.log' not in files or not (root/'attachments-export.log').is_file() or
                    type(stages['attachments']) is not int or stages['attachments'] != 0): raise DiagnosticError()
            elif stages['attachments'] is not None or (root/'tests.xcresult').exists(): raise DiagnosticError()
        order = [stages[k] for k in ('clone','toolchain','gradle','generate','simulator','test')]
        if ex['outcome'] == 'incomplete' and run['conclusion'] == 'failure':
            failures = [i for i,v in enumerate(order) if v is not None and v != 0]
            result_path = root/'tests.xcresult'
            if (order[:5] == [0]*5 and type(order[5]) is int and order[5] > 0 and
                type(ex['xcode_exit']) is int and ex['xcode_exit'] == order[5] and
                all(type(ex[k]) is int and ex[k] == 0 for k in ('passed','failed','skipped')) and
                'test.log' in files and (root/'test.log').is_file() and
                type(ex['xcresult_present']) is bool and
                ((ex['xcresult_present'] and result_path.is_dir()) or
                 (not ex['xcresult_present'] and not result_path.exists()))):
                return 1
            if (len(failures) == 1 and failures[0] < 5 and all(v == 0 for v in order[:failures[0]]) and
                all(v is None for v in order[failures[0]+1:]) and ex['xcode_exit'] is None and
                ex['xcresult_present'] is False and all(ex[k] == 0 and type(ex[k]) is int for k in ('passed','failed','skipped'))):
                return 1
            raise DiagnosticError()
        if ex['outcome'] not in ('passed','failed') or any(type(ex[k]) is not int or ex[k] < 0 for k in ('passed','failed','skipped')) or ex['passed']+ex['failed'] < 1 or ex['xcresult_present'] is not True or not (root/'tests.xcresult').is_dir(): raise DiagnosticError()
        if not rt.get('destination_udid') or rt.get('runtime') != 'com.apple.CoreSimulator.SimRuntime.iOS-26-5' or rt.get('arch') != 'arm64': raise DiagnosticError()
        if ex['xcode_exit'] != stages['test'] or type(ex['xcode_exit']) is not int: raise DiagnosticError()
        if ex['outcome']=='passed':
            if any(v != 0 for v in stages.values()) or ex['xcode_exit'] != 0 or ex['failed'] != 0 or run['conclusion'] != 'success': raise DiagnosticError()
            return 0
        if run['conclusion'] != 'failure': raise DiagnosticError()
        return 1
    except Exception: raise DiagnosticError() from None

def token_from_config():
    text=(Path.home()/'.config/gh/hosts.yml').read_text()
    match=re.search(r'^\s+oauth_token:\s*(\S+)\s*$', text, re.M)
    if not match: raise DiagnosticError()
    return match.group(1).strip('"\'')

def process(args, **kwargs):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, **kwargs).stdout

def decrypt(files, work, runner=process):
    try:
        for name,data in files.items(): (work/name).write_bytes(data)
        runner(['openssl','pkeyutl','-decrypt','-inkey',str(KEY),'-pkeyopt','rsa_padding_mode:oaep','-pkeyopt','rsa_oaep_md:sha256','-in',str(work/'key.enc'),'-out',str(work/'key')])
        runner(['openssl','enc','-d','-aes-256-cbc','-pbkdf2','-iter','200000','-pass','file:'+str(work/'key'),'-in',str(work/'bundle.tgz.enc'),'-out',str(work/'bundle.tgz')])
        return (work/'bundle.tgz').read_bytes()
    except Exception: raise DiagnosticError() from None
    finally: (work/'key').unlink(missing_ok=True)

def collection(api, url, key):
    items=[]
    for page in range(1, 11):
        data=api.json('GET',url + '&page=' + str(page))
        batch=data[key]; items.extend(batch)
        total=data.get('total_count',len(items))
        if type(total) is not int or total < len(items): raise DiagnosticError()
        if len(items) == total: return items
        if len(batch) != 100: raise DiagnosticError()
    raise DiagnosticError()

def list_runs(api):
    return collection(api,API+'/actions/workflows/ios-diagnostic-tests.yml/runs?event=workflow_dispatch&per_page=100','workflow_runs')

def execute_fetch(req, api, work, deadline, runner=process, sleep=time.sleep, clock=time.monotonic):
    while True:
        runs=list_runs(api)
        named=[r for r in runs if r.get('display_title')=='ios-diagnostic '+req['request_id']]
        if named:
            r=select_run(runs,req)
            if r['status']=='completed': break
        if clock() >= deadline: raise DiagnosticError()
        sleep(5)
    if r['conclusion'] not in ('success','failure'): raise DiagnosticError()
    jobs=collection(api,API+'/actions/runs/%s/attempts/%s/jobs?per_page=100' % (r['id'],r['run_attempt']),'jobs')
    if len(jobs)!=1 or jobs[0].get('name')!='diagnostic' or jobs[0].get('status')!='completed' or jobs[0].get('conclusion') != r['conclusion']: raise DiagnosticError()
    artifact=select_artifact(collection(api,API+'/actions/runs/%s/artifacts?per_page=100' % r['id'],'artifacts'), req['request_id'])
    files=unpack_zip(api.archive(artifact['id']))
    attempt=Path(tempfile.mkdtemp(prefix='attempt-',dir=work))
    try: bundle=decrypt(files,attempt,runner)
    except Exception: raise DiagnosticError() from None
    evidence=attempt/'evidence'; evidence.mkdir()
    extract_tar(bundle,evidence)
    result=verify_manifest(evidence,req,r)
    api.json('DELETE',API+'/actions/artifacts/%s' % artifact['id'])
    print('DIAGNOSTIC_PASS' if result==0 else 'DIAGNOSTIC_FAIL')
    print('run='+r['html_url']); print('evidence='+str(evidence))
    return result

def main(argv=None):
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('run'); p.add_argument('ref'); p.add_argument('sha'); p.add_argument('selector'); p.add_argument('--device',default='iPhone 17')
    p=sub.add_parser('fetch'); p.add_argument('descriptor')
    try:
        args=parser.parse_args(argv); os.umask(0o077)
        if args.command=='run':
            req=validate_inputs(args.ref,args.sha,args.selector,str(uuid.uuid4()),args.device)
            branch_tip=process(['git','-C',SOURCE,'ls-remote','--exit-code','origin','refs/heads/'+req['ref']]).decode().split()[0]
            if not re.fullmatch('[0-9a-f]{40}',branch_tip): raise DiagnosticError()
            process(['git','-C',SOURCE,'merge-base','--is-ancestor',req['sha'],branch_tip])
            api=Transport(token_from_config())
            req['workflow_sha']=api.json('GET',API+'/commits/main')['sha']
            if not re.fullmatch('[0-9a-f]{40}',req['workflow_sha']): raise DiagnosticError()
            ROOT.mkdir(mode=0o700,parents=True,exist_ok=True); work=ROOT/req['request_id']; work.mkdir(mode=0o700)
            descriptor=work/'request.json'; descriptor.write_text(json.dumps(req)+'\n'); os.chmod(descriptor,0o600)
            print('request='+str(descriptor),flush=True)
            api.json('POST',API+'/actions/workflows/ios-diagnostic-tests.yml/dispatches',{'ref':'main','inputs':{k:req[k] for k in ('ref','sha','selector','request_id','device')}})
        else:
            descriptor=Path(args.descriptor); req=json.loads(descriptor.read_text())
            validate_inputs(*(req[k] for k in ('ref','sha','selector','request_id','device')))
            if not re.fullmatch('[0-9a-f]{40}',req['workflow_sha']): raise DiagnosticError()
            work=ROOT/req['request_id']
            if descriptor.resolve() != (work/'request.json').resolve(): raise DiagnosticError()
            api=Transport(token_from_config())
        return execute_fetch(req,api,work,time.monotonic()+600)
    except Exception:
        print('DIAGNOSTIC_ERROR',file=sys.stderr); return 2

if __name__=='__main__': sys.exit(main())
