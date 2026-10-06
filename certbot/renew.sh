#!/bin/sh
set -eu
trap 'exit 0' TERM INT
while :; do
    python3 /opt/xdocker/scripts/certificates.py check --runtime container \
        --report /var/lib/certbot-events/certificates-check.json || \
        echo 'Certificate inventory needs attention; see check report and logs.' >&2
    if ! python3 /opt/xdocker/scripts/certificates.py renew --runtime container \
        --reload container-event --report /var/lib/certbot-events/certificates-renew.json; then
        echo 'Certificate renewal has failures or expiry warnings; inspect reports and logs.' >&2
    fi
    # Refresh the expiry report after renewal so it does not retain resolved warnings.
    python3 /opt/xdocker/scripts/certificates.py check --runtime container \
        --report /var/lib/certbot-events/certificates-check.json || \
        echo 'Certificate inventory still needs attention after renewal.' >&2
    sleep 43200 &
    wait $! || true
done
