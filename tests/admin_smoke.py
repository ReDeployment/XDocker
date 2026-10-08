"""CI-only real Docker integration, operating solely on an isolated disposable container."""
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import json

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/admin'))
from app import create_app

image=os.environ['ADMIN_SMOKE_IMAGE']
with tempfile.TemporaryDirectory() as temp:
    root=Path(temp)
    for directory in ('state','config/sites','reports'):(root/directory).mkdir(parents=True)
    token=secrets.token_urlsafe(48)
    (root/'state/access-token').write_text(token)
    (root/'config/root.env').write_text('ENABLED_SITES=""\nENABLED_SERVICES=""\n')
    (root/'config/certificates.json').write_text('{"certificates":[]}')
    identifier=subprocess.check_output(['docker','run','-d','--label','com.docker.compose.project=xdocker-web-hosting',
        '--label','com.docker.compose.service=nginx','--name','xdocker-admin-ci-'+secrets.token_hex(4),
        '--entrypoint','python',image,'-u','-c',"import time; print('admin-real-smoke'); time.sleep(600)"],text=True).strip()
    try:
        app=create_app({'TESTING':True,'STATE':str(root/'state'),'CONFIG':str(root/'config'),'REPORTS':str(root/'reports')})
        client=app.test_client()
        auth=client.post('/api/login',json={'token':token},headers={'Origin':'http://localhost'})
        assert auth.status_code==200
        headers={'Origin':'http://localhost','X-CSRF-Token':auth.json['csrf']}
        snapshot=client.get('/api/snapshot')
        assert snapshot.status_code==200,snapshot.text
        assert any(s['id']==identifier for s in snapshot.json['services'])
        logs=client.get('/api/services/'+identifier+'/logs')
        assert 'admin-real-smoke' in logs.json['text']
        assert client.get('/api/services/'+identifier+'/stats').status_code==200
        for action in ('stop','start','restart'):
            response=client.post('/api/services/'+identifier+'/action',json={'action':action,'confirmation':'nginx'},headers=headers)
            assert response.status_code==202,response.text
            deadline=time.monotonic()+60
            while time.monotonic()<deadline:
                row=next(j for j in client.get('/api/jobs').json['jobs'] if j['id']==response.json['id'])
                if row['state']!='running':break
                time.sleep(.2)
            assert row['state']=='success',row
        assert client.post('/api/logout',json={},headers=headers).status_code==200
        assert client.get('/api/snapshot').status_code==401
        print('Real Docker snapshot, logs, stats, stop/start/restart and logout passed.')
    finally:
        subprocess.run(['docker','rm','-f',identifier],check=True,stdout=subprocess.DEVNULL)
