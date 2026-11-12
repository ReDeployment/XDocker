"""Real committed Git transfer; source/index isolation, scan gates and durable migration."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'services/admin'))
from host_metrics import private_directory,atomic_json
from sync_runtime import GitTransfer,git,text_git,scan_source,SyncError,branch_name
from sync_agent import CopyAgent,agent_sha
from sync_store import SyncStore,SCHEMA
from native_services import SystemdServices
from test_native_services import FakeSystemd
from code_sync import CodeSync,CodeSyncWorker,next_daily,default_policy
import test_admin


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        self.sources=self.root/'workspace';self.sources.mkdir();self.source=self.sources/'project';self.source.mkdir()
        git(self.source,'init','-b','main');git(self.source,'config','user.name','Fixture Author');git(self.source,'config','user.email','fixture@example.invalid')
        (self.source/'app.py').write_text('print("first")\n');(self.source/'.gitignore').write_text('ignored.txt\n.env\n')
        self.commit('feat: initial application');git(self.source,'remote','add','origin','https://github.com/example/project.git')
        self.remote=self.root/'remote.git';git(self.root,'init','--bare',str(self.remote));self.resolver=lambda repo:str(self.remote)
        self.hub=CodeSync(self.root/'state',operations=True,transfer=GitTransfer(private_directory(self.root/'cache'),self.resolver))
        policy=default_policy();policy.update(execution=True,source_roots=[str(self.sources)],identities={'fixture':str(self.root/'key')});self.hub.import_policy(policy)
        self.hub.save_credential({'id':'fixture','name':'Fixture','token':'fixture_token_123456789'})
        self.profile={'id':'example','revision':1,'project':None,'name':'Example','directory':str(self.source),'repository':'example/project','branch':'main','credential':'fixture','enabled':True,'confirmation':'Example'}
        self.hub.save('sources',self.profile)
        self.copies=self.root/'copies';self.copies.mkdir();self.destination=self.copies/'example'
        self.agent_root=private_directory(self.root/'agent');self.agent_file=self.agent_root/'policy.json'
        self.agent_policy={'schema':2,'roots':[str(self.copies)],'credentials':{},'targets':{'example':{'directory':str(self.destination),'repository':'example/project','branch':'main','credential':None,'service':None}}}
        atomic_json(self.agent_file,self.agent_policy);self.agent=CopyAgent(self.agent_file,self.resolver)
        self.hub.transport=lambda device,body:self.agent.handle({**body,'expected_agent_sha':agent_sha()})
        self.hub.save('devices',{'id':'copy','revision':1,'name':'Copy','host':'100.100.0.4','port':22,'username':'fixture','identity':'fixture','host_key':'ssh-ed25519 '+base64.b64encode(b'x'*40).decode(),'enabled':True})
        self.target={'id':'copy-example','revision':1,'source':'example','device':'copy','remote_target':'example','directory':str(self.destination),'restart':False,'enabled':True,'confirmation':'Example'}
        self.hub.save('targets',self.target)

    def commit(self,message):
        git(self.source,'add','.');git(self.source,'commit','-m',message);return text_git(self.source,'rev-parse','HEAD')

    def job(self,kind='upload'):
        job=self.hub.enqueue({'source':'example','kind':kind,'confirmation':'Example','idempotency_key':os.urandom(16).hex()});self.hub.execute(job);return self.hub.store.get('jobs',job['id'])

    def test_original_commit_sha_message_author_and_dirty_worktree_are_preserved(self):
        (self.source/'new.js').write_text('export const value = 1;\n');head=self.commit('fix: preserve the real developer description')
        (self.source/'app.py').write_text('dirty working copy');(self.source/'scratch.tmp').write_text('private scratch')
        (self.source/'.env').write_text('PASSWORD=not-for-upload');(self.source/'ignored.txt').write_text('ignored')
        # Both staged and unstaged source changes remain untouched.
        git(self.source,'add','app.py');index=(self.source/'.git/index').read_bytes()
        preview=self.hub.preview('example');self.assertGreater(preview['pending']['untracked']['count'],0);self.assertFalse(preview['blocked'])
        result=self.job();self.assertEqual(result['state'],'success',result);self.assertEqual(result['sha'],head)
        self.assertEqual(text_git(self.destination,'rev-parse','HEAD'),head);self.assertEqual((self.destination/'app.py').read_text(),'print("first")\n')
        self.assertEqual(text_git(self.remote,'show','-s','--format=%B',head),'fix: preserve the real developer description')
        self.assertEqual(text_git(self.remote,'show','-s','--format=%an',head),'Fixture Author')
        self.assertEqual(text_git(self.source,'rev-parse','HEAD'),head);self.assertEqual((self.source/'.git/index').read_bytes(),index)
        self.assertEqual((self.source/'app.py').read_text(),'dirty working copy')
        for name in ('.env','scratch.tmp','ignored.txt'):self.assertFalse((self.destination/name).exists())
        self.assertEqual(text_git(self.remote,'for-each-ref','--format=%(refname)'),'refs/heads/main')
        again=self.job();self.assertEqual(again['sha'],head);self.assertFalse(again['result'][0]['updated'])
        self.assertNotIn('fixture_token_123456789',json.dumps(self.hub.listing()));self.assertEqual(list(self.hub.transient.iterdir()),[])

    def test_committed_temporary_file_is_blocked_without_value_disclosure(self):
        (self.source/'debug.tmp').write_text('temporary private business data');self.commit('debug preparation')
        result=self.job();self.assertEqual(result['state'],'failed');self.assertEqual(result['error'],'XD_SYNC_SENSITIVE_CONTENT')
        self.assertIsNone(result['sha']);self.assertIsNone(self.hub.transfer.remote_head(self.root,self.profile,{}));self.assertFalse(self.destination.exists())
        self.assertTrue(result['snapshot']['blocked']);self.assertNotIn('temporary private business data',json.dumps(result))

    def test_secret_added_then_deleted_in_intermediate_commit_still_blocks(self):
        value='ghp_'+'A'*36;(self.source/'configuration.txt').write_text(value);self.commit('configuration')
        (self.source/'configuration.txt').unlink();self.commit('remove configuration')
        result=self.job();self.assertEqual(result['error'],'XD_SYNC_SENSITIVE_CONTENT')
        self.assertTrue(any(f['rule']=='github-token' for f in result['snapshot']['findings']));self.assertNotIn(value,json.dumps(result))
        self.assertIsNone(self.hub.transfer.remote_head(self.root,self.profile,{}))

    def test_deleted_secret_and_merge_history_scanned_between_remote_and_local(self):
        first=self.job();git(self.source,'switch','-c','feature')
        (self.source/'settings.conf').write_text('api_key="'+'sensitive'+'1234567890'+'"\n');self.commit('settings')
        (self.source/'settings.conf').unlink();self.commit('cleanup');git(self.source,'switch','main');git(self.source,'merge','--no-ff','feature','-m','merge feature')
        result=self.job();self.assertEqual(result['error'],'XD_SYNC_SENSITIVE_CONTENT');self.assertEqual(self.hub.transfer.remote_head(self.root,self.profile,{}),first['sha'])

    def test_only_outgoing_changes_are_scanned_and_divergence_does_not_force_push(self):
        first=self.job();(self.source/'app.py').write_text('print("second")');head=self.commit('feat: second')
        result=self.job();self.assertEqual(result['state'],'success');self.assertEqual(result['snapshot']['commit_count'],1);self.assertEqual(result['sha'],head)
        git(self.source,'reset','--hard',first['sha']);(self.source/'app.py').write_text('divergence');self.commit('different history')
        result=self.job();self.assertEqual(result['error'],'XD_SYNC_REMOTE_DIVERGED');self.assertEqual(self.hub.transfer.remote_head(self.root,self.profile,{}),head)

    def test_dirty_copy_and_branch_divergence_are_not_overwritten(self):
        first=self.job();(self.destination/'app.py').write_text('local work');(self.source/'app.py').write_text('incoming');self.commit('incoming change')
        result=self.job();self.assertEqual(result['state'],'partial');self.assertEqual((self.destination/'app.py').read_text(),'local work')
        with self.assertRaises(SyncError):self.hub.enqueue({'source':'example','kind':'upload','confirmation':'Example','idempotency_key':'a'*32})
        self.hub.close_job(result['id'],{'confirmation':'Example'})
        git(self.destination,'add','app.py');git(self.destination,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','divergence')
        head=text_git(self.destination,'rev-parse','HEAD');result=self.job();self.assertEqual(result['state'],'partial');self.assertEqual(text_git(self.destination,'rev-parse','HEAD'),head)

    def test_pending_remote_receipt_reconciliation_and_agent_idempotence(self):
        original=self.hub.transfer.publish
        def uncertain(*args,**kwargs):original(*args,**kwargs);raise SyncError('XD_SYNC_UPLOAD_UNCONFIRMED')
        with patch.object(self.hub.transfer,'publish',uncertain):job=self.job()
        self.assertEqual(job['state'],'upload-unconfirmed')
        self.hub.store.request_reconcile(job['id']);done=self.hub.reconcile(job['id']);self.assertEqual(done['state'],'success')
        observed=self.agent.inspect('example');request={'operation':'sync','expected_agent_sha':agent_sha(),'target':'example','sha':job['sha'],'observation':observed['observation'],'grant_sha':observed['grant_sha'],'request_id':'c'*64,'restart':False}
        first=self.agent.handle(request);self.assertEqual(first,self.agent.handle(request))
        with self.assertRaises(SyncError):self.agent.handle({**request,'restart':True})

    def test_branch_identity_and_detached_source_require_review(self):
        for branch in ('-bad','HEAD','HEAD@'+'a'*40,'x/../main','bad.lock'):
            with self.assertRaises(SyncError):branch_name(branch)
        branch_name('develop');branch_name('feature/normal-branch')
        git(self.source,'checkout','--detach');head=text_git(self.source,'rev-parse','HEAD')
        item=self.hub.inventory(True)[0];self.assertFalse(item['pushable'])
        with self.assertRaises(SyncError):self.hub.save('sources',{**self.profile,'revision':2,'branch':item['branch']})
        self.hub.save('sources',{**self.profile,'revision':2,'branch':item['branch'],'enabled':False})
        preview=scan_source(self.hub.source('example'),[str(self.sources)])
        self.assertFalse(preview['pushable']);self.assertEqual(text_git(self.source,'rev-parse','HEAD'),head)

    def test_scan_limits_and_unscannable_committed_binary_fail_closed(self):
        with patch('sync_runtime.MAX_COMMITS',0):
            result=self.job();self.assertEqual(result['error'],'XD_SYNC_SCAN_COMMIT_LIMIT')
        (self.source/'opaque.bin').write_bytes(b'\0\x80\x81private binary');self.commit('binary')
        result=self.job();self.assertEqual(result['error'],'XD_SYNC_SENSITIVE_CONTENT');self.assertTrue(any(f['rule']=='unscannable-binary' for f in result['snapshot']['findings']))
        self.assertIsNone(self.hub.transfer.remote_head(self.root,self.profile,{}))

    def test_restart_is_fixed_pinned_and_only_after_actual_update(self):
        unit=self.root/'example.service';unit.write_text('[Service]\nExecStart=/usr/bin/fixture\n');runner=FakeSystemd(unit);adapter=SystemdServices(runner)
        grant={'scope':'user','unit':'example.service','actions':['status','restart'],'definition_sha256':None};grant['definition_sha256']=adapter.status(grant)['definition_sha256']
        self.agent_policy['targets']['example']['service']=grant;atomic_json(self.agent_file,self.agent_policy);self.agent=CopyAgent(self.agent_file,self.resolver,adapter)
        self.hub.save('targets',{**self.target,'revision':2,'restart':True})
        self.assertEqual(self.job()['state'],'success');self.job();self.assertEqual(sum('restart' in c for c in runner.calls),1)
        unit.write_text('[Service]\nExecStart=/usr/bin/changed\n');(self.source/'app.py').write_text('new');self.commit('new change')
        self.assertEqual(self.job()['state'],'partial');self.assertEqual(sum('restart' in c for c in runner.calls),1)

    def test_ignored_file_collision_is_preserved_and_sql_source_is_included(self):
        (self.source/'.gitignore').write_text('collision\n.env\n');(self.source/'migrations').mkdir();(self.source/'migrations/001.sql').write_text('CREATE TABLE fixture(id INTEGER);');self.commit('database migration source')
        first=self.job();self.assertTrue((self.destination/'migrations/001.sql').is_file());(self.destination/'collision').write_text('local generated')
        (self.source/'collision').write_text('incoming');git(self.source,'add','-f','collision');git(self.source,'commit','-m','collision')
        self.assertEqual(self.job()['state'],'partial');self.assertEqual((self.destination/'collision').read_text(),'local generated');self.assertEqual(text_git(self.destination,'rev-parse','HEAD'),first['sha'])

    def test_commit_message_secret_is_hidden_and_no_remote_write_occurs(self):
        value='ghp_'+'B'*36;(self.source/'app.py').write_text('new')
        self.commit('fix: '+value);result=self.job()
        self.assertEqual(result['error'],'XD_SYNC_SENSITIVE_CONTENT');self.assertNotIn(value,json.dumps(result))
        self.assertEqual(result['snapshot']['commits'][-1]['title'],'提交说明含敏感信息，已隐藏')
        self.assertIsNone(self.hub.transfer.remote_head(self.root,self.profile,{}))

    def test_source_head_change_after_intent_stops_before_push(self):
        original=self.hub.transfer.publish
        def publish(profile,roots,env,callback):
            def move(sha,summary):
                callback(sha,summary);(self.source/'app.py').write_text('concurrent developer change');self.commit('concurrent developer commit')
            return original(profile,roots,env,move)
        with patch.object(self.hub.transfer,'publish',publish):result=self.job()
        self.assertEqual(result['error'],'XD_SYNC_SOURCE_CHANGED');self.assertIsNone(self.hub.transfer.remote_head(self.root,self.profile,{}))

    def test_legacy_agent_grant_cannot_be_reinterpreted_as_existing_branch(self):
        self.agent_policy['schema']=1;grant=self.agent_policy['targets']['example'];grant['backup_branch']='xdocker-backup/example';grant.pop('branch')
        atomic_json(self.agent_file,self.agent_policy)
        with self.assertRaises(SyncError):CopyAgent(self.agent_file,self.resolver)

    def test_archive_preserves_agent_and_controller_idempotency(self):
        first=self.job();self.hub.archive()
        repeated=self.hub.enqueue({'source':'example','kind':'upload','confirmation':'Example','idempotency_key':first['idempotency_key']});self.assertEqual(repeated['id'],first['id'])
        archive=self.hub.listing()['archives'][0];self.assertEqual(self.hub.read_archive(archive)['jobs'][0]['id'],first['id'])
        observed=self.agent.inspect('example');request={'operation':'sync','expected_agent_sha':agent_sha(),'target':'example','sha':first['sha'],'observation':observed['observation'],'grant_sha':observed['grant_sha'],'request_id':'c'*64,'restart':False}
        receipt=self.agent.handle(request)
        with sqlite3.connect(self.agent.database) as db:
            db.execute('UPDATE runs SET at=0 WHERE id=?',(request['request_id'],))
            for n in range(270):db.execute('INSERT INTO runs VALUES(?,?,?,?,?)',(f'{n:064x}','fixture',1,'observed',json.dumps(receipt)))
        self.agent.handle({**request,'request_id':'d'*64})
        with patch('sync_agent.pull_copy',side_effect=AssertionError('replayed')):self.assertEqual(self.agent.handle(request),receipt)
        with self.assertRaises(SyncError):self.agent.handle({**request,'restart':True})

    def test_workspace_inventory_deduplicates_temporary_clone(self):
        duplicate=self.sources/'project-production-fix';git(self.sources,'clone',str(self.source),str(duplicate));git(duplicate,'remote','set-url','origin','https://github.com/example/project.git')
        inventory=self.hub.inventory(True);self.assertEqual(len(inventory),1);self.assertEqual(inventory[0]['directory'],str(self.source));self.assertIn('pending',inventory[0])

    def test_schedule_recovery_archive_and_no_replay(self):
        self.hub.save('schedules',{'id':'daily','revision':1,'source':'example','mode':'upload','hour':2,'minute':0,'timezone':'Asia/Shanghai','enabled':True,'confirmation':'Example'})
        with self.hub.store.db(write=True) as db:db.execute('UPDATE schedules SET next_at=?',(time.time()-1,))
        worker=CodeSyncWorker(self.hub);worker.tick();worker.tick();jobs=self.hub.store.all('jobs');self.assertEqual(len(jobs),1);self.assertEqual(jobs[0]['state'],'success')
        self.hub.archive();repeated=self.hub.enqueue({'source':'example','kind':'upload','confirmation':'Example','idempotency_key':jobs[0]['idempotency_key']});self.assertEqual(repeated['id'],jobs[0]['id'])
        pending=self.hub.enqueue({'source':'example','kind':'upload','confirmation':'Example','idempotency_key':'d'*32});self.hub.store.patch_job(pending['id'],state='running',sha='e'*40)
        self.hub.store.recover();self.assertEqual(self.hub.store.get('jobs',pending['id'])['state'],'interrupted');self.assertGreater(next_daily(2,0,'Asia/Shanghai',0),0)


class ContentScanTests(unittest.TestCase):
    def test_minified_json_secrets_and_private_keys_are_not_returned(self):
        from sync_scan import content_findings
        value='sensitive'+'1234567890'
        cases=[('settings.json',json.dumps({'api_key':value},separators=(',',':'))),'-----BEGIN '+'PRIVATE KEY-----\nprivate data']
        for case in cases:
            name,body=case if isinstance(case,tuple) else ('key.txt',case)
            issues=content_findings(name,body.encode());self.assertTrue(issues);self.assertNotIn(value,json.dumps(issues))

    def test_templates_with_explicit_placeholders_and_known_assets(self):
        from sync_scan import content_findings
        self.assertEqual(content_findings('.env.example',b'API_KEY="${API_KEY}"\nPASSWORD="change-me-before-use"\n'),[])
        self.assertEqual(content_findings('logo.png',b'\x89PNG\r\n\x1a\n\x00\x80'),[])
        self.assertTrue(content_findings('.env.production',b'HELLO=world'))


class MigrationTests(unittest.TestCase):
    def test_legacy_snapshot_configuration_is_disabled_and_history_retained(self):
        with tempfile.TemporaryDirectory() as raw:
            state=Path(raw).resolve();root=private_directory(state/'code-sync');path=root/'control.sqlite3';path.touch(mode=0o600)
            with sqlite3.connect(path) as db:
                db.executescript('''CREATE TABLE sources(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,uid TEXT NOT NULL,project TEXT,name TEXT NOT NULL,directory TEXT NOT NULL,repository TEXT NOT NULL,branch TEXT NOT NULL,backup_branch TEXT NOT NULL,includes TEXT NOT NULL,excludes TEXT NOT NULL,credential TEXT,enabled INTEGER NOT NULL);'''+SCHEMA[SCHEMA.index('CREATE TABLE IF NOT EXISTS devices'):])
                db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',('old',3,'stable-uid',None,'Old','/workspace/old','example/project','develop','xdocker-backup/old','["."]','[]',None,1))
                db.execute('INSERT INTO schedules VALUES(?,?,?,?,?,?,?,?,?)',('daily',2,'old','upload',2,0,'Asia/Shanghai',1,0))
                db.execute('INSERT INTO jobs(id,source,kind,at,updated,state,config_sha,idempotency_key,sha) VALUES(?,?,?,?,?,?,?,?,?)',('pending','old','upload',1,1,'running','previous','key','a'*40))
                db.execute('PRAGMA application_id=1481857358');db.execute('PRAGMA user_version=1')
            store=SyncStore(state);source=store.get('sources','old');self.assertEqual(source['uid'],'stable-uid');self.assertEqual(source['branch'],'develop');self.assertFalse(source['enabled']);self.assertEqual(source['revision'],4)
            self.assertNotIn('backup_branch',source);self.assertFalse(store.get('schedules','daily')['enabled']);self.assertEqual(store.get('jobs','pending')['state'],'interrupted')
            again=SyncStore(state);self.assertEqual(again.get('sources','old')['revision'],4)


class CodeSyncAPITests(unittest.TestCase):
    setUp=test_admin.AdminTests.setUp
    login=test_admin.AdminTests.login
    post=test_admin.AdminTests.post

    def test_auth_csrf_secret_redaction_and_default_disabled_execution(self):
        self.assertEqual(self.client.get('/api/code-sync').status_code,401);self.login()
        self.assertEqual(self.post('/api/code-sync/credentials',{'id':'test','name':'Test','token':'fake_token_123456789'},csrf=False).status_code,403)
        result=self.post('/api/code-sync/credentials',{'id':'test','name':'Test','token':'fake_token_123456789'});self.assertEqual(result.status_code,200);self.assertNotIn('fake_token',result.get_data(as_text=True))
        listing=self.client.get('/api/code-sync');self.assertFalse(listing.json['execution_enabled']);self.assertEqual(listing.json['sync_mode'],'committed-only');self.assertNotIn('fake_token',listing.get_data(as_text=True))
        self.assertEqual(self.post('/api/code-sync/sources',{'command':'git add .'}).status_code,409)
        self.assertFalse(any(method!='GET' for method,_,_ in self.docker.calls))


if __name__=='__main__':unittest.main()
