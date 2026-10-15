"""Signed intake -> durable queue -> authenticated admin inbox; no Docker or GitHub network."""
import concurrent.futures
import copy
import hashlib
import hmac
import importlib.util
import io
from urllib.error import HTTPError
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from urllib.request import Request, urlopen
from werkzeug.serving import make_server, WSGIRequestHandler
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/admin'))
spec = importlib.util.spec_from_file_location('hook_receiver',ROOT/'services/github-webhook/receiver.py')
receiver = importlib.util.module_from_spec(spec);spec.loader.exec_module(receiver)
from github_watch import PROJECTS, WatchStore, WatchError, collect_project
from github_webhooks import WebhookInbox
from app import webhook_probe
from test_github_watch import Reader
import test_admin

SECRET = 'test-webhook-secret-'+'x'*48
SHA = 'a'*40


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.secrets=self.root/'secrets';self.secrets.mkdir(mode=0o700)
        for project in PROJECTS:
            path=self.secrets/(project+'.secret');path.write_text(SECRET);path.chmod(0o600)
        self.app=receiver.create_app({'TESTING':True,'QUEUE':str(self.root/'inbox'),'SECRETS':str(self.secrets)})
        self.client=self.app.test_client();self.queue=self.app.extensions['queue']
        self.store=WatchStore(self.root/'state');self.consumer=WebhookInbox(self.store,self.root/'inbox')
        self.data={'repository':{'full_name':PROJECTS['PowerX']},'sender':{'login':'reviewer'},'action':'completed',
                   'workflow_run':{'id':91,'name':'Validate','head_sha':SHA,'run_attempt':2,'status':'completed','conclusion':'failure'}}

    def send(self,data=None,event='workflow_run',delivery=None,secret=SECRET,project='PowerX',raw=None,**extra):
        raw=raw if raw is not None else json.dumps(self.data if data is None else data,ensure_ascii=False).encode()
        headers={'Content-Type':'application/json','X-GitHub-Event':event,'X-GitHub-Delivery':delivery or str(uuid.uuid4()),
                 'X-Hub-Signature-256':'sha256='+hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()}
        headers.update(extra)
        return self.client.post('/github/webhook/'+project,data=raw,headers=headers)

    def test_all_six_mapping_is_fixed_and_signed_ping_works(self):
        self.assertEqual(receiver.PROJECTS,PROJECTS)
        for project,repo in PROJECTS.items():
            response=self.send({'repository':{'full_name':repo},'zen':'fixture'},event='ping',project=project)
            self.assertEqual(response.status_code,202)
        self.assertEqual(self.consumer.consume()['imported'],6)
        self.assertTrue(all(p['status']=='not-collected' for p in self.store.listing()['projects']))

    def test_non_ping_import_durably_queues_one_project_refresh_without_network(self):
        self.send({'repository':{'full_name':PROJECTS['PowerX']},'zen':'fixture'},event='ping')
        self.consumer.consume()
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM auto_requests').fetchone()[0],0)
        delivery=str(uuid.uuid4());self.send(delivery=delivery);self.consumer.consume()
        with self.store.db() as db:
            first=dict(db.execute('SELECT * FROM auto_requests').fetchone())
            self.assertEqual(first['project'],'PowerX');self.assertEqual(first['reason'],'webhook')
        self.send(delivery=delivery);self.consumer.consume()
        with self.store.db() as db:self.assertEqual(dict(db.execute('SELECT * FROM auto_requests').fetchone()),first)
        self.send();self.consumer.consume()
        with self.store.db() as db:
            rows=db.execute('SELECT * FROM auto_requests').fetchall();self.assertEqual(len(rows),1)
            self.assertNotEqual(rows[0]['generation'],first['generation']);self.assertEqual(rows[0]['due_at'],first['due_at'])
        # Status details retain project events independently of the global inbox's first page.
        with self.store.db() as db:
            db.executemany('INSERT INTO notifications(project,kind,at,payload) VALUES(?,?,?,?)',
                [('PowerXPlugin','test','now','{}')]*60)
        listing=self.store.listing();project=next(p for p in listing['projects'] if p['project']=='PowerX')
        self.assertTrue(all(n['project']=='PowerXPlugin' for n in listing['notifications']))
        self.assertEqual(project['event_count'],3);self.assertEqual(len(project['recent_events']),3)

    def test_real_loopback_http_delivery_enters_private_inbox(self):
        class QuietHandler(WSGIRequestHandler):
            def log(self,*args,**kwargs): pass
        server=make_server('127.0.0.1',0,self.app,request_handler=QuietHandler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        raw=json.dumps(self.data).encode();delivery=str(uuid.uuid4())
        headers={'Content-Type':'application/json','X-GitHub-Event':'workflow_run','X-GitHub-Delivery':delivery,
                 'X-Hub-Signature-256':'sha256='+hmac.new(SECRET.encode(),raw,hashlib.sha256).hexdigest()}
        try:
            with urlopen(Request('http://127.0.0.1:'+str(server.server_port)+'/github/webhook/PowerX',data=raw,headers=headers),timeout=5) as response:
                self.assertEqual(response.status,202);self.assertEqual(json.load(response)['delivery'],delivery)
            self.assertEqual(self.consumer.consume()['imported'],1)
            self.assertEqual(self.store.listing()['notifications'][0]['payload']['delivery'],delivery)
        finally:
            server.shutdown();worker.join(timeout=2);server.server_close()

    def test_modified_body_wrong_secret_sha1_or_missing_signature_are_rejected(self):
        self.assertEqual(self.send(secret='wrong-'+'y'*50).status_code,401)
        self.assertEqual(self.send(**{'X-Hub-Signature-256':'sha1='+'a'*40}).status_code,401)
        self.assertEqual(self.send(**{'X-Hub-Signature-256':''}).status_code,401)
        raw=json.dumps(self.data).encode();signature='sha256='+hmac.new(SECRET.encode(),raw,hashlib.sha256).hexdigest()
        self.assertEqual(self.send(raw=raw+b' ',**{'X-Hub-Signature-256':signature}).status_code,401)
        self.assertEqual(self.consumer.status()['pending'],0)

    def test_raw_utf8_signature_and_json_key_ambiguity(self):
        data=copy.deepcopy(self.data);data['workflow_run']['name']='检查 🧪'
        self.assertEqual(self.send(data).status_code,202)
        self.assertEqual(self.send(raw=b'{"repository":{},"repository":{}}').status_code,400)
        self.assertEqual(self.send(raw=b'{"x":NaN}').status_code,400)
        self.assertEqual(self.send(raw=b'not json').status_code,400)

    def test_whitelist_repository_event_delivery_and_content_type(self):
        data=copy.deepcopy(self.data);data['repository']['full_name']='other/PowerX'
        self.assertEqual(self.send(data).status_code,400)
        self.assertEqual(self.send(project='Other').status_code,404)
        self.assertEqual(self.send(delivery='bad-delivery').status_code,400)
        self.assertEqual(self.send(event='delete').status_code,422)
        self.assertEqual(self.send(**{'Content-Type':'text/plain'}).status_code,400)
        self.assertEqual(self.client.get('/api/github').status_code,404)
        self.assertEqual(self.client.get('/github/webhook/PowerX').status_code,405)

    def test_missing_weak_or_symlinked_secret_fails_without_queue_write(self):
        path=self.secrets/'PowerX.secret';path.unlink()
        self.assertEqual(self.send().status_code,503)
        path.write_text(SECRET);path.chmod(0o644);self.assertEqual(self.send().status_code,503)
        path.unlink();path.symlink_to(self.secrets/'PowerXPlugin.secret');self.assertEqual(self.send().status_code,503)
        self.assertEqual(self.consumer.status()['pending'],0)

    def test_redelivery_after_restart_and_after_import_is_idempotent(self):
        delivery=str(uuid.uuid4());self.assertEqual(self.send(delivery=delivery).status_code,202)
        self.assertEqual(self.send(delivery=delivery).status_code,200)
        self.app=receiver.create_app({'TESTING':True,'QUEUE':str(self.root/'inbox'),'SECRETS':str(self.secrets)});self.client=self.app.test_client()
        self.assertTrue(self.send(delivery=delivery).json['duplicate'])
        self.assertEqual(self.consumer.consume()['imported'],1);self.assertEqual(self.consumer.status()['pending'],0)
        self.assertEqual(self.send(delivery=delivery).status_code,200)
        self.assertEqual(self.consumer.consume()['imported'],0);self.assertEqual(self.store.summary()['unread'],1)

    def test_delivery_content_or_unsigned_event_header_conflict(self):
        delivery=str(uuid.uuid4());self.send(delivery=delivery)
        data=copy.deepcopy(self.data);data['workflow_run']['conclusion']='success'
        self.assertEqual(self.send(data,delivery=delivery).status_code,409)
        # Same body with a different event header cannot overwrite a committed delivery.
        data['check_run']=data.pop('workflow_run')
        self.assertEqual(self.send(data,event='check_run',delivery=delivery).status_code,409)
        self.assertEqual(self.consumer.status()['pending'],1)

    def test_concurrent_redelivery_only_accepts_one(self):
        delivery=str(uuid.uuid4());payload=receiver.normalize('PowerX','workflow_run',self.data,SECRET)
        def accept(_):return self.queue.accept(delivery,'a'*64,'PowerX','workflow_run',payload)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:self.assertEqual(sum(pool.map(accept,range(12))),1)
        self.assertEqual(self.consumer.consume()['imported'],1)

    def test_body_limit_pending_capacity_and_receipt_capacity_fail_closed(self):
        self.assertEqual(self.send(raw=b' '*(receiver.LIMIT+1)).status_code,413)
        self.queue.pending_limit=1;self.assertEqual(self.send().status_code,202)
        self.assertEqual(self.send().status_code,503)
        self.consumer.consume();self.queue.receipt_limit=1
        self.assertEqual(self.send().status_code,503)

    def test_locked_database_returns_503_without_success_ack(self):
        db=sqlite3.connect(self.queue.file);db.execute('BEGIN IMMEDIATE')
        try:self.assertEqual(self.send().status_code,503)
        finally:db.rollback();db.close()
        self.assertEqual(self.consumer.status()['pending'],0)

    def test_private_comments_redaction_sha_path_and_attempt_survive(self):
        body='<script>alert(1)</script> password=hidden '+SECRET+' ghp_privateFakeToken123456789'
        data={'repository':{'full_name':PROJECTS['PowerX']},'pull_request':{'number':3,'head':{'sha':SHA}},'action':'created',
              'comment':{'id':6,'body':body,'commit_id':'b'*40,'path':'app.go','line':42},'sender':{'login':'bot'}}
        response=self.send(data,event='pull_request_review_comment');self.assertEqual(response.status_code,202)
        with self.queue.db() as db:stored=db.execute('SELECT payload FROM events').fetchone()[0]
        self.assertNotIn(SECRET,stored);self.assertNotIn('hidden',stored);self.assertNotIn('ghp_private',stored)
        self.consumer.consume();payload=self.store.listing()['notifications'][0]['payload']
        self.assertEqual(payload['sha'],'b'*40);self.assertEqual(payload['line'],42);self.assertEqual(payload['url'],'https://github.com/ArtisanCloud/PowerX/pull/3')
        self.assertIn('<script>',payload['body']);self.assertFalse(payload['publish_authorized'])
        self.send();self.consumer.consume();self.assertEqual(self.store.listing()['notifications'][0]['payload']['attempt'],2)

    def test_long_unicode_comment_and_failure_step_details_import(self):
        data={'repository':{'full_name':PROJECTS['PowerX']},'comment':{'id':6,'body':'🧪'*9000},'issue':{'number':3}}
        self.assertEqual(self.send(data,event='issue_comment').status_code,202)
        self.assertEqual(self.consumer.consume()['imported'],1)
        data={'repository':{'full_name':PROJECTS['PowerX']},'workflow_job':{'id':1,'name':'Backend','head_sha':SHA,
              'conclusion':'failure','steps':[{'name':'Go vet','conclusion':'failure'},{'name':'Setup','conclusion':'success'}]}}
        self.send(data,event='workflow_job');self.consumer.consume()
        self.assertEqual(self.store.listing()['notifications'][0]['payload']['body'],'Go vet')

    def test_crash_between_commit_and_ack_retries_without_second_notification(self):
        self.send()
        original=self.consumer.queue
        def queue(write=False):
            if write:raise OSError('fixture interruption')
            return original()
        with patch.object(self.consumer,'queue',side_effect=queue),self.assertRaises(WatchError):self.consumer.consume()
        self.assertEqual(self.store.summary()['unread'],1);self.assertEqual(self.consumer.status()['pending'],1)
        restarted=WebhookInbox(WatchStore(self.root/'state'),self.root/'inbox')
        self.assertEqual(restarted.consume(),{'imported':0,'acknowledged':1,'github_writes':False})
        self.assertEqual(self.store.summary()['unread'],1)

    def test_old_success_never_overwrites_latest_snapshot_or_late_poll_result(self):
        settings=self.store.settings('PowerX');snapshot=collect_project(settings,Reader())
        self.store.finish(settings,snapshot=snapshot);before=self.store.settings('PowerX')['snapshot']
        collecting=self.store.settings('PowerX');self.store.begin('PowerX')
        data=copy.deepcopy(self.data);data['workflow_run'].update(head_sha='b'*40,conclusion='success',run_attempt=1)
        self.send(data);self.consumer.consume();self.assertEqual(self.store.settings('PowerX')['status'],'webhook-needs-refresh')
        self.store.finish(collecting,snapshot=snapshot)
        self.assertNotEqual(self.store.settings('PowerX')['status'],'collected');self.assertEqual(self.store.settings('PowerX')['snapshot'],before)
        self.assertFalse(self.store.listing()['publish_authorized'])

    def test_inbox_full_preserves_pending_then_can_retry(self):
        with self.store.db() as db:
            db.executemany('INSERT INTO notifications(project,kind,at,payload) VALUES(?,?,?,?)', [('PowerX','test','now','{}')]*10000)
        self.send()
        with self.assertRaisesRegex(WatchError,'inbox-full'):self.consumer.consume()
        self.assertEqual(self.consumer.status()['pending'],1)
        with self.store.db() as db:db.execute('UPDATE notifications SET read_at="read"')
        self.assertEqual(self.consumer.consume()['imported'],1)

    def test_unknown_schema_symlink_and_tampered_queue_preserve_files(self):
        self.send();self.consumer.consume()
        with self.queue.db() as db:db.execute('PRAGMA user_version=9')
        with self.assertRaisesRegex(ValueError,'schema'):receiver.Queue(self.root/'inbox')
        self.assertFalse(self.consumer.status()['ready'])
        with sqlite3.connect(self.queue.file) as db:db.execute('PRAGMA user_version=1')
        self.send();self.consumer.consume();self.send()
        with self.queue.db() as db:db.execute("UPDATE events SET project='other'")
        with self.assertRaisesRegex(WatchError,'record-invalid'):self.consumer.consume()
        self.assertEqual(self.consumer.status()['pending'],1)
        other=self.root/'link';other.symlink_to(self.root/'inbox',target_is_directory=True)
        self.assertFalse(WebhookInbox(self.store,other).status()['ready'])

    def test_default_disabled_consumer_and_readonly_status_do_not_import(self):
        self.send();self.consumer.start();self.assertIsNone(self.consumer.worker)
        self.assertEqual(self.consumer.status()['pending'],1);self.assertEqual(self.store.summary()['unread'],0)
        self.assertFalse(self.consumer.status()['github_writes'])

    def test_operator_script_uses_stdlib_preserves_secret_and_stays_off(self):
        workspace=self.root/'workspace';workspace.mkdir()
        for folder in ('scripts','services/github-webhook'):
            shutil.copytree(ROOT/folder,workspace/folder,ignore=shutil.ignore_patterns('.env','__pycache__'))
        (workspace/'.env').write_text('ENABLED_SERVICES=""\n')
        def run(*args):return subprocess.run(['sh','scripts/github-webhook.sh',*args],cwd=workspace,text=True,capture_output=True)
        self.assertEqual(run('init').returncode,0);self.assertEqual(run('secret','PowerX').returncode,0)
        secret=workspace/'data/github-webhook/secrets/PowerX.secret';value=secret.read_text()
        again=run('secret','PowerX');self.assertEqual(again.returncode,0);self.assertEqual(secret.read_text(),value)
        self.assertNotIn(value.strip(),again.stdout);self.assertEqual(secret.stat().st_mode&0o777,0o600)
        self.assertEqual(run('secret-show','PowerX').stdout.strip(),value.strip())
        self.assertEqual((workspace/'.env').read_text(),'ENABLED_SERVICES=""\n');self.assertNotEqual(run('start').returncode,0)


class WebhookProbeTests(unittest.TestCase):
    def test_unsigned_probe_checks_exact_rejection_without_following_urls(self):
        failure=HTTPError('https://hooks.example.com/github/webhook/PowerX',401,'Unauthorized',{},io.BytesIO(b'{"error":"invalid-signature"}'))
        opener=unittest.mock.Mock();opener.open.side_effect=failure
        state,detail=webhook_probe(opener,'hooks.example.com')
        self.assertEqual(state,'reachable');self.assertIn('未创建事件',detail)
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'https://hooks.example.com/github/webhook/PowerX')
        self.assertEqual(request.get_method(),'POST');self.assertEqual(request.data,b'{}')
        self.assertFalse(request.has_header('Authorization'));self.assertFalse(request.has_header('X-hub-signature-256'))
        self.assertTrue(failure.closed)

    def test_other_401_and_redirect_are_not_accepted(self):
        for code,body in ((401,b'{"error":"login-required"}'),(302,b'{}'),(503,b'{"error":"secret-unavailable"}')):
            failure=HTTPError('https://hooks.example.com',code,'fixture',{},io.BytesIO(body))
            opener=unittest.mock.Mock();opener.open.side_effect=failure
            with self.assertRaises(ValueError):webhook_probe(opener,'hooks.example.com')
            self.assertTrue(failure.closed)


