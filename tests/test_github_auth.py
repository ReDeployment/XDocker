"""GitHub App identity, narrow grants, safe file installation and memory-only refresh."""
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/admin'))
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
import github_auth as auth
from github_watch import GitHubWatch, PROJECTS, safe_text, WatchError
ROOT=Path(__file__).resolve().parents[1]


class AppAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cls.pem=cls.key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption())

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()/'github';self.directory=self.root/'credentials'
        self.directory.mkdir(parents=True);(self.directory/'app-keys').mkdir()
        self.key_id=hashlib.sha256(self.pem).hexdigest()
        self.key_file=self.directory/'app-keys'/(self.key_id+'.pem');self.key_file.write_bytes(self.pem);self.key_file.chmod(0o600)
        self.config={'app_id':12345,'installations':{'artisancloud':101,'matrix-x':102},'key_id':self.key_id}
        self.save_config();auth._cache.clear();self.now=10000
        self.clock=patch('github_auth.time.time',side_effect=lambda:self.now);self.clock.start();self.addCleanup(self.clock.stop)
        self.calls=[]
        self.api=patch('github_auth.api',side_effect=self.response);self.api.start();self.addCleanup(self.api.stop)
        self.watch=GitHubWatch(self.root.parent);self.addCleanup(self.watch.close)

    def save_config(self):
        self.config_file=self.directory/'app.json';self.config_file.write_text(json.dumps(self.config));self.config_file.chmod(0o600)

    def response(self,opener,path,authorization,body=None):
        self.calls.append((path,body,authorization))
        owner='artisancloud' if '/101' in path else 'matrix-x'
        if body is None:return {'id':self.config['installations'][owner],'app_id':12345,'account':{'login':owner},'permissions':auth.PERMISSIONS}
        return {'token':'ghs_APPID_'+'x'*50+'.'+'y'*1500+'.signature','permissions':auth.PERMISSIONS,
            'expires_at':datetime.fromtimestamp(self.now+3600,timezone.utc).isoformat()}

    def test_mapping_matches_watcher_and_grants_are_readonly_fixed_and_owner_scoped(self):
        self.assertEqual({r.split('/')[1] for r in PROJECTS.values()},{r for rows in auth.REPOSITORIES.values() for r in rows})
        for repo in ('ArtisanCloud/PowerX','Matrix-X/PowerXPluginCRM'):self.assertTrue(auth.token(self.root,repo))
        grants=[body for _,body,_ in self.calls if body is not None]
        self.assertEqual(len(grants),2);self.assertEqual(grants[0]['repositories'],['PowerX'])
        self.assertEqual(grants[1]['repositories'],['PowerXPluginCRM'])
        self.assertTrue(all(set(g['permissions'].values())=={'read'} for g in grants))
        with self.assertRaisesRegex(auth.AuthError,'allowlisted'):auth.token(self.root,'Other/PowerX')

    def test_jwt_has_verified_rs256_signature_and_clock_skew_bounds(self):
        auth.token(self.root,'ArtisanCloud/PowerX');value=self.calls[0][2]
        header,body,signature=value.split('.')
        def decode(s):return base64.urlsafe_b64decode(s+'='*(-len(s)%4))
        self.assertEqual(json.loads(decode(header))['alg'],'RS256')
        payload=json.loads(decode(body));self.assertEqual(payload,{'iss':'12345','iat':9940,'exp':10540})
        self.key.public_key().verify(decode(signature),(header+'.'+body).encode(),padding.PKCS1v15(),hashes.SHA256())

    def test_repository_cache_refreshes_before_expiry_and_invalidation_mints_again(self):
        first=auth.token(self.root,'ArtisanCloud/PowerX');auth.token(self.root,'ArtisanCloud/PowerX')
        self.assertEqual(len(self.calls),2)
        auth.token(self.root,'ArtisanCloud/PowerXPlugin');self.assertEqual(len(self.calls),4)
        self.now+=3299;self.assertEqual(auth.token(self.root,'ArtisanCloud/PowerX'),first);self.assertEqual(len(self.calls),4)
        self.now+=2;auth.token(self.root,'ArtisanCloud/PowerX');self.assertEqual(len(self.calls),6)
        auth.invalidate(self.root,'ArtisanCloud/PowerX');auth.token(self.root,'ArtisanCloud/PowerX');self.assertEqual(len(self.calls),8)
        self.assertNotIn(first,self.watch.store.file.read_bytes().decode(errors='ignore'))
        self.assertNotIn(first,json.dumps(self.watch.listing()))
        self.assertEqual(len(list(self.directory.rglob('*'))),3)
        self.assertNotIn(first,safe_text('credential '+first,first))
        self.assertNotIn('.signature',safe_text(first))

    def test_one_missing_repository_does_not_block_other_repository_in_same_installation(self):
        original=self.response
        def response(opener,path,authorization,body=None):
            if body and body['repositories']==['PowerXPluginMediaXStudio']:raise auth.AuthError('github-app-http-422')
            return original(opener,path,authorization,body)
        with patch('github_auth.api',side_effect=response):
            with self.assertRaisesRegex(auth.AuthError,'422'):auth.token(self.root,'Matrix-X/PowerXPluginMediaXStudio')
            self.assertTrue(auth.token(self.root,'Matrix-X/PowerXPluginCRM'))

    def test_concurrent_collectors_share_one_owner_token(self):
        values=[]
        threads=[threading.Thread(target=lambda:values.append(auth.token(self.root,'ArtisanCloud/PowerX'))) for _ in range(8)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=2)
        self.assertEqual(len(values),8);self.assertEqual(len(self.calls),2)

    def test_readonly_page_and_describe_never_request_installation_token(self):
        view=self.watch.listing();access=view['projects'][0]['access']
        self.assertEqual(access['mode'],'github-app');self.assertEqual(access['app_id'],12345)
        self.assertTrue(access['configured']);self.assertEqual(self.calls,[])
        self.assertNotIn('BEGIN RSA',json.dumps(view))

    def test_wrong_installation_owner_app_suspension_or_permissions_reject_before_grant(self):
        for change in ({'id':999},{'app_id':999},{'account':{'login':'other'}},{'suspended_at':'now'},{'permissions':{'contents':'read'}}):
            with self.subTest(change=change):
                auth._cache.clear();self.calls.clear()
                original=self.response
                def response(*args,**kw):return dict(original(*args,**kw),**change)
                with patch('github_auth.api',side_effect=response),self.assertRaises(auth.AuthError):auth.token(self.root,'ArtisanCloud/PowerX')
                self.assertEqual(len(self.calls),1)

    def test_bad_grant_write_permissions_bad_token_or_expiry_never_enter_cache(self):
        for change in ({'permissions':dict(auth.PERMISSIONS,contents='write')},{'token':'bad token'},
            {'expires_at':'invalid'},{'expires_at':datetime.fromtimestamp(self.now+30,timezone.utc).isoformat()}):
            with self.subTest(change=change):
                auth._cache.clear();original=self.response
                def response(opener,path,authorization,body=None):
                    result=original(opener,path,authorization,body)
                    return dict(result,**change) if body is not None else result
                with patch('github_auth.api',side_effect=response),self.assertRaises(auth.AuthError):auth.token(self.root,'ArtisanCloud/PowerX')
                self.assertFalse(auth._cache)

    def test_missing_installation_bad_config_key_permissions_or_symlink_do_not_fallback(self):
        legacy=self.directory/'artisancloud.token';legacy.write_text('ghp_'+'x'*40);legacy.chmod(0o600)
        self.config['installations'].pop('artisancloud');self.save_config()
        with self.assertRaisesRegex(auth.AuthError,'installation-required'):auth.token(self.root,'ArtisanCloud/PowerX')
        self.config['installations']['artisancloud']=101;self.save_config();self.key_file.chmod(0o644)
        self.assertFalse(self.watch.listing()['projects'][0]['access']['configured'])
        with self.assertRaisesRegex(auth.AuthError,'unsafe'):auth.token(self.root,'ArtisanCloud/PowerX')
        self.key_file.chmod(0o600);self.config['key_id']='../key';self.save_config()
        with self.assertRaisesRegex(auth.AuthError,'config-invalid'):auth.token(self.root,'ArtisanCloud/PowerX')
        self.config_file.unlink();self.assertEqual(auth.token(self.root,'ArtisanCloud/PowerX'),'ghp_'+'x'*40)
        self.config_file.symlink_to(legacy)
        with self.assertRaisesRegex(auth.AuthError,'unsafe'):auth.token(self.root,'ArtisanCloud/PowerX')

    def test_duplicate_json_invalid_or_non_rsa_key_and_missing_key_are_clear_errors(self):
        self.config_file.write_text('{"app_id":1,"app_id":2}')
        with self.assertRaisesRegex(auth.AuthError,'config-invalid'):auth.describe(self.root,'ArtisanCloud/PowerX')
        self.save_config();self.key_file.unlink()
        with self.assertRaisesRegex(auth.AuthError,'key-required'):auth.describe(self.root,'ArtisanCloud/PowerX')
        self.key_file.write_bytes(b'bad key');self.key_file.chmod(0o600)
        with self.assertRaisesRegex(auth.AuthError,'key-invalid'):auth.describe(self.root,'ArtisanCloud/PowerX')

    def test_init_refuses_accidental_overwrite_and_installs_only_private_files_without_network(self):
        pem=self.root.parent/'download.pem';pem.write_bytes(self.pem);pem.chmod(0o644)
        state=self.root.parent/'new-state'
        cmd=[sys.executable,str(ROOT/'scripts/github-app.py'),'init','--state',str(state),'--app-id','12345',
            '--installation','artisancloud=101','--installation','matrix-x=102','--pem',str(pem)]
        first=subprocess.run(cmd,capture_output=True,text=True);self.assertEqual(first.returncode,0,first.stdout)
        self.assertNotIn(self.pem.decode(),first.stdout+first.stderr)
        config=state/'github/credentials/app.json';self.assertEqual(config.stat().st_mode&0o777,0o600)
        key=next(config.parent.glob('app-keys/*.pem'));self.assertEqual(key.stat().st_mode&0o777,0o777&0o600)
        self.assertEqual(auth.describe(state/'github','ArtisanCloud/PowerX')['mode'],'github-app')
        self.assertEqual(subprocess.run(cmd,capture_output=True).returncode,2)
        self.assertEqual(subprocess.run(cmd+['--replace'],capture_output=True).returncode,0)


