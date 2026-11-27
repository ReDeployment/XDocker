"""Independent project registration, scoped systemd operations and native Admin isolation."""
import copy
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/admin'))
from host_metrics import atomic_json, read_json
from native_services import ServiceHub, ServiceError, SystemdServices, default_policy, validate_policy, fingerprint, probe_endpoint
from native_runtime import initialize, create_native_app, native_trusted_hosts
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
            return {'code':0,'truncated':False,'stdout':f'Id={command[-1]}\nLoadState=loaded\nActiveState={self.state}\nSubState=running\nMainPID=42\nExecMainStatus=0\nFragmentPath={self.unit_file}\nDropInPaths=\nNeedDaemonReload={"yes" if self.reload else "no"}\n'}
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

    def test_standard_gateway_ports_and_explicit_unit_capability(self):
        value=policy();value['gateway'].update(http_port=80,https_port=443);validate_policy(value)
        for field,port in (('http_port',22),('http_port',443),('https_port',80),('https_port',True),('http_port',0)):
            bad=copy.deepcopy(value);bad['gateway'][field]=port
            with self.assertRaises(ServiceError):validate_policy(bad)
        command=[sys.executable,str(ROOT/'scripts/admin-native-unit.py'),'--checkout','/data/workspace/XDocker','--state','/data/workspace/XDocker/data/native-admin','--python','/data/workspace/XDocker/.venv/bin/python','--user','michael','--group','michael']
        default=subprocess.check_output(command,text=True);self.assertNotIn('AmbientCapabilities=',default)
        self.assertNotEqual(subprocess.run([*command,'--gateway-web-ports'],capture_output=True).returncode,0)
        unit=subprocess.check_output([*command,'--operations','--gateway-web-ports'],text=True)
        self.assertIn('User=michael\n',unit);self.assertIn('NoNewPrivileges=yes\n',unit)
        self.assertIn('Wants=network-online.target\nAfter=network-online.target\n',unit)
        self.assertIn('AmbientCapabilities=CAP_NET_BIND_SERVICE\nCapabilityBoundingSet=CAP_NET_BIND_SERVICE\n',unit)
        domain_unit=subprocess.check_output([*command,'--admin-domain','xdocker.home.test'],text=True)
        self.assertIn('--admin-domain xdocker.home.test\n',domain_unit)
        self.assertNotEqual(subprocess.run([*command,'--admin-domain','*.home.test'],capture_output=True).returncode,0)

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

    def test_domain_host_probe_and_last_failure_survive_reload(self):
        import threading
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                received.append(self.headers.get('Host'));self.send_response(403 if self.headers.get('Host')=='blocked.home.test' else 200);self.end_headers()
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            value=policy();value['revision']=3;value['endpoints']['example']['port']=server.server_port
            self.hub.import_policy(value);self.hub.save(project(domain='blocked.home.test'));self.hub.probe=probe_endpoint
            result=self.hub.health('example');self.assertEqual(result['status'],403);self.assertEqual(result['state'],'unexpected-status');self.assertEqual(received,['blocked.home.test'])
            reopened=ServiceHub(self.root/'state')
            with patch('native_services.time.time',return_value=time.time()+60):
                row=reopened.listing()['projects'][0];self.assertIsNone(row['health']);self.assertEqual(row['last_health']['status'],403)
            reopened.save(project(revision=2,domain='allowed.home.test'));self.assertIsNone(reopened.listing()['projects'][0]['last_health'])
            self.assertEqual(reopened.health('example')['status'],200)
        finally:server.shutdown();worker.join(timeout=3);server.server_close()

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

    def batch_fixture(self):
        _,runner,grant=self.setup_service()
        value=self.hub.policy();value['revision']+=1
        for alias in ('backend','worker'):
            value['services'][alias]={**grant,'unit':alias+'.service','actions':['status','logs'],'definition_sha256':None}
        self.hub.import_policy(value)
        self.hub.save(project(id='other',domain='other.home.test'))
        entries=[{'alias':a,'project':'example','revision':1} for a in ('backend','worker')]
        return runner,{'policy_sha256':fingerprint(value),'bindings':entries}

    def test_batch_is_atomic_idempotent_and_does_not_touch_runtime_or_gateway_inputs(self):
        runner,body=self.batch_fixture()
        original=self.hub.data();runner.calls.clear()
        for override in ({'alias':'missing'},{'project':'missing'},{'revision':5},{'alias':'backend'}):
            bad=copy.deepcopy(body);bad['bindings'][1].update(override)
            with self.assertRaises(ServiceError):self.hub.bind_services(bad)
            self.assertEqual(self.hub.store.additional_bindings(),{})
        with self.assertRaises(ServiceError):self.hub.bind_services({**body,'policy_sha256':'changed'})
        self.assertEqual(self.hub.bind_services(body),{'added':2,'already_bound':0})
        self.assertEqual(self.hub.bind_services(body),{'added':0,'already_bound':2})
        self.assertEqual(runner.calls,[])
        reopened=ServiceHub(self.root/'state');self.assertEqual(reopened.data(),original)
        self.assertEqual(reopened.listing()['projects'][0]['bound_services'],['example','backend','worker'])
        with self.assertRaisesRegex(ServiceError,'ALREADY_BOUND'):
            reopened.save(project(id='other',domain='other.home.test',revision=2,service='backend'))
        with self.assertRaisesRegex(ServiceError,'ALREADY_BOUND'):
            reopened.bind_services({**body,'bindings':[{**body['bindings'][0],'project':'other'}]})
        reopened.save(project(service='example',revision=2))
        self.assertEqual(len(reopened.store.additional_bindings()),2)
        reopened.remove('example',{'revision':2,'confirmation':'示例项目'})
        self.assertEqual(reopened.store.additional_bindings(),{})

    def test_sql_error_rolls_back_every_addition(self):
        from registry_store import RegistryError
        _,body=self.batch_fixture()
        self.hub.store.add_bindings([])
        with self.hub.store.connection(write=True) as db:
            db.execute("""CREATE TRIGGER reject_worker BEFORE INSERT ON project_service_bindings WHEN NEW.alias='worker'
                        BEGIN SELECT RAISE(ABORT,'fixture failure'); END""")
        with self.assertRaises(RegistryError):self.hub.bind_services(body)
        self.assertEqual(self.hub.store.additional_bindings(),{})

    def test_overlapping_batches_cannot_claim_the_same_grant(self):
        runner,body=self.batch_fixture()
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier=Barrier(2)
        def claim(project_id):
            hub=ServiceHub(self.root/'state')
            barrier.wait(timeout=3)
            try:
                hub.bind_services({**body,'bindings':[{'alias':'backend','project':project_id,'revision':1}]})
                return 'bound'
            except ServiceError as exc:return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(claim,['example','other']))
        self.assertEqual(results.count('bound'),1)
        self.assertTrue(set(results)<={'bound','XD_SERVICE_BUSY','XD_SERVICE_UNIT_ALREADY_BOUND'})
        self.assertIn(self.hub.store.additional_bindings()['backend'],('example','other'))

    def test_scoped_target_access_revocation_and_operation_identity(self):
        runner,body=self.batch_fixture();self.hub.bind_services(body);runner.calls.clear()
        with self.assertRaisesRegex(ServiceError,'BINDING_UNAVAILABLE'):self.hub.status('other','backend')
        self.assertEqual(runner.calls,[])
        status=self.hub.status('example','backend')
        self.assertEqual(status['unit'],'backend.service')
        self.assertEqual(runner.calls[-1][-1],'backend.service')
        with self.assertRaisesRegex(ServiceError,'ACTION_NOT_GRANTED'):
            self.hub.action('example',{**self.action_body(status),'confirmation':'backend.service'},'backend')
        primary=self.hub.status('example','example')
        action={**self.action_body(primary),'confirmation':'example.service'}
        receipt=self.hub.action('example',action,'example')
        self.assertEqual(receipt['result']['unit'],'example.service')
        with self.assertRaisesRegex(ServiceError,'IDEMPOTENCY_CONFLICT'):
            self.hub.action('example',action,'backend')
        self.assertEqual(len([c for c in runner.calls if 'restart' in c]),1)
        policy=self.hub.policy();policy['revision']+=1;del policy['services']['backend'];self.hub.import_policy(policy)
        with self.assertRaisesRegex(ServiceError,'BINDING_UNAVAILABLE'):self.hub.status('example','backend')
        self.assertEqual(self.hub.status('example')['unit'],'example.service')

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

    def test_batch_endpoint_requires_login_csrf_and_existing_grants(self):
        hub=self.app.extensions['service_hub']
        value=policy();value['services']['api']={'scope':'user','unit':'api.service','actions':['status'],'definition_sha256':None}
        hub.import_policy(value);hub.save(project())
        body={'policy_sha256':fingerprint(value),'bindings':[{'project':'example','revision':1,'alias':'api'}]}
        self.assertEqual(self.post('/api/service-hub/bindings',body,csrf=False).status_code,401)
        preview='/api/service-hub/bindings/preview'
        self.assertEqual(self.client.get(preview).status_code,401)
        self.login();self.assertEqual(self.post('/api/service-hub/bindings',body,csrf=False).status_code,403)
        row,=self.client.get(preview).json['rows']
        self.assertEqual((row['alias'],row['suggested']),('api',None))
        self.assertFalse(hub.store.additional_bindings())
        self.assertEqual(self.post('/api/service-hub/bindings',body).json['added'],1)
        self.assertEqual(self.client.get(preview).json['rows'],[])
        self.assertEqual(self.client.get('/api/service-hub/projects/example/services/api/logs').json['error'],'XD_SERVICE_ACTION_NOT_GRANTED')
        self.assertEqual(self.client.get('/api/service-hub/projects/example/services/unknown/status').status_code,409)

    def test_host_discovery_does_not_grant_binding_or_logs(self):
        path = '/api/service-hub?project_services=1'
        self.assertEqual(self.client.get(path).status_code, 401)
        self.login()
        hub = self.app.extensions['service_hub']
        value = policy()
        hub.import_policy(value)
        directory = hub.root.parent / 'work' / 'example'
        working = directory / 'backend'
        working.mkdir(parents=True)
        hub.save(project(directory=str(directory)))
        def reader(command, **kwargs):
            output = ''
            if command[0] == 'systemctl' and '--user' in command:
                output = f'Id=worker.service\nLoadState=loaded\nWorkingDirectory={working}'
            return {'code': 0, 'stdout': output, 'truncated': False}
        self.app.config.update(NATIVE_MODE=True, SERVICE_HUB_MATCH_RUNNER=reader)
        self.assertNotIn('project_services', self.client.get('/api/service-hub').json)
        result = self.client.get(path).json
        candidate, = result['project_services']['candidates']['example']
        self.assertEqual((candidate['unit'], candidate['scope'], candidate['alias']), ('worker.service', 'user', None))
        self.assertTrue(candidate['suggested'])
        self.assertFalse(result['services'])
        body = {'policy_sha256': fingerprint(value), 'bindings': [{'project': 'example', 'revision': 1, 'alias': 'worker'}]}
        self.assertEqual(self.post('/api/service-hub/bindings', body).status_code, 409)
        for action in ('status', 'logs'):
            self.assertEqual(self.client.get('/api/service-hub/projects/example/services/worker/' + action).status_code, 409)
        self.assertFalse(hub.store.additional_bindings())

    def test_web_read_grant_rechecks_host_and_preserves_gateway(self):
        hub = self.app.extensions['service_hub']
        hub.import_policy(policy())
        directory = hub.root.parent / 'work' / 'example'
        working = directory / 'backend'
        working.mkdir(parents=True)
        hub.save(project(directory=str(directory)))
        def reader(command, **kwargs):
            output = ''
            if command[0] == 'systemctl':
                units = ['worker.service', 'xdocker-native.service', 'example-name.service']
                output = '\n\n'.join('Id=' + u + '\nLoadState=loaded\nWorkingDirectory=' +
                                     ('/other' if 'name' in u else str(working)) for u in units)
            return {'code': 0, 'stdout': output, 'truncated': False}
        self.app.config.update(NATIVE_MODE=True, SERVICE_HUB_MATCH_RUNNER=reader)
        path = '/api/service-hub/projects/example/read-grants'
        body = {'revision': 1, 'policy_sha256': fingerprint(hub.policy()), 'unit': 'worker.service', 'scope': 'user'}
        self.assertEqual(self.post(path, body, csrf=False).status_code, 401)
        self.login()
        self.assertEqual(self.post(path, body, csrf=False).status_code, 403)
        self.assertEqual(self.post(path, body).json['error'], 'XD_SERVICE_OPERATIONS_DISABLED')
        self.app.config['NATIVE_OPERATIONS_ENABLED'] = True
        hub.probe = lambda endpoint: {'state': 'healthy', 'status': 200, 'at': time.time()}
        hub.health('example')
        before = self.app.extensions['native_gateway'].preview()
        for change, code in [
                ({'actions': ['restart']}, 'XD_SERVICE_GRANT_INVALID'),
                ({'scope': 'system'}, 'XD_SERVICE_READ_MATCH_REQUIRED'),
                ({'unit': 'xdocker-native.service'}, 'XD_SERVICE_READ_MATCH_REQUIRED'),
                ({'unit': 'absent.service'}, 'XD_SERVICE_READ_MATCH_REQUIRED'),
                ({'unit': 'example-name.service'}, 'XD_SERVICE_READ_MATCH_REQUIRED'),
                ({'revision': 2}, 'XD_SERVICE_REVISION_CONFLICT'),
                ({'policy_sha256': 'stale'}, 'XD_SERVICE_CONFIGURATION_CHANGED')]:
            self.assertEqual(self.post(path, {**body, **change}).json['error'], code)
        self.assertFalse(hub.policy()['services'])
        with patch.dict(self.app.config, SERVICE_HUB_MATCH_RUNNER=lambda *a, **kw: {'code': 1, 'truncated': False, 'stdout': ''}):
            self.assertEqual(self.post(path, body).json['error'], 'XD_SERVICE_READ_MATCH_REQUIRED')
        result = self.post(path, body)
        self.assertEqual(result.status_code, 201)
        alias = result.json['grant']['alias']
        self.assertEqual(hub.policy()['services'][alias], {'actions': ['status', 'logs'], 'scope': 'user',
                         'unit': 'worker.service', 'definition_sha256': None})
        self.assertFalse(hub.listing()['projects'][0]['bound_services'])
        self.assertEqual(self.client.get('/api/service-hub/projects/example/services/' + alias + '/logs').status_code, 409)
        self.assertEqual(self.post(path, body).json['error'], 'XD_SERVICE_CONFIGURATION_CHANGED')
        self.assertEqual(hub.policy()['revision'], 3)
        from gateway_runtime import GatewayRuntime
        reopened = ServiceHub(hub.root.parent)
        self.assertEqual(reopened.listing()['projects'][0]['health']['state'], 'healthy')
        after = GatewayRuntime(reopened).preview()
        self.assertEqual(before['fingerprint'], after['fingerprint'])
        self.assertEqual(before['configuration'], after['configuration'])
        self.assertNotEqual(before['policy_sha256'], after['policy_sha256'])
        reopened.import_policy(policy(revision=4))
        self.assertFalse(hub.policy()['services'])
        self.assertEqual(*hub.policies())

    def test_batch_read_grants_are_atomic_and_leave_projects_unbound(self):
        hub = self.app.extensions['service_hub']
        hub.import_policy(policy())
        projects = ['first', 'second']
        work = hub.root.parent / 'work'
        for name in projects:
            (work / name / 'web').mkdir(parents=True)
            hub.save(project(id=name, domain=name + '.home.test', directory=str(work / name)))
        workspace_alias = work.with_name('workspace-alias')
        workspace_alias.symlink_to(work, target_is_directory=True)
        listing = hub.listing()
        entries = [{'project': name, 'revision': 1, 'unit': name + '.service', 'scope': 'user'} for name in projects]
        body = {'grants': entries, 'policy_sha256': listing['policy_sha256']}
        path = '/api/service-hub/read-grants'
        inventories = []
        def reader(command, **kwargs):
            output = ''
            if command[0] == 'systemctl' and '--user' in command:
                inventories.append(command)
                output = '\n\n'.join(f"Id={p}.service\nLoadState=loaded\nWorkingDirectory={workspace_alias / p / 'web'}" for p in projects)
            return {'code': 0, 'stdout': output, 'truncated': False}
        self.app.config.update(NATIVE_MODE=True, SERVICE_HUB_MATCH_RUNNER=reader)
        self.assertEqual(self.post(path, body).status_code, 401)
        self.login()
        self.assertEqual(self.post(path, body, csrf=False).status_code, 403)
        self.assertEqual(self.post(path, body).json['error'], 'XD_SERVICE_OPERATIONS_DISABLED')
        self.app.config['NATIVE_OPERATIONS_ENABLED'] = True
        before = hub.policies()
        for change, error in [({'revision': 3}, 'XD_SERVICE_REVISION_CONFLICT'),
                              ({'project': 'first'}, 'XD_SERVICE_READ_MATCH_REQUIRED'),
                              ({'unit': 'removed.service'}, 'XD_SERVICE_READ_MATCH_REQUIRED'),
                              ({'actions': ['restart']}, 'XD_SERVICE_GRANT_INVALID'),
                              (entries[0], 'XD_SERVICE_GRANT_INVALID')]:
            response = self.post(path, {**body, 'grants': [entries[0], {**entries[1], **change}]})
            self.assertEqual(response.json['error'], error)
            self.assertEqual(hub.policies(), before)
        inventories.clear()
        result = self.post(path, body)
        self.assertEqual(result.status_code, 201)
        self.assertEqual(len(inventories), 1)
        self.assertEqual([row['project'] for row in result.json['grants']], projects)
        reopened = ServiceHub(hub.root.parent)
        effective, imported = reopened.policies()
        self.assertEqual(imported, before[1])
        self.assertEqual(effective['revision'], before[0]['revision'] + 2)
        self.assertEqual([g['actions'] for g in effective['services'].values()], [['status', 'logs']] * 2)
        self.assertFalse(any(p['bound_services'] for p in reopened.listing()['projects']))
        self.assertEqual(self.post(path, body).json['error'], 'XD_SERVICE_CONFIGURATION_CHANGED')
        self.assertEqual(reopened.policies(), (effective, imported))

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

    def test_exact_admin_domain_preserves_host_origin_auth_and_csrf_checks(self):
        state=self.root/'native';initialize(state,ROOT)
        with patch.dict(os.environ,{'ADMIN_NATIVE_STATE':str(state),'ADMIN_NATIVE_HOSTNAME':'xdocker.home.test','ADMIN_NATIVE_WITHOUT_DOCKER':'true'},clear=False):app=create_native_app()
        client=app.test_client();base='http://xdocker.home.test'
        self.assertEqual(client.get('/healthz',base_url=base).status_code,200)
        for host in ('evil.home.test','evil.xdocker.home.test'):
            self.assertEqual(client.get('/healthz',base_url='http://'+host).status_code,400)
        self.assertEqual(client.get('/api/service-hub',base_url=base).status_code,401)
        token=(state/'access-token').read_text().strip()
        self.assertEqual(client.post('/api/login',base_url=base,json={'token':token},headers={'Origin':'http://evil.home.test'}).status_code,403)
        login=client.post('/api/login',base_url=base,json={'token':token},headers={'Origin':base})
        self.assertEqual(login.status_code,200)
        hub=app.extensions['service_hub'];hub.import_policy(policy())
        self.assertEqual(client.post('/api/service-hub/projects',base_url=base,json=project(),headers={'Origin':base}).status_code,403)
        headers={'Origin':base,'X-CSRF-Token':login.json['csrf']}
        saved=client.post('/api/service-hub/projects',base_url=base,json=project(),headers=headers)
        self.assertEqual(saved.status_code,200);self.assertFalse(saved.json['runtime_started'])
        self.assertEqual(client.post('/api/logout',base_url=base,json={},headers=headers).status_code,200)
        self.assertEqual(client.get('/api/service-hub',base_url=base).status_code,401)
        for hostname in ('*.home.test','.home.test','xdocker.home.test:80','http://xdocker.home.test'):
            with self.assertRaises(ServiceError):native_trusted_hosts(hostname)


if __name__=='__main__':unittest.main()
