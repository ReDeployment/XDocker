"""Native Admin with disposable state and explicit fake systemd/Nginx grants."""
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'services/admin'),str(ROOT/'tests')]
from werkzeug.serving import make_server, WSGIRequestHandler
from app import create_app
from docker_api import DockerError
from host_metrics import atomic_json
from native_runtime import initialize
from native_services import SystemdServices
from test_admin import container, ID
from test_native_services import FakeSystemd, policy
from test_resource_monitor import sample_report, FIRST
from test_native_gateway import free_port


def main():
    class Quiet(WSGIRequestHandler):
        def log(self,*args,**kwargs):pass
    class LogDocker:
        def __init__(self):self.calls=[]
        def request(self,method,path,*args,**kwargs):
            self.calls.append(method)
            assert method == 'GET'
            if path.startswith('/containers/json'):
                c = container(ID, 'gateway', 'demo')
                deployment = ROOT/'data/standalone/demo'
                c['Labels'].update({'com.docker.compose.project.working_dir':str(deployment/'instances/demo'),
                                    'com.docker.compose.project.config_files':str(deployment/'apps/powerx/compose.yml')})
                return [c]
            if path == '/version':
                return {'Version':'fixture','Os':'linux'}
            if path == '/containers/'+ID+'/json':
                return {'HostConfig':{'LogConfig':{'Type':'json-file','Config':{}}}}
            if path.startswith('/containers/'+ID+'/logs?'):
                return b'fixture log password=private-fixture'
            raise DockerError('Fixture has no other Docker resources')
    with tempfile.TemporaryDirectory(prefix='xdocker-hub-browser-') as raw:
        root=Path(raw).resolve();state=root/'state';initialize(state,ROOT)
        unit=root/'example.service';unit.write_text('[Service]\nExecStart=/usr/bin/example\n')
        runner=FakeSystemd(unit);adapter=SystemdServices(runner)
        grant={'scope':'user','unit':'example.service','actions':['status','logs','restart'],'definition_sha256':None}
        grant['definition_sha256']=adapter.status(grant)['definition_sha256']
        value=policy();value['services']['example']=grant;value['endpoints']['padelu']={'scheme':'http','host':'127.0.0.1','port':15173,'health_path':'/','expected_status':200};value['gateway'].update(enabled=True,http_port=free_port(),https_port=free_port())
        for alias in ('browser-project-backend','browser-project-worker'):
            value['services'][alias]={'scope':'user','unit':alias+'.service','actions':['status','logs'],'definition_sha256':None}
        live={'value':False};gateway_root=state/'service-hub/gateway'
        def nginx(command,**kwargs):
            if '-t' not in command:live['value']=True;(gateway_root/'nginx.pid').write_text('9000')
            return {'code':0,'stdout':'','truncated':False}
        (state/'native-config/sites/legacy').mkdir();(state/'native-config/sites/legacy/site.conf').write_text('SITE_DOMAIN=legacy.example.test\nSITE_SERVICE=legacy-web\nSITE_KIND=static\n');(state/'native-config/root.env').write_text('ENABLED_SITES=legacy\n')
        docker=LogDocker()
        def probe(endpoint):
            if endpoint.get('host_header')=='padelu-admin.home.test':
                return {'state':'unexpected-status','at':time.time()-60,'status':403}
            return {'state':'healthy','at':time.time(),'status':200}
        app=create_app({'TESTING':True,'STATE':str(state),'CONFIG':str(state/'native-config'),
            'REPORTS':str(state/'reports'),'BACKUPS':str(state/'backups'),'HOST_ROOT':str(ROOT),'HOST_METRICS':str(state/'host-metrics'),
            'NATIVE_MODE':True,'NATIVE_OPERATIONS_ENABLED':True,'SERVICE_HUB_ADAPTER':adapter,
            'SERVICE_HUB_PROBE':probe,
            'SERVICE_HUB_GATEWAY_ADAPTER':{'runner':nginx,'owner_reader':lambda *args:{'pid':9000,'started':42} if live['value'] else None,'marker_reader':lambda *args:True}},docker)
        app.extensions['service_hub'].import_policy(value)
        atomic_json(state/'host-metrics/report.json',sample_report())
        app.extensions['resource_monitor'].save({'id':'example-resources','revision':1,'name':'资源分组','targets':[FIRST]})
        server=make_server('127.0.0.1',0,app,threaded=True,request_handler=Quiet)
        print(json.dumps({'url':f'http://127.0.0.1:{server.server_port}/#service-hub','fixture_token':(state/'access-token').read_text().strip()}),flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:server.server_close();app.extensions['github_watch'].close()
        assert all(v=='GET' for v in docker.calls),'Native hub invoked Docker runtime control'
        assert sum('restart' in c for c in runner.calls)==1,'Unexpected automatic/replayed service restart'


if __name__=='__main__':main()