class TransportTests(unittest.TestCase):
    def test_fixed_api_methods_no_redirect_and_never_echo_error_bodies(self):
        class Opener:
            def open(self,request,timeout):
                self.request=request
                raise HTTPError(request.full_url,403,'forbidden',{'Retry-After':'180'},io.BytesIO(b'private JWT never print'))
        opener=Opener()
        with self.assertRaises(auth.AuthError) as raised:auth.api(opener,'/app/installations/123/access_tokens','secret',{'permissions':auth.PERMISSIONS})
        self.assertEqual(opener.request.get_method(),'POST');self.assertGreater(raised.exception.retry_at,0)
        self.assertNotIn('secret',str(raised.exception));self.assertNotIn('JWT',str(raised.exception))
        with self.assertRaisesRegex(auth.AuthError,'endpoint-invalid'):auth.api(opener,'https://other.example/','secret')
        self.assertIsNone(auth.NoRedirect().redirect_request(None,None,None,None,None,None))

    def test_collector_uses_stdin_for_credential_and_invalidates_on_401(self):
        with tempfile.TemporaryDirectory() as directory:
            watch=GitHubWatch(directory)
            settings=watch.store.settings('PowerX');secret='ghs_APPID_'+'x'*1500+'.signature'
            result=type('Result',(),{'returncode':2,'stdout':b'{"error":"github-http-401"}'})()
            with patch('github_watch.credential_status',return_value={'mode':'github-app'}),patch('github_watch.owner_token',return_value=secret),patch('github_watch.subprocess.run',return_value=result) as run,patch('github_watch.invalidate_token') as invalidate:
                with self.assertRaisesRegex(WatchError,'github-http-401'):watch.collect(settings)
                self.assertNotIn(secret,str(run.call_args.args));self.assertNotIn('env',run.call_args.kwargs)
                self.assertEqual(json.loads(run.call_args.kwargs['input'])['token'],secret)
                invalidate.assert_called_once_with(watch.store.root,settings['repository'])
            watch.close()


if __name__=='__main__':unittest.main()
