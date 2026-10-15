"""Evidence coverage, bounded history, optimistic workflow and no repository side effects."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/admin'))
from github_triage import GitHubTriage
from github_watch import GitHubWatch, WatchStore, WatchError, collect_project, stamp
from test_github_watch import Reader, settings, HEAD, OLD
import test_github_watch as watch_fixtures
import test_github_webhook as hook_fixtures


class TriageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=WatchStore(Path(self.tmp.name).resolve());self.triage=GitHubTriage(self.store)

    def add(self,**payload):
        value={'kind':'webhook','event':'workflow_run','title':'workflow_run · completed · Build','sha':HEAD,
               'object_id':9,'attempt':1,'conclusion':'failure','status':'completed','url':'https://github.com/ArtisanCloud/PowerX/actions/runs/9',
               'source':'signed-webhook',**payload}
        with self.store.db() as db:db.execute('INSERT INTO notifications(project,kind,at,payload) VALUES(?,?,?,?)',('PowerX','webhook',stamp(),json.dumps(value)))

    def issue(self):return self.triage.listing()['issues'][0]

    def test_metadata_is_sha_scoped_and_read_does_not_resolve_failure(self):
        self.add();i=self.issue();self.assertEqual(i['workflow_state'],'new');self.assertEqual(i['head_binding'],'unconfirmed')
        self.assertEqual(i['publish_signal'],'needs-verification')
        with self.store.db() as db:identifier=db.execute('SELECT id FROM notifications').fetchone()[0]
        self.store.mark_read([identifier]);self.assertEqual(self.issue()['version'],i['version'])
        self.assertFalse(self.triage.listing()['publish_authorized'])

    def test_attempt_order_wins_over_late_delivery_and_other_sha_success(self):
        self.add();self.add(attempt=2,conclusion='success');self.add(attempt=1,conclusion='failure')
        self.assertEqual(self.issue()['evidence_state'],'recovered-observed')
        self.add(sha=OLD,object_id=12,attempt=2,conclusion='failure');self.add(sha=HEAD,object_id=13,attempt=3,conclusion='success')
        items=self.triage.listing()['issues'];self.assertEqual(len(items),2)
        old=next(i for i in items if i['sha']==OLD);self.assertEqual(old['evidence_state'],'failed-observed')

    def test_manual_resolution_never_changes_ci_and_new_evidence_reopens(self):
        self.add();i=self.issue()
        self.triage.update(i['id'],i['version'],'resolved','Reviewed and fixed in linked commit','PowerX')
        r=self.issue();self.assertEqual(r['workflow_state'],'resolved');self.assertEqual(r['evidence_state'],'failed-observed')
        with self.assertRaisesRegex(WatchError,'changed'):self.triage.update(i['id'],i['version'],'dismissed','Review was superseded','PowerX')
        self.add(object_id=10,attempt=2,body='new failing test')
        self.assertEqual(self.issue()['workflow_state'],'new');self.assertTrue(self.issue()['evidence_changed'])

    def test_target_validation_and_notes_do_not_accept_commands_or_publish(self):
        self.add();i=self.issue()
        with self.assertRaisesRegex(WatchError,'target'):self.triage.update(i['id'],i['version'],'resolved','A proper review note','PowerXPlugin')
        with self.assertRaisesRegex(WatchError,'note'):self.triage.update(i['id'],i['version'],'resolved','done','PowerX')
        with self.assertRaisesRegex(WatchError,'note'):self.triage.update(i['id'],i['version'],'merge','A proper review note','PowerX')
        self.triage.update(i['id'],i['version'],'investigating','password=never-print; only recorded as data','PowerX')
        self.assertNotIn('never-print',self.issue()['note'])

    def test_diagnosis_is_evidence_only_redacted_and_stale_after_new_comment(self):
        self.add(event='pull_request_review_comment',title='review comment',number=415,body='<script>run code</script> password=do-not-leak',path='backend/setup.go',line=42)
        i=self.issue()
        with patch('github_watch.subprocess.run',side_effect=AssertionError('No network or code')):
            report=self.triage.diagnose(i['id'],i['version'],'PowerX')
        self.assertEqual(report['locations'][0]['line'],42);self.assertEqual(report['network_requests'],0)
        self.assertFalse(report['code_execution']);self.assertIn('未确认代码根因',report['root_cause'])
        self.assertNotIn('do-not-leak',json.dumps(report));self.assertEqual(len(self.triage.listing()['diagnostics']),1)
        self.add(event='pull_request_review_comment',number=415,body='updated comment',object_id=10)
        self.assertTrue(self.triage.listing()['diagnostics'][0]['stale'])

    def test_real_current_snapshot_blocks_even_when_issue_manually_resolved(self):
        reader=Reader();reader.data['/commits/'+HEAD+'/check-runs?filter=latest&per_page=100']['check_runs'][0]['conclusion']='failure'
        snapshot=collect_project(settings(),reader)
        with self.store.db() as db:db.execute("UPDATE projects SET status='collected',success=?,snapshot=? WHERE id='PowerX'",(stamp(),json.dumps(snapshot)))
        self.add();i=self.issue();self.assertEqual(i['publish_signal'],'blocked')
        self.triage.update(i['id'],i['version'],'resolved','Manually reviewed this failure','PowerX')
        self.assertEqual(self.issue()['publish_signal'],'blocked')
        with self.store.db() as db:db.execute("UPDATE projects SET status='webhook-needs-refresh' WHERE id='PowerX'")
        self.assertEqual(self.issue()['head_binding'],'unconfirmed')

    def test_releases_are_not_bugs_and_deleted_release_is_retained_as_history(self):
        self.add(event='release',title='release · published · docker-dev-test',sha=None,object_id=1,status='prerelease',conclusion='',url='https://github.com/ArtisanCloud/PowerX/releases/tag/docker-dev-test')
        self.assertEqual(self.triage.listing()['issues'],[])
        self.add(event='release',title='release · deleted · docker-dev-test',sha=None,object_id=1,status='prerelease',action='deleted',conclusion='',url='https://github.com/ArtisanCloud/PowerX/releases/tag/docker-dev-test')
        self.assertEqual(len(self.triage.listing()['releases']),1)
        self.assertEqual(self.triage.listing()['releases'][0]['action'],'deleted')

    def test_handled_issue_archive_survives_notification_retention(self):
        self.add();i=self.issue();self.triage.update(i['id'],i['version'],'investigating','Preserve this investigation','PowerX')
        with self.store.db() as db:db.execute('DELETE FROM notifications')
        saved=self.issue();self.assertTrue(saved['archived']);self.assertEqual(saved['workflow_state'],'investigating')
        self.assertEqual(saved['head_binding'],'unconfirmed')

    def test_restart_preserves_notes_and_diagnostics(self):
        self.add();i=self.issue();self.triage.diagnose(i['id'],i['version'],'PowerX')
        self.triage.update(i['id'],i['version'],'investigating','Need to inspect the failed step','PowerX')
        restored=GitHubTriage(WatchStore(self.store.root.parent))
        self.assertEqual(restored.listing()['issues'][0]['workflow_state'],'investigating')
        self.assertEqual(len(restored.listing()['diagnostics']),1)


class HistoryTests(unittest.TestCase):
    def test_explicit_backfill_imports_baseline_comments_and_releases_once(self):
        with tempfile.TemporaryDirectory() as directory:
            reader=Reader();reader.data['/releases?per_page=10']=[{'id':8,'tag_name':'docker-dev-test','target_commitish':'develop','prerelease':True,'body':'password=never-print'}]
            watch=GitHubWatch(Path(directory).resolve(),reader_factory=lambda _:reader)
            watch.sync('PowerX',background=False);self.assertEqual(watch.listing()['unread'],1)
            watch.sync('PowerX',background=False,backfill=True)
            data=watch.listing();count=data['unread'];self.assertGreater(count,3)
            kinds={n['payload']['kind'] for n in data['notifications']};self.assertIn('comment',kinds);self.assertIn('release',kinds)
            self.assertNotIn('never-print',json.dumps(data))
            watch.sync('PowerX',background=False,backfill=True);self.assertEqual(watch.listing()['unread'],count)
            restored=GitHubWatch(Path(directory).resolve(),reader_factory=lambda _:reader)
            restored.sync('PowerX',background=False,backfill=True);self.assertEqual(restored.listing()['unread'],count)
            self.assertIsNone(next(n['payload'] for n in data['notifications'] if n['payload']['kind']=='release')['sha'])

    def test_receipt_capacity_refuses_new_history_without_losing_existing_notifications(self):
        with tempfile.TemporaryDirectory() as directory:
            watch=GitHubWatch(Path(directory).resolve(),reader_factory=lambda _:Reader())
            watch.sync('PowerX',background=False)
            with watch.store.db() as db:
                db.executemany('INSERT INTO observation_receipts VALUES(?,?,?)',[('PowerX','capacity-'+str(i),'fixture') for i in range(99999)])
            row=watch.store.settings('PowerX');snapshot=collect_project(settings(),Reader())
            with self.assertRaisesRegex(WatchError,'observation-history-full'):watch.store.finish(row,snapshot=snapshot,backfill=True)
            self.assertEqual(watch.listing()['unread'],1)

    def test_closed_pr_reviews_are_collected_only_by_history_mode(self):
        reader=Reader();reader.data['/pulls?state=all&sort=updated&direction=desc&per_page=10'][0]['state']='closed'
        self.assertFalse(any(k.startswith('review:') for k in collect_project(settings(),reader)['observations']))
        self.assertTrue(any(k.startswith('review:') for k in collect_project(settings(),reader,history=True)['observations']))

    def test_backfill_is_bounded_and_requires_an_explicit_call(self):
        with tempfile.TemporaryDirectory() as directory:
            watch=GitHubWatch(Path(directory).resolve(),reader_factory=lambda _:Reader())
            row=watch.store.settings('PowerX');snapshot=collect_project(settings(),Reader())
            snapshot['observations']={str(i):{'kind':'comment','title':'historical '+str(i),'url':'https://github.com/ArtisanCloud/PowerX/pull/1'} for i in range(125)}
            watch.store.finish(row,snapshot=snapshot);self.assertEqual(watch.listing()['unread'],1)
            watch.store.finish(row,snapshot=snapshot,backfill=True);self.assertEqual(watch.listing()['unread'],101)
            self.assertEqual(watch.store.settings('PowerX')['snapshot']['backfill']['remaining_in_snapshot'],25)
            watch.store.finish(row,snapshot=snapshot,backfill=True);self.assertEqual(watch.listing()['unread'],126)


class ReleaseIntakeTests(unittest.TestCase):
    setUp=hook_fixtures.IntakeTests.setUp
    send=hook_fixtures.IntakeTests.send
    # Exercise the real signed HTTP handler and queue importer with a new event kind.
    def test_signed_release_tag_is_fixed_safe_url_not_a_guessed_commit(self):
        data={'repository':{'full_name':'ArtisanCloud/PowerX'},'action':'prereleased','release':{'id':99,'tag_name':'dev/test','target_commitish':'develop','prerelease':True,'body':'password=never-print'}}
        self.assertEqual(self.send(data,event='release').status_code,202)
        self.consumer.consume();payload=self.store.listing()['notifications'][0]['payload']
        self.assertEqual(payload['url'],'https://github.com/ArtisanCloud/PowerX/releases/tag/dev%2Ftest')
        self.assertIsNone(payload['sha']);self.assertEqual(payload['status'],'prerelease');self.assertNotIn('never-print',payload['body'])
        data['release']['tag_name']='invalid\nrelease';self.assertEqual(self.send(data,event='release').status_code,400)


class TriageApiTests(unittest.TestCase):
    setUp=watch_fixtures.ApiTests.setUp
    def test_authenticated_explicit_controls_no_docker_no_github_writes(self):
        self.assertEqual(self.post_backfill(False).status_code,403)
        self.assertEqual(self.client.post('/api/github/action',json={'action':'backfill','project':'PowerX','confirmation':'wrong'},headers=self.headers).status_code,409)
        self.assertEqual(self.post_backfill().status_code,202);self.watch.worker.join(2)
        payload=self.client.get('/api/github').json;issue=payload['triage']['issues'][0]
        command={'action':'diagnose','issue':issue['id'],'version':issue['version'],'confirmation':'PowerX'}
        result=self.client.post('/api/github/action',json=command,headers=self.headers)
        self.assertEqual(result.status_code,200);self.assertFalse(result.json['code_execution'])
        self.assertEqual(self.engine.calls,[])
        command.update(action='issue-state',state='resolved',note='Reviewed the supplied evidence')
        self.assertEqual(self.client.post('/api/github/action',json=command,headers=self.headers).status_code,200)
        self.assertEqual(self.client.post('/api/github/action',json=command,headers=self.headers).status_code,409)

    def post_backfill(self,csrf=True):
        return self.client.post('/api/github/action',json={'action':'backfill','project':'PowerX','confirmation':'PowerX'},headers=self.headers if csrf else {'Origin':'http://localhost'})


if __name__=='__main__':unittest.main()
