"""Real Gunicorn/native CLI, private throwaway state, no Docker connection or app starts."""
import http.cookiejar
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

ROOT=Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='xdocker-native-http-') as raw:
        root=Path(raw).resolve();state=root/'state'
        init=subprocess.run([sys.executable,'-B',str(ROOT/'scripts/admin-native.py'),'init','--state',str(state)],capture_output=True,text=True,check=True)
        assert json.loads(init.stdout)['docker_required'] is False
        with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        with (root/'server.log').open('w+') as log:
            child=subprocess.Popen([sys.executable,'-B',str(ROOT/'scripts/admin-native.py'),'serve','--state',str(state),'--port',str(port),'--without-docker'],stdout=log,stderr=log)
            origin=f'http://127.0.0.1:{port}'
            client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),urllib.request.ProxyHandler({}))
            def read(path,body=None,csrf=None):
                headers={}
                if body is not None:headers={'Origin':origin,'Content-Type':'application/json',**({'X-CSRF-Token':csrf} if csrf else {})}
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
                token=(state/'access-token').read_text().strip()
                session=read('/api/login',{'token':token})
                snapshot=read('/api/snapshot');assert snapshot['runtime_mode']=='native' and snapshot['engine']['available'] is False
                listing=read('/api/service-hub');assert listing['native'] and not listing['operations_enabled'] and not listing['projects']
                with client.open(origin+'/assets/service_hub.js',timeout=3) as response:assert b'renderServiceHubPage' in response.read()
                try:read('/api/service-hub/gateway/apply',{},session['csrf']);raise AssertionError('Runtime operations unexpectedly enabled')
                except urllib.error.HTTPError as error:assert error.code==409;error.close()
                read('/api/logout',{},session['csrf'])
                print(json.dumps({'passed':True,'real_gunicorn':True,'docker_connected':False,'private_state':True,'login_and_csrf':True,'runtime_operations':False}))
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:child.wait(timeout=15)
                    except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
                log.flush()
                if child.returncode not in (0,-15,143):
                    log.seek(0);raise AssertionError('Native process failed: '+log.read()[-3000:])


if __name__=='__main__':main()
