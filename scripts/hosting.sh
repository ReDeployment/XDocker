#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

die() { echo "$*" >&2; exit 1; }
valid_key() {
    case $1 in ''|*[!a-z0-9-]*) die 'Site keys must contain lowercase letters, digits or hyphens.' ;; esac
}
action=${1:-help}
if [ "$action" = help ]; then
    echo 'Usage: sh scripts/hosting.sh init|list|status|compose <args>'
    echo '       sh scripts/hosting.sh http <site> [--no-pull]'
    echo '       sh scripts/hosting.sh issue-test|issue|https|renew-test|update <site>'
    exit 0
fi
if [ "$action" = init ]; then
    [ -f .env ] || cp .env.example .env
    mkdir -p data/nginx data/acme data/letsencrypt data/events
    for directory in sites/*; do
        [ -d "$directory" ] || continue
        [ -f "$directory/.env" ] || cp "$directory/.env.example" "$directory/.env"
    done
    for directory in services/*; do
        [ -d "$directory" ] || continue
        [ -f "$directory/.env" ] || cp "$directory/.env.example" "$directory/.env"
    done
    [ -f data/nginx/00-default.conf ] || cp nginx/default.conf data/nginx/00-default.conf
    echo 'Edit .env and sites/<site>/.env. Then run: sh scripts/hosting.sh http <site>'
    exit 0
fi
[ -f .env ] || die 'Run init and edit .env first.'
set -f
set -a
. ./.env
set +a

load_site() {
    valid_key "$1"
    [ -f "sites/$1/site.conf" ] || die "Unknown site: $1"
    [ -f "sites/$1/.env" ] || die "Initialize sites/$1/.env first."
    unset SITE_KIND SITE_SERVICE SITE_IMAGE_VAR SITE_DOMAIN SITE_IMAGE SITE_CERT_NAME
    . "./sites/$1/site.conf"
    . "./sites/$1/.env"
    case ${SITE_DOMAIN:-} in ''|*[!a-z0-9.-]*|.*|-*) die "Invalid SITE_DOMAIN for $1." ;; esac
    SITE_CERT_NAME=${SITE_CERT_NAME:-$SITE_DOMAIN}
    case $SITE_CERT_NAME in ''|*[!a-z0-9.-]*|.*|-*) die "Invalid SITE_CERT_NAME for $1." ;; esac
    SITE_KIND=${SITE_KIND:-static}
    if [ "$SITE_KIND" = frp_http ]; then
        case " ${ENABLED_SERVICES:-} " in *" frps "*) ;; *) die 'Enable frps in ENABLED_SERVICES first.' ;; esac
        SITE_SERVICE=frps
        return
    fi
    [ "$SITE_KIND" = static ] || die "Unknown SITE_KIND for $1."
    [ -f "sites/$1/compose.yml" ] || die "Missing site compose file: $1"
    case ${SITE_SERVICE:-} in ''|*[!a-z0-9-]*) die "Invalid SITE_SERVICE for $1." ;; esac
    case ${SITE_IMAGE_VAR:-} in ''|*[!A-Z0-9_]*) die "Invalid SITE_IMAGE_VAR for $1." ;; esac
    case ${SITE_IMAGE:-} in ''|*REPLACE_WITH*) die "Set a published SITE_IMAGE for $1 before enabling it." ;; esac
    export "$SITE_IMAGE_VAR=$SITE_IMAGE"
}

# Compose merges the shared infrastructure with exactly the enabled sites.
COMPOSE_FILE=compose.yml
COMPOSE_PATH_SEPARATOR=:
infrastructure_names=''
for infrastructure in ${ENABLED_SERVICES:-}; do
    [ "$infrastructure" = frps ] || die "Unknown infrastructure service: $infrastructure"
    case " $infrastructure_names " in *" $infrastructure "*) die "Duplicate infrastructure: $infrastructure" ;; esac
    infrastructure_names="$infrastructure_names $infrastructure"
    [ -f services/frps/.env ] || die 'Run hosting.sh init and configure services/frps/.env.'
    set -a
    . ./services/frps/.env
    set +a
    case ${FRPS_IMAGE:-} in ''|*REPLACE_WITH*) die 'Set a published FRPS_IMAGE.' ;; esac
    COMPOSE_FILE="$COMPOSE_FILE:services/frps/compose.yml"
done
domains=''
services="$infrastructure_names"
image_vars=''
[ -z "$infrastructure_names" ] || image_vars=' FRPS_IMAGE'
for enabled in ${ENABLED_SITES:-}; do
    load_site "$enabled"
    case " $domains " in *" $SITE_DOMAIN "*) die "Duplicate domain: $SITE_DOMAIN" ;; esac
    domains="$domains $SITE_DOMAIN"
    if [ "$SITE_KIND" = frp_http ]; then continue; fi
    case " $services " in *" $SITE_SERVICE "*) die "Duplicate service: $SITE_SERVICE" ;; esac
    case " $image_vars " in *" $SITE_IMAGE_VAR "*) die "Duplicate image variable: $SITE_IMAGE_VAR" ;; esac
    services="$services $SITE_SERVICE"
    image_vars="$image_vars $SITE_IMAGE_VAR"
    COMPOSE_FILE="$COMPOSE_FILE:sites/$enabled/compose.yml"
done
export COMPOSE_FILE COMPOSE_PATH_SEPARATOR
compose() { docker compose "$@"; }

case "$action" in
    compose) shift; compose "$@"; exit 0 ;;
    status) compose --profile tls ps; exit 0 ;;
    list)
        set +f
        for directory in sites/*; do
            key=${directory#sites/}
            case " ${ENABLED_SITES:-} " in *" $key "*) state=enabled ;; *) state=disabled ;; esac
            echo "$key: $state"
        done
        exit 0 ;;
esac

site=${2:-}
valid_key "$site"
case " ${ENABLED_SITES:-} " in *" $site "*) ;; *) die "Site $site is not enabled in .env ENABLED_SITES." ;; esac
load_site "$site"
mkdir -p data/nginx data/acme data/letsencrypt data/events
[ ! -f data/nginx/default.conf ] || die 'Legacy single-site data/nginx/default.conf found. Follow the migration guide first.'
config="data/nginx/$site.conf"
backup="data/$site.conf.backup"

render() {
    template=$1
    if [ "$SITE_KIND" = frp_http ]; then
        template="frp-$1"
        [ -f data/nginx/01-frp-headers.conf ] || cp nginx/frp-headers.conf data/nginx/01-frp-headers.conf
    fi
    sed -e "s/__DOMAIN__/$SITE_DOMAIN/g" -e "s/__SERVICE__/$SITE_SERVICE/g" -e "s/__CERT_NAME__/$SITE_CERT_NAME/g" \
        "nginx/$template.conf.template" > "$config.tmp"
    mv "$config.tmp" "$config"
}
save_config() {
    [ ! -f "$backup" ] || die "Unresolved backup $backup exists; inspect it first."
    if [ -f "$config" ]; then cp "$config" "$backup"; fi
}
restore_config() {
    if [ -f "$backup" ]; then mv "$backup" "$config"; else rm -f "$config"; fi
}
refresh_acme() {
    if [ -f data/nginx/10-certificates-acme.conf ]; then
        if ! compose run --rm --no-deps --entrypoint python3 certbot \
            /opt/xdocker/scripts/certificates.py acme-config > data/nginx/10-certificates-acme.conf.tmp; then
            rm -f data/nginx/10-certificates-acme.conf.tmp
            return 1
        fi
        mv data/nginx/10-certificates-acme.conf.tmp data/nginx/10-certificates-acme.conf
    fi
}
validate_reload() {
    if ! refresh_acme; then
        restore_config
        die 'Could not refresh inventory ACME paths; site configuration restored.'
    fi
    if ! compose exec -T nginx nginx -t || ! compose exec -T nginx nginx -s reload; then
        restore_config
        refresh_acme || true
        die "Nginx rejected $site configuration; previous file restored."
    fi
    rm -f "$backup"
}
issue() {
    case ${CERTBOT_EMAIL:-} in
        ''|replace-with-*|*[!a-zA-Z0-9@._+-]*) die 'Set CERTBOT_EMAIL in .env.' ;;
    esac
    cert_action=issue
    if [ "${1:-}" = --dry-run ]; then cert_action=issue-test; fi
    compose run --rm --no-deps --entrypoint python3 certbot /opt/xdocker/scripts/certificates.py \
        "$cert_action" "$SITE_CERT_NAME" --email "$CERTBOT_EMAIL" --runtime container --reload container-event
}
case "$action" in
    http)
        case ${3:-} in ''|--no-pull) ;; *) die 'http accepts only --no-pull.' ;; esac
        if [ -f "$config" ] && grep -q 'listen 443 ssl' "$config"; then
            die "HTTPS already configured for $site; use compose or update for routine operations."
        fi
        if [ "${3:-}" != --no-pull ]; then compose pull "$SITE_SERVICE" nginx certbot; fi
        # Start only the selected website, waiting for its static server to be healthy.
        compose up -d --wait --wait-timeout 120 "$SITE_SERVICE"
        save_config
        render http
        if ! compose up -d nginx; then restore_config; die 'Nginx startup failed; previous config restored.'; fi
        validate_reload
        ;;
    issue-test) issue --dry-run ;;
    issue) issue ;;
    https)
        compose run --rm --entrypoint /bin/sh certbot -c \
            'test -s "/etc/letsencrypt/live/$1/fullchain.pem" && test -s "/etc/letsencrypt/live/$1/privkey.pem"' sh "$SITE_CERT_NAME"
        compose run --rm --no-deps --entrypoint python3 certbot /opt/xdocker/scripts/certificates.py \
            check "$SITE_CERT_NAME" --runtime container --require-usable
        [ -f "$config" ] || die "Start HTTP for $site first."
        save_config
        render https
        validate_reload
        compose --profile tls up -d certbot-renew
        ;;
    renew-test)
        compose run --rm --no-deps --entrypoint python3 certbot /opt/xdocker/scripts/certificates.py \
            renew-test "$SITE_CERT_NAME" --runtime container
        ;;
    update)
        [ "$SITE_KIND" != frp_http ] || die 'FRP routes share frps; update the infrastructure image and use frp.sh start.'
        compose pull "$SITE_SERVICE"
        compose up -d --wait --wait-timeout 120 "$SITE_SERVICE"
        ;;
    *) die "Unknown action: $action" ;;
esac
