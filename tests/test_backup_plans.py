"""Deterministic schedule admission tests; only temporary metadata and fake Docker."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/admin'))
from backup_plans import BackupPlans, BackupScheduler, OperationBusy, PlanConflict, defaults, validate, next_slot
from app import create_app
import backups

START=datetime(2026,10,11,18,59,tzinfo=timezone.utc)  # Shanghai Monday 02:59.
DUE=START+timedelta(minutes=1)
TOKEN='fixture-admin-token-'+'x'*40


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.allowed=['powerx-dev','powerx-prod']
        self.plans=BackupPlans(self.root,lambda:self.allowed)

    def enable(self,instance='powerx-dev',**fields):
        config=defaults();config.update(enabled=True,**fields)
        return self.plans.update(instance,config,'',START)

    def test_defaults_are_disabled_private_and_restricted_to_registered_instances(self):
        rows=self.plans.listing();self.assertTrue(all(not r['enabled'] and not r['auto_prune'] for r in rows))
        row=self.enable();file=self.root/'backup-plans.json'
        self.assertEqual(file.stat().st_mode&0o777,0o600)
        self.assertEqual(row['next_run_at'],DUE.isoformat())
        with self.assertRaises(KeyError):self.enable('../unrelated')
        self.allowed.remove('powerx-dev');self.assertNotIn('powerx-dev',[p['instance'] for p in self.plans.listing()])

    def test_daily_weekly_timezone_and_enabling_inside_window_are_future_only(self):
        plan=defaults()
        self.assertEqual(next_slot(plan,START),DUE)
        self.assertEqual(next_slot(plan,DUE),DUE+timedelta(days=1))
        plan.update(frequency='weekly',weekday=6)
        self.assertEqual(next_slot(plan,START),DUE+timedelta(days=6))
        plan.update(frequency='daily',timezone='UTC')
        self.assertEqual(next_slot(plan,START),datetime(2026,10,12,3,tzinfo=timezone.utc))
        row=self.plans.update('powerx-dev',{**defaults(),'enabled':True},'',DUE+timedelta(minutes=2))
        self.assertEqual(row['next_run_at'],(DUE+timedelta(days=1)).isoformat())

    def test_invalid_config_and_stale_edit_cannot_overwrite_plan(self):
        for field,value in [('keep',0),('keep',True),('grace_minutes',31),('time','3:00'),('weekday',7),('timezone','Europe/London'),('enabled','true')]:
            config=defaults();config[field]=value
            with self.assertRaises((ValueError,TypeError)):validate(config)
        row=self.enable()
        with self.assertRaises(PlanConflict):self.plans.update('powerx-dev',defaults(),'',START)
        self.assertEqual(self.plans.listing()[0]['revision'],row['revision'])

    def test_opt_in_and_consumed_slot_prevent_duplicate_execution_after_restart(self):
        self.enable();executed=[]
        def submit(plan,at):
            if self.plans.claim(plan['instance'],plan['revision'],plan['next_run_at'],'a'*32,at):executed.append(plan['instance'])
        disabled=BackupScheduler(self.plans,submit,enabled=False);disabled.tick(DUE);self.assertEqual(executed,[])
        scheduler=BackupScheduler(self.plans,submit,enabled=True);scheduler.tick(DUE);scheduler.tick(DUE+timedelta(seconds=30))
        self.assertEqual(executed,['powerx-dev'])
        restarted=BackupPlans(self.root,lambda:self.allowed);restarted.recover()
        row=restarted.listing()[0];self.assertEqual(row['last_run']['state'],'interrupted')
        BackupScheduler(restarted,submit,enabled=True).tick(DUE+timedelta(minutes=1));self.assertEqual(len(executed),1)

    def test_busy_admission_retries_only_inside_window_then_skips_once(self):
        self.enable();submit=Mock(side_effect=OperationBusy);audit=Mock()
        scheduler=BackupScheduler(self.plans,submit,enabled=True,audit=audit)
        scheduler.tick(DUE);scheduler.tick(DUE+timedelta(minutes=4));self.assertEqual(submit.call_count,2)
        scheduler.tick(DUE+timedelta(minutes=6));scheduler.tick(DUE+timedelta(minutes=7))
        self.assertEqual(submit.call_count,2);self.assertEqual(audit.call_count,1)
        self.assertEqual(self.plans.listing()[0]['last_skip']['reason'],'window-expired')

    def test_long_shutdown_and_backward_clock_never_backfill_consumed_slot(self):
        row=self.enable();submit=Mock();scheduler=BackupScheduler(self.plans,submit,True)
        scheduler.tick(DUE+timedelta(days=5,hours=1));submit.assert_not_called()
        next_run=datetime.fromisoformat(self.plans.listing()[0]['next_run_at']);self.assertGreater(next_run,DUE+timedelta(days=5))
        scheduler.tick(DUE);submit.assert_not_called()

    def test_disable_during_admission_cancels_and_running_job_status_is_retained(self):
        row=self.enable()
        self.plans.disable('powerx-dev',row['revision'],DUE)
        self.assertFalse(self.plans.claim('powerx-dev',row['revision'],row['next_run_at'],'a'*32,DUE))
        row=self.plans.update('powerx-dev',{**defaults(),'enabled':True},self.plans.listing()[0]['revision'],START)
        self.assertTrue(self.plans.claim('powerx-dev',row['revision'],row['next_run_at'],'a'*32,DUE))
        self.plans.disable('powerx-dev',row['revision'],DUE+timedelta(seconds=1))
        self.plans.stage('powerx-dev','a'*32,backup_state='ready');self.plans.finish('powerx-dev','a'*32,'success')
        current=self.plans.listing()[0];self.assertFalse(current['enabled']);self.assertEqual(current['last_run']['state'],'success')

    def test_two_stores_use_optimistic_revision_and_scheduler_leader_is_unique(self):
        self.enable();other=BackupPlans(self.root,lambda:self.allowed)
        stale=other.listing()[0];self.plans.disable('powerx-dev',stale['revision'],START)
        with self.assertRaises(PlanConflict):other.update('powerx-dev',defaults(),stale['revision'],START)
        first=BackupScheduler(self.plans,Mock(),True);second=BackupScheduler(other,Mock(),True)
        try:
            first.start();second.start();self.assertTrue(first.worker.is_alive());self.assertIsNone(second.worker);self.assertIsNotNone(second.error)
        finally:first.stop();second.stop()

    def test_corrupt_json_or_symlink_is_preserved_and_never_executed(self):
        file=self.root/'backup-plans.json';file.write_text('{broken')
        with self.assertRaises(ValueError):self.plans.listing()
        self.assertEqual(file.read_text(),'{broken');file.unlink()
        target=self.root/'other';target.write_text('untouched');file.symlink_to(target)
        with self.assertRaises(ValueError):self.enable()
        self.assertEqual(target.read_text(),'untouched')


class Engine:
    def __init__(self):self.calls=[]
    def request(self,method,path,body=None,**kwargs):
        self.calls.append((method,path,body))
        if path.startswith('/containers/json'):
            return [{'Id':str(n+1)*64,'Labels':{'com.docker.compose.project':'powerx-dev','com.docker.compose.service':service}}
                    for n,service in enumerate(('backend','web-admin','postgres','redis'))]
        raise AssertionError((method,path))


class PlanAPITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        for name in ('state','config/instances/powerx-dev','config/sites','reports'):(self.root/name).mkdir(parents=True)
        (self.root/'state/access-token').write_text(TOKEN)
        (self.root/'config/root.env').write_text('ENABLED_SITES=""\n')
        (self.root/'config/instances/powerx-dev/instance.json').write_text(json.dumps({'app':'powerx','project':'powerx-dev','enabled':True}))
        self.clock=[DUE]
        self.engine=Engine()
        self.app=create_app({'TESTING':True,'STATE':str(self.root/'state'),'CONFIG':str(self.root/'config'),'REPORTS':str(self.root/'reports'),
                             'BACKUPS':str(self.root/'backups'),'HOST_ROOT':'/host/xdocker','BACKUP_SCHEDULER_ENABLED':True,'BACKUP_SCHEDULER_CLOCK':lambda:self.clock[0]},self.engine)
        self.client=self.app.test_client();self.csrf=''
        self.plans=self.app.extensions['backup_plans'];self.scheduler=self.app.extensions['backup_scheduler']

    def login(self):
        response=self.client.post('/api/login',json={'token':TOKEN},headers={'Origin':'http://localhost'});self.csrf=response.json['csrf']

    def post(self,body,instance='powerx-dev',csrf=True):
        headers={'Origin':'http://localhost'}
        if csrf:headers['X-CSRF-Token']=self.csrf
        return self.client.post('/api/backup-plans/'+instance,json=body,headers=headers)

    def wait(self):
        for _ in range(200):
            rows=self.client.get('/api/jobs').json['jobs']
            if rows and rows[0]['state']!='running' and self.plans.listing()[0].get('last_run',{}).get('state') in ('success','failed'):return rows[0]
            time.sleep(.01)
        self.fail('Scheduled job did not finish')

    def test_auth_confirmation_acks_and_no_immediate_backup_on_save(self):
        self.assertEqual(self.client.get('/api/backup-plans').status_code,401);self.login()
        body={'action':'save','plan':{**defaults(),'enabled':True},'revision':'','confirmation':'powerx-dev'}
        self.assertEqual(self.post(body,csrf=False).status_code,403)
        self.assertEqual(self.post(body).status_code,400)
        body['maintenance_ack']=True;self.assertEqual(self.post(body,instance='unrelated').status_code,404)
        response=self.post(body);self.assertEqual(response.status_code,200)
        self.assertFalse(any(c[0]=='POST' for c in self.engine.calls));self.assertEqual(self.client.get('/api/jobs').json['jobs'],[])
        body['revision']=response.json['plan']['revision'];body['plan']['auto_prune']=True
        self.assertEqual(self.post(body).status_code,400);body['prune_ack']=True
        self.assertEqual(self.post(body).status_code,200)
        self.assertNotIn(TOKEN,self.client.get('/api/backup-plans').text)

    def test_due_backup_and_retention_are_recorded_once_with_verified_survivor(self):
        self.login();self.plans.update('powerx-dev',{**defaults(),'enabled':True,'auto_prune':True},'',START)
        with patch.object(backups.InstanceBackups,'create',return_value='fixture backup ready') as create,patch.object(backups.InstanceBackups,'verify') as verify,patch.object(backups.InstanceBackups,'prune',return_value='fixture cleanup') as prune:
            self.scheduler.tick(DUE);job=self.wait();self.scheduler.tick(DUE+timedelta(seconds=1))
            self.assertEqual(job['state'],'success');self.assertEqual(create.call_count,1);self.assertEqual(verify.call_count,1)
            prune.assert_called_once_with('powerx-dev',7,require_valid_survivor=True)
        row=self.client.get('/api/backup-plans').json['plans'][0];self.assertEqual(row['last_run']['backup_state'],'ready');self.assertEqual(row['last_run']['cleanup_state'],'success')

    def test_failure_never_prunes_or_retries_and_manual_busy_never_overlaps(self):
        self.login();self.plans.update('powerx-dev',{**defaults(),'enabled':True,'auto_prune':True},'',START)
        block=threading.Event();entered=threading.Event()
        def failing(*args):entered.set();block.wait(2);raise ValueError('fixture failure')
        with patch.object(backups.InstanceBackups,'create',side_effect=failing) as create,patch.object(backups.InstanceBackups,'prune') as prune:
            self.scheduler.tick(DUE);self.assertTrue(entered.wait(1));self.scheduler.tick(DUE)
            response=self.client.post('/api/backups/action',json={'instance':'powerx-dev','action':'create','confirmation':'powerx-dev'},headers={'Origin':'http://localhost','X-CSRF-Token':self.csrf})
            self.assertEqual(response.status_code,409);block.set();self.assertEqual(self.wait()['state'],'failed');self.scheduler.tick(DUE+timedelta(minutes=1))
            self.assertEqual(create.call_count,1);prune.assert_not_called()

    def test_cleanup_failure_preserves_ready_backup_and_reports_partial_result(self):
        self.login();self.plans.update('powerx-dev',{**defaults(),'enabled':True,'auto_prune':True},'',START)
        with patch.object(backups.InstanceBackups,'create',return_value='ready'),patch.object(backups.InstanceBackups,'verify'),patch.object(backups.InstanceBackups,'prune',side_effect=ValueError('private-secret')):
            self.scheduler.tick(DUE);job=self.wait()
        self.assertEqual(job['state'],'failed');self.assertNotIn('private-secret',job['output'])
        row=self.client.get('/api/backup-plans').json['plans'][0];self.assertEqual(row['last_run']['backup_state'],'ready');self.assertEqual(row['last_run']['cleanup_state'],'failed')

    def test_runtime_gate_off_never_submits_an_enabled_plan(self):
        self.plans.update('powerx-dev',{**defaults(),'enabled':True},'',START)
        self.scheduler.enabled=False
        with patch.object(backups.InstanceBackups,'create') as create:self.scheduler.tick(DUE);create.assert_not_called()

    def test_scheduled_job_detail_is_authenticated_and_survives_recent_list_limit(self):
        self.assertEqual(self.client.get('/api/jobs/'+'a'*32).status_code,401);self.login()
        self.plans.update('powerx-dev',{**defaults(),'enabled':True},'',START)
        with patch.object(backups.InstanceBackups,'create',return_value='ready'),patch.object(backups.InstanceBackups,'verify'):
            self.scheduler.tick(DUE);job=self.wait()
        with sqlite3.connect(self.root/'state/admin.sqlite3') as database:
            for index in range(31):
                database.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',
                    (f'{index+1:032x}','probe','fixture','check','success','2099-01-01T00:00:00+00:00','2099-01-01T00:00:01+00:00','fixture'))
        self.assertNotIn(job['id'],[j['id'] for j in self.client.get('/api/jobs').json['jobs']])
        self.assertEqual(self.client.get('/api/jobs/'+job['id']).json['job']['state'],'success')
        self.assertEqual(self.client.get('/api/jobs/not-a-job').status_code,404)
        self.assertEqual(self.client.get('/api/jobs/'+'0'*32).status_code,404)

    def test_worker_start_failure_consumes_slot_without_pause_and_releases_operation_lock(self):
        self.login();self.plans.update('powerx-dev',{**defaults(),'enabled':True},'',START)
        worker=Mock();worker.start.side_effect=RuntimeError('fixture start failure')
        with patch('app.threading.Thread',return_value=worker),patch.object(backups.InstanceBackups,'create') as create:
            with self.assertRaises(RuntimeError):self.scheduler.tick(DUE)
            create.assert_not_called()
        self.assertEqual(self.plans.listing()[0]['last_run']['state'],'failed')
        # A later manual operation can be admitted; no lock remains leaked.
        with patch.object(backups.InstanceBackups,'create',return_value='manual ready'):
            response=self.client.post('/api/backups/action',json={'instance':'powerx-dev','action':'create','confirmation':'powerx-dev'},headers={'Origin':'http://localhost','X-CSRF-Token':self.csrf})
            self.assertEqual(response.status_code,202)
            for _ in range(200):
                events=self.client.get('/api/audit').json['events']
                if any(e['action']=='backup-create' and e['result']=='success' for e in events):break
                time.sleep(.01)
            else:self.fail('Manual job finalization did not finish before fixture cleanup')
        self.assertEqual(response.status_code,202)

    def test_manual_backup_first_defers_schedule_without_consuming_slot(self):
        self.login();self.plans.update('powerx-dev',{**defaults(),'enabled':True},'',START)
        entered=threading.Event();release=threading.Event();calls=[]
        def create(*args):
            calls.append(args)
            if len(calls)==1:entered.set();release.wait(2)
            return 'fixture ready'
        with patch.object(backups.InstanceBackups,'create',side_effect=create),patch.object(backups.InstanceBackups,'verify'):
            self.client.post('/api/backups/action',json={'instance':'powerx-dev','action':'create','confirmation':'powerx-dev'},headers={'Origin':'http://localhost','X-CSRF-Token':self.csrf})
            self.assertTrue(entered.wait(1));self.scheduler.tick(DUE)
            self.assertEqual(len(calls),1);self.assertEqual(self.plans.listing()[0]['next_run_at'],DUE.isoformat())
            release.set()
            for _ in range(200):
                self.scheduler.tick(DUE)
                if self.plans.listing()[0].get('last_run'):break
                time.sleep(.01)
            self.assertEqual(self.wait()['state'],'success');self.assertEqual(len(calls),2)

    def test_admission_delayed_beyond_window_never_stops_application(self):
        self.login();self.plans.update('powerx-dev',{**defaults(),'enabled':True},'',START)
        self.clock[0]=DUE+timedelta(minutes=6)
        with patch.object(backups.InstanceBackups,'create') as create:
            self.scheduler.tick(DUE)
            create.assert_not_called()
        row=self.client.get('/api/jobs').json['jobs'][0];self.assertEqual(row['state'],'cancelled')
        self.scheduler.tick();self.assertEqual(self.plans.listing()[0]['last_skip']['reason'],'window-expired')
