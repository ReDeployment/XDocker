import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'services/admin'))
from app import create_app
from instances import InstanceReader
from test_admin import FakeDocker, container, ID, TOKEN

PROJECT='powerx-dev'
POSTGRES_PASSWORD='postgres-fixture-secret'
REDIS_PASSWORD='redis-fixture-secret'
JWT_SECRET='jwt-fixture-never-expose'


def archive(config, name='config.yaml', symlink=False):
    output=io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as tar:
        content=yaml.safe_dump(config).encode()
        info=tarfile.TarInfo(name);info.size=len(content)
        if symlink:info.type=tarfile.SYMTYPE;info.linkname='/other/private';info.size=0
        tar.addfile(info,io.BytesIO(content) if not symlink else None)
    return output.getvalue()


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for path in ('state','config/sites','config/instances/'+PROJECT,'reports'):(self.root/path).mkdir(parents=True)
        (self.root/'state/access-token').write_text(TOKEN)
        (self.root/'config/root.env').write_text('ENABLED_SITES=""\nENABLED_SERVICES=""\n')
        (self.root/'config/certificates.json').write_text('{"certificates":[]}')
        (self.root/'config/instances'/PROJECT/'instance.json').write_text(json.dumps({'app':'powerx','project':PROJECT,'enabled':True}))
        self.config={'database':{'host':'postgres','port':5432,'database':'powerx','username':'powerx','password':POSTGRES_PASSWORD,'dsn':'contains-fixture-secret'},
                     'cache':{'host':'redis','port':6379,'db':0,'password':REDIS_PASSWORD},
                     'auth':{'jwt_secret':JWT_SECRET},'install':{'status':'uninstalled'},'deployment':{'env':'dev'}}
        self.blob=archive(self.config)
        self.source=str(self.root/'instances'/PROJECT/'config')
        self.engine=FakeDocker();original=self.engine.request
        def request(method,path,body=None,raw=False,timeout=15):
            if path.startswith('/containers/json'):return [container(ID,'backend',PROJECT)]
            if path=='/containers/'+ID+'/json':return {'Config':{'Labels':{'com.docker.compose.project':PROJECT,'com.docker.compose.service':'backend'}},
                'Mounts':[{'Type':'bind','Destination':'/etc/powerx','Source':self.source}]}
            if '/archive?' in path:
                self.assertEqual(path,'/containers/'+ID+'/archive?path=%2Fetc%2Fpowerx%2Fconfig.yaml')
                return self.blob
            return original(method,path,body,raw,timeout)
        self.engine.request=request
        self.app=create_app({'TESTING':True,'STATE':str(self.root/'state'),'CONFIG':str(self.root/'config'),
            'REPORTS':str(self.root/'reports'),'BACKUPS':str(self.root/'backups'),'HOST_ROOT':str(self.root)},self.engine)
        self.client=self.app.test_client()
        auth=self.client.post('/api/login',json={'token':TOKEN},headers={'Origin':'http://localhost'})
        self.headers={'Origin':'http://localhost','X-CSRF-Token':auth.json['csrf']}

    def reveal(self,service='postgres',confirmation=PROJECT,headers=None):
        return self.client.post('/api/instances/'+PROJECT+'/credentials',json={'service':service,'confirmation':confirmation},headers=self.headers if headers is None else headers)

    def test_detail_is_useful_for_setup_without_any_password_or_key(self):
        response=self.client.get('/api/instances/'+PROJECT);self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['postgres']['host'],'postgres')
        self.assertEqual(response.json['redis']['host'],'redis')
        self.assertEqual(response.json['install_status'],'uninstalled')
        for secret in (POSTGRES_PASSWORD,REDIS_PASSWORD,JWT_SECRET,'contains-fixture-secret'):self.assertNotIn(secret,response.text)
        self.assertIn('no-store',response.headers['Cache-Control'])

    def test_reveal_only_requested_secret_and_audit_never_records_it(self):
        for service,secret,other in [('postgres',POSTGRES_PASSWORD,REDIS_PASSWORD),('redis',REDIS_PASSWORD,POSTGRES_PASSWORD)]:
            response=self.reveal(service);self.assertEqual(response.status_code,200)
            self.assertEqual(response.json['password'],secret)
            self.assertNotIn(other,response.text);self.assertNotIn(JWT_SECRET,response.text)
            self.assertIn('no-store',response.headers['Cache-Control'])
        audit=self.client.get('/api/audit').text
        self.assertIn('credential-view',audit)
        for secret in (POSTGRES_PASSWORD,REDIS_PASSWORD,JWT_SECRET):self.assertNotIn(secret,audit)
        self.assertNotIn(POSTGRES_PASSWORD,self.client.get('/api/snapshot').text)
        self.assertNotIn(POSTGRES_PASSWORD,self.client.get('/api/jobs').text)

    def test_auth_origin_csrf_and_full_instance_confirmation_required(self):
        self.assertEqual(self.reveal(confirmation='wrong').status_code,400)
        self.assertEqual(self.reveal(headers={'Origin':'http://localhost'}).status_code,403)
        self.assertEqual(self.reveal(headers={'Origin':'http://other','X-CSRF-Token':self.headers['X-CSRF-Token']}).status_code,403)
        self.client.post('/api/logout',json={},headers=self.headers)
        self.assertEqual(self.reveal().status_code,401)
        self.assertEqual(self.client.get('/api/instances/'+PROJECT).status_code,401)

    def test_unregistered_instance_or_arbitrary_secret_not_available(self):
        self.assertEqual(self.client.get('/api/instances/other').status_code,404)
        self.assertEqual(self.client.post('/api/instances/other/credentials',json={'service':'postgres','confirmation':'other'},headers=self.headers).status_code,404)
        self.assertEqual(self.reveal(service='jwt_secret').status_code,400)

    def test_mount_mismatch_and_symlink_archive_rejected(self):
        self.source='/unrelated/private'
        self.assertEqual(self.client.get('/api/instances/'+PROJECT).status_code,409)
        self.source=str(self.root/'instances'/PROJECT/'config')
        self.blob=archive({},symlink=True)
        self.assertEqual(self.reveal().status_code,409)
        self.blob=archive({},name='../config.yaml')
        self.assertEqual(self.reveal().status_code,409)

    def test_nested_unexpected_values_do_not_expose_secrets(self):
        self.config['storage']={'local':{'public_base_url':self.config['database']}}
        self.config['deployment']={'env':self.config['database']}
        self.blob=archive(self.config)
        response=self.client.get('/api/instances/'+PROJECT)
        self.assertEqual(response.status_code,200)
        self.assertNotIn(POSTGRES_PASSWORD,response.text)


if __name__=='__main__':unittest.main()
