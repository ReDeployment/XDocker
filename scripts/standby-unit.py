#!/usr/bin/env python3
"""Render the selected-disk hd-idle unit; never install or start it."""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/admin'))
from smart_sensors import validate_config
from importlib.util import spec_from_file_location,module_from_spec
spec=spec_from_file_location('smart_unit',ROOT/'scripts/smart-monitor-unit.py');smart_unit=module_from_spec(spec);spec.loader.exec_module(smart_unit)


def device_unit(path):
    # systemd-escape --path --suffix=device; hyphens in path components escape.
    text=path.strip('/');result=''
    for byte in text.encode():
        char=chr(byte)
        result+='-' if char=='/' else char if char.isascii() and (char.isalnum() or char in '_.:') else '\\x'+format(byte,'02x')
    return result+'.device'


def render(config,seconds=1800):
    validate_config(config)
    selected=[r for r in config['devices'] if r.get('idle_pause_seconds') is not None]
    if len(selected)!=2 or any(r['type']!='sat' for r in selected) or type(seconds) is not int or not 300<=seconds<=3600:raise ValueError('XD_STANDBY_SCOPE_INVALID')
    paths=[r['path'] for r in selected];devices=' '.join(device_unit(p) for p in paths)
    # Pinned Go implementation: Ubuntu's C 1.05 lacks ATA command support.
    command='/usr/local/lib/xdocker-standby/hd-idle-1.21 -i 0 -c ata -s 0'+''.join(' -a '+smart_unit.quoted(p)+' -i '+str(seconds)+' -c ata' for p in paths)
    allows=''.join('DeviceAllow='+smart_unit.quoted(p)+' rw\n' for p in paths)
    return f'''[Unit]
Description=XDocker idle standby for the two approved USB backup disks
BindsTo={devices}
After={devices}
JobTimeoutSec=30

[Service]
Type=simple
User=root
ExecStart={command}
Restart=on-failure
RestartSec=5
TimeoutStopSec=10
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
CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SYS_RAWIO
DevicePolicy=closed
{allows}SystemCallFilter=~@mount @reboot @swap
Environment=GOMAXPROCS=1
MemoryMax=64M
TasksMax=16
SyslogIdentifier=xdocker-standby
LogRateLimitIntervalSec=30s
LogRateLimitBurst=30

[Install]
WantedBy=multi-user.target
'''


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',required=True)
    args=parser.parse_args();print(render(json.loads(Path(args.config).read_text())),end='')
