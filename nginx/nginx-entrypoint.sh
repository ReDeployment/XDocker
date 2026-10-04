#!/bin/sh
set -eu

# Certbot writes this event only after an actual successful renewal.
# The watcher reloads Nginx without granting Certbot access to the Docker socket.
watch_certificates() {
    previous=$(cat /var/lib/certbot-events/reload 2>/dev/null || true)
    while sleep 30; do
        current=$(cat /var/lib/certbot-events/reload 2>/dev/null || true)
        if [ "$current" != "$previous" ]; then
            if nginx -t && nginx -s reload; then
                previous=$current
                echo 'Certificate renewal loaded by Nginx.'
            else
                echo 'Certificate reload failed; will retry.' >&2
            fi
        fi
    done
}
watch_certificates &
exec /docker-entrypoint.sh nginx -g 'daemon off;'
