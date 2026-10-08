#!/bin/sh
set -eu
umask 077
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
die() { echo "$*" >&2; exit 1; }
action=${1:-help}
case $action in
    help) echo 'Usage: sh scripts/admin.sh init|token|start|status|stop'; exit 0 ;;
    init|token|start|status|stop) ;;
    *) die 'Unknown admin action.' ;;
esac
[ -f .env ] || die 'Run hosting.sh init first.'
if [ "$action" = init ]; then
    [ -f services/admin/.env ] || cp services/admin/.env.example services/admin/.env
    python3 - <<'PY'
import os
from pathlib import Path
import re
import secrets
import subprocess
import pwd
owner = Path('.env').stat()
socket = Path('/var/run/docker.sock')
if not socket.exists():
    raise SystemExit('Docker socket not found; run init on the Docker host.')
state = Path('data/admin')
state.mkdir(mode=0o700, parents=True, exist_ok=True)
state.chmod(0o700)
token = state / 'access-token'
if not token.exists():
    token.write_text(secrets.token_urlsafe(48)+'\n')
token.chmod(0o600)
if os.geteuid() == 0:
    os.chown(state, owner.st_uid, owner.st_gid)
    os.chown(token, owner.st_uid, owner.st_gid)
backups = Path('data/instance-backups')
backups.mkdir(mode=0o700, parents=True, exist_ok=True)
backups.chmod(0o700)
if os.geteuid() == 0:
    os.chown(backups, owner.st_uid, owner.st_gid)
p = Path('services/admin/.env')
text = p.read_text()
git = ['git', 'rev-parse', 'HEAD']
if os.geteuid() == 0 and owner.st_uid != 0:
    git = ['runuser', '-u', pwd.getpwuid(owner.st_uid).pw_name, '--'] + git
revision = subprocess.check_output(git, text=True).strip()
for key, value in {'ADMIN_UID':owner.st_uid,'ADMIN_GID':owner.st_gid,
                   'ADMIN_DOCKER_GID':socket.stat().st_gid,
                   'ADMIN_DEPLOY_REVISION':revision,
                   'ADMIN_HOST_ROOT':str(Path.cwd())}.items():
    if re.search(r'^'+key+r'=',text,re.M):
        text = re.sub(r'^'+key+r'=.*$',key+'='+str(value),text,flags=re.M)
    else:
        text += '\n'+key+'='+str(value)+'\n'
p.write_text(text)
p.chmod(0o600)
if os.geteuid() == 0:
    os.chown(p, owner.st_uid, owner.st_gid)
print('Initialized private admin data and runtime UID/GID. Configure ADMIN_IMAGE and enable admin in ENABLED_SERVICES.')
PY
    exit 0
fi
if [ "$action" = token ]; then
    [ -s data/admin/access-token ] || die 'Run sudo sh scripts/admin.sh init first.'
    cat data/admin/access-token
    exit 0
fi
[ -f services/admin/.env ] || die 'Run admin.sh init first.'
. ./.env
. ./services/admin/.env
case " ${ENABLED_SERVICES:-} " in *" admin "*) ;; *) die 'Enable admin in root .env ENABLED_SERVICES first.' ;; esac
case ${ADMIN_PORT:-9080} in ''|*[!0-9]*) die 'Invalid ADMIN_PORT.' ;; esac
[ "${ADMIN_PORT:-9080}" -ge 1 ] && [ "${ADMIN_PORT:-9080}" -le 65535 ] || die 'Invalid ADMIN_PORT.'
case $action in
    start) [ -s data/admin/access-token ] || die 'Run admin.sh init first.'
        sh scripts/hosting.sh compose up -d --no-deps --wait --wait-timeout 120 admin
        echo "XDocker UI: server 127.0.0.1:${ADMIN_PORT:-9080}; forward with SSH to local 19080." ;;
    status) sh scripts/hosting.sh compose ps admin ;;
    stop) sh scripts/hosting.sh compose stop admin ;;
esac
