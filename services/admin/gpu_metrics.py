"""Bounded, read-only DRM sampling. No device opens, commands or driver changes."""
import hashlib
from itertools import islice
import math
import os
from pathlib import Path
import re
import stat

MAX_GPUS = 8
METRICS = ('utilization_percent', 'vram_total_bytes', 'vram_used_bytes',
           'shared_total_bytes', 'shared_used_bytes', 'temperature_celsius')
PCI = re.compile(r'^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$')
HEX_ID = re.compile(r'^[0-9a-f]{4}:[0-9a-f]{4}$')
IDENTIFIER = re.compile(r'^[0-9a-f]{64}$')


def empty_gpu(state='unavailable'):
    return {'state':state, 'devices':[], 'limit_reached':False}


def _read(path, root):
    # DRM and hwmon contain kernel symlinks. Resolve only inside the sysfs root,
    # then refuse a swapped final symlink or a non-regular node (including FIFOs).
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root): raise ValueError('outside sysfs')
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('not a sysfs attribute')
        raw = os.read(fd, 4097)
        if len(raw)>4096: raise ValueError('oversized attribute')
        return raw.decode('utf-8').strip()
    finally:
        os.close(fd)


def _text(path, root):
    try: return _read(path, root)
    except (OSError, ValueError, UnicodeError, RuntimeError): return ''


def _numeric(path, root, minimum, maximum, scale=1):
    raw = _text(path, root)
    if not re.fullmatch(r'-?[0-9]{1,20}', raw): return None
    value = int(raw) if scale==1 else int(raw)/scale
    return value if minimum<=value<=maximum else None


def _device(device, root):
    info = dict(line.split('=',1) for line in _text(device/'uevent',root).splitlines() if '=' in line)
    bus = info.get('PCI_SLOT_NAME','').lower()
    if not PCI.fullmatch(bus): bus = ''
    pci_id = info.get('PCI_ID','').lower()
    if not HEX_ID.fullmatch(pci_id): pci_id = ''
    driver = info.get('DRIVER','')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', driver): driver = 'unknown'
    vendor = {'1002':'AMD','10de':'NVIDIA','8086':'Intel'}.get(pci_id[:4], '未知厂商')
    integrated = True if pci_id=='1002:1638' else None
    name = _text(device/'product_name',root)
    if not name or len(name)>120 or not name.isprintable():
        name = 'AMD Radeon Vega / Cezanne' if integrated else f'{vendor} GPU'+(f' ({pci_id})' if pci_id else '')
    row = {'id':hashlib.sha256((bus or str(device.relative_to(root))).encode()).hexdigest(),
           'name':name, 'vendor':vendor, 'driver':driver, 'bus_id':bus,
           'pci_id':pci_id, 'integrated':integrated, **dict.fromkeys(METRICS)}
    if driver=='amdgpu':
        row['utilization_percent'] = _numeric(device/'gpu_busy_percent',root,0,100)
        for prefix, attribute in (('vram','vram'),('shared','gtt')):
            total = _numeric(device/f'mem_info_{attribute}_total',root,1,2**60)
            used = _numeric(device/f'mem_info_{attribute}_used',root,0,2**60)
            row[prefix+'_total_bytes'] = int(total) if total is not None else None
            row[prefix+'_used_bytes'] = int(used) if used is not None and total is not None and used<=total else None
        try:
            monitors = sorted(islice((device/'hwmon').iterdir(),16), key=lambda p:p.name)
        except OSError: monitors = []
        for monitor in monitors:
            row['temperature_celsius'] = _numeric(monitor/'temp1_input',root,-40,200,1000)
            if row['temperature_celsius'] is not None: break
    row['unavailable_metrics'] = [k for k in METRICS if row[k] is None]
    row['state'] = ('unsupported' if driver!='amdgpu' else
                    'unavailable' if len(row['unavailable_metrics'])==len(METRICS) else
                    'partial' if row['unavailable_metrics'] else 'ready')
    return row


def gpu_sample(sys_root=Path('/sys')):
    try:
        root = Path(sys_root).resolve(strict=True)
        directory = root/'class/drm'
        devices, seen, limit = [], set(), False
        # Stop iterating even when a malformed fixture/host exposes many entries.
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index>=256: limit=True; break
                if not re.fullmatch(r'card[0-9]+',entry.name): continue
                try:
                    device = (Path(entry.path)/'device').resolve(strict=True)
                    if not device.is_relative_to(root): raise ValueError('outside sysfs')
                    if device in seen: continue
                    seen.add(device)
                    if len(devices)>=MAX_GPUS: limit=True; break
                    devices.append(_device(device,root))
                except (OSError, ValueError, RuntimeError):
                    return empty_gpu()
        devices.sort(key=lambda d:(d['bus_id'],d['id']))
        state = 'not-detected' if not devices else 'ready' if all(d['state']=='ready' for d in devices) and not limit else 'partial'
        return {'state':state, 'devices':devices, 'limit_reached':limit}
    except (OSError, ValueError, RuntimeError):
        return empty_gpu()


def public_gpu(raw):
    """Keep malformed optional GPU data from suppressing valid CPU/RAM reports."""
    if raw is None: return empty_gpu('not-collected')
    try:
        if not isinstance(raw,dict) or type(raw.get('limit_reached')) is not bool: raise ValueError()
        devices = raw['devices']
        if not isinstance(devices,list) or len(devices)>MAX_GPUS: raise ValueError()
        result, seen = [], set()
        for row in devices:
            if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not IDENTIFIER.fullmatch(row['id']) or row['id'] in seen: raise ValueError()
            safe = {'id':row['id']}
            for k in ('name','vendor','driver','bus_id','pci_id'):
                v = row[k]
                if not isinstance(v,str) or len(v)>120 or (v and not v.isprintable()): raise ValueError()
                safe[k] = v
            if safe['bus_id'] and not PCI.fullmatch(safe['bus_id']): raise ValueError()
            if safe['pci_id'] and not HEX_ID.fullmatch(safe['pci_id']): raise ValueError()
            if row.get('integrated') is not None and type(row['integrated']) is not bool: raise ValueError()
            safe['integrated'] = row.get('integrated')
            for k in METRICS:
                v = row.get(k)
                lo,hi = (-40,200) if k=='temperature_celsius' else (0,100) if k=='utilization_percent' else (0,2**60)
                if v is not None and (type(v) not in (int,float) or not math.isfinite(v) or not lo<=v<=hi): raise ValueError()
                safe[k] = v
            for prefix in ('vram','shared'):
                total,used = (safe[prefix+k] for k in ('_total_bytes','_used_bytes'))
                if used is not None and (total is None or total<=0 or used>total): raise ValueError()
            # Unsupported drivers cannot acquire plausible-looking zero metrics.
            if safe['driver']!='amdgpu': safe.update(dict.fromkeys(METRICS))
            safe['unavailable_metrics'] = [k for k in METRICS if safe[k] is None]
            safe['state'] = ('unsupported' if safe['driver']!='amdgpu' else
                             'unavailable' if len(safe['unavailable_metrics'])==len(METRICS) else
                             'partial' if safe['unavailable_metrics'] else 'ready')
            result.append(safe); seen.add(safe['id'])
        state = raw.get('state')
        if result: state = 'ready' if all(d['state']=='ready' for d in result) and not raw['limit_reached'] else 'partial'
        elif state not in ('not-detected','unavailable','not-collected'): raise ValueError()
        return {'state':state, 'devices':result, 'limit_reached':raw['limit_reached']}
    except (ValueError, TypeError, KeyError, OverflowError):
        return empty_gpu()
