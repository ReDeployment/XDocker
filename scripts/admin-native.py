#!/usr/bin/env python3
"""Run XDocker directly in Python. Application projects keep their own launch commands."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/admin'))
from native_runtime import initialize,native_trusted_hosts
from host_metrics import collect, read_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['init','serve','token'])
    parser.add_argument('--state',required=True)
    parser.add_argument('--port',type=int,default=19081)
    parser.add_argument('--reload',action='store_true',help='Reload Python sources in development; runtime operations remain disabled')
    parser.add_argument('--collect',action='store_true',help='Sample this Linux host outside the reloadable web worker')
    parser.add_argument('--operations',action='store_true',help='Allow explicitly granted native service/gateway operations')
    parser.add_argument('--without-docker',action='store_true',help='Disable Docker reads and collect native processes only')
    parser.add_argument('--admin-domain',default='',help='One exact hostname for accessing Admin through the native gateway')
    args=parser.parse_args();state=Path(args.state).resolve()
    if args.action=='init':print(json.dumps(initialize(state,ROOT)));return
    if args.action=='token':
        if read_json(state/'native-runtime.json')!={'schema':1,'checkout':str(ROOT)}:parser.error('State checkout mismatch')
        print((state/'access-token').read_text().strip());return
    if not 1024<=args.port<=65535:parser.error('Use a non-privileged port between 1024 and 65535')
    if args.reload and args.operations:parser.error('--reload and --operations cannot be combined')
    if args.collect and sys.platform!='linux':parser.error('--collect requires Linux; metrics are not simulated on this host')
    native_trusted_hosts(args.admin_domain)
    if read_json(state/'native-runtime.json')!={'schema':1,'checkout':str(ROOT)}:parser.error('Run init for this checkout first')
    if args.operations:
        from native_services import ServiceHub,ServiceError
        from gateway_runtime import GatewayRuntime
        from registry_store import RegistryError
        try:GatewayRuntime(ServiceHub(state)).restore()
        except (OSError,ValueError) as exc:
            code=str(exc) if isinstance(exc,(ServiceError,RegistryError)) else 'XD_GATEWAY_RESTORE_UNCONFIRMED'
            print(code+': review the saved gateway plan in Admin',file=sys.stderr,flush=True)
    env={**os.environ,'ADMIN_NATIVE_STATE':str(state),'ADMIN_NATIVE_OPERATIONS':'true' if args.operations else 'false',
         'ADMIN_NATIVE_HOSTNAME':args.admin_domain,
         'ADMIN_NATIVE_WITHOUT_DOCKER':'true' if args.without_docker else 'false',
         'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(ROOT/'services/admin')}
    from code_sync import CodeSync,CodeSyncWorker
    sync_worker=CodeSyncWorker(CodeSync(state,operations=args.operations));sync_worker.start()
    command=[sys.executable,'-m','gunicorn','--config','python:native_runtime','--bind',f'127.0.0.1:{args.port}','--workers','1','--threads','4','--timeout','30']
    if args.reload:command+=['--reload']
    command+=['native_runtime:create_native_app()']
    child=subprocess.Popen(command,cwd=ROOT/'services/admin',env=env)
    stopping=threading.Event()
    def sampler():
        while not stopping.is_set():
            try:collect(state/'host-metrics',include_docker=not args.without_docker)
            except (OSError,ValueError):print('XD_METRICS_COLLECTION_FAILED',file=sys.stderr,flush=True)
            stopping.wait(15)
    worker=threading.Thread(target=sampler,daemon=True) if args.collect else None
    if worker:worker.start()
    def shutdown(*unused):
        stopping.set()
        sync_worker.stopping.set()
        if child.poll() is None:child.terminate()
    signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown)
    print(f'XDocker native: http://127.0.0.1:{args.port}/#service-hub; metrics={args.collect}, operations={args.operations}',flush=True)
    try:code=child.wait()
    finally:
        shutdown()
        sync_worker.close()
        if worker:worker.join(timeout=2)
    raise SystemExit(code)


if __name__=='__main__':main()
