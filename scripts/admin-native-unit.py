#!/usr/bin/env python3
"""Render the native Admin's systemd unit; never installs, enables or starts it."""
import argparse
from pathlib import PurePosixPath
import re


def quoted(value):
    if not value.startswith('/') or any(c in value for c in '\n\r\0$"\\') or '..' in PurePosixPath(value).parts:
        raise ValueError('An absolute reviewed Linux path is required')
    return '"'+value.replace('%','%%')+'"'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('checkout','state','python','user','group'):parser.add_argument('--'+key,required=True)
    parser.add_argument('--port',type=int,default=19081)
    parser.add_argument('--operations',action='store_true')
    parser.add_argument('--gateway-web-ports',action='store_true',help='Grant CAP_NET_BIND_SERVICE for the native gateway on HTTP 80 / HTTPS 443')
    args=parser.parse_args()
    if not all(re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',v) for v in (args.user,args.group)) or not 1024<=args.port<=65535:parser.error('Invalid user, group or port')
    if args.gateway_web_ports and not args.operations:parser.error('--gateway-web-ports requires --operations')
    capabilities='AmbientCapabilities=CAP_NET_BIND_SERVICE\nCapabilityBoundingSet=CAP_NET_BIND_SERVICE\n' if args.gateway_web_ports else ''
    command=f'{quoted(args.python)} {quoted(args.checkout.rstrip("/")+"/scripts/admin-native.py")} serve --state {quoted(args.state)} --port {args.port} --collect'+(' --operations' if args.operations else '')
    print(f'''[Unit]
Description=XDocker native service administration
After=network.target

[Service]
Type=simple
User={args.user}
Group={args.group}
WorkingDirectory={quoted(args.checkout)[1:-1]}
ExecStart={command}
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=40
UMask=0077
NoNewPrivileges=yes
{capabilities}
[Install]
WantedBy=multi-user.target
''',end='')
