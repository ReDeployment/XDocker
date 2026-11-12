#!/usr/bin/env python3
"""Render units locally for a reviewed host path; does not install or start anything."""
import argparse
from pathlib import PurePosixPath
import re


def quote(value):
    return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'


def render(root, user, group):
    if not root.startswith('/') or root=='/' or any(v in root for v in ('\n','\r','\x00','$')) or '..' in PurePosixPath(root).parts:
        raise ValueError('An absolute Linux checkout path is required')
    if not all(re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', v) for v in (user,group)):
        raise ValueError('Explicit Linux user and group names are required')
    root = root.rstrip('/')
    return f'''[Unit]
Description=XDocker read-only host resource sampler
After=docker.service

[Service]
Type=oneshot
User={user}
Group={group}
WorkingDirectory={quote(root)}
ExecStart=/usr/bin/python3 {quote(root+'/scripts/host-monitor.py')} --output {quote(root+'/data/host-metrics')}
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_UNIX
ReadWritePaths={quote(root+'/data/host-metrics')}
TimeoutStartSec=30
'''


TIMER='''[Unit]
Description=Sample XDocker host resources every 15 seconds

[Timer]
OnBootSec=30
OnUnitInactiveSec=15
AccuracySec=1
Unit=xdocker-host-monitor.service

[Install]
WantedBy=timers.target
'''

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind',choices=['service','timer'])
    parser.add_argument('--root',required=True)
    parser.add_argument('--user',required=True)
    parser.add_argument('--group',required=True)
    args=parser.parse_args()
    service=render(args.root,args.user,args.group)
    print(service if args.kind=='service' else TIMER,end='')
