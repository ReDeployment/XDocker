#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
# Legacy-host mode needs neither Docker nor website image configuration.
if [ "${1:-}" = --host ]; then
    shift
    exec python3 scripts/certificates.py "$@" --runtime host
fi
if [ -f .env ]; then
    set -a
    . ./.env
    set +a
fi
# ACME-only ingress does not start any business website containers.
if [ "${1:-}" = acme-http ]; then
    [ "$#" = 1 ] || { echo 'acme-http takes no certificate selector.' >&2; exit 1; }
    [ ! -f data/nginx/default.conf ] || { echo 'Migrate legacy default.conf first.' >&2; exit 1; }
    mkdir -p data/nginx data/acme data/letsencrypt data/events
    [ -f data/nginx/00-default.conf ] || cp nginx/default.conf data/nginx/00-default.conf
    target=data/nginx/10-certificates-acme.conf
    backup=data/certificates-acme.conf.backup
    [ ! -f "$backup" ] || { echo 'Inspect the unresolved ACME backup first.' >&2; exit 1; }
    docker compose -f compose.yml run --rm --no-deps --entrypoint python3 certbot \
        /opt/xdocker/scripts/certificates.py acme-config > "$target.tmp"
    if [ -f "$target" ]; then cp "$target" "$backup"; fi
    mv "$target.tmp" "$target"
    if ! docker compose -f compose.yml up -d nginx || \
        ! docker compose -f compose.yml exec -T nginx nginx -t || \
        ! docker compose -f compose.yml exec -T nginx nginx -s reload; then
        if [ -f "$backup" ]; then mv "$backup" "$target"; else rm -f "$target"; fi
        echo 'ACME ingress rejected; previous file restored.' >&2
        exit 1
    fi
    rm -f "$backup"
    echo 'Inventory ACME paths enabled. Business application routes were not created.'
    exit 0
fi
# Certificate-only operations must not depend on enabled website services.
exec docker compose -f compose.yml run --rm --no-deps --entrypoint python3 certbot \
    /opt/xdocker/scripts/certificates.py "$@" --runtime container --reload container-event
