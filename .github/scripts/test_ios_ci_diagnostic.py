import hashlib
import io
import json
import tarfile
import unittest
import zipfile
import warnings
from pathlib import Path
import tempfile
import subprocess
from unittest.mock import patch
import contextlib

import ios_ci_diagnostic as d

RID = '12345678-1234-4234-8234-123456789abc'
SHA = 'a' * 40
CI = 'b' * 40
REQ = dict(ref='feature/test', sha=SHA, selector='RadioShakeTests/Example', request_id=RID, device='iPhone 17', workflow_sha=CI)


def run(**overrides):
    value = dict(id=123, run_attempt=1, display_title='ios-diagnostic '+RID,
                 event='workflow_dispatch', path=d.WORKFLOW, head_sha=CI,
                 status='completed', conclusion='success', html_url='https://github.com/hitenpatel/radioshake-ci/actions/runs/123')
    value.update(overrides)
    return value


def zipdata(names):
    b = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(b, 'w') as z:
            for name in names: z.writestr(name, b'ciphertext')
    return b.getvalue()


class DiagnosticTest(unittest.TestCase):
    def test_symlink_entrypoint_can_import_sibling_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            link=Path(tmp)/'ios-ci-diagnostic'; link.symlink_to(Path(d.__file__).resolve())
            result=subprocess.run([str(link),'--help'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_request_association_rejects_duplicates_wrong_fields_and_main_race(self):
        self.assertEqual(d.select_run([run(), run(id=124, display_title='other')], REQ)['id'], 123)
        for changes in ({'event':'push'}, {'path':'wrong'}, {'head_sha':'c'*40}, {'display_title':'ios-diagnostic different'}):
            with self.subTest(changes=changes), self.assertRaises(d.DiagnosticError):
                d.select_run([run(**changes)], REQ)
        with self.assertRaises(d.DiagnosticError): d.select_run([run(), run(id=124)], REQ)

    def test_artifact_requires_unique_unexpired_matching_identity(self):
        good = dict(id=5, name='ios-diagnostic-encrypted-'+RID, expired=False)
        self.assertEqual(d.select_artifact([good], RID)['id'], 5)
        for items in ([], [dict(good, expired=True)], [dict(good, name='other')], [good,good]):
            with self.assertRaises(d.DiagnosticError): d.select_artifact(items, RID)

    def test_zip_requires_exact_regular_members(self):
        self.assertEqual(set(d.unpack_zip(zipdata(['key.enc','bundle.tgz.enc']))), {'key.enc','bundle.tgz.enc'})
        for names in (['../key.enc','bundle.tgz.enc'], ['key.enc'], ['key.enc','bundle.tgz.enc','extra'], ['key.enc','key.enc','bundle.tgz.enc']):
            with self.assertRaises(d.DiagnosticError): d.unpack_zip(zipdata(names))

    def test_tar_rejects_traversal_absolute_and_links(self):
        for name, kind in [('../escape',tarfile.REGTYPE),('/escape',tarfile.REGTYPE),('link',tarfile.SYMTYPE)]:
            b=io.BytesIO()
            with tarfile.open(fileobj=b,mode='w:gz') as t:
                item=tarfile.TarInfo(name); item.type=kind; item.linkname='../escape'; t.addfile(item)
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(d.DiagnosticError): d.extract_tar(b.getvalue(),Path(tmp))

    def test_manifest_rejects_source_and_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); (p/'test.log').write_bytes(b'generic')
            manifest=dict(schema_version=1, inputs={k:REQ[k] for k in ('ref','sha','selector','request_id','device')}, runtime=dict(source_sha=SHA, workflow_repository=d.REPO, workflow_path=d.WORKFLOW, workflow_commit=CI, run_id='123', run_attempt='1', job='diagnostic', configuration='Debug', optimization='native Debug -Onone', destination_udid='ABC', runtime='com.apple.CoreSimulator.SimRuntime.iOS-26-5', arch='arm64'), stages={k:0 for k in ('clone','toolchain','gradle','generate','simulator','test')}, execution=dict(outcome='passed', xcode_exit=0, xcresult_present=True, passed=1, failed=0, skipped=0), file_hashes={'test.log':hashlib.sha256(b'generic').hexdigest()})
            (p/'tests.xcresult').mkdir()
            (p/'manifest.json').write_text(json.dumps(manifest))
            self.assertEqual(d.verify_manifest(p,REQ,run()),0)
            manifest['runtime']['source_sha']='c'*40; (p/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(d.DiagnosticError): d.verify_manifest(p,REQ,run())
            manifest['runtime']['source_sha']=SHA; manifest['file_hashes']['test.log']='0'*64; (p/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(d.DiagnosticError): d.verify_manifest(p,REQ,run())

class FakeAPI:
    def __init__(self, selected=None, jobs=None, artifacts=None, archive=None):
        self.selected=selected or run(conclusion='failure')
        self.jobs=jobs if jobs is not None else [dict(name='diagnostic',status='completed',conclusion='failure')]
        self.artifacts=artifacts if artifacts is not None else [dict(id=5,name='ios-diagnostic-encrypted-'+RID,expired=False)]
        self.data=archive; self.deleted=[]
    def json(self,method,url,data=None):
        if method=='DELETE': self.deleted.append(url); return {}
        if '/jobs?' in url: return {'jobs':self.jobs}
        if '/artifacts?' in url: return {'artifacts':self.artifacts}
        return {'workflow_runs':[self.selected]}
    def archive(self, artifact_id): return self.data

class FetchTest(unittest.TestCase):
    def fixture(self, outcome='failed'):
        manifest=dict(schema_version=1,inputs={k:REQ[k] for k in ('ref','sha','selector','request_id','device')}, runtime=dict(source_sha=SHA,workflow_repository=d.REPO,workflow_path=d.WORKFLOW,workflow_commit=CI,run_id='123',run_attempt='1',job='diagnostic',configuration='Debug',optimization='native Debug -Onone',destination_udid='ABC',runtime='com.apple.CoreSimulator.SimRuntime.iOS-26-5',arch='arm64'),stages=dict(clone=0,toolchain=0,gradle=0,generate=0,simulator=0,test=65),execution=dict(outcome=outcome,xcode_exit=65,xcresult_present=True,passed=0,failed=1,skipped=0),file_hashes={'test.log':hashlib.sha256(b'generic').hexdigest()})
        b=io.BytesIO()
        with tarfile.open(fileobj=b,mode='w:gz') as t:
            for name,data in [('manifest.json',json.dumps(manifest).encode()),('test.log',b'generic')]:
                info=tarfile.TarInfo(name); info.size=len(data); t.addfile(info,io.BytesIO(data))
            info=tarfile.TarInfo('tests.xcresult'); info.type=tarfile.DIRTYPE; t.addfile(info)
        return b.getvalue()

    def test_failed_terminal_run_retrieves_evidence_and_deletes_only_matching_artifact(self):
        api=FakeAPI(archive=zipdata(['key.enc','bundle.tgz.enc']))
        with tempfile.TemporaryDirectory() as tmp, patch.object(d,'decrypt',return_value=self.fixture()), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(d.execute_fetch(REQ,api,Path(tmp),1),1)
            self.assertEqual(len(list(Path(tmp).glob('attempt-*/evidence/manifest.json'))),1)
            self.assertIn('DIAGNOSTIC_FAIL',output.getvalue())
            self.assertEqual(api.deleted,[d.API+'/actions/artifacts/5'])

    def test_wrong_job_and_decrypt_failure_preserve_remote_artifact(self):
        for api in (FakeAPI(jobs=[dict(name='wrong',status='completed',conclusion='failure')]), FakeAPI(archive=zipdata(['key.enc','bundle.tgz.enc']))):
            with tempfile.TemporaryDirectory() as tmp, patch.object(d,'decrypt',side_effect=ValueError('private canary')), self.assertRaises(d.DiagnosticError):
                d.execute_fetch(REQ,api,Path(tmp),1)
            self.assertEqual(api.deleted,[])

    def test_fetch_can_resume_after_partial_evidence(self):
        api=FakeAPI(archive=zipdata(['key.enc','bundle.tgz.enc']))
        with tempfile.TemporaryDirectory() as tmp, patch.object(d,'decrypt',return_value=self.fixture()), contextlib.redirect_stdout(io.StringIO()):
            (Path(tmp)/'evidence').mkdir()
            self.assertEqual(d.execute_fetch(REQ,api,Path(tmp),1),1)

    def test_decrypt_failure_does_not_leave_plain_key(self):
        def runner(args):
            if 'pkeyutl' in args:
                Path(args[args.index('-out')+1]).write_text('private-canary')
                return b''
            raise RuntimeError('private-canary')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(d.DiagnosticError): d.decrypt({'key.enc':b'x','bundle.tgz.enc':b'y'},Path(tmp),runner)
            self.assertFalse((Path(tmp)/'key').exists())

    def test_failed_build_with_verified_source_retains_evidence_and_returns_failure(self):
        b=io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(self.fixture()),mode='r:gz') as original:
            manifest=json.load(original.extractfile('manifest.json'))
        manifest['stages'].update(gradle=1,generate=None,simulator=None,test=None)
        manifest['execution']=dict(outcome='incomplete',xcode_exit=None,xcresult_present=False,passed=0,failed=0,skipped=0)
        manifest['runtime'].update(destination_udid=None,runtime=None)
        with tarfile.open(fileobj=b,mode='w:gz') as t:
            for name,data in [('manifest.json',json.dumps(manifest).encode()),('test.log',b'generic')]:
                info=tarfile.TarInfo(name); info.size=len(data); t.addfile(info,io.BytesIO(data))
        api=FakeAPI(archive=zipdata(['key.enc','bundle.tgz.enc']))
        with tempfile.TemporaryDirectory() as tmp, patch.object(d,'decrypt',return_value=b.getvalue()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(d.execute_fetch(REQ,api,Path(tmp),1),1)

    def native_failure_bundle(self, *, result_present=False, stage_exit=65, xcode_exit=65, log=True, outcome='incomplete', bad_hash=False, omit_exit=False):
        with tarfile.open(fileobj=io.BytesIO(self.fixture()),mode='r:gz') as original:
            manifest=json.load(original.extractfile('manifest.json'))
        manifest['stages']['test']=stage_exit
        manifest['execution']=dict(outcome=outcome,xcode_exit=xcode_exit,xcresult_present=result_present,passed=0,failed=0,skipped=0)
        manifest['file_hashes']={'test.log':('0'*64 if bad_hash else hashlib.sha256(b'generic').hexdigest())} if log else {}
        if omit_exit: del manifest['execution']['xcode_exit']
        b=io.BytesIO()
        with tarfile.open(fileobj=b,mode='w:gz') as t:
            members=[('manifest.json',json.dumps(manifest).encode())]
            if log: members.append(('test.log',b'generic'))
            for name,data in members:
                info=tarfile.TarInfo(name); info.size=len(data); t.addfile(info,io.BytesIO(data))
            if result_present:
                info=tarfile.TarInfo('tests.xcresult'); info.type=tarfile.DIRTYPE; t.addfile(info)
        return b.getvalue()

    def test_native_compilation_failure_returns_one_and_reports_retained_evidence(self):
        for present in (False, True):
            with self.subTest(result_present=present):
                api=FakeAPI(archive=zipdata(['key.enc','bundle.tgz.enc']))
                with tempfile.TemporaryDirectory() as tmp, patch.object(d,'decrypt',return_value=self.native_failure_bundle(result_present=present)), contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(d.execute_fetch(REQ,api,Path(tmp),1),1)
                    evidence=list(Path(tmp).glob('attempt-*/evidence'))
                    self.assertEqual(len(evidence),1)
                    self.assertIn('evidence='+str(evidence[0]),output.getvalue())
                    self.assertTrue((evidence[0]/'test.log').is_file())
                    self.assertEqual(api.deleted,[d.API+'/actions/artifacts/5'])

    def test_native_incomplete_with_missing_or_inconsistent_evidence_is_unverifiable(self):
        cases=[dict(stage_exit=0,xcode_exit=0),dict(log=False),dict(stage_exit=65,xcode_exit=1),dict(stage_exit=65,xcode_exit='65'),dict(stage_exit=65,xcode_exit=None),dict(stage_exit=True,xcode_exit=True),dict(bad_hash=True),dict(omit_exit=True)]
        for case in cases:
            with self.subTest(case=case):
                api=FakeAPI(archive=zipdata(['key.enc','bundle.tgz.enc']))
                with tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp); work=root/RID; work.mkdir()
                    descriptor=work/'request.json'; descriptor.write_text(json.dumps(REQ))
                    with patch.object(d,'ROOT',root), patch.object(d,'token_from_config',return_value='fake'), patch.object(d,'Transport',return_value=api), patch.object(d,'decrypt',return_value=self.native_failure_bundle(**case)), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(d.main(['fetch',str(descriptor)]),2)
                    self.assertEqual(api.deleted,[])
                    self.assertNotIn('DIAGNOSTIC_PASS',output.getvalue())

    def test_duplicate_request_on_later_page_is_rejected(self):
        class Pages(FakeAPI):
            def json(self,method,url,data=None):
                if '/runs?' in url:
                    if 'page=2' in url: return {'total_count':101,'workflow_runs':[run(id=124)]}
                    return {'total_count':101,'workflow_runs':[run()]+[run(id=i+1000,display_title='other') for i in range(99)]}
                return super().json(method,url,data)
        runs=d.list_runs(Pages())
        self.assertEqual(len(runs),101)
        with self.assertRaises(d.DiagnosticError): d.select_run(runs,REQ)

    def test_cancelled_or_incomplete_never_deletes_artifact(self):
        for value in (run(conclusion='cancelled'),run(status='in_progress',conclusion=None)):
            api=FakeAPI(selected=value)
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(d.DiagnosticError):
                d.execute_fetch(REQ,api,Path(tmp),0,clock=lambda:1)
            self.assertEqual(api.deleted,[])

    def test_run_saves_private_descriptor_before_single_dispatch(self):
        class API:
            def __init__(self): self.requests=[]
            def json(self,method,url,data=None):
                if method=='GET': return {'sha':CI}
                self.requests.append((method,url,data))
                self.assert_descriptor_exists()
            def assert_descriptor_exists(self):
                paths=list(root.glob('*/request.json')); assert len(paths)==1
                assert paths[0].stat().st_mode & 0o777 == 0o600
        api=API()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def fake_process(args,**kwargs): return (SHA+'\trefs/heads/feature/test\n').encode()
            with patch.object(d,'ROOT',root), patch.object(d,'process',fake_process), patch.object(d,'token_from_config',return_value='secret-canary'), patch.object(d,'Transport',return_value=api), patch.object(d,'execute_fetch',return_value=0), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(d.main(['run','feature/test',SHA,'RadioShakeTests/Example']),0)
            self.assertEqual(len(api.requests),1)
            self.assertEqual(api.requests[0][2]['ref'],'main')
            self.assertEqual(api.requests[0][2]['inputs']['sha'],SHA)
            self.assertNotIn('secret-canary',output.getvalue())

    def test_raw_transport_errors_are_not_printed(self):
        with patch.object(d,'process',side_effect=RuntimeError('private-canary')), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(d.main(['run','feature/test',SHA,'RadioShakeTests/Example']),2)
            self.assertNotIn('private-canary',out.getvalue()+err.getvalue())

    def test_redirect_download_does_not_forward_authorization(self):
        calls=[]
        class Headers(dict): pass
        class Response:
            status=200; headers={}
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return b'archive'
        class Opener:
            def open(self,request,timeout):
                calls.append(request)
                if len(calls)==1:
                    raise d.urllib.error.HTTPError(request.full_url,302,'canary',Headers(Location='https://storage.example/archive'),None)
                return Response()
        transport=d.Transport('secret-canary'); transport.opener=Opener()
        self.assertEqual(transport.archive(5),b'archive')
        self.assertEqual(calls[0].get_header('Authorization'),'Bearer secret-canary')
        self.assertIsNone(calls[1].get_header('Authorization'))

if __name__ == '__main__': unittest.main()
