"""Deployment checks must report uncertainty and never execute Docker writes."""
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/admin'))
from app import create_app
import preflight
spec=importlib.util.spec_from_file_location('preflight_host',ROOT/'scripts/preflight_host.py')
host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)

TOKEN='fixture-private-token-'+'z'*40


class Engine:
    def __init__(self):self.calls=[];self.ports=[];self.offline=False
    def request(self,method,path,body=None,**kwargs):
        self.calls.append((method,path,body))
        if self.offline:raise OSError('fixture-private-error')
        if path=='/version':return {'Version':'29','Os':'linux','Arch':'amd64'}
        if path.startswith('/containers/json'):return self.ports
        if path.startswith('/images/'):return {'Id':'sha256:'+'a'*64}
        raise AssertionError((method,path))


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in ('state','reports','config/sites/docs','config/instances/powerx-dev'):(self.root/name).mkdir(parents=True)
        (self.root/'state/access-token').write_text(TOKEN)
        self.env=self.root/'config/root.env'
        self.env.write_text('ENABLED_SITES=docs\nENABLED_SERVICES=admin\nPREFLIGHT_EXPECTED_IPS="160.202.238.184"\nSECRET=never-output-me\n')
        self.env.chmod(0o600)
        (self.root/'config/sites/docs/site.conf').write_text('SITE_SERVICE=powerx-doc\n')
        (self.root/'config/sites/docs/.env').write_text('SITE_DOMAIN=docs.example.com\nSITE_IMAGE=ghcr.io/example/docs:dev\nPRIVATE_SECRET=private-site-secret\n')
        (self.root/'config/sites/docs/.env').chmod(0o600)
        (self.root/'config/instances/powerx-dev/instance.json').write_text(json.dumps({'app':'powerx','project':'powerx-dev','enabled':True}))
        (self.root/'config/instances/powerx-dev/.env').write_text('POWERX_BACKEND_IMAGE=ghcr.io/example/backend:dev\nSECRET=private-instance-secret\n')
        (self.root/'config/instances/powerx-dev/.env').chmod(0o600)
        self.engine=Engine()
        self.app=create_app({'TESTING':True,'STATE':str(self.root/'state'),'CONFIG':str(self.root/'config'),'REPORTS':str(self.root/'reports'),'HOST_ROOT':'/deployment/XDocker'},self.engine)
        self.client=self.app.test_client()

    def login(self):
        response=self.client.post('/api/login',json={'token':TOKEN},headers={'Origin':'http://localhost'})
        self.assertEqual(response.status_code,200)

    def checker(self):
        return preflight.Preflight(self.engine,self.root/'config',self.root/'state',self.root/'reports','/deployment/XDocker',
            lambda:[{'key':'docs','domain':'docs.example.com','enabled':True,'image':'ghcr.io/example/docs:dev'}],lambda:['powerx-dev'])

    def report(self,**overrides):
        data={'schema':1,'root':'/deployment/XDocker','at':preflight.now(),'revision':'development',
              'checks':{key:preflight.result('pass','fixture host check','fixture advice') for key in preflight.HOST_CHECKS},
              'images':['ghcr.io/example/frps:dev']}
        data.update(overrides);(self.root/'reports/deployment-host.json').write_text(json.dumps(data));return data

    def test_api_auth_allowlist_no_mutations_and_no_secrets(self):
        self.assertEqual(self.client.get('/api/preflight').status_code,401)
        self.assertEqual(self.client.get('/api/preflight/network-ghcr').status_code,401)
        self.login();listing=self.client.get('/api/preflight');self.assertEqual(listing.status_code,200)
        for secret in (TOKEN,'never-output-me','private-site-secret','private-instance-secret'):self.assertNotIn(secret,listing.text)
        self.assertEqual(self.client.get('/api/preflight/unknown').status_code,404)
        self.assertEqual(self.client.get('/api/preflight/network-ghcr?url=http://127.0.0.1/').status_code,400)
        self.assertEqual(self.client.post('/api/preflight/docker-engine',json={},headers={'Origin':'http://localhost'}).status_code,403)
        response=self.client.get('/api/preflight/docker-engine');self.assertEqual(response.json['result']['status'],'pass')
        self.assertTrue(all(method=='GET' for method,_,_ in self.engine.calls))

    def test_catalog_and_checks_work_when_engine_offline(self):
        self.login();self.engine.offline=True
        self.assertEqual(self.client.get('/api/preflight').status_code,200)
        response=self.client.get('/api/preflight/docker-engine')
        self.assertEqual(response.status_code,200);self.assertEqual(response.json['result']['status'],'fail')
        self.assertNotIn('fixture-private-error',response.text)

    def test_config_permissions_and_missing_files_are_separate_from_host_rights(self):
        check=self.checker();self.assertEqual(check.run('config-permissions')['result']['status'],'pass')
        self.env.chmod(0o644);self.assertEqual(check.run('config-permissions')['result']['status'],'warn')
        self.env.unlink();self.assertEqual(check.run('config-permissions')['result']['status'],'fail')

    def test_host_report_identity_age_revision_and_result_projection(self):
        check=self.checker();self.assertEqual(check.run('host-compose')['result']['status'],'unknown')
        self.report();self.assertEqual(check.run('host-compose')['result']['status'],'pass')
        self.report(root='/other');self.assertEqual(check.run('host-compose')['result']['status'],'unknown')
        self.report(at=(datetime.now(timezone.utc)-timedelta(minutes=16)).isoformat())
        self.assertEqual(check.run('host-compose')['result']['status'],'unknown')
        self.report();
        with patch.dict('os.environ',{'ADMIN_DEPLOY_REVISION':'b'*40}):self.assertEqual(check.run('host-compose')['result']['status'],'unknown')
        data=self.report();data['checks']['host-compose']['private']='fixture-secret';self.report(checks=data['checks'])
        self.assertNotIn('fixture-secret',json.dumps(check.run('host-compose')))

    def test_cached_dns_is_invalidated_when_expected_addresses_change(self):
        check=self.checker()
        with patch.object(preflight,'isolated_probe',return_value=preflight.result('pass','resolved',addresses=['160.202.238.184'])):
            self.assertEqual(check.run('dns-docs')['result']['status'],'pass')
        self.assertFalse(next(i for i in check.listing()['items'] if i['key']=='dns-docs')['result']['stale'])
        self.env.write_text(self.env.read_text().replace('160.202.238.184','192.0.2.1'))
        row=next(i for i in check.listing()['items'] if i['key']=='dns-docs')
        self.assertTrue(row['result']['stale'])

    def test_dns_old_aaaa_and_missing_expected_ip_cannot_pass(self):
        check=self.checker()
        with patch.object(preflight,'isolated_probe',return_value=preflight.result('pass','resolved',addresses=['160.202.238.184','2001:db8::2'])):
            self.assertEqual(check.run('dns-docs')['result']['status'],'fail')
        self.env.write_text('ENABLED_SITES=docs\n')
        with patch.object(preflight,'isolated_probe',return_value=preflight.result('pass','resolved',addresses=['160.202.238.184'])):
            self.assertEqual(check.run('dns-docs')['result']['status'],'warn')

    def test_port_conflict_counts_other_containers_without_exposing_them(self):
        check=self.checker();self.engine.ports=[{'Labels':{'com.docker.compose.project':'unrelated-secret-name'},'Ports':[{'PublicPort':80,'IP':'0.0.0.0'}]}]
        row=check.run('docker-ports')['result'];self.assertEqual(row['status'],'fail');self.assertNotIn('unrelated-secret-name',json.dumps(row))
        self.engine.ports[0]['Labels']={'com.docker.compose.project':'xdocker-web-hosting','com.docker.compose.service':'nginx'}
        self.assertEqual(check.run('docker-ports')['result']['status'],'pass')

    def test_image_reference_selection_and_anonymous_permission_warning(self):
        check=self.checker();self.report()
        image=next(i for i in check.catalog() if i.get('image')=='ghcr.io/example/frps:dev')
        with patch.object(preflight,'isolated_probe',return_value=preflight.result('warn','private or permission denied')):
            row=check.run(image['key'])['result'];self.assertEqual(row['status'],'warn');self.assertTrue(row['local_cached'])
        self.assertTrue(all(c[0]=='GET' for c in self.engine.calls))
        for value in ('https://user:secret@ghcr.io/a/b','ghcr.io/a/b;rm -rf /','ghcr.io/a/b\nsecret'):
            with self.assertRaises(ValueError):preflight.image_parts(value)
        self.assertEqual(preflight.image_parts('nginx:alpine'),('docker.io','library/nginx','alpine'))

    def test_single_check_lock_rejects_duplicate_network_workers(self):
        check=self.checker()
        check.lock.acquire()
        with self.assertRaises(preflight.BusyCheck):check.run('network-ghcr')
        check.lock.release()

    def test_registry_401_is_reachable_but_does_not_prove_auth(self):
        with patch.object(preflight,'http_read',return_value=(401,{'Docker-Distribution-Api-Version':'registry/2.0'},b'')):
            row=preflight.probe_network('network-ghcr');self.assertEqual(row['status'],'pass');self.assertIn('不验证私有镜像权限',row['advice'])
        with patch.object(preflight,'http_read',return_value=(200,{},b'{}')):
            self.assertEqual(preflight.probe_network('network-ghcr')['status'],'fail')

    def test_manifest_token_is_anonymous_fixed_scoped_and_never_returned(self):
        replies=[(401,{},b''),(200,{},b'{"token":"fixture-anonymous-secret"}'),(200,{},b'{"schemaVersion":2,"layers":[]}')]
        with patch.object(preflight,'http_read',side_effect=replies) as fetch:
            row=preflight.probe_manifest('ghcr.io/example/app:dev')
            self.assertEqual(row['status'],'pass');self.assertNotIn('fixture-anonymous-secret',json.dumps(row))
            self.assertTrue(fetch.call_args_list[1].args[0].startswith('https://ghcr.io/token?'))
            self.assertIn('repository%3Aexample%2Fapp%3Apull',fetch.call_args_list[1].args[0])
        with patch.object(preflight,'http_read',side_effect=[(401,{},b''),(403,{},b'')]):
            self.assertEqual(preflight.probe_manifest('ghcr.io/example/private:dev')['status'],'warn')

    def test_probe_deadline_and_network_errors_do_not_leak_raw_output(self):
        with patch.object(preflight.subprocess,'run',side_effect=subprocess.TimeoutExpired('fixture',10)):
            self.assertEqual(preflight.isolated_probe('dns','docs.example.com')['status'],'fail')
        for error,detail in [(socket.gaierror('fixture-secret'),'DNS'),(URLError(TimeoutError('secret')),'超时'),(ssl.SSLError('secret'),'TLS')]:
            stream=io.StringIO()
            with patch.object(preflight,'probe_network',side_effect=error),patch('sys.stdout',stream):preflight.probe_main('network','network-ghcr')
            self.assertIn(detail,stream.getvalue());self.assertNotIn('secret',stream.getvalue())

    def test_actual_isolated_worker_runs_without_external_network_or_credentials(self):
        row=preflight.isolated_probe('manifest','registry.example.com/fixture/app:1')
        self.assertEqual(row['status'],'unknown')
        self.assertIn('未实现匿名清单检查',row['detail'])