class HookAdminTests(unittest.TestCase):
    post = test_admin.AdminTests.post
    login = test_admin.AdminTests.login
    wait_job = test_admin.AdminTests.wait_job
    def setUp(self):
        test_admin.AdminTests.setUp(self)
        self.root=self.root.resolve()
        self.hooks=self.app.extensions['github_webhooks'];self.queue_root=self.root/'hook-inbox'
        self.hooks.file=self.queue_root/'queue.sqlite3';self.queue=receiver.Queue(self.queue_root)
        data={'repository':{'full_name':PROJECTS['PowerX']},'workflow_run':{'id':1,'head_sha':SHA,'name':'CI','conclusion':'failure'}}
        self.queue.accept(str(uuid.uuid4()),'a'*64,'PowerX','workflow_run',receiver.normalize('PowerX','workflow_run',data,SECRET))

    def test_registered_hook_site_uses_rejection_probe_and_records_success(self):
        site=self.root/'config/sites/github-webhook';site.mkdir()
        (site/'site.conf').write_text('SITE_KIND=github_webhook\nSITE_SERVICE=github-webhook\n')
        (site/'.env').write_text('SITE_DOMAIN=hooks.example.com\n')
        env=self.root/'config/root.env';env.write_text(env.read_text().replace('ENABLED_SITES="powerxdoc"','ENABLED_SITES="powerxdoc github-webhook"'))
        self.login()
        failure=HTTPError('https://hooks.example.com',401,'fixture',{},io.BytesIO(b'{"error":"invalid-signature"}'))
        with patch('app.urllib.request.build_opener') as factory:
            factory.return_value.open.side_effect=failure
            response=self.post('/api/sites/probe',{'site':'github-webhook'})
            self.assertEqual(response.status_code,202)
            self.assertEqual(self.wait_job(response.json['id'])['state'],'success')
            self.assertEqual(factory.return_value.open.call_args.args[0].get_method(),'POST')
        sites=self.client.get('/api/snapshot').json['sites']
        saved=next(row for row in sites if row['key']=='github-webhook')
        self.assertEqual(saved['kind'],'github_webhook');self.assertEqual(saved['probe']['status'],'reachable')

    def test_hook_import_requires_session_csrf_and_origin_but_no_docker(self):
        before=list(self.docker.calls)
        self.assertEqual(self.client.post('/api/github/action',json={'action':'consume-webhooks'},headers={'Origin':'http://localhost'}).status_code,401)
        self.login()
        self.assertEqual(self.post('/api/github/action',{'action':'consume-webhooks'},csrf=False).status_code,403)
        self.assertEqual(self.post('/api/github/action',{'action':'consume-webhooks'},origin=False).status_code,403)
        self.assertEqual(self.client.get('/api/github').json['webhooks']['pending'],1)
        response=self.post('/api/github/action',{'action':'consume-webhooks'});self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['imported'],1);self.assertEqual(self.docker.calls,before)
        data=self.client.get('/api/github').json;self.assertEqual(data['webhooks']['pending'],0)
        self.assertEqual(data['notifications'][0]['payload']['source'],'signed-webhook')
        self.assertNotIn(SECRET,json.dumps(data));self.assertFalse(data['publish_authorized'])
        self.assertEqual(self.post('/github/webhook/PowerX',{}).status_code,404)


if __name__=='__main__':unittest.main()
