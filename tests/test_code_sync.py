"""Real Git snapshot -> bare remote -> target agent, with isolated auth/CSRF checks."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'services/admin'))
from host_metrics import private_directory,atomic_json
from sync_runtime import GitTransfer,git,text_git,scan_source,digest,SyncError,backup_branch
from sync_agent import CopyAgent,agent_sha
from native_services import SystemdServices
from test_native_services import FakeSystemd
from code_sync import CodeSync,CodeSyncWorker,next_daily,default_policy
import test_admin


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        self.sources=self.root/'workspace';self.sources.mkdir();self.source=self.sources/'project';self.source.mkdir()
        git(self.source,'init','-b','main');git(self.source,'config','user.name','Fixture');git(self.source,'config','user.email','fixture@example.invalid')
        (self.source/'app.py').write_text('print("first")\n');(self.source/'.gitignore').write_text('ignored.txt\n.env\n')
        git(self.source,'add','.');git(self.source,'commit','-m','initial');git(self.source,'remote','add','origin','https://github.com/example/project.git')
        self.remote=self.root/'remote.git';git(self.root,'init','--bare',str(self.remote));self.resolver=lambda repo:str(self.remote)
        self.hub=CodeSync(self.root/'state',operations=True,transfer=GitTransfer(private_directory(self.root/'cache'),self.resolver))
        policy=default_policy();policy.update(execution=True,source_roots=[str(self.sources)],identities={'fixture':str(self.root/'key')});self.hub.import_policy(policy)
        self.hub.save_credential({'id':'fixture','name':'Fixture','token':'fixture_token_123456789'})
        self.profile={'id':'example','revision':1,'project':None,'name':'Example','directory':str(self.source),'repository':'example/project','branch':'main','backup_branch':'xdocker-backup/example','includes':['.'],'excludes':[],'credential':'fixture','enabled':True,'confirmation':'Example'}
        self.hub.save('sources',self.profile)
        self.copies=self.root/'copies';self.copies.mkdir();self.destination=self.copies/'example'
        self.agent_root=private_directory(self.root/'agent');self.agent_file=self.agent_root/'policy.json'
        self.agent_policy={'schema':1,'roots':[str(self.copies)],'credentials':{},'targets':{'example':{'directory':str(self.destination),'repository':'example/project','backup_branch':'xdocker-backup/example','credential':None,'service':None}}}
        atomic_json(self.agent_file,self.agent_policy);self.agent=CopyAgent(self.agent_file,self.resolver)
        self.hub.transport=lambda device,body:self.agent.handle({**body,'expected_agent_sha':agent_sha()})
        device={'id':'copy','revision':1,'name':'Copy','host':'100.100.0.4','port':22,'username':'fixture','identity':'fixture','host_key':'ssh-ed25519 '+base64.b64encode(b'x'*40).decode(),'enabled':True}
        self.hub.save('devices',device)
        self.target={'id':'copy-example','revision':1,'source':'example','device':'copy','remote_target':'example','directory':str(self.destination),'restart':False,'enabled':True,'confirmation':'Example'}
        self.hub.save('targets',self.target)

    def job(self,kind='upload'):
        job=self.hub.enqueue({'source':'example','kind':kind,'confirmation':'Example','idempotency_key':os.urandom(16).hex()});self.hub.execute(job);return self.hub.store.get('jobs',job['id'])

    def test_real_snapshot_upload_copy_and_no_source_git_mutation_or_secret_upload(self):
        (self.source/'app.py').write_text('print("dirty change")\n');(self.source/'new.js').write_text('export const value = 1;\n')
        (self.source/'.env').write_text('PASSWORD=must-not-upload');(self.source/'private.key').write_text('must-not-upload');(self.source/'ignored.txt').write_text('ignored')
        branch=text_git(self.source,'symbolic-ref','--short','HEAD');head=text_git(self.source,'rev-parse','HEAD');index=(self.source/'.git/index').read_bytes()
        result=self.job();self.assertEqual(result['state'],'success',result)
        self.assertEqual(text_git(self.destination,'rev-parse','HEAD'),result['sha']);self.assertEqual((self.destination/'new.js').read_text(),'export const value = 1;\n')
        names=text_git(self.remote,'ls-tree','-r','--name-only',result['sha']).splitlines()
        self.assertNotIn('.env',names);self.assertNotIn('private.key',names);self.assertNotIn('ignored.txt',names);self.assertNotIn('index',names)
        self.assertEqual(text_git(self.source,'symbolic-ref','--short','HEAD'),branch);self.assertEqual(text_git(self.source,'rev-parse','HEAD'),head);self.assertEqual((self.source/'.git/index').read_bytes(),index)
        again=self.job();self.assertEqual(again['sha'],result['sha']);self.assertFalse(again['result'][0]['updated'])
        (self.source/'app.py').write_text('print("next")\n');changed=self.job();self.assertNotEqual(changed['sha'],result['sha']);self.assertEqual(text_git(self.destination,'rev-parse','HEAD'),changed['sha'])
        self.assertNotIn('fixture_token_123456789',json.dumps(self.hub.listing()));self.assertEqual(list(self.hub.transient.iterdir()),[])

    def test_dirty_copy_and_branch_divergence_are_not_overwritten(self):
        first=self.job();(self.destination/'app.py').write_text('local work')
        (self.source/'app.py').write_text('incoming change');failed=self.job();self.assertEqual(failed['state'],'partial');self.assertEqual((self.destination/'app.py').read_text(),'local work')
        self.assertEqual(text_git(self.destination,'rev-parse','HEAD'),first['sha'])
        with self.assertRaises(SyncError):self.hub.enqueue({'source':'example','kind':'upload','confirmation':'Example','idempotency_key':'a'*32})
        self.hub.close_job(failed['id'],{'confirmation':'Example'})
        git(self.destination,'add','app.py');git(self.destination,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','local divergence')
        head=text_git(self.destination,'rev-parse','HEAD');failed=self.job();self.assertEqual(failed['state'],'partial');self.assertEqual(text_git(self.destination,'rev-parse','HEAD'),head)

    def test_pending_remote_receipt_reconciliation_and_agent_idempotence(self):
        original=self.hub.transfer.publish
        def uncertain(*args,**kwargs):original(*args,**kwargs);raise SyncError('XD_SYNC_UPLOAD_UNCONFIRMED')
        with patch.object(self.hub.transfer,'publish',uncertain):job=self.job()
        self.assertEqual(job['state'],'upload-unconfirmed');self.assertFalse(self.destination.exists())
        with self.assertRaises(SyncError):self.hub.enqueue({'source':'example','kind':'upload','confirmation':'Example','idempotency_key':'a'*32})
        self.hub.store.request_reconcile(job['id']);done=self.hub.reconcile(job['id']);self.assertEqual(done['state'],'success');self.assertEqual(text_git(self.destination,'rev-parse','HEAD'),job['sha'])
        observed=self.agent.inspect('example');request={'operation':'sync','expected_agent_sha':agent_sha(),'target':'example','sha':job['sha'],'observation':observed['observation'],'grant_sha':observed['grant_sha'],'request_id':'c'*64,'restart':False}
        first=self.agent.handle(request);self.assertEqual(first,self.agent.handle(request))
        with self.assertRaises(SyncError):self.agent.handle({**request,'restart':True})

    def test_policy_drift_permissions_source_symlinks_and_canonical_branch_scope(self):
        for branch in ('main','develop','xdocker-backup/a/../main','xdocker-backup/x.lock'):
            with self.assertRaises(SyncError):backup_branch(branch)
        outside=self.root/'private';outside.write_text('secret');(self.source/'linked').symlink_to(outside)
        with self.assertRaises(SyncError):self.hub.preview('example')
        (self.source/'linked').unlink()
        self.agent_policy['targets']['example']['directory']=str(self.copies/'changed');atomic_json(self.agent_file,self.agent_policy);self.agent=CopyAgent(self.agent_file,self.resolver)
        failed=self.job();self.assertEqual(failed['state'],'partial');self.assertFalse((self.copies/'changed').exists())

    def test_restart_is_fixed_pinned_and_only_after_actual_update(self):
        unit=self.root/'example.service';unit.write_text('[Service]\nExecStart=/usr/bin/fixture\n')
        runner=FakeSystemd(unit);adapter=SystemdServices(runner)
        grant={'scope':'user','unit':'example.service','actions':['status','restart'],'definition_sha256':None}
        grant['definition_sha256']=adapter.status(grant)['definition_sha256'];self.agent_policy['targets']['example']['service']=grant
        atomic_json(self.agent_file,self.agent_policy);self.agent=CopyAgent(self.agent_file,self.resolver,adapter)
        self.hub.save('targets',{**self.target,'revision':2,'restart':True})
        done=self.job();self.assertEqual(done['state'],'success');self.assertEqual(sum('restart' in command for command in runner.calls),1)
        self.job();self.assertEqual(sum('restart' in command for command in runner.calls),1)
        old=text_git(self.destination,'rev-parse','HEAD');unit.write_text('[Service]\nExecStart=/usr/bin/changed\n');(self.source/'app.py').write_text('new version')
        done=self.job();self.assertEqual(done['state'],'partial');self.assertEqual(text_git(self.destination,'rev-parse','HEAD'),old);self.assertEqual(sum('restart' in command for command in runner.calls),1)

    def test_ignored_file_collision_is_preserved_and_sql_source_is_included(self):
        (self.source/'.gitignore').write_text('collision\n.env\n');(self.source/'migrations').mkdir();(self.source/'migrations/001.sql').write_text('CREATE TABLE fixture(id INTEGER);')
        first=self.job();self.assertTrue((self.destination/'migrations/001.sql').is_file())
        (self.destination/'collision').write_text('local generated file')
        (self.source/'collision').write_text('incoming code');git(self.source,'add','-f','collision')
        result=self.job();self.assertEqual(result['state'],'partial');self.assertEqual((self.destination/'collision').read_text(),'local generated file');self.assertEqual(text_git(self.destination,'rev-parse','HEAD'),first['sha'])

    def test_detached_source_is_pinned_and_remains_read_only(self):
        git(self.source,'checkout','--detach');head=text_git(self.source,'rev-parse','HEAD')
        item=self.hub.inventory(True)[0];self.assertEqual(item['branch'],'HEAD@'+head)
        self.hub.save('sources',{**self.profile,'revision':2,'branch':item['branch']})
        index=(self.source/'.git/index').read_bytes();done=self.job()
        self.assertEqual(done['state'],'success');self.assertEqual((self.source/'.git/index').read_bytes(),index)
        self.assertEqual(text_git(self.source,'rev-parse','HEAD'),head)
        git(self.source,'checkout','main')
        with self.assertRaises(SyncError):self.hub.preview('example')

    def test_workspace_inventory_prefers_primary_over_temporary_clone(self):
        duplicate=self.sources/'project-production-fix-20261112'
        git(self.sources,'clone',str(self.source),str(duplicate))
        git(duplicate,'remote','set-url','origin','https://github.com/example/project.git')
        inventory=self.hub.inventory(True)
        self.assertEqual(len(inventory),1);self.assertEqual(inventory[0]['directory'],str(self.source))

    def test_archive_keeps_idempotency_and_agent_receipts_without_replaying(self):
        first=self.job();self.hub.archive()
        repeated=self.hub.enqueue({'source':'example','kind':'upload','confirmation':'Example','idempotency_key':first['idempotency_key']})
        self.assertEqual(repeated['id'],first['id']);self.assertEqual(repeated['state'],'success')
        archive=self.hub.listing()['archives'][0];self.assertEqual(self.hub.read_archive(archive)['jobs'][0]['id'],first['id'])
        observed=self.agent.inspect('example');request={'operation':'sync','expected_agent_sha':agent_sha(),'target':'example','sha':first['sha'],'observation':observed['observation'],'grant_sha':observed['grant_sha'],'request_id':'c'*64,'restart':False}
        receipt=self.agent.handle(request)
        import sqlite3
        with sqlite3.connect(self.agent.database) as db:
            db.execute("UPDATE runs SET at=0 WHERE id=?",(request['request_id'],))
            for n in range(270):db.execute('INSERT INTO runs VALUES(?,?,?,?,?)',(f'{n:064x}','fixture',1,'observed',json.dumps(receipt)))
        self.agent.handle({**request,'request_id':'d'*64})
        self.assertTrue((self.agent.root/'receipts'/(request['request_id']+'.json')).is_file())
        with patch('sync_agent.pull_copy',side_effect=AssertionError('replay')):self.assertEqual(self.agent.handle(request),receipt)
        with self.assertRaises(SyncError):self.agent.handle({**request,'restart':True})

    def test_schedule_leases_restart_recovery_and_archive_keep_unconfirmed(self):
        scheduled={'id':'daily','revision':1,'source':'example','mode':'upload','hour':2,'minute':0,'timezone':'Asia/Shanghai','enabled':True,'confirmation':'Example'}
        row=self.hub.save('schedules',scheduled)
        with self.hub.store.db(write=True) as db:db.execute('UPDATE schedules SET next_at=? WHERE id=?',(time.time()-1,'daily'))
        worker=CodeSyncWorker(self.hub);worker.tick();jobs=self.hub.store.all('jobs');self.assertEqual(len(jobs),1);self.assertEqual(jobs[0]['state'],'success')
        worker.tick();self.assertEqual(len(self.hub.store.all('jobs')),1)
        pending=self.hub.enqueue({'source':'example','kind':'upload','confirmation':'Example','idempotency_key':'d'*32});self.hub.store.patch_job(pending['id'],state='running',sha='e'*40)
        self.hub.store.recover();self.assertEqual(self.hub.store.get('jobs',pending['id'])['state'],'interrupted')
        receipt=self.hub.archive();self.assertEqual(receipt['count'],1);self.assertIsNotNone(self.hub.store.get('jobs',pending['id']))
        self.assertGreater(next_daily(2,0,'Asia/Shanghai',0),0)
        self.hub.close_job(pending['id'],{'confirmation':'Example'});self.assertEqual(self.hub.store.get('jobs',pending['id'])['state'],'closed-unconfirmed')


class CodeSyncAPITests(unittest.TestCase):
    setUp=test_admin.AdminTests.setUp
    login=test_admin.AdminTests.login
    post=test_admin.AdminTests.post

    def test_auth_csrf_secret_redaction_and_default_disabled_execution(self):
        self.assertEqual(self.client.get('/api/code-sync').status_code,401);self.login()
        self.assertEqual(self.post('/api/code-sync/credentials',{'id':'test','name':'Test','token':'fake_token_123456789'},csrf=False).status_code,403)
        result=self.post('/api/code-sync/credentials',{'id':'test','name':'Test','token':'fake_token_123456789'});self.assertEqual(result.status_code,200);self.assertNotIn('fake_token',result.get_data(as_text=True))
        listing=self.client.get('/api/code-sync');self.assertEqual(listing.status_code,200);self.assertFalse(listing.json['execution_enabled']);self.assertNotIn('fake_token',listing.get_data(as_text=True))
        self.assertEqual(self.post('/api/code-sync/sources',{'command':'git push --force'}).status_code,409)
        self.assertFalse(any(method!='GET' for method,_,_ in self.docker.calls))


if __name__=='__main__':unittest.main()
