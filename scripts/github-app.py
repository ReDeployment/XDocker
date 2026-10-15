#!/usr/bin/env python3
"""Install private App files locally, or verify fixed repository access. Never prints credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/admin'))
from github_auth import REPOSITORIES


def private_directory(path):
    if any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('unsafe-directory')
    path.mkdir(parents=True,exist_ok=True,mode=0o700);path.chmod(0o700)


def atomic(path,data):
    if path.is_symlink():raise ValueError('unsafe-file')
    fd,name=tempfile.mkstemp(prefix='.app-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.chmod(name,0o600);os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('init','verify'))
    parser.add_argument('--state',required=True,help='private Admin state directory, e.g. data/admin')
    parser.add_argument('--app-id',type=int)
    parser.add_argument('--installation',action='append',default=[],metavar='OWNER=ID')
    parser.add_argument('--pem',type=Path)
    parser.add_argument('--project',choices=['all',*[name for names in REPOSITORIES.values() for name in names]],default='all',help='verify only this fixed project, or all six')
    parser.add_argument('--replace',action='store_true',help='explicitly replace an existing App configuration')
    args=parser.parse_args()
    try:
        root=Path(args.state)/'github';directory=root/'credentials'
        if args.action=='init':
            if not args.app_id or not 0<args.app_id<2**63 or not args.pem or not args.installation:raise ValueError('missing-inputs')
            installations={}
            for item in args.installation:
                owner,number=item.split('=',1);owner=owner.lower()
                if owner not in REPOSITORIES or owner in installations or not re.fullmatch(r'[1-9][0-9]{0,17}',number):raise ValueError('invalid-installation')
                installations[owner]=int(number)
            if len(set(installations.values()))!=len(installations):raise ValueError('duplicate-installation')
            if any(p.is_symlink() for p in (args.pem,*args.pem.parents)) or not args.pem.is_file() or args.pem.stat().st_size>32768:raise ValueError('invalid-pem-file')
            pem=args.pem.read_bytes()
            if not (pem.startswith(b'-----BEGIN RSA PRIVATE KEY-----') or pem.startswith(b'-----BEGIN PRIVATE KEY-----')):raise ValueError('invalid-pem-format')
            if directory.joinpath('app.json').exists() and not args.replace:raise ValueError('app-already-configured-use-replace')
            for path in (Path(args.state),root,directory,directory/'app-keys'):private_directory(path)
            key_id=hashlib.sha256(pem).hexdigest()
            # New immutable key path + atomic config swap avoids mixing generations during rotation.
            atomic(directory/'app-keys'/(key_id+'.pem'),pem)
            config={'app_id':args.app_id,'installations':installations,'key_id':key_id}
            atomic(directory/'app.json',(json.dumps(config,indent=2)+'\n').encode())
            print(json.dumps({'configured':True,'owners':list(installations),'network_used':False,'service_restarted':False}))
            return 0
        # Explicit verification issues installation credentials, then calls fixed repository GET endpoints.
        from github_auth import describe, token
        from github_watch import GitHubReader
        result=[]
        for owner,names in REPOSITORIES.items():
            for name in names:
                if args.project!='all' and name!=args.project:continue
                repo=('ArtisanCloud' if owner=='artisancloud' else 'Matrix-X')+'/'+name
                try:
                    access=describe(root,repo)
                    if not access['configured'] or access['mode']!='github-app':raise ValueError('app-not-configured')
                    reader=GitHubReader(repo,token(root,repo))
                    metadata,_=reader.get()
                    if str(metadata.get('full_name','')).lower()!=repo.lower():raise ValueError('repository-identity-unconfirmed')
                    branch=metadata.get('default_branch')
                    if not isinstance(branch,str) or not branch:raise ValueError('default-branch-unconfirmed')
                    from urllib.parse import quote
                    commit,_=reader.get('/branches/'+quote(branch,safe=''))
                    sha=commit.get('commit',{}).get('sha')
                    if not isinstance(sha,str) or not re.fullmatch(r'[a-f0-9]{40}',sha):raise ValueError('head-unconfirmed')
                    for path in ('/commits/'+sha+'/check-runs?per_page=1','/actions/runs?per_page=1','/commits/'+sha+'/status',
                        '/issues/comments?per_page=1','/pulls?state=all&per_page=1','/releases?per_page=1'):reader.get(path)
                    result.append({'repository':repo,'status':'verified','repository_writes':False})
                except Exception as error:
                    code=str(error)
                    result.append({'repository':repo,'status':code if re.fullmatch(r'[a-z0-9-]{1,80}',code) else 'verification-unconfirmed','repository_writes':False})
        print(json.dumps({'results':result,'credential_post_only':True,'repository_writes':False},ensure_ascii=False))
        return 0 if all(r['status']=='verified' for r in result) else 2
    except Exception as error:
        code=str(error)
        print(json.dumps({'error':code if re.fullmatch(r'[a-z0-9-]{1,80}',code) else 'app-configuration-unconfirmed'}));return 2


if __name__=='__main__':sys.exit(main())
