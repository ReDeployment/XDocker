"""Independent project registration, scoped systemd operations and native Admin isolation."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/admin'))
from host_metrics import atomic_json, read_json
from native_services import ServiceHub, ServiceError, SystemdServices, default_policy, validate_policy, fingerprint
from native_runtime import initialize, create_native_app
from docker_api import DockerError
from app import create_app
import test_admin


def project(**changes):
    return {'id':'example','revision':1,'name':'示例项目','endpoint':'example','domain':'example.home.test',
        'service':None,'monitor':None,'delivery':'external','tls':None,'enabled':True,**changes}


def policy(**changes):
    value=default_policy();value['revision']=2
    value['endpoints']['example']={'scheme':'http','host':'127.0.0.1','port':3000,'health_path':'/healthz','expected_status':200}
    value.update(changes);return value


class FakeSystemd:
    def __init__(self,unit_file):self.unit_file=unit_file;self.state='active';self.calls=[];self.timeout=False;self.reload=False
    def __call__(self,command,**kwargs):
        self.calls.append(command)
        if 'show' in command:
            return {'code':0,'truncated':False,'stdout':f'Id=example.service\nLoadState=loaded\nActiveState={self.state}\nSubState=running\nMainPID=42\nExecMainStatus=0\nFragmentPath={self.unit_file}\nDropInPaths=\nNeedDaemonReload={"yes" if self.reload else "no"}\n'}
        if command[0]=='journalctl':return {'code':0,'truncated':False,'stdout':'password=fixture-secret\nready'}
        if self.timeout:raise ServiceError('XD_SERVICE_COMMAND_TIMEOUT')
        self.state='inactive' if 'stop' in command else 'active'
        return {'code':0,'stdout':'','truncated':False}


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        self.hub=ServiceHub(self.root/'state',probe=lambda e:{'state':'healthy','at':time.time(),'status':200})
        self.hub.import_policy(policy())

    def test_registration_revision_domains_no_launcher_and_reopen(self):
        self.hub.save(project());self.assertEqual(self.hub.listing()['projects'][0]['delivery'],'external')
        with self.assertRaises(ServiceError):self.hub.save(project())
        with self.assertRaises(ServiceError):self.hub.save(project(id='other'))
        for change in ({'command':'npm run dev'},{'domain':'test;include /tmp/evil;'}, {'endpoint':'unknown'},{'monitor':'unknown'}):
            with self.assertRaises(ServiceError):self.hub.save(project(revision=2,**change))
        self.hub.save(project(revision=2,domain='changed.home.test'))
        reopened=ServiceHub(self.root/'state');self.assertEqual(reopened.listing()['projects'][0]['revision'],2)
        with self.assertRaises(ServiceError):reopened.remove('example',{'revision':1,'confirmation':'示例项目'})
        reopened.remove('example',{'revision':2,'confirmation':'示例项目'});self.assertEqual(reopened.listing()['projects'],[])

    def test_policy_blocks_injection_metadata_and_unpinned_service_writes(self):
        for address in ('metadata.internal','169.254.169.254','0.0.0.0','fe80::1'):
            value=policy();value['endpoints']['example']['host']=address
            with self.assertRaises(ServiceError):validate_policy(value)
        value=policy();value['endpoints']['example']['health_path']='/health?token=secret'
        with self.assertRaises(ServiceError):validate_policy(value)
        value=policy();value['services']['app']={'scope':'system','unit':'example.service','actions':['status','restart'],'definition_sha256':None}
        with self.assertRaises(ServiceError):validate_policy(value)
        value['services']['app']['definition_sha256']='a'*64;validate_policy(value)
        value['services']['app']['unit']='example.service;reboot'
        with self.assertRaises(ServiceError):validate_policy(value)

    def test_gateway_loop_and_existing_domain_are_rejected(self):
        value=policy();value['gateway']['listen']='0.0.0.0';value['endpoints']['example']['port']=8088;value['revision']=3
        self.hub.import_policy(value)
        with self.assertRaises(ServiceError):self.hub.save(project())
        other=ServiceHub(self.root/'other',domains=lambda:{'example.home.test'});other.import_policy(policy())
        with self.assertRaises(ServiceError):other.save(project())

    def test_health_is_versioned_and_does_not_survive_binding_changes(self):
        self.hub.save(project());self.assertEqual(self.hub.health('example')['state'],'healthy')
        self.assertIsNotNone(self.hub.listing()['projects'][0]['health'])
        self.hub.save(project(revision=2,domain='new.home.test'))
        self.assertIsNone(self.hub.listing()['projects'][0]['health'])

    def setup_service(self):
        unit=self.root/'example.service';unit.write_text('[Service]\nExecStart=/usr/bin/example\n')
        runner=FakeSystemd(unit);adapter=SystemdServices(runner)
        grant={'scope':'user','unit':'example.service','actions':['status','logs','restart','stop'],'definition_sha256':None}
        grant['definition_sha256']=adapter.status(grant)['definition_sha256']
        value=policy();value['revision']=3;value['services']['example']=grant;self.hub.import_policy(value);self.hub.adapter=adapter
        self.hub.save(project(service='example'))
        return unit,runner,grant

    def action_body(self,status,action='restart'):
        return {'action':action,'revision':status['revision'],'policy_sha256':status['policy_sha256'],
                'observation':status['observation'],'confirmation':'示例项目','idempotency_key':os.urandom(16).hex()}

    def test_exact_unit_grant_state_check_and_no_sudo(self):
        unit,runner,grant=self.setup_service();before=self.hub.status('example')
        result=self.hub.action('example',self.action_body(before,'stop'))
        self.assertEqual(result['state'],'observed');self.assertEqual(result['result']['state'],'inactive')
        self.assertTrue(any(c[:2]==['systemctl','--user'] and 'stop' in c and c[-1]=='example.service' for c in runner.calls))
        self.assertFalse(any('sudo' in c for c in runner.calls))
        self.assertIn('password=',self.hub.adapter.logs(grant)['text'])
        reopened=ServiceHub(self.root/'state');self.assertEqual(reopened.listing()['operations'][0]['state'],'observed')

    def test_changed_definition_stale_observation_and_timeout_never_replay(self):
        unit,runner,grant=self.setup_service();before=self.hub.status('example')
        unit.write_text('[Service]\nExecStart=/usr/bin/changed\n')
        with self.assertRaises(ServiceError):self.hub.action('example',self.action_body(before))
        self.assertFalse(any('restart' in c for c in runner.calls))
        # Re-pin explicitly, then a timeout leaves an immutable unconfirmed operation.
        value=self.hub.policy();value['revision']+=1;value['services']['example']['definition_sha256']=self.hub.adapter.status(grant)['definition_sha256'];self.hub.import_policy(value)
        before=self.hub.status('example');runner.timeout=True
        with self.assertRaises(ServiceError):self.hub.action('example',self.action_body(before))
        reopened=ServiceHub(self.root/'state');self.assertEqual(reopened.listing()['operations'][-1]['state'],'unconfirmed')
        self.assertEqual(sum('restart' in c for c in runner.calls),1)

    def test_identical_request_returns_receipt_without_restarting_again(self):
        unit,runner,grant=self.setup_service();body=self.action_body(self.hub.status('example'))
        first=self.hub.action('example',body);second=self.hub.action('example',body)
        self.assertEqual(first['id'],second['id']);self.assertEqual(sum('restart' in c for c in runner.calls),1)
        with self.assertRaises(ServiceError):self.hub.action('example',{**body,'action':'stop'})


class HubAPITests(unittest.TestCase):
    setUp=test_admin.AdminTests.setUp
    post=test_admin.AdminTests.post
    login=test_admin.AdminTests.login

    def test_auth_csrf_no_automatic_start_and_disabled_operations(self):
        self.assertEqual(self.client.get('/api/service-hub').status_code,401);self.login()
        self.app.extensions['service_hub'].import_policy(policy())
        self.assertEqual(self.post('/api/service-hub/projects',project(),csrf=False).status_code,403)
        result=self.post('/api/service-hub/projects',project());self.assertEqual(result.status_code,200);self.assertFalse(result.json['runtime_started'])
        self.assertEqual(self.post('/api/service-hub/projects/example/action',{}).status_code,409)
        self.assertFalse(any(method!='GET' for method,_,_ in self.docker.calls))
        self.assertEqual(self.post('/api/service-hub/gateway/apply',{}).status_code,409)


class NativeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()

    def test_private_initialization_is_idempotent_and_does_not_reuse_docker_state(self):
        value=initialize(self.root/'native',ROOT);token=(self.root/'native/access-token').read_text()
        initialize(self.root/'native',ROOT);self.assertFalse(value['docker_required'])
        self.assertEqual(token,(self.root/'native/access-token').read_text());self.assertEqual((self.root/'native/access-token').stat().st_mode&0o777,0o600)
        other=self.root/'docker';other.mkdir(mode=0o700);(other/'admin.sqlite3').write_text('existing')
        with self.assertRaises(ServiceError):initialize(other,ROOT)

    def test_full_native_auth_and_snapshot_work_without_docker(self):
        state=self.root/'native';initialize(state,ROOT)
        class DeadDocker:
            def request(self,method,path,*args,**kwargs):
                if method!='GET':raise AssertionError('Native startup attempted a runtime mutation')
                raise DockerError('Docker unavailable')
        with patch.dict(os.environ,{'ADMIN_NATIVE_STATE':str(state)},clear=False),patch('app.Docker',DeadDocker):app=create_native_app()
        client=app.test_client();login=client.post('/api/login',json={'token':(state/'access-token').read_text().strip()},headers={'Origin':'http://localhost'})
        self.assertEqual(login.status_code,200);response=client.get('/api/snapshot')
        self.assertEqual(response.status_code,200);self.assertEqual(response.json['runtime_mode'],'native');self.assertFalse(response.json['engine']['available'])
        self.assertEqual(client.get('/api/service-hub').status_code,200)
        result=client.post('/api/services/'+'a'*64+'/action',json={'action':'stop'},headers={'Origin':'http://localhost','X-CSRF-Token':login.json['csrf']})
        self.assertEqual(result.status_code,409)


if __name__=='__main__':unittest.main()
