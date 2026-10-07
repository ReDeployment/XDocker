#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
[ -f .env ] || { echo 'Run sh scripts/hosting.sh init first.' >&2; exit 1; }
case ${1:-} in
    ghcr)
        nginx=ghcr.io/redeployment/xdocker-nginx:alpine
        certbot=ghcr.io/redeployment/xdocker-certbot:latest
        ;;
    upstream)
        nginx=nginx:alpine
        certbot=certbot/certbot:latest
        ;;
    *) echo 'Usage: sh scripts/runtime-images.sh {ghcr|upstream}' >&2; exit 1 ;;
esac

# Preserve all other settings, and keep backups containing secrets outside Git.
umask 077
mkdir -p data/backups
temporary=$(mktemp data/.runtime-images.XXXXXX)
backup=$(mktemp data/backups/env-before-runtime-images.XXXXXX)
trap 'rm -f "$temporary"' EXIT
cp .env "$backup"
awk -v nginx="$nginx" -v certbot="$certbot" '
    /^[[:space:]]*NGINX_IMAGE=/ { if (!n++) print "NGINX_IMAGE=" nginx; next }
    /^[[:space:]]*CERTBOT_IMAGE=/ { if (!c++) print "CERTBOT_IMAGE=" certbot; next }
    { print }
    END {
        if (!n) print "NGINX_IMAGE=" nginx
        if (!c) print "CERTBOT_IMAGE=" certbot
    }
' .env > "$temporary"
mv "$temporary" .env
printf 'NGINX_IMAGE=%s\nCERTBOT_IMAGE=%s\nBackup: %s\n' "$nginx" "$certbot" "$backup"
