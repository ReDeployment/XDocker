#!/usr/bin/env python3
"""Stage explicit SMART device configuration and render hardened root oneshot units.

Never installs units, changes permissions or starts services. Review the private
staging files, then install the standalone source under a root-owned fixed SHA.
"""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/admin'))
from smart_sensors import validate_config,device_identity


def configuration(specs):
    rows=[]
    for spec in specs:
        name,separator,kind=spec.partition(':')
        if not separator or not re.fullmatch(r'nvme[0-9]+n[0-9]+|sd[a-z]{1,4}',name) or kind not in ('nvme','ata','sat'):raise ValueError('XD_SMART_DEVICE_INVALID')
        device=Path('/dev')/name
        aliases=sorted(p for p in Path('/dev/disk/by-id').iterdir() if '-part' not in p.name and p.resolve()==device and re.fullmatch(r'[a-zA-Z0-9_.:+-]{1,200}',p.name))
        preferred=[p for p in aliases if p.name.startswith('nvme-' if kind=='nvme' else 'usb-' if kind=='sat' else 'ata-')]
        if not preferred:raise ValueError('XD_SMART_STABLE_ID_REQUIRED')
        alias=preferred[0];device_identity(alias,kind)
        rows.append({'path':str(alias),'type':kind})
    return validate_config({'schema':1,'devices':rows})


def quoted(value):
    if not value.startswith('/') or any(c in value for c in '\r\n\0$"\\') or '..' in Path(value).parts:raise ValueError('XD_SMART_PATH_INVALID')
    return '"'+value.replace('%','%%')+'"'


def render(config,sha,group):
    validate_config(config)
    if not re.fullmatch(r'[a-f0-9]{40}',sha) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',group):raise ValueError('XD_SMART_UNIT_INVALID')
    allows=''.join('DeviceAllow='+quoted(r['path'])+' r\n' for r in config['devices'])
    return f'''[Unit]
Description=XDocker fixed read-only SMART sensor probe

[Service]
Type=oneshot
User=root
Group={group}
ExecStart=/usr/bin/python3 -I /usr/local/lib/xdocker-smart/{sha}/smart_sensors.py --config /etc/xdocker-smart/devices.json --output /var/lib/xdocker-smart/report.json
UMask=0027
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_UNIX
RestrictNamespaces=yes
RestrictSUIDSGID=yes
LockPersonality=yes
CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SYS_RAWIO
DevicePolicy=closed
{allows}ReadWritePaths=/var/lib/xdocker-smart
SystemCallFilter=~@mount @reboot @swap
TimeoutStartSec=25
MemoryMax=96M
TasksMax=8
Nice=10
'''


TIMER='''[Unit]
Description=Read XDocker SMART sensors every minute without waking sleeping disks

[Timer]
OnBootSec=30
OnUnitActiveSec=60
AccuracySec=5
Unit=xdocker-smart.service

[Install]
WantedBy=timers.target
'''


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind',choices=('config','service','timer'))
    parser.add_argument('--device',action='append',default=[])
    parser.add_argument('--config');parser.add_argument('--sha');parser.add_argument('--group')
    args=parser.parse_args()
    if args.kind=='config':print(json.dumps(configuration(args.device)))
    elif args.kind=='timer':print(TIMER,end='')
    else:print(render(json.loads(Path(args.config).read_text()),args.sha or '',args.group or ''),end='')
