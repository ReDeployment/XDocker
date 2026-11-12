"""Monitor registration does not expand runtime authority; stale/partial data fail closed."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/admin'))
from host_metrics import atomic_json, private_directory, write_history
from resource_monitor import ResourceMonitor, MonitorError, report_read
import test_admin

FIRST='d'*64
SECOND='e'*64


def sample_report():
    return {'schema':1,'at':time.time(),'host':{'id':'a'*64,'hostname':'fixture-host','cpu_logical':16,'cpu_percent':12,
        'memory_total':64*1073741824,'memory_available':48*1073741824,'swap_used':0,'load':[.1,.2,.3],
        'disk_total':1024*1073741824,'disk_free':800*1073741824},
        'targets':[{'id':identifier,'kind':kind,'name':name,'description':'/work/project' if kind=='process' else 'example:v1',
                    'state':'running','cpu_percent':10,'memory_bytes':1000000,'pids':2,'memory_kind':'rss' if kind=='process' else 'working-set','complete':True}
                   for identifier,kind,name in [(FIRST,'process','powerx'),(SECOND,'docker','project/web')]],
        'processes':[{'pid':42,'ppid':1,'target':FIRST,'name':'powerx','cpu_percent':10,'memory_bytes':1000000}],
        'errors':[],'coverage':{'processes_scanned':2,'processes_unreadable':1},'_counters':{'private':'not-public'}}


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        self.reports=private_directory(self.root/'reports');self.report=sample_report();self.write()
        self.monitor=ResourceMonitor(self.root/'state',self.reports)

    def write(self):atomic_json(self.reports/'report.json',self.report)
    def save(self,**extra):return self.monitor.save({'id':'project','name':'项目','revision':1,'targets':[FIRST,SECOND],**extra})

    def test_binding_persistence_revision_scope_and_unknown_input(self):
        self.save();listing=self.monitor.listing();self.assertEqual(listing['apps'][0]['memory_bytes'],2000000)
        self.assertNotIn('_counters',json.dumps(listing));self.assertNotIn('not-public',json.dumps(listing))
        reopened=ResourceMonitor(self.root/'state',self.reports);self.assertEqual(reopened.listing()['apps'][0]['id'],'project')
        for body in ({'revision':1},{'revision':2,'targets':['f'*64]},{'id':'host'},{'command':'rm -rf /'}):
            with self.assertRaises(MonitorError):self.save(**body)
        with self.assertRaises(MonitorError):self.save(id='another')
        self.save(revision=2,targets=[FIRST]);self.assertEqual(self.monitor.listing()['apps'][0]['revision'],2)
        with self.assertRaises(MonitorError):self.monitor.remove('project',{'revision':1,'confirmation':'项目'})
        self.monitor.remove('project',{'revision':2,'confirmation':'项目'});self.assertEqual(self.monitor.listing()['apps'],[])

    def test_stale_missing_capacity_and_wrong_host_never_show_zero(self):
        self.save()
        for mutate in ('stale','missing','partial','capacity','host'):
            self.report=sample_report()
            if mutate=='stale':self.report['at']-=200
            if mutate=='missing':self.report['targets'].pop()
            if mutate=='partial':self.report['targets'][1]['complete']=False
            if mutate=='capacity':self.report['coverage']['process_limit_reached']=True
            if mutate=='host':self.report['host']['id']='b'*64
            self.write();app=self.monitor.listing()['apps'][0]
            self.assertEqual(app['status'],'unconfirmed',mutate);self.assertIsNone(app['memory_bytes'],mutate)
        self.report=sample_report();self.report['at']-=200;self.write()
        with self.assertRaises(MonitorError):self.save(revision=2)

    def test_history_requires_all_components_and_covers_rolling_samples(self):
        self.save();write_history(self.reports,self.report)
        at=int(time.time()//300)*300-600
        with sqlite3.connect(self.reports/'history.sqlite3') as db:
            db.executemany('INSERT INTO samples VALUES(?,?,?,?,?)',[(at,FIRST,10,100,1),(at,SECOND,20,200,2),(at-300,FIRST,90,900,1)])
        result=self.monitor.history('project');point=next(p for p in result['points'] if p['at']==at)
        self.assertEqual(point['memory_bytes'],300);self.assertEqual(point['cpu_percent'],30)
        self.assertNotIn(at-300,[p['at'] for p in result['points']])
        self.assertEqual(self.monitor.history('host')['window_seconds'],86400)

    def test_unsafe_or_malformed_reports_do_not_leak_or_overwrite(self):
        self.report['host']['cpu_percent']=float('nan')
        with self.assertRaises(ValueError):self.write()
        self.report=sample_report();self.write();(self.reports/'report.json').chmod(0o644)
        self.assertIsNone(self.monitor.listing()['report'])
        (self.reports/'report.json').unlink();(self.reports/'report.json').symlink_to(self.root/'unrelated')
        self.assertIsNone(self.monitor.listing()['report'])


class MonitorAPITests(unittest.TestCase):
    # Reuse fixture helpers without rediscovering and repeating the legacy job tests.
    setUp = test_admin.AdminTests.setUp
    post = test_admin.AdminTests.post
    login = test_admin.AdminTests.login
    def test_resource_monitor_auth_csrf_registration_and_no_runtime_grant(self):
        self.assertEqual(self.client.get('/api/resources').status_code,401)
        self.login();reports=private_directory(self.root.resolve()/'host-metrics')
        atomic_json(reports/'report.json',sample_report())
        self.app.extensions['resource_monitor'].reports=reports
        body={'id':'project','name':'项目','revision':1,'targets':[SECOND]}
        self.assertEqual(self.post('/api/resources/apps',body,csrf=False).status_code,403)
        self.assertEqual(self.post('/api/resources/apps',body,origin=False).status_code,403)
        self.assertEqual(self.post('/api/resources/apps',body).status_code,200)
        self.assertFalse(self.client.get('/api/resources').json['runtime_control'])
        # A registered monitoring target is not a Docker lifecycle target.
        self.assertEqual(self.post('/api/services/'+'b'*64+'/action',{'action':'stop','confirmation':'nginx'}).status_code,404)
        self.assertFalse(any(method!='GET' for method,_,_ in self.docker.calls))
        self.assertEqual(self.post('/api/resources/apps/project/remove',{'revision':1,'confirmation':'项目'}).status_code,200)


if __name__=='__main__':unittest.main()
