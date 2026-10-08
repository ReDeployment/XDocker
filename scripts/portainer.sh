#!/bin/sh
set -eu
umask 077
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
die() { echo "$*" >&2; exit 1; }
action=${1:-help}
case $action in
    help)
        echo 'Usage: sh scripts/portainer.sh start|status|restart|stop|setup-token'
        exit 0 ;;
    start|status|restart|stop|setup-token) ;;
    *) die 'Unknown Portainer action.' ;;
esac
[ -f .env ] && [ -f services/portainer/.env ] || die 'Run hosting.sh init first.'
. ./.env
case " ${ENABLED_SERVICES:-} " in *" portainer "*) ;; *) die 'Enable portainer in root .env ENABLED_SERVICES first.' ;; esac
. ./services/portainer/.env
case ${PORTAINER_PORT:-9000} in ''|*[!0-9]*) die 'Invalid PORTAINER_PORT.' ;; esac
[ "${PORTAINER_PORT:-9000}" -ge 1 ] && [ "${PORTAINER_PORT:-9000}" -le 65535 ] || die 'Invalid PORTAINER_PORT.'
compose() { sh scripts/hosting.sh compose "$@"; }
case $action in
    start)
        mkdir -p data/portainer
        chmod 700 data/portainer
        compose up -d --no-deps --wait --wait-timeout 120 portainer
        echo "Portainer listens on server 127.0.0.1:${PORTAINER_PORT:-9000}. Use an SSH tunnel; see docs/guides/portainer/README.md."
        ;;
    status) compose ps portainer ;;
    restart) compose restart portainer ;;
    stop) compose stop portainer ;;
    setup-token)
        # Explicit operator command: never put this output in shared logs or Git.
        compose logs --no-color --tail 100 portainer | sed -n 's/.*setup_token=\([^[:space:]]*\).*/\1/p' | tail -n 1
        ;;
esac
