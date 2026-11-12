#!/usr/bin/env python3
"""Review/import host-private endpoint and unit grants. Never starts application projects."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/admin'))
from host_metrics import read_json
from native_services import ServiceHub, default_policy
from gateway_runtime import GatewayRuntime
from registry_store import RegistryStore

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['policy-example','policy-import','project-register','service-status','status','gateway-preview','registry-status','registry-backup'])
    parser.add_argument('--state',required=True)
    parser.add_argument('--file')
    parser.add_argument('--alias')
    args=parser.parse_args()
    if args.action=='policy-example':print(json.dumps(default_policy(),indent=2));raise SystemExit()
    if args.action in ('registry-status','registry-backup'):
        store=RegistryStore(args.state)
        if args.action=='registry-backup':
            if not args.file:parser.error('--file must be a new private backup destination')
            print(json.dumps(store.backup(args.file)))
        else:
            with store.connection() as db:
                counts={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('projects','endpoints','monitor_apps','monitor_bindings','native_operations','registry_migrations')}
            print(json.dumps({'engine':'sqlite','database':str(store.path),'schema':1,'counts':counts}))
        raise SystemExit()
    hub=ServiceHub(args.state)
    if args.action=='policy-import':
        if not args.file:parser.error('--file is required; input must be a private 0600 JSON file')
        print(json.dumps(hub.import_policy(read_json(Path(args.file).resolve()))))
    elif args.action=='project-register':
        if not args.file:parser.error('--file is required; input must be private 0600 JSON')
        print(json.dumps(hub.save(read_json(Path(args.file).resolve())),ensure_ascii=False))
    elif args.action=='service-status':
        if not args.alias or args.alias not in hub.policy()['services']:parser.error('Choose an alias from the host-private service grants')
        print(json.dumps(hub.adapter.status(hub.policy()['services'][args.alias]),ensure_ascii=False))
    elif args.action=='status':print(json.dumps(hub.listing(),ensure_ascii=False))
    else:print(json.dumps(GatewayRuntime(hub).preview(),ensure_ascii=False))
