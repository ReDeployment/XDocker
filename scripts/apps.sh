#!/bin/sh
set -eu
umask 077
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
die() { echo "$*" >&2; exit 1; }
action=${1:-help}
[ "$action" != help ] || { echo 'Usage: sh scripts/apps.sh init|start|status|logs|credentials|migrate|stop|compose <instance> [args]'; exit 0; }
instance=${2:-}
case $instance in ''|*[!a-z0-9-]*) die 'Invalid instance key.' ;; esac
directory="$root/instances/$instance"
if [ "$action" = init ]; then
    mkdir -p "$directory" "$root/data/instances/$instance"
    chmod 755 "$root/instances" "$directory"
    chmod 700 "$root/data/instances/$instance"
    if [ ! -f "$directory/.env" ]; then
        if [ -f "$directory/.env.example" ]; then cp "$directory/.env.example" "$directory/.env";
        else
            sed "s/COMPOSE_PROJECT_NAME=powerx-dev/COMPOSE_PROJECT_NAME=$instance/" apps/powerx/.env.example > "$directory/.env"
        fi
    fi
    if [ ! -f "$directory/instance.json" ]; then
        printf '{"app":"powerx","project":"%s","enabled":true}\n' "$instance" > "$directory/instance.json"
        chmod 644 "$directory/instance.json"
    fi
    echo "Edit instances/$instance/.env. This command does not initialize a database."
    exit 0
fi
[ -f "$directory/.env" ] && [ -f "$directory/instance.json" ] || die 'Run apps.sh init first.'
set -a
. "$directory/.env"
set +a
[ "${COMPOSE_PROJECT_NAME:-}" = "$instance" ] || die 'COMPOSE_PROJECT_NAME must match the instance directory.'
for image in "${POWERX_BACKEND_IMAGE:-}" "${POWERX_WEB_IMAGE:-}"; do
    case $image in ''|*REPLACE_WITH*) die 'Select published application images first.' ;; esac
done
export POWERX_DATA_DIR="$root/data/instances/$instance"
compose() { docker compose -p "$instance" --project-directory "$directory" --env-file "$directory/.env" -f "$root/apps/powerx/compose.yml" "$@"; }
case $action in
    start)
        compose run --rm --no-deps init init
        compose up -d --wait --wait-timeout 180 postgres redis
        compose run --rm --no-deps init bootstrap
        compose up -d --wait --wait-timeout 300 backend web-admin
        ;;
    credentials) compose run --rm --no-deps init credentials ;;
    migrate) compose run --rm --no-deps init migrate ;;
    status) compose ps ;;
    logs) compose logs --tail 100 backend web-admin ;;
    stop) compose stop backend web-admin postgres redis ;;
    compose) shift 2; compose "$@" ;;
    *) die 'Unknown application action.' ;;
esac
