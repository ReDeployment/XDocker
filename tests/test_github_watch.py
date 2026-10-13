"""Read-only GitHub fixtures: SHA/attempt binding, review ambiguity, inbox durability and controls."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from email.message import Message

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/admin'))
from github_watch import PROJECTS, GitHubReader, GitHubWatch, WatchStore, WatchError, collect_project, condition, safe_text
from app import create_app
from test_admin import FakeDocker, TOKEN

HEAD='a'*40;OLD='b'*40


class Reader:
    def __init__(self,repo='ArtisanCloud/PowerX'):
        self.repo=repo;self.token='private-collector-token';self.calls=[]
        self.data={
            '':{'full_name':repo,'default_branch':'develop','private':False},
            '/branches/develop':{'commit':{'sha':HEAD}},
            '/pulls?state=all&sort=updated&direction=desc&per_page=10':[{'number':1,'title':'fixture PR','state':'open','draft':False,'head':{'sha':HEAD}}],
            '/pulls/1/reviews?per_page=100':[{'id':1,'state':'APPROVED','commit_id':HEAD,'user':{'login':'reviewer'},'body':'review body'}],
            '/commits/'+HEAD+'/check-runs?filter=latest&per_page=100':{'total_count':1,'check_runs':[{'id':1,'name':'lint','head_sha':HEAD,'status':'completed','conclusion':'success'}]},
            '/actions/runs?head_sha='+HEAD+'&per_page=100':{'total_count':1,'workflow_runs':[{'id':9,'workflow_id':2,'name':'Build','head_sha':HEAD,'run_attempt':1,'status':'completed','conclusion':'success'}]},
            '/commits/'+HEAD+'/status?per_page=100':{'total_count':0,'statuses':[]},
            '/issues/comments?sort=updated&direction=desc&per_page=30':[{'id':10,'issue_url':'https://api.github.com/repos/'+repo+'/issues/1',
                'body':'<script>execute()</script> password=never-print '+self.token,'user':{'login':'bot[bot]'},'updated_at':'2026-10-12T00:00:00Z'}],
            '/pulls/comments?sort=updated&direction=desc&per_page=30':[{'id':11,'pull_request_url':'https://api.github.com/repos/'+repo+'/pulls/1',
                'body':'old line comment','path':'app.go','line':12,'commit_id':OLD,'user':{'login':'bot[bot]'}}],
        };self.more=set()

    def get(self,path=''):
        self.calls.append(path)
        if path not in self.data:raise AssertionError('Unknown fixed GET '+path)
        return copy.deepcopy(self.data[path]),path in self.more


def settings(project='PowerX'):
    return {'project':project,'repository':PROJECTS[project],'branches':[],'required_checks':[],'enabled':True,'revision':'fixture','status':'not-collected'}


class CollectorTests(unittest.TestCase):
    def test_current_sha_reviews_and_plain_comments_are_separate(self):
        reader=Reader();data=collect_project(settings(),reader)
        self.assertEqual(data['state'],'observed-pass');self.assertFalse(data['publish_authorized'])
        self.assertEqual(data['pulls'][0]['review'],'approved-observed')
        self.assertEqual(data['comments'][0]['binding'],'unbound');self.assertEqual(data['comments'][1]['binding'],'historical')
        text=json.dumps(data);self.assertNotIn(reader.token,text);self.assertNotIn('never-print',text)
        self.assertIn('<script>',text)  # Stored as data; the UI must escape it, not interpret it.

    def test_rerun_latest_attempt_supersedes_old_failure(self):
        reader=Reader();path='/actions/runs?head_sha='+HEAD+'&per_page=100'
        good=reader.data[path]['workflow_runs'][0];old=copy.deepcopy(good);old.update(run_attempt=1,conclusion='failure')
        good['run_attempt']=2;reader.data[path].update(total_count=2,workflow_runs=[old,good])
        data=collect_project(settings(),reader);self.assertEqual(data['state'],'observed-pass')
        self.assertEqual(data['ci'][0]['checks'][-1]['attempt'],2)
        self.assertFalse(any('/jobs?' in p for p in reader.calls))

    def test_failure_step_and_annotations_are_visible_without_logs(self):
        reader=Reader();run=reader.data['/actions/runs?head_sha='+HEAD+'&per_page=100']['workflow_runs'][0];run['conclusion']='failure'
        check=reader.data['/commits/'+HEAD+'/check-runs?filter=latest&per_page=100']['check_runs'][0]
        check.update(conclusion='failure',output={'summary':'password=never-print','annotations_count':1})
        reader.data['/check-runs/1/annotations?per_page=100']=[{'path':'app.go','start_line':42,'annotation_level':'failure','message':'lint failed'}]
        reader.data['/actions/runs/9/attempts/1/jobs?per_page=100']={'total_count':1,'jobs':[{'name':'backend','conclusion':'failure','steps':[{'name':'Go vet','conclusion':'failure'}]}]}
        data=collect_project(settings(),reader);self.assertEqual(data['state'],'blocked')
        self.assertEqual(data['failed_jobs'][0]['steps'],['Go vet']);self.assertEqual(data['ci'][0]['checks'][0]['annotations'][0]['line'],42)
        self.assertFalse(any('/logs' in p for p in reader.calls));self.assertNotIn('never-print',json.dumps(data))

    def test_historical_changes_requested_and_commented_do_not_become_fresh_approval(self):
        reader=Reader();reviews=reader.data['/pulls/1/reviews?per_page=100']
        reviews[0].update(state='CHANGES_REQUESTED',commit_id=OLD)
        reviews.append({'id':2,'state':'COMMENTED','commit_id':HEAD,'user':{'login':'reviewer'},'body':'Completed'})
        data=collect_project(settings(),reader);self.assertEqual(data['pulls'][0]['review'],'needs-recheck');self.assertEqual(data['state'],'attention')
        reviews[0]['commit_id']=HEAD
        self.assertEqual(collect_project(settings(),reader)['state'],'blocked')

    def test_incomplete_missing_skipped_and_wrong_sha_never_turn_green(self):
        reader=Reader();reader.more.add('/commits/'+HEAD+'/check-runs?filter=latest&per_page=100')
        self.assertEqual(collect_project(settings(),reader)['ci'][0]['state'],'unknown')
        reader.more.clear();reader.data['/commits/'+HEAD+'/check-runs?filter=latest&per_page=100']['check_runs'][0]['head_sha']=OLD
        with self.assertRaises(WatchError):collect_project(settings(),reader)
        self.assertEqual(condition([{'name':'lint','status':'completed','conclusion':'skipped'}],True,[]),'unknown')
        self.assertEqual(condition([{'name':'lint','status':'completed','conclusion':'success'}],True,['missing']),'unknown')

    def test_transport_only_uses_explicit_get_and_does_not_echo_stderr(self):
        reader=GitHubReader(PROJECTS['PowerX'],gh=True)
        call=type('Call',(),{'returncode':0,'stdout':b'{"ok":true}'})()
        with patch('github_watch.subprocess.run',return_value=call) as invoke:
            self.assertEqual(reader.get('/pulls')[0],{'ok':True})
        self.assertEqual(invoke.call_args.args[0],['gh','api','--method','GET','repos/ArtisanCloud/PowerX/pulls'])
        with self.assertRaises(WatchError):GitHubReader('unrelated/repo')
        self.assertNotIn('secret',safe_text('password=secret Bearer abcdefg'))

    def test_rate_limit_returns_cooldown_not_body_or_token(self):
        reader=GitHubReader(PROJECTS['PowerX'],'private-token');headers=Message();headers['X-RateLimit-Remaining']='0';headers['X-RateLimit-Reset']=str(int(time.time()+600))
        error=HTTPError('https://api.github.com',403,'private-body',headers,None)
        with patch.object(reader.opener,'open',side_effect=error),self.assertRaises(WatchError) as ctx:reader.get()
        self.assertEqual(str(ctx.exception),'rate-limited');self.assertGreater(ctx.exception.retry_at,time.time()+500)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.watch=GitHubWatch(self.root,reader_factory=lambda s:Reader(s['repository']))

    def test_all_six_repo_mappings_defaults_and_private_storage(self):
        listing=self.watch.listing();self.assertEqual(len(listing['projects']),6);self.assertFalse(listing['auto_enabled'])
        self.assertEqual(PROJECTS['PowerXPluginCRM'],'Matrix-X/PowerXPluginCRM')
        self.assertEqual(PROJECTS['PowerXPluginMediaXStudio'],'Matrix-X/PowerXPluginMediaXStudio')
        self.assertEqual(self.watch.store.file.stat().st_mode&0o777,0o600);self.assertEqual(self.watch.store.root.stat().st_mode&0o777,0o700)

    def test_dedup_read_receipt_and_restart_do_not_change_ci(self):
        self.watch.sync('PowerX',background=False);first=self.watch.listing();self.assertEqual(first['unread'],1)
        self.watch.sync('PowerX',background=False);self.assertEqual(self.watch.listing()['unread'],1)
        self.watch.store.mark_read([first['notifications'][0]['id']]);self.assertEqual(self.watch.listing()['unread'],0)
        reader=Reader();reader.data['/issues/comments?sort=updated&direction=desc&per_page=30'][0]['body']='new review'
        self.watch.reader_factory=lambda _:reader;self.watch.sync('PowerX',background=False)
        self.assertEqual(self.watch.listing()['unread'],1)
        restored=GitHubWatch(self.root);self.assertEqual(restored.listing()['unread'],1)
        self.assertEqual(restored.store.settings('PowerX')['snapshot']['state'],'observed-pass')

    def test_permission_failure_retains_cache_but_marks_unknown_and_dedups_error(self):
        self.watch.sync('PowerX',background=False)
        class Denied:
            def get(self,*a):raise WatchError('github-http-404')
        self.watch.reader_factory=lambda _:Denied();self.watch.sync('PowerX',background=False)
        row=self.watch.store.settings('PowerX');self.assertEqual(row['status'],'github-http-404');self.assertIsNotNone(row['snapshot'])
        before=self.watch.listing()['unread']
        with self.watch.store.db() as db:db.execute('UPDATE projects SET retry_at=0')
        self.watch.sync('PowerX',background=False);self.assertEqual(self.watch.listing()['unread'],before)
        with self.assertRaisesRegex(WatchError,'cooling'):self.watch.sync('PowerX',background=False)

    def test_optimistic_settings_and_cross_instance_lock(self):
        row=self.watch.store.settings('PowerX');self.watch.store.configure('PowerX',row['revision'],False,['develop'],['lint'])
        with self.assertRaisesRegex(WatchError,'changed'):self.watch.store.configure('PowerX',row['revision'],True,[],[])
        with self.assertRaisesRegex(WatchError,'disabled'):self.watch.sync('PowerX',background=False)
        lease=self.watch.lease('collect')
        try:
            other=GitHubWatch(self.root)
            with self.assertRaisesRegex(WatchError,'busy'):other.sync('PowerXPlugin',background=False)
        finally:lease.close()

    def test_credentials_are_owner_scoped_and_symlinks_are_refused(self):
        directory=self.watch.store.root/'credentials';directory.mkdir();file=directory/'matrix-x.token'
        file.write_text('github_pat_'+'x'*30);file.chmod(0o600)
        self.watch.reader_factory=None
        self.assertEqual(self.watch.reader(self.watch.store.settings('PowerXPluginCRM')).token,file.read_text())
        self.assertEqual(self.watch.reader(self.watch.store.settings('PowerX')).token,'')
        file.chmod(0o644)
        with self.assertRaisesRegex(WatchError,'unsafe'):self.watch.reader(self.watch.store.settings('PowerXPluginCRM'))
        file.unlink();file.symlink_to(directory/'elsewhere')
        with self.assertRaisesRegex(WatchError,'unsafe'):self.watch.reader(self.watch.store.settings('PowerXPluginCRM'))

    def test_unknown_schema_is_preserved_and_no_auto_network_calls(self):
        with self.watch.store.db() as db:db.execute('PRAGMA user_version=9')
        with patch('github_watch.GitHubReader.get',side_effect=AssertionError('Unexpected network')):
            invalid=GitHubWatch(self.root);self.assertEqual(invalid.error,'watch-store-unavailable')
        with sqlite3.connect(self.watch.store.file) as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],9)

    def test_default_network_isolation_has_no_token_in_arguments_or_input(self):
        self.watch.reader_factory=None;row=self.watch.store.settings('PowerX')
        snapshot=collect_project(settings(),Reader());call=type('Call',(),{'returncode':0,'stdout':json.dumps(snapshot).encode()})()
        with patch('github_watch.subprocess.run',return_value=call) as invoke:self.assertEqual(self.watch.collect(row)['state'],'observed-pass')
        self.assertEqual(invoke.call_args.kwargs['timeout'],50)
        self.assertNotIn('token',invoke.call_args.kwargs['input'].decode())
        with patch('github_watch.subprocess.run',side_effect=__import__('subprocess').TimeoutExpired('fixture',50)):
            with self.assertRaisesRegex(WatchError,'hard-timeout'):self.watch.collect(row)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        for part in ('state','config/sites','reports'):(self.root/part).mkdir(parents=True)
        (self.root/'state/access-token').write_text(TOKEN);(self.root/'config/root.env').write_text('ENABLED_SITES=""\n')
        self.engine=FakeDocker();self.app=create_app({'TESTING':True,'STATE':str(self.root/'state'),'CONFIG':str(self.root/'config'),'REPORTS':str(self.root/'reports')},self.engine)
        self.client=self.app.test_client();auth=self.client.post('/api/login',json={'token':TOKEN},headers={'Origin':'http://localhost'})
        self.headers={'Origin':'http://localhost','X-CSRF-Token':auth.json['csrf']};self.watch=self.app.extensions['github_watch']
        self.watch.reader_factory=lambda s:Reader(s['repository'])

    def test_auth_scope_read_controls_and_no_docker_or_github_writes(self):
        self.assertEqual(self.app.test_client().get('/api/github').status_code,401)
        self.assertEqual(self.client.post('/api/github/action',json={},headers={'Origin':'http://localhost'}).status_code,403)
        self.assertEqual(self.client.get('/api/github?url=https://evil.example').status_code,400)
        self.assertEqual(self.client.post('/api/github/action',json={'action':'merge','project':'PowerX'},headers=self.headers).status_code,400)
        self.assertEqual(self.client.post('/api/github/action',json={'action':'sync','project':'other'},headers=self.headers).status_code,409)
        response=self.client.post('/api/github/action',json={'action':'sync','project':'PowerX'},headers=self.headers);self.assertEqual(response.status_code,202)
        self.watch.worker.join(2);self.assertFalse(self.watch.worker.is_alive())
        result=self.client.get('/api/github').json;self.assertEqual(len(result['projects']),6);self.assertFalse(result['publish_authorized'])
        self.assertNotIn(TOKEN,json.dumps(result));self.assertNotIn('private-collector-token',json.dumps(result))
        self.assertEqual(self.engine.calls,[])
        self.assertEqual(self.client.get('/api/github/summary').json['unread'],1)
        identifier=result['notifications'][0]['id']
        self.assertEqual(self.client.post('/api/github/action',json={'action':'read','ids':[identifier]},headers=self.headers).status_code,200)
        self.assertEqual(self.client.get('/api/github/summary').json['unread'],0)


if __name__=='__main__':unittest.main()
