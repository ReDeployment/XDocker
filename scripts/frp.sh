#!/bin/sh
set -eu
umask 077
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
die() { echo "$*" >&2; exit 1; }
action=${1:-help}
if [ "$action" = help ]; then
    echo 'Usage: sh scripts/frp.sh init|verify|start|status'
    echo '       sh scripts/frp.sh client-config <server-address>'
    echo '       FRPC_BIN=/path/to/frpc sh scripts/frp.sh start-client|stop-client'
    exit 0
fi
[ -f services/frps/.env ] || cp services/frps/.env.example services/frps/.env
set -a
. ./services/frps/.env
set +a
case ${FRPS_SUBDOMAIN_HOST:-} in ''|*[!a-z0-9.-]*) die 'Invalid FRPS_SUBDOMAIN_HOST.' ;; esac
case ${FRPS_PORT:-7000} in ''|*[!0-9]*) die 'Invalid FRPS_PORT.' ;; esac
directory=data/frp
if [ "$action" = init ]; then
    mkdir -p "$directory"
    chmod 700 "$directory"
    if [ ! -f "$directory/token" ]; then
        od -An -N32 -tx1 /dev/urandom | tr -d ' \n' > "$directory/token"
        printf '\n' >> "$directory/token"
    fi
    if [ ! -f "$directory/server.crt" ] && [ ! -f "$directory/server.key" ]; then
        openssl req -x509 -newkey rsa:3072 -nodes -days 365 \
            -subj '/CN=xdocker-frps' -addext 'subjectAltName=DNS:xdocker-frps' \
            -keyout "$directory/server.key" -out "$directory/server.crt" >/dev/null 2>&1
    fi
    [ -s "$directory/server.crt" ] && [ -s "$directory/server.key" ] || die 'Incomplete FRP certificate; restore the matching certificate and key.'
    if [ ! -f "$directory/frps.ini" ]; then
        IFS= read -r frp_token < "$directory/token"
        case $frp_token in ''|*[!a-f0-9]*) die 'Invalid private token.' ;; esac
        {
            printf '[common]\nbind_port = 7000\nvhost_http_port = 8080\nsubdomain_host = %s\n' "$FRPS_SUBDOMAIN_HOST"
            printf 'token = %s\ntls_only = true\n' "$frp_token"
            printf 'tls_cert_file = /etc/frp/server.crt\ntls_key_file = /etc/frp/server.key\n'
            printf 'log_level = info\n'
        } > "$directory/frps.ini"
    fi
    chmod 600 "$directory/token" "$directory/server.key" "$directory/frps.ini"
    echo 'Private FRPS configuration initialized. Enable frps in root .env ENABLED_SERVICES.'
    exit 0
fi
if [ "$action" = client-config ]; then
    address=${2:-}
    case $address in ''|*[!a-zA-Z0-9.-]*) die 'Provide an IPv4 address or hostname.' ;; esac
    [ -s "$directory/token" ] && [ -s "$directory/server.crt" ] || die 'Run init first.'
    [ ! -f "$directory/frpc.ini" ] || die 'Client configuration already exists; back it up before generating a replacement.'
    IFS= read -r frp_token < "$directory/token"
    case $frp_token in ''|*[!a-f0-9]*) die 'Invalid private token.' ;; esac
    while IFS= read -r line || [ -n "$line" ]; do
        case $line in *__TOKEN__*) line="${line%%__TOKEN__*}$frp_token${line#*__TOKEN__}" ;; esac
        case $line in *__SERVER_ADDR__*) line="${line%%__SERVER_ADDR__*}$address${line#*__SERVER_ADDR__}" ;; esac
        case $line in *__SERVER_PORT__*) line="${line%%__SERVER_PORT__*}${FRPS_PORT:-7000}${line#*__SERVER_PORT__}" ;; esac
        printf '%s\n' "$line"
    done < clients/frpc/main.ini.example > "$directory/frpc.ini"
    chmod 600 "$directory/frpc.ini"
    echo 'Generated data/frp/frpc.ini. Transfer it and server.crt privately to the client.'
    exit 0
fi
case "$action" in
    verify) sh scripts/hosting.sh compose run --rm --no-deps --entrypoint frps frps verify -c /etc/frp/frps.ini ;;
    start)
        [ -s "$directory/frps.ini" ] || die 'Run init first.'
        sh scripts/hosting.sh compose run --rm --no-deps --entrypoint frps frps verify -c /etc/frp/frps.ini
        sh scripts/hosting.sh compose up -d --wait --wait-timeout 90 frps
        ;;
    status) sh scripts/hosting.sh compose ps frps ;;
    start-client)
        binary=${FRPC_BIN:-frpc}
        [ -s "$directory/frpc.ini" ] && [ -s "$directory/server.crt" ] || die 'Transfer client config and trusted certificate first.'
        [ "$("$binary" --version)" = 0.52.3 ] || die 'Use the matching FRPC 0.52.3 binary.'
        if [ -f "$directory/frpc.pid" ] && kill -0 "$(cat "$directory/frpc.pid")" 2>/dev/null; then die 'Managed client is already running.'; fi
        "$binary" verify -c "$directory/frpc.ini"
        : > "$directory/frpc.log"
        nohup "$binary" -c "$directory/frpc.ini" </dev/null > "$directory/frpc.log" 2>&1 &
        client_pid=$!
        printf '%s\n' "$client_pid" > "$directory/frpc.pid"
        sleep 2
        kill -0 "$client_pid" 2>/dev/null || die 'FRPC exited during startup; inspect the private log. Exit code alone does not prove login success.'
        echo 'Started managed FRPC; private log is data/frp/frpc.log.'
        ;;
    stop-client)
        [ -f "$directory/frpc.pid" ] || die 'No managed client PID.'
        pid=$(cat "$directory/frpc.pid")
        case $pid in ''|*[!0-9]*) die 'Invalid managed PID.' ;; esac
        command=$(ps -p "$pid" -o command= 2>/dev/null || true)
        case $command in *frpc*'-c data/frp/frpc.ini'*) kill "$pid" ;; '') ;; *) die 'PID no longer belongs to the managed client.' ;; esac
        rm -f "$directory/frpc.pid"
        ;;
    *) die 'Unknown FRP action.' ;;
esac
