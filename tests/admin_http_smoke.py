"""Real packaged Gunicorn on disposable state and a loopback ephemeral port."""
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]


def cli(*args):return subprocess.check_output(['docker',*args],text=True).strip()


def main():
    image=os.environ['ADMIN_SMOKE_IMAGE'];revision=os.getenv('ADMIN_SMOKE_REVISION')
    created=[]
    # Docker Desktop exposes the VM's socket group 0; Linux uses the host socket group.
    socket_gid=0 if sys.platform=='darwin' else Path('/var/run/docker.sock').stat().st_gid
    with tempfile.TemporaryDirectory(prefix='xdocker-admin-http-') as temp:
        root=Path(temp)
        for name in ('state','config/sites','config/instances','reports','backups'):(root/name).mkdir(parents=True)
        token=secrets.token_urlsafe(48)
        (root/'state/access-token').write_text(token);(root/'state/access-token').chmod(0o600)
        (root/'config/root.env').write_text('ENABLED_SITES=""\nENABLED_SERVICES=""\n');(root/'config/root.env').chmod(0o600)
        (root/'config/certificates.json').write_text('{"certificates":[]}')
        (root/'config/frpc-template.ini').write_text('')
        (root/'config/powerx-versions.json').write_text('{"schema":1,"versions":[]}')
        try:
            identifier=cli('run','-d','--pull=never','--name','xdocker-http-acceptance-'+secrets.token_hex(4),
                '--read-only','--cap-drop=ALL','--security-opt','no-new-privileges:true','--tmpfs','/tmp:size=64m,mode=1777',
                '--user',f'{os.getuid()}:{os.getgid()}','--group-add',str(socket_gid),'--publish','127.0.0.1::8080',
                '--env','ADMIN_BACKUP_SCHEDULER_ENABLED=false','--env','ADMIN_HOST_ROOT='+str(root),
                '--mount','type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock',
                '--mount','type=bind,source='+str(root/'state')+',target=/state',
                '--mount','type=bind,source='+str(root/'config')+',target=/config,readonly',
                '--mount','type=bind,source='+str(root/'reports')+',target=/reports,readonly',
                '--mount','type=bind,source='+str(root/'backups')+',target=/backups',image)
            created.append(identifier)
            published=cli('port',identifier,'8080/tcp');assert published.startswith('127.0.0.1:')
            base='http://'+published
            client=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

            def request(path,payload=None,csrf=None,origin=True):
                headers={};body=None
                if payload is not None:
                    body=json.dumps(payload).encode();headers={'Content-Type':'application/json'}
                    if origin:headers['Origin']=base
                    if csrf is not None:headers['X-CSRF-Token']=csrf
                try:
                    with client.open(urllib.request.Request(base+path,body,headers),timeout=8) as response:
                        return response.status,response.read(),dict(response.headers)
                except urllib.error.HTTPError as response:
                    try:return response.code,response.read(),dict(response.headers)
                    finally:response.close()

            deadline=time.monotonic()+45
            while True:
                try:
                    if request('/healthz')[0]==200:break
                except OSError:pass
                if time.monotonic()>deadline:raise RuntimeError('Temporary packaged HTTP service did not become healthy')
                time.sleep(.2)
            for path in ('/api/preflight','/api/backup-plans','/api/image-versions','/api/github','/api/github/summary'):assert request(path)[0]==401
            assert request('/api/login',{'token':token},origin=False)[0]==403
            status,body,headers=request('/api/login',{'token':token});assert status==200
            csrf=json.loads(body)['csrf']
            for path in ('/api/preflight','/api/backup-plans','/api/image-versions','/api/github','/api/snapshot'):
                status,body,headers=request(path);assert status==200,(path,status)
                assert token.encode() not in body;assert headers.get('Cache-Control')=='no-store'
            if revision:assert json.loads(request('/api/snapshot')[1])['image_revision']==revision
            assert json.loads(request('/api/backup-plans')[1])['scheduler_enabled'] is False
            assert json.loads(request('/api/image-versions')[1])['candidates']==[]
            assert json.loads(request('/api/preflight/docker-engine')[1])['result']['status']=='pass'
            assert json.loads(request('/api/preflight/host-compose')[1])['result']['status']=='unknown'
            assert request('/api/image-versions/unregistered')[0]==404
            assert json.loads(request('/api/github')[1])['auto_enabled'] is False
            assert len(json.loads(request('/api/github')[1])['projects'])==6
            for name in ('index.html','app.js','github.js','style.css'):
                path='/' if name=='index.html' else '/assets/'+name
                expected=(ROOT/'services/admin/static'/name).read_bytes()
                assert hashlib.sha256(request(path)[1]).digest()==hashlib.sha256(expected).digest(),name
            assert request('/api/logout',{},'wrong-csrf')[0]==403
            assert request('/api/logout',{},csrf)[0]==200
            assert request('/api/backup-plans')[0]==401
            print('Real packaged HTTP passed: non-root/read-only runtime, auth/Origin/CSRF/logout, default scheduler off, new APIs and exact assets.')
        finally:
            for identifier in created:subprocess.run(['docker','rm','-f','-v',identifier],check=True,stdout=subprocess.DEVNULL)


if __name__=='__main__':main()
