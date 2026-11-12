"""Native Admin bootstrap: private development state, optional Docker and no legacy recovery."""
import os
from pathlib import Path
import secrets

from host_metrics import atomic_json, private_directory, read_json
from native_services import require
from registry_store import RegistryStore


def initialize(state,checkout):
    root=private_directory(Path(state).resolve());checkout=Path(checkout).resolve()
    marker=root/'native-runtime.json'
    if marker.exists():
        value=read_json(marker)
        require(value=={'schema':1,'checkout':str(checkout)},'XD_NATIVE_CHECKOUT_MISMATCH')
    else:
        require(not (root/'admin.sqlite3').exists() and not (root/'access-token').exists() and not (root/'registry.sqlite3').exists(),'XD_NATIVE_EXISTING_STATE_REQUIRES_REVIEW')
        atomic_json(marker,{'schema':1,'checkout':str(checkout)})
    token=root/'access-token'
    require(not token.is_symlink(),'XD_NATIVE_UNSAFE_TOKEN')
    if not token.exists():
        fd=os.open(token,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:stream.write(secrets.token_urlsafe(48)+'\n');stream.flush();os.fsync(stream.fileno())
    require(token.is_file() and not token.stat().st_mode&0o077 and len(token.read_text().strip())>=32,'XD_NATIVE_UNSAFE_TOKEN')
    config=private_directory(root/'native-config')
    for name in ('sites','instances'):private_directory(config/name)
    if not (config/'root.env').exists():
        fd=os.open(config/'root.env',os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:stream.write('ENABLED_SITES=""\nENABLED_SERVICES=""\n')
    if not (config/'certificates.json').exists():atomic_json(config/'certificates.json',{'certificates':[]})
    for name in ('reports','backups','host-metrics','service-hub'):private_directory(root/name)
    RegistryStore(root)
    return {'initialized':True,'state':str(root),'checkout':str(checkout),'docker_required':False}


def create_native_app():
    from app import create_app
    from docker_api import DockerError
    class NoDocker:
        def request(self,*args,**kwargs):raise DockerError('Docker disabled for this native instance')
    root=Path(os.environ['ADMIN_NATIVE_STATE']).resolve();checkout=Path(__file__).resolve().parents[2]
    require(read_json(root/'native-runtime.json')=={'schema':1,'checkout':str(checkout)},'XD_NATIVE_CHECKOUT_MISMATCH')
    return create_app({'STATE':str(root),'CONFIG':str(root/'native-config'),'HOST_ROOT':str(checkout),
        'REPORTS':str(root/'reports'),'BACKUPS':str(root/'backups'),'HOST_METRICS':str(root/'host-metrics'),
        'NATIVE_MODE':True,'NATIVE_OPERATIONS_ENABLED':os.getenv('ADMIN_NATIVE_OPERATIONS')=='true',
        'BACKUP_SCHEDULER_ENABLED':False,'GITHUB_WATCH_ENABLED':False,'GITHUB_WEBHOOK_ENABLED':False,
        'RELEASE_OBSERVER_ENABLED':False,'DELIVERY_CI_OBSERVER_ENABLED':False},
        docker=NoDocker() if os.getenv('ADMIN_NATIVE_WITHOUT_DOCKER')=='true' else None)
