#!/usr/bin/env python3
"""Bootstrap approved source roots/SSH identities; all project plans live in SQLite."""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'services/admin'))
from code_sync import CodeSync,default_policy
from host_metrics import read_json
from sync_agent import agent_sha
from sync_runtime import digest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['policy-example','policy-import','inventory','bootstrap-workspace','status','agent-example','agent-sha','backup'])
    parser.add_argument('--state',required=True);parser.add_argument('--file');args=parser.parse_args()
    if args.action=='policy-example':print(json.dumps(default_policy(),indent=2));return
    if args.action=='agent-sha':print(agent_sha());return
    if args.action=='agent-example':
        print(json.dumps({'schema':2,'roots':['/data/workspace'],'credentials':{},'targets':{'example':{'directory':'/data/workspace/example-copy','repository':'example/project','branch':'main','credential':None,'service':None}}},indent=2));return
    hub=CodeSync(args.state)
    if args.action=='policy-import':
        if not args.file:parser.error('--file is required (private 0600 policy)')
        print(json.dumps(hub.import_policy(read_json(Path(args.file).absolute())),ensure_ascii=False))
    elif args.action=='inventory':print(json.dumps(hub.inventory(True),ensure_ascii=False))
    elif args.action=='bootstrap-workspace':
        sources=hub.store.all('sources');known={s['directory'] for s in sources};ids={s['id'] for s in sources};created=[]
        for item in hub.inventory(True):
            if item['directory'] in known:continue
            identifier=item['project'] or re.sub('[^a-z0-9-]+','-',item['name'].lower()).strip('-')[:50] or 'project'
            if identifier in ids:identifier=identifier[:50]+'-'+digest(item['directory'])[:8]
            source=hub.save('sources',{'id':identifier,'revision':1,'project':item['project'],'name':item['name'],'directory':item['directory'],
                'repository':item['repository'],'branch':item['branch'],
                'credential':None,'enabled':False})
            hub.save('schedules',{'id':identifier,'revision':1,'source':identifier,'mode':'upload','hour':2,'minute':0,'timezone':'Asia/Shanghai','enabled':False})
            created.append(source['id']);known.add(item['directory']);ids.add(identifier)
        print(json.dumps({'created':created,'enabled':False,'default_time':'02:00 Asia/Shanghai'},ensure_ascii=False))
    elif args.action=='backup':
        if not args.file:parser.error('--file is required and must not exist')
        import sqlite3,os,hashlib
        destination=Path(args.file).absolute();destination.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(fd)
        with hub.store.db() as source:
            with sqlite3.connect(destination) as target:source.backup(target)
        print(json.dumps({'file':str(destination),'sha256':hashlib.sha256(destination.read_bytes()).hexdigest()}))
    else:print(json.dumps(hub.listing(),ensure_ascii=False))


if __name__=='__main__':main()
