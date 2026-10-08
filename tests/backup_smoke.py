"""Real pg_dump/Redis/file backup and isolated pg_restore on disposable CI data."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/admin'))
from backups import InstanceBackups
from docker_api import Docker

def cli(*args):
    return subprocess.check_output(['docker',*args],text=True).strip()

image=os.environ['ADMIN_SMOKE_IMAGE']
engine=Docker()
instance='powerx-ci-'+secrets.token_hex(4)
created=[]
with tempfile.TemporaryDirectory() as temp:
    root=Path(temp)
    config=root/'instances'/instance/'config';config.mkdir(parents=True)
    runtime=root/'data/instances'/instance/'runtime';runtime.mkdir(parents=True)
    (runtime/'uploads').mkdir();(runtime/'uploads/file.txt').write_text('file fixture')
    password=secrets.token_urlsafe(32)
    (config/'postgres-password').write_text(password)
    (config/'redis-password').write_text(password)
    (config/'config.yaml').write_text('install:\n  status: uninstalled\n')
    containers={}
    try:
        for service in ('postgres','redis','backend','web-admin'):
            args=['run','-d','--label','com.docker.compose.project='+instance,'--label','com.docker.compose.service='+service]
            if service=='postgres':
                args+=['-e','POSTGRES_DB=powerx','-e','POSTGRES_USER=powerx','-e','POSTGRES_PASSWORD_FILE=/run/secrets/password',
                       '--mount','type=bind,source='+str(config/'postgres-password')+',target=/run/secrets/password,readonly','pgvector/pgvector:pg16']
            elif service=='redis':
                args+=['--mount','type=bind,source='+str(config/'redis-password')+',target=/run/secrets/password,readonly',
                       '--entrypoint','/bin/sh','redis:7-alpine','-ec','exec redis-server --requirepass "$(cat /run/secrets/password)"']
            else:
                args+=['--mount','type=bind,source='+str(config)+',target=/etc/powerx',
                       '--mount','type=bind,source='+str(runtime)+',target=/data',
                       '--entrypoint','python',image,'-c','import time; time.sleep(600)']
            identifier=cli(*args);created.append(identifier);containers[service]={'Id':identifier}
        for _ in range(60):
            result=subprocess.run(['docker','exec',containers['postgres']['Id'],'pg_isready','-h','127.0.0.1','-U','powerx','-d','powerx'],capture_output=True)
            if result.returncode==0:break
            time.sleep(1)
        cli('exec',containers['postgres']['Id'],'psql','-U','powerx','-d','powerx','-v','ON_ERROR_STOP=1','-c',"CREATE TABLE backup_fixture(id int PRIMARY KEY, value text); INSERT INTO backup_fixture VALUES(1,'preserved');")
        manager=InstanceBackups(engine,root/'data/instance-backups',str(root))
        identifier=secrets.token_hex(16)
        manager.create(instance,containers,identifier)
        folder,record=manager.verify(instance,identifier)
        assert record['state']=='ready'
        manager.drill(instance,identifier,secrets.token_hex(16))
        for service in ('backend','web-admin'):
            assert engine.request('GET','/containers/'+containers[service]['Id']+'/json')['State']['Running']
        assert cli('exec',containers['postgres']['Id'],'psql','-U','powerx','-d','powerx','-Atc','SELECT value FROM backup_fixture WHERE id=1')=='preserved'
        print('Real maintenance backup, Redis RDB validation, bundle checksums, isolated PostgreSQL restore and live-data preservation passed.')
    finally:
        for identifier in created:subprocess.run(['docker','rm','-f','-v',identifier],stdout=subprocess.DEVNULL,check=True)
