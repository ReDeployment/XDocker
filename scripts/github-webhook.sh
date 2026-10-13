#!/bin/sh
set -eu
umask 077
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
action=${1:-help}
case $action in
    help) echo 'Usage: sh scripts/github-webhook.sh init|secret <project>|secret-show <project>|start|status|stop'; exit 0 ;;
    init|secret|secret-show|start|status|stop) ;;
    *) echo 'Unknown webhook action.' >&2; exit 1 ;;
esac
[ -f .env ] || { echo 'Run hosting.sh init first.' >&2; exit 1; }
case $action in
    init|secret|secret-show)
        python3 - "$action" "${2:-}" <<'PY'
import os
import json
from pathlib import Path
import re
import secrets
import sys
PROJECTS = json.loads(Path('services/github-webhook/projects.json').read_text())
def secret_for(directory, project):
    path = directory/(project+'.secret')
    if any(p.is_symlink() for p in (path,*path.parents)) or path.stat().st_mode & 0o777 not in (0o400,0o600):
        raise SystemExit('Secret permissions or path are unsafe.')
    if path.stat().st_size > 257: raise SystemExit('Invalid secret file.')
    value = path.read_text().strip()
    if not re.fullmatch('[A-Za-z0-9_-]{32,256}',value): raise SystemExit('Invalid secret file.')
    return value

action, project = sys.argv[1:]
owner = Path('.env').stat()
def private(path, directory=False):
    if any(p.is_symlink() for p in (path,*path.parents)): raise SystemExit('Refusing symlink in private storage.')
    if directory: path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700 if directory else 0o600)
    if os.geteuid() == 0: os.chown(path, owner.st_uid, owner.st_gid)
root = Path('data/github-webhook')
if action == 'init':
    for directory in (root,root/'inbox',root/'secrets'): private(directory, True)
    env = Path('services/github-webhook/.env')
    if env.is_symlink(): raise SystemExit('Refusing symlink runtime environment.')
    if not env.exists(): env.write_text(Path('services/github-webhook/.env.example').read_text())
    text = env.read_text()
    for key,value in {'GITHUB_WEBHOOK_UID':owner.st_uid,'GITHUB_WEBHOOK_GID':owner.st_gid}.items():
        text = re.sub(r'^'+key+r'=.*$', key+'='+str(value), text, flags=re.M) if re.search(r'^'+key+'=',text,re.M) else text+'\n'+key+'='+str(value)+'\n'
    env.write_text(text);private(env)
    print('Initialized private queue and secret directories. No secret, network request or service was created.')
else:
    if project not in PROJECTS: raise SystemExit('Choose one of the six fixed project names.')
    path = root/'secrets'/(project+'.secret')
    if action == 'secret':
        if not path.parent.is_dir(): raise SystemExit('Run github-webhook.sh init first.')
        if any(p.is_symlink() for p in (path,*path.parents)): raise SystemExit('Refusing symlink secret.')
        if not path.exists():
            fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as file: file.write(secrets.token_urlsafe(48)+'\n')
            private(path)
        secret_for(path.parent,project)
        print('Secret ready at '+str(path)+'; existing secrets are preserved. Use secret-show in a private terminal.')
    else:
        print(secret_for(path.parent,project))
PY
        exit 0 ;;
esac
. ./.env
case " ${ENABLED_SERVICES:-} " in *" github-webhook "*) ;; *) echo 'Enable github-webhook in ENABLED_SERVICES first.' >&2; exit 1 ;; esac
case $action in
    start) sh scripts/hosting.sh compose up -d --no-deps --wait --wait-timeout 120 github-webhook ;;
    status) sh scripts/hosting.sh compose ps github-webhook ;;
    stop) sh scripts/hosting.sh compose stop github-webhook ;;
esac
