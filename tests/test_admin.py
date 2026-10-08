"""Auth, scope, state and command safety tests for the XDocker control plane."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/admin'))
from app import create_app, env_values, redact
from docker_api import log_text

ID = 'a'*64
OTHER = 'b'*64
SELF = 'c'*64
TOKEN = 'test-token-never-log-this-'+'x'*40


def container(identifier=ID, service='nginx', project='xdocker-web-hosting'):
    return {'Id': identifier, 'Names': ['/test-'+service], 'Image': 'example:image', 'State': 'running',
            'Status': 'Up 1 minute', 'Created': 1, 'Ports': [],
            'Labels': {'com.docker.compose.project': project, 'com.docker.compose.service': service}}


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.state = 'running'

    def request(self, method, path, body=None, raw=False, timeout=15):
        self.calls.append((method, path, body))
        if path.startswith('/containers/json'):
            return [container(), container(OTHER, project='unrelated'), container(SELF, service='admin')]
        if path == '/version':
            return {'Version': '29.8.2', 'ApiVersion': '1.56', 'Os': 'linux', 'Arch': 'amd64'}
        if '/logs?' in path:
            return ('setup_token=private-test-secret password=hunter2 '+TOKEN+'\n'+
                    'DB_SECRET=cached-db-secret Bearer a-secret-token').encode()
        if '/stats?' in path:
            return {'cpu_stats': {'cpu_usage': {'total_usage': 30}, 'system_cpu_usage': 100, 'online_cpus': 2},
                    'precpu_stats': {'cpu_usage': {'total_usage': 10}, 'system_cpu_usage': 50},
                    'memory_stats': {'usage': 100, 'stats': {'inactive_file': 30}, 'limit': 200},
                    'pids_stats': {'current': 2}}
        if method == 'POST':
            if '/stop?' in path:self.state='exited'
            return {}
        if '/json' in path:
            return {'Config': {'Env': ['DB_SECRET=cached-db-secret']}, 'State': {'Status': self.state}}
        raise AssertionError('Unexpected Docker API request '+method+' '+path)


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for folder in ('state', 'config/sites/powerxdoc', 'reports'):
            (self.root / folder).mkdir(parents=True)
        (self.root/'state/access-token').write_text(TOKEN)
        (self.root/'config/root.env').write_text('ENABLED_SITES="powerxdoc"\nENABLED_SERVICES="admin"\nSECRET=should-not-appear\n')
        (self.root/'config/sites/powerxdoc/site.conf').write_text('SITE_SERVICE=powerx-doc\n')
        (self.root/'config/sites/powerxdoc/.env').write_text('SITE_DOMAIN=docs.example.com\nSITE_IMAGE=example:docs\nPRIVATE_KEY=do-not-echo\n')
        (self.root/'config/certificates.json').write_text(json.dumps({'certificates':[{'name':'docs.example.com','domains':['docs.example.com'],'enabled':True}, {'name':'disabled.example.com','domains':['disabled.example.com'],'enabled':False}]}))
        (self.root/'reports/certificates-check.json').write_text(json.dumps({'action':'check','checked_at':'2026-10-08T00:00:00+00:00','results':[{'name':'docs.example.com','status':'LOCAL_VALID','expiry':'2099-01-01T00:00:00+00:00','days_remaining':90}]}))
        self.docker = FakeDocker()
        self.app = create_app({'TESTING':True,'STATE':str(self.root/'state'),'CONFIG':str(self.root/'config'),'REPORTS':str(self.root/'reports')}, self.docker)
        self.client = self.app.test_client()
        self.csrf = ''

    def post(self, url, data, origin=True, csrf=True):
        headers = {}
        if origin:headers['Origin']='http://localhost'
        if csrf:headers['X-CSRF-Token']=self.csrf
        return self.client.post(url,json=data,headers=headers)

    def login(self):
        response = self.post('/api/login', {'token':TOKEN})
        self.assertEqual(response.status_code,200)
        self.assertIn('HttpOnly',response.headers['Set-Cookie'])
        self.assertIn('SameSite=Strict',response.headers['Set-Cookie'])
        self.csrf = response.json['csrf']

    def wait_job(self, identifier):
        for _ in range(100):
            rows = self.client.get('/api/jobs').json['jobs']
            row = next(j for j in rows if j['id']==identifier)
            if row['state']!='running':return row
            time.sleep(.01)
        self.fail('Job never completed')

    def test_auth_required_no_secret_in_public_page(self):
        self.assertEqual(self.client.get('/api/snapshot').status_code,401)
        page=self.client.get('/')
        self.assertEqual(page.status_code,200)
        self.assertNotIn(TOKEN,page.text)
        self.assertIn("frame-ancestors 'none'",page.headers['Content-Security-Policy'])
        page.close()
        self.assertEqual(self.client.get('/assets/../../state/access-token').status_code,404)

    def test_origin_host_and_csrf_rejected(self):
        self.assertEqual(self.post('/api/login',{'token':TOKEN},origin=False).status_code,403)
        self.assertEqual(self.client.get('/',headers={'Host':'attacker.example.com'}).status_code,400)
        self.login()
        self.assertEqual(self.post('/api/services/'+ID+'/action',{'action':'stop','confirmation':'nginx'},csrf=False).status_code,403)
        self.assertFalse(any(c[0]=='POST' for c in self.docker.calls))

    def test_wrong_login_rate_limited(self):
        for _ in range(10):self.assertEqual(self.post('/api/login',{'token':'wrong'}).status_code,401)
        self.assertEqual(self.post('/api/login',{'token':TOKEN}).status_code,429)

    def test_snapshot_filters_unrelated_containers_and_secrets(self):
        self.login()
        response=self.client.get('/api/snapshot')
        self.assertEqual(response.status_code,200)
        self.assertEqual({s['id'] for s in response.json['services']},{ID,SELF})
        self.assertEqual(response.json['sites'][0]['domain'],'docs.example.com')
        self.assertEqual(response.json['certificates'][0]['status'],'LOCAL_VALID')
        for secret in (TOKEN,'should-not-appear','do-not-echo'):self.assertNotIn(secret,response.text)

    def test_unrelated_container_and_self_controls_blocked(self):
        self.login()
        self.assertEqual(self.post('/api/services/'+OTHER+'/action',{'action':'stop','confirmation':'nginx'}).status_code,404)
        self.assertEqual(self.post('/api/services/'+SELF+'/action',{'action':'stop','confirmation':'admin'}).status_code,409)
        self.assertFalse(any(c[0]=='POST' for c in self.docker.calls))

    def test_confirmation_and_arbitrary_commands_rejected(self):
        self.login()
        for body in ({'action':'stop','confirmation':'wrong'},{'action':'exec','confirmation':'nginx'},{'action':'rm','confirmation':'nginx'}):
            self.assertEqual(self.post('/api/services/'+ID+'/action',body).status_code,400)
        self.assertFalse(any(c[0]=='POST' for c in self.docker.calls))

    def test_lifecycle_job_checks_actual_state_and_records_audit(self):
        self.login()
        response=self.post('/api/services/'+ID+'/action',{'action':'stop','confirmation':'nginx'})
        self.assertEqual(response.status_code,202)
        row=self.wait_job(response.json['id'])
        self.assertEqual(row['state'],'success')
        self.assertIn('exited',row['output'])
        events=self.client.get('/api/audit').json['events']
        self.assertTrue(any(e['action']=='stop' and e['result']=='success' for e in events))
        self.assertTrue(any('/stop?t=15' in c[1] for c in self.docker.calls))

    def test_logs_redacted_and_stats_calculated(self):
        self.login()
        response=self.client.get('/api/services/'+ID+'/logs')
        self.assertEqual(response.status_code,200)
        for secret in (TOKEN,'hunter2','private-test-secret','cached-db-secret','a-secret-token'):self.assertNotIn(secret,response.text)
        stats=self.client.get('/api/services/'+ID+'/stats').json
        self.assertEqual(stats['cpu_percent'],80)
        self.assertEqual(stats['memory_bytes'],70)

    def test_disabled_certificate_and_force_issuance_rejected(self):
        self.login()
        for name,action in [('disabled.example.com','renew'),('docs.example.com','issue'),('../etc','check')]:
            self.assertEqual(self.post('/api/certificates/action',{'name':name,'action':action,'confirmation':name}).status_code,400)
        self.assertFalse(any(c[0]=='POST' for c in self.docker.calls))

    def test_certificate_job_uses_fixed_command_mounts_and_cleans_up(self):
        self.login()
        original = self.docker.request
        job_id = 'd'*64
        destinations = ['/etc/letsencrypt','/var/lib/certbot-events','/var/www/certbot',
                        '/opt/xdocker/certbot/certificates.json','/opt/xdocker/scripts/certificates.py']
        def request(method,path,body=None,raw=False,timeout=15):
            if path.startswith('/containers/json'):
                return [container(ID,'certbot-renew')]
            if path == '/containers/'+ID+'/json':
                return {'Config':{'Image':'certbot:fixed'},'Mounts':[{'Type':'bind','Source':'/private'+d,'Destination':d,'RW':d not in destinations[-2:]} for d in destinations], 'NetworkSettings':{'Networks':{'project-default':{}}}}
            if path.startswith('/containers/create'):
                self.created=body
                return {'Id':job_id}
            if path == '/containers/'+job_id+'/json':return {'State':{'Running':False,'ExitCode':0},'Config':{'Env':[]}}
            if '/logs?' in path:return b'certificate check passed'
            if method=='DELETE':self.deleted=path;return {}
            return original(method,path,body,raw,timeout)
        self.docker.request=request
        response=self.post('/api/certificates/action',{'name':'docs.example.com','action':'check','confirmation':'docs.example.com'})
        self.assertEqual(response.status_code,202)
        result=self.wait_job(response.json['id'])
        self.assertEqual(result['state'],'success')
        self.assertEqual(self.created['Image'],'certbot:fixed')
        self.assertEqual(self.created['Entrypoint'][:4],['python3','/opt/xdocker/scripts/certificates.py','check','docs.example.com'])
        self.assertIn('/var/lib/certbot-events/certificates-admin-'+response.json['id']+'.json',self.created['Entrypoint'])
        self.assertEqual({m['Target'] for m in self.created['HostConfig']['Mounts']},set(destinations))
        self.assertNotIn('/var/run/docker.sock',json.dumps(self.created))
        self.assertEqual(self.deleted,'/containers/'+job_id+'?force=true&v=true')

    def test_concurrent_mutations_rejected_until_first_result(self):
        self.login()
        release=threading.Event()
        original=self.docker.request
        def request(method,path,body=None,raw=False,timeout=15):
            if method=='POST':release.wait(2)
            return original(method,path,body,raw,timeout)
        self.docker.request=request
        first=self.post('/api/services/'+ID+'/action',{'action':'stop','confirmation':'nginx'})
        try:
            second=self.post('/api/services/'+ID+'/action',{'action':'restart','confirmation':'nginx'})
            self.assertEqual(second.status_code,409)
        finally:
            release.set()
            self.wait_job(first.json['id'])

    def test_probe_failure_preserves_specific_reason_and_actual_state(self):
        self.login()
        opener=Mock()
        opener.open.side_effect=URLError('temporary DNS failure')
        with patch('app.urllib.request.build_opener',return_value=opener):
            response=self.post('/api/sites/probe',{'site':'powerxdoc'})
            row=self.wait_job(response.json['id'])
        self.assertEqual(row['state'],'failed')
        self.assertIn('temporary DNS failure',row['output'])
        site=self.client.get('/api/snapshot').json['sites'][0]
        self.assertEqual(site['probe']['status'],'failed')
        self.assertIn('temporary DNS failure',site['probe']['output'])

    def test_logout_invalidates_session(self):
        self.login()
        self.assertEqual(self.post('/api/logout',{}).status_code,200)
        self.assertEqual(self.client.get('/api/snapshot').status_code,401)

    def test_env_parser_does_not_execute_shell(self):
        marker=self.root/'should-not-exist'
        path=self.root/'host.env'
        path.write_text('TEST=$(touch '+str(marker)+')\nVALID="a b"\n')
        values=env_values(path)
        self.assertEqual(values['VALID'],'a b')
        self.assertFalse(marker.exists())

    def test_docker_log_frames_and_private_keys(self):
        raw=b'\x01\x00\x00\x00\x00\x00\x00\x05hello'+b'\x02\x00\x00\x00\x00\x00\x00\x05error'
        self.assertEqual(log_text(raw),'helloerror')
        self.assertNotIn('PRIVATE KEY',redact('-----BEGIN RSA PRIVATE KEY-----\nsecret\n-----END RSA PRIVATE KEY-----').replace('[PRIVATE KEY REDACTED]',''))


if __name__=='__main__':unittest.main()
