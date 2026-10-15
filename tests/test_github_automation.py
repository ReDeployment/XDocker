"""Credential-gated startup, durable event coalescing, periodic checks and backoff."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/admin'))
from github_watch import GitHubWatch, WatchError, collect_project
from test_github_watch import Reader


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.now=10000
        self.clock=patch('github_watch.time.time',side_effect=lambda:self.now);self.clock.start();self.addCleanup(self.clock.stop)
        self.reader=Reader();self.watch=GitHubWatch(self.root,enabled=True,reader_factory=lambda _:self.reader)
        self.addCleanup(self.watch.close)
        for project in self.watch.store.listing()['projects']:
            if project['project']=='PowerX':continue
            self.watch.store.configure(project['project'],project['revision'],False,[],[])

    def state(self):
        with self.watch.store.db() as db:return dict(db.execute("SELECT * FROM auto_state WHERE project='PowerX'").fetchone())

    def pending(self):
        with self.watch.store.db() as db:return [dict(r) for r in db.execute('SELECT * FROM auto_requests')]

    def test_first_automatic_check_backfills_then_periodic_update_needs_no_browser(self):
        self.watch.prepare_auto(startup=True);self.assertTrue(self.watch.auto_step())
        self.assertEqual(self.watch.store.settings('PowerX')['status'],'collected')
        self.assertEqual(self.state()['initial_complete'],1)
        self.assertGreater(self.watch.store.listing()['unread'],1)
        count=len(self.reader.calls);self.assertFalse(self.watch.auto_step());self.assertEqual(len(self.reader.calls),count)
        # Periodic fallback also finds missed reviews after a PR was closed.
        self.reader.data['/pulls?state=all&sort=updated&direction=desc&per_page=10'][0]['state']='closed'
        self.reader.calls.clear();self.now+=301;self.assertTrue(self.watch.auto_step())
        self.assertIn('/pulls/1/reviews?per_page=100',self.reader.calls)

    def test_background_start_runs_without_browser_and_releases_leader_on_close(self):
        self.watch.start()
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and self.watch.store.settings('PowerX')['status']!='collected':time.sleep(.01)
        self.assertEqual(self.watch.store.settings('PowerX')['status'],'collected')
        self.assertTrue(self.watch.listing()['automation']['running'])
        other=GitHubWatch(self.root,enabled=True,reader_factory=lambda _:Reader());self.addCleanup(other.close)
        other.start();self.assertIsNone(other.leader)
        self.watch.close()
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and self.watch.leader is not None:time.sleep(.01)
        self.assertIsNone(self.watch.leader)

    def test_event_burst_coalesces_and_survives_restart_without_resetting_due_time(self):
        self.watch.auto_step();self.now+=100
        for _ in range(50):self.watch.store.queue_refresh('PowerX')
        self.assertEqual(len(self.pending()),1);due=self.pending()[0]['due_at']
        self.now+=14;self.assertFalse(self.watch.auto_step());self.assertEqual(self.pending()[0]['due_at'],due)
        self.now+=1
        other=GitHubWatch(self.root,enabled=True,reader_factory=lambda _:self.reader);self.addCleanup(other.close)
        self.assertTrue(other.auto_step());self.assertEqual(self.pending(),[])

    def test_event_during_collection_is_not_deleted_by_older_completion(self):
        old=self.watch.collect
        def arriving(settings,history=False):
            self.watch.store.queue_refresh('PowerX')
            return old(settings,history=history)
        with patch.object(self.watch,'collect',side_effect=arriving):self.assertTrue(self.watch.auto_step())
        self.assertEqual(len(self.pending()),1)
        self.now+=31;self.assertTrue(self.watch.auto_step());self.assertEqual(self.pending(),[])

    def test_global_collection_lease_preserves_queued_work(self):
        self.watch.prepare_auto();lease=self.watch.lease('collect')
        try:
            with patch.object(self.watch,'collect',side_effect=AssertionError('Busy work must not run')):
                self.assertFalse(self.watch.auto_step());self.assertEqual(len(self.pending()),1)
        finally:lease.close()
        self.assertTrue(self.watch.auto_step())

    def test_failed_requests_use_exponential_backoff_and_keep_original_snapshot(self):
        self.watch.auto_step();old=self.watch.store.settings('PowerX')['snapshot']
        self.now+=301
        with patch.object(self.watch,'collect',side_effect=WatchError('github-http-503')):
            self.assertTrue(self.watch.auto_step());row=self.watch.store.settings('PowerX')
            self.assertEqual(row['snapshot'],old);self.assertEqual(row['retry_at'],self.now+60)
            self.now+=31;self.assertFalse(self.watch.auto_step())
            self.now+=29;self.assertTrue(self.watch.auto_step());self.assertEqual(self.watch.store.settings('PowerX')['retry_at'],self.now+120)

    def test_remote_rate_limit_cooldown_is_not_shortened(self):
        with patch.object(self.watch,'collect',side_effect=WatchError('rate-limited',self.now+1200)):
            self.watch.auto_step()
        self.assertEqual(self.watch.store.settings('PowerX')['retry_at'],self.now+1200)
        self.now+=300;self.assertFalse(self.watch.auto_step())

    def test_missing_credentials_never_call_network_and_install_is_detected_automatically(self):
        self.watch.reader_factory=None
        with patch.object(self.watch,'collect',side_effect=AssertionError('No credential, no network')):
            self.assertFalse(self.watch.auto_step());self.assertFalse(self.watch.auto_step())
        self.assertEqual(self.watch.store.settings('PowerX')['status'],'credentials-required')
        self.assertIsNone(self.watch.store.settings('PowerX')['attempted_at'])
        self.assertEqual(self.watch.store.listing()['unread'],0)
        directory=self.watch.store.root/'credentials';directory.mkdir();file=directory/'artisancloud.token'
        file.write_text('github_pat_'+'x'*40);file.chmod(0o600)
        snapshot=collect_project(self.watch.store.settings('PowerX'),Reader(),history=True)
        with patch.object(self.watch,'collect',return_value=snapshot):self.assertTrue(self.watch.auto_step())
        self.assertEqual(self.watch.store.settings('PowerX')['status'],'collected')
        self.assertNotIn(file.read_text(),json.dumps(self.watch.listing()))

    def test_insecure_or_unreadable_credentials_do_not_break_existing_event_view(self):
        self.watch.reader_factory=None;directory=self.watch.store.root/'credentials';directory.mkdir();file=directory/'artisancloud.token'
        file.write_text('github_pat_'+'x'*40);file.chmod(0o644)
        self.assertFalse(self.watch.auto_step());self.assertEqual(self.watch.store.settings('PowerX')['status'],'github-credential-unsafe')
        with patch('github_watch.credential_status',side_effect=PermissionError('never echo this')):
            self.assertFalse(self.watch.listing()['projects'][0]['access']['configured'])
            self.assertFalse(self.watch.auto_step())
        self.assertEqual(self.watch.store.settings('PowerX')['status'],'github-credential-unreadable')

    def test_disabled_global_or_project_monitor_never_starts_work(self):
        self.watch.enabled=False
        with patch.object(self.watch,'collect',side_effect=AssertionError('Not opted in')):self.assertFalse(self.watch.auto_step())
        self.watch.enabled=True;p=self.watch.store.settings('PowerX');self.watch.store.configure('PowerX',p['revision'],False,[],[])
        self.watch.store.queue_refresh('PowerX')
        self.assertFalse(self.watch.auto_step());self.assertEqual(self.pending(),[])

    def test_initial_history_larger_than_one_batch_resumes_automatically(self):
        snapshot=collect_project(self.watch.store.settings('PowerX'),Reader(),history=True)
        snapshot['observations']={str(i):{'kind':'comment','title':'history '+str(i),'url':'https://github.com/ArtisanCloud/PowerX/pull/1'} for i in range(125)}
        with patch.object(self.watch,'collect',return_value=snapshot):
            self.watch.auto_step();self.assertEqual(self.state()['initial_complete'],0)
            self.now+=31;self.watch.auto_step();self.assertEqual(self.state()['initial_complete'],1)
        self.assertEqual(self.watch.store.listing()['unread'],125)

    def test_monitor_configuration_change_schedules_a_refresh(self):
        self.watch.auto_step();p=self.watch.store.settings('PowerX')
        self.watch.store.configure('PowerX',p['revision'],True,['develop'],['lint'])
        self.assertEqual(self.pending()[0]['reason'],'settings')
        self.now+=31;self.assertTrue(self.watch.auto_step());self.assertEqual(self.pending(),[])


if __name__=='__main__':unittest.main()