class HostReportTests(unittest.TestCase):
    def test_host_commands_are_read_only_and_saved_report_has_private_permissions(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            for part in ('data/events','data/instance-backups'):(root/part).mkdir(parents=True)
            (root/'.env').write_text('PASSWORD=fixture-never-output\nHTTP_PORT=0\n')
            calls=[]
            def command(args,where):
                calls.append(args)
                if args[:3]==['docker','compose','version']:return 0,'5.6.0'
                if args[:2]==['docker','info']:return 0,json.dumps(name)
                if args[:2]==['git','rev-parse']:return 0,'a'*40
                if args[-1]=='--images':return 0,'nginx:alpine\ncertbot/certbot:latest'
                if args[-1]=='--quiet':return 0,''
                raise AssertionError(args)
            with patch.object(host,'command',side_effect=command),patch.object(host.os,'geteuid',return_value=1000):report=host.collect(root)
            self.assertEqual(report['checks']['host-compose']['status'],'pass')
            self.assertNotIn('fixture-never-output',json.dumps(report))
            self.assertEqual(report['images'],['certbot/certbot:latest','nginx:alpine'])
            self.assertFalse(any(any(a in ('pull','up','init','chmod','chown') for a in args) for args in calls))
            host.save(root,report);file=root/'data/events/deployment-host.json'
            self.assertEqual(file.stat().st_mode&0o777,0o600)
            self.assertEqual((root/'.env').read_text(),'PASSWORD=fixture-never-output\nHTTP_PORT=0\n')

    def test_host_save_rejects_symlink_and_does_not_touch_target(self):
        with tempfile.TemporaryDirectory() as name,tempfile.TemporaryDirectory() as other:
            root=Path(name);(root/'data').mkdir();(root/'data/events').symlink_to(other)
            with self.assertRaises(ValueError):host.save(root,{'fixture':True})
            self.assertEqual(list(Path(other).iterdir()),[])
