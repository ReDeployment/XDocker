#!/bin/sh
set -eu
trap 'exit 0' TERM INT
while :; do
    if ! certbot renew --non-interactive --webroot --webroot-path /var/www/certbot \
        --deploy-hook 'date -u +%s > /var/lib/certbot-events/reload'; then
        echo 'Certificate renewal failed; inspect certbot-renew logs.' >&2
    fi
    sleep 43200 &
    wait $! || true
done
