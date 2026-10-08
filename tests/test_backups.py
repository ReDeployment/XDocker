import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/admin'))
from backups import InstanceBackups, FILES

class FakeEngine:
    def __init__(self):
        self.calls=[]
        self.values={}
        for n,s in enumerate(('backend','web-admin','postgres','redis')):
            identifier=str(n+1)*64
            self.values[identifier]={'State':{'Running':True},'Image':'sha256:'+str(n+1)*64,
                'Config':{'Image':'example:'+s,'Labels':{'com.docker.compose.project':'powerx-dev','com.docker.compose.service':s}},
                'Mounts':[{'Type':'bind','Destination':'/etc/powerx','Source':'/host/xdocker/instances/powerx-dev/config'},
                          {'Type':'bind','Destination':'/data','Source':'/host/xdocker/data/instances/powerx-dev/runtime'}]}
    def request(self,method,path,body=None,**kwargs):
        self.calls.append((method,path,body))
        identifier=path.split('/')[2]
        if method=='GET':return self.values[identifier]
        if '/stop?' in path:self.values[identifier]['State']['Running']=False
        if '/start' in path:self.values[identifier]['State']['Running']=True
        return {}

class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.engine=FakeEngine()
        self.manager=InstanceBackups(self.engine,self.root,'/host/xdocker')
        self.containers={value['Config']['Labels']['com.docker.compose.service']:{'Id':identifier} for identifier,value in self.engine.values.items()}
        self.identifier='a'*32
        def helper(image,command,mounts,network,job_id,**kwargs):
            self.helper_commands.append((command,mounts,network,kwargs))
            folder=self.manager.folder('powerx-dev',job_id)
            if 'pg_dump' in command[-1]:
                for name in FILES[:-3]+('config.tar.gz','runtime.tar.gz'):(folder/name).write_bytes(b'fixture-'+name.encode())
            if 'redis-cli' in command[-1]:(folder/'redis.rdb').write_bytes(b'fixture-rdb')
        self.helper_commands=[]
        self.manager.helper=helper

    def create(self):
        return self.manager.create('powerx-dev',self.containers,self.identifier)

    def test_full_backup_uses_pg_dump_and_restores_original_service_states(self):
        self.create()
        folder,record=self.manager.verify('powerx-dev',self.identifier)
        self.assertEqual(record['state'],'ready')
        self.assertTrue(all(v['State']['Running'] for v in self.engine.values.values()))
        stop_paths=[p for method,p,_ in self.engine.calls if '/stop?' in p]
        self.assertEqual(len(stop_paths),2)
        self.assertEqual(set(record['files']),set(FILES))
        script=self.helper_commands[0][0][-1]
        self.assertIn('pg_dump',script)
        self.assertIn('--format=custom',script)
        self.assertNotIn('/var/lib/postgresql/data',script)
        self.assertEqual(folder.stat().st_mode & 0o777,0o700)
        self.assertEqual((folder/'backup.tar.gz').stat().st_mode & 0o777,0o600)

    def test_failure_resumes_only_previously_running_application(self):
        web=self.containers['web-admin']['Id'];self.engine.values[web]['State']['Running']=False
        def fail(*a,**kw):raise ValueError('helper unavailable')
        self.manager.helper=fail
        with self.assertRaises(ValueError):self.create()
        self.assertTrue(self.engine.values[self.containers['backend']['Id']]['State']['Running'])
        self.assertFalse(self.engine.values[web]['State']['Running'])
        self.assertEqual(self.manager.read('powerx-dev',self.identifier)[1]['state'],'failed')

    def test_checksum_corruption_is_refused(self):
        self.create();folder=self.manager.folder('powerx-dev',self.identifier)
        (folder/'database.dump').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):self.manager.verify('powerx-dev',self.identifier)

    def test_wrong_mount_or_container_project_is_refused_before_stop(self):
        backend=self.containers['backend']['Id']
        self.engine.values[backend]['Mounts'][0]['Source']='/etc'
        with self.assertRaises(ValueError):self.create()
        self.assertFalse(any('/stop?' in p for _,p,_ in self.engine.calls))

    def test_traversal_symlinks_and_zero_retention_are_refused(self):
        with self.assertRaises(ValueError):self.manager.folder('../escape',self.identifier)
        with self.assertRaises(ValueError):self.manager.folder('powerx-dev','../../path')
        with self.assertRaises(ValueError):self.manager.prune('powerx-dev',0)

    def test_restore_drill_never_joins_live_network_or_mounts_live_data(self):
        self.create()
        calls=[]
        self.manager.helper=lambda *args,**kwargs:calls.append((args,kwargs))
        self.manager.drill('powerx-dev',self.identifier,'b'*32)
        args,kwargs=calls[0]
        self.assertEqual(args[3],'none')
        self.assertTrue(args[2][0]['ReadOnly'])
        self.assertEqual(args[2][0]['Target'],'/backup')
        self.assertIn('/var/lib/postgresql/data',kwargs['tmpfs'])
        self.assertEqual(self.manager.read('powerx-dev',self.identifier)[1]['last_restore_drill']['state'],'success')

    def test_restart_recovery_resumes_recorded_instance_and_marks_interrupted(self):
        folder=self.manager.folder('powerx-dev',self.identifier);folder.mkdir(parents=True)
        backend=self.containers['backend']['Id'];self.engine.values[backend]['State']['Running']=False
        self.manager.save(folder,{'id':self.identifier,'instance':'powerx-dev','state':'running','created':'2026-10-08','resume':[{'id':backend,'service':'backend'}]})
        self.manager.recover(['powerx-dev'])
        self.assertTrue(self.engine.values[backend]['State']['Running'])
        self.assertEqual(self.manager.read('powerx-dev',self.identifier)[1]['state'],'interrupted')

if __name__=='__main__':unittest.main()
