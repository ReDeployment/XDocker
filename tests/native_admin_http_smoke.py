"""Real Gunicorn/native CLI, private throwaway state, no Docker connection or app starts."""
import http.cookiejar
import json
import os
from pathlib import Path
import socket
import signal
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

ROOT=Path(__file__).resolve().parents[1]


def run_case(wait_for_address=False):
    with tempfile.TemporaryDirectory(prefix='xdocker-native-http-') as raw:
        root=Path(raw).resolve();state=root/'state'
        checkout=ROOT
        if not wait_for_address:
            checkout=root/'checkout'
            for folder in ('scripts','services/admin'):
                shutil.copytree(ROOT/folder,checkout/folder,ignore=shutil.ignore_patterns('__pycache__','.venv'))
            # A worker should import new application code after HUP, even on a stable master.
            module=checkout/'services/admin/native_services.py'
            source=module.read_text()
            flag="'batch_binding_available':True"
            assert flag in source
            module.write_text(source.replace(flag,"'batch_binding_available':False"))
        init=subprocess.run([sys.executable,'-B',str(checkout/'scripts/admin-native.py'),'init','--state',str(state)],capture_output=True,text=True,check=True)
        assert json.loads(init.stdout)['docker_required'] is False
        if wait_for_address:
            sys.path.insert(0,str(ROOT/'services/admin'))
            from native_services import ServiceHub
            from gateway_runtime import GatewayRuntime,atomic_text
            hub=ServiceHub(state);policy=hub.policy();policy['revision']+=1
            policy['gateway'].update(enabled=True,listen='192.0.2.1',binary=str(root/'unused-nginx'))
            hub.import_policy(policy);gateway=GatewayRuntime(hub);plan=gateway.preview()
            atomic_text(gateway.root/'active.conf',plan['configuration'])
            hub.store.write_gateway({'fingerprint':plan['fingerprint'],'owner':{'pid':1073741824,'started':1},'at':time.time()})
        with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        with (root/'server.log').open('w+') as log:
            command=[sys.executable,'-B',str(checkout/'scripts/admin-native.py'),'serve','--state',str(state),'--port',str(port),'--without-docker','--admin-domain','xdocker.home.test']
            if wait_for_address:command.append('--operations')
            pidfile=root/'gunicorn.pid'
            env={**os.environ,'GUNICORN_CMD_ARGS':'--pid '+shlex.quote(str(pidfile))}
            child=subprocess.Popen(command,stdout=log,stderr=log,env=env)
            origin=f'http://127.0.0.1:{port}'
            client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),urllib.request.ProxyHandler({}))
            def read(path,body=None,csrf=None):
                headers={'Host':'xdocker.home.test'}
                if body is not None:headers.update({'Origin':'http://xdocker.home.test','Content-Type':'application/json',**({'X-CSRF-Token':csrf} if csrf else {})})
                with client.open(urllib.request.Request(origin+path,data=json.dumps(body).encode() if body is not None else None,headers=headers),timeout=3) as response:return json.load(response)
            try:
                ready=False
                for _ in range(100):
                    if child.poll() is not None:raise AssertionError('Native process exited before readiness')
                    try:
                        with client.open(origin+'/healthz',timeout=.3) as response:ready=response.status==200
                        if ready:break
                    except OSError:time.sleep(.1)
                assert ready
                try:
                    client.open(urllib.request.Request(origin+'/healthz',headers={'Host':'evil.home.test'}),timeout=3)
                    raise AssertionError('Unknown management hostname accepted')
                except urllib.error.HTTPError as error:assert error.code==400;error.close()
                token=(state/'access-token').read_text().strip()
                session=read('/api/login',{'token':token})
                snapshot=read('/api/snapshot');assert snapshot['runtime_mode']=='native' and snapshot['engine']['available'] is False
                listing=read('/api/service-hub');assert listing['native'] and listing['operations_enabled']==wait_for_address and not listing['projects']
                if not wait_for_address:
                    assert listing['batch_binding_available'] is False
                    master=int(pidfile.read_text())
                    module.write_text(source)
                    os.kill(master,signal.SIGHUP)
                    for _ in range(100):
                        if read('/api/service-hub')['batch_binding_available']:break
                        time.sleep(.1)
                    else:raise AssertionError('Reloaded worker still served old application code')
                    assert int(pidfile.read_text())==master
                if wait_for_address:
                    for _ in range(30):
                        listing=read('/api/service-hub')
                        if (listing['gateway'].get('recovery') or {}).get('state')=='waiting-address':break
                        time.sleep(.1)
                    assert listing['gateway']['recovery']['state']=='waiting-address'
                    assert listing['gateway']['recovery']['error']=='XD_GATEWAY_ADDRESS_UNAVAILABLE'
                    assert not (gateway.root/'candidate.conf').exists(), 'A failed address guard ran Nginx'
                with client.open(origin+'/assets/service_hub.js',timeout=3) as response:assert b'renderServiceHubPage' in response.read()
                if not wait_for_address:
                    try:read('/api/service-hub/gateway/apply',{},session['csrf']);raise AssertionError('Runtime operations unexpectedly enabled')
                    except urllib.error.HTTPError as error:assert error.code==409;error.close()
                read('/api/logout',{},session['csrf'])
                print(json.dumps({'passed':True,'real_gunicorn':True,'docker_connected':False,'private_state':True,'admin_domain':'xdocker.home.test','login_and_csrf':True,'runtime_operations':wait_for_address,'web_ready_while_gateway_waits':wait_for_address}))
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:child.wait(timeout=15)
                    except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
                log.flush()
                if child.returncode not in (0,-15,143):
                    log.seek(0);raise AssertionError('Native process failed: '+log.read()[-3000:])


if __name__=='__main__':
    run_case()
    run_case(wait_for_address=True)
