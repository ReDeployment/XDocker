"""Run only on an authorized Docker host/CI, never implicitly from unit discovery."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

ROOT=Path(__file__).resolve().parents[1]


def main():
    image=os.environ['HOOK_SMOKE_IMAGE'];revision=os.environ['HOOK_SMOKE_REVISION']
    metadata=json.loads(subprocess.check_output(['docker','image','inspect',image],text=True))[0]
    assert metadata['Config']['Labels']['org.opencontainers.image.revision']==revision
    assert 'receiver:create_app()' in metadata['Config']['Cmd']
    container=None
    with tempfile.TemporaryDirectory(prefix='xdocker-webhook-smoke-') as temporary:
        root=Path(temporary);queue=root/'inbox';credentials=root/'secrets'
        queue.mkdir(mode=0o700);credentials.mkdir(mode=0o700)
        secret=secrets.token_urlsafe(48);file=credentials/'PowerX.secret';file.write_text(secret);file.chmod(0o600)
        try:
            container=subprocess.check_output(['docker','run','-d','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges:true',
                '--user',str(os.getuid())+':'+str(os.getgid()),'--tmpfs','/tmp:size=32m,mode=1777','-p','127.0.0.1::8080',
                '-v',str(queue)+':/inbox','-v',str(credentials)+':/secrets:ro',image],text=True).strip()
            port=subprocess.check_output(['docker','port',container,'8080/tcp'],text=True).strip().split(':')[-1]
            base='http://127.0.0.1:'+port
            for _ in range(80):
                try:
                    with urlopen(base+'/healthz',timeout=2) as response:
                        if response.status==200:break
                except (OSError,HTTPError):time.sleep(.25)
            else:raise AssertionError('Receiver failed to become ready')
            payload=json.dumps({'repository':{'full_name':'ArtisanCloud/PowerX'},'action':'completed',
                'workflow_run':{'id':1,'name':'CI','head_sha':'a'*40,'run_attempt':2,'conclusion':'failure'}}).encode()
            delivery=str(uuid.uuid4())
            headers={'Content-Type':'application/json','X-GitHub-Event':'workflow_run','X-GitHub-Delivery':delivery,
                'X-Hub-Signature-256':'sha256='+hmac.new(secret.encode(),payload,hashlib.sha256).hexdigest()}
            def send(raw=payload):
                try:
                    with urlopen(Request(base+'/github/webhook/PowerX',data=raw,headers=headers),timeout=8) as response:
                        return response.status,json.load(response)
                except HTTPError as error:return error.code,json.load(error)
            assert send()[0]==202
            assert send()[1]['duplicate'] is True
            assert send(payload+b' ')[0]==401
            with sqlite3.connect(queue/'queue.sqlite3') as db:
                assert db.execute('SELECT count(*) FROM events').fetchone()[0]==1
                stored=db.execute('SELECT payload FROM events').fetchone()[0]
                assert secret not in stored and json.loads(stored)['attempt']==2
            subprocess.run(['docker','restart',container],check=True,stdout=subprocess.DEVNULL)
            for _ in range(80):
                try:
                    status,data=send()
                    if status==200:
                        assert data['duplicate'] is True;break
                except OSError:pass
                time.sleep(.25)
            else:raise AssertionError('Delivery dedup did not survive restart')
            # Test the actual published receiver bytes, not merely the build source.
            source=subprocess.check_output(['docker','exec',container,'cat','/app/receiver.py'])
            assert source==(ROOT/'services/github-webhook/receiver.py').read_bytes()
            try:urlopen(base+'/api/github',timeout=2)
            except HTTPError as error:assert error.code==404
            else:raise AssertionError('Receiver exposed an administration endpoint')
            print('Container smoke passed: signed intake, tamper rejection, durable dedup/restart, private mounts and no admin API.')
        finally:
            if container:subprocess.run(['docker','rm','-f',container],check=True,stdout=subprocess.DEVNULL)


if __name__=='__main__':main()
