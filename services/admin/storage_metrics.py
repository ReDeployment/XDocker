"""Read-only local block inventory, filesystem capacity and interval I/O.

No block writes, mount commands, self-tests, privilege escalation or filesystem scans.
"""
import hashlib
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time

from gpu_metrics import _text, _numeric

MAX_DEVICES = 64
MAX_MOUNTS = 128
NAME = re.compile(r'^[a-zA-Z0-9_.!+-]{1,80}$')
IDENTIFIER = re.compile(r'^[a-f0-9]{64}$')
IO_FIELDS = ('read_bytes_per_second','write_bytes_per_second','read_iops','write_iops','busy_percent')


def _require(condition):
    if not condition: raise ValueError('invalid storage report')


def empty_storage(state='unavailable'):
    return {'state':state,'devices':[],'filesystems':[],'limit_reached':False,'unreadable_devices':0}


def _bounded(path, limit=1024*1024):
    fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK)
    try:
        data=os.read(fd,limit+1)
        if len(data)>limit: raise ValueError('storage input limit')
        return data.decode('utf-8')
    finally: os.close(fd)


def _clean(value, limit=160):
    return ''.join(c for c in value if c.isprintable())[:limit]


def _io(raw, old, elapsed, generation):
    result={k:None for k in IO_FIELDS};result.update(state='sampling',interval_seconds=None)
    try:
        values=[int(v) for v in raw.split()]
        if len(values)<11 or any(v<0 or v>2**64 for v in values): raise ValueError()
        counts=[values[k] for k in (0,2,4,6,9)]
    except (ValueError,TypeError): return {**result,'state':'unavailable'},None
    next_state={'generation':generation,'counts':counts}
    if not old or old.get('generation')!=generation or not 0<elapsed<=90: return result,next_state
    previous=old.get('counts')
    if not isinstance(previous,list) or len(previous)!=5 or any(type(v) is not int or v<0 for v in previous): return result,next_state
    delta=[a-b for a,b in zip(counts,previous)]
    if any(v<0 for v in delta): return {**result,'state':'reset'},next_state
    return {'state':'ready','interval_seconds':round(elapsed,3),
            'read_bytes_per_second':round(delta[1]*512/elapsed,2),
            'write_bytes_per_second':round(delta[3]*512/elapsed,2),
            'read_iops':round(delta[0]/elapsed,2),'write_iops':round(delta[2]/elapsed,2),
            'busy_percent':round(min(100,delta[4]/(elapsed*1000)*100),2)},next_state


def _sensors(name, node, root, previous, now, budget):
    result={'temperature_celsius':None,'health':'unavailable','health_at':None}
    # hwmon reads do not open the drive. Never walk unrelated system sensors.
    for parent in (node,node.parent,node.parent.parent):
        for directory in (parent/'hwmon',parent/'device/hwmon'):
            try: monitors=list(islice(directory.iterdir(),8))
            except OSError: continue
            for monitor in monitors:
                temperature=_numeric(monitor/'temp1_input',root,-40,200,1000)
                if temperature is not None: result['temperature_celsius']=temperature;break
            if result['temperature_celsius'] is not None:break
        if result['temperature_celsius'] is not None:break
    # SMART is optional and cached. Use the existing user's device permissions,
    # at most four 1-second reads per pass. No sudo or SMART self-test commands.
    if isinstance(previous,dict) and type(previous.get('health_at')) in (int,float) and 0<=now-previous['health_at']<60:
        for key in ('health','health_at'): result[key]=previous[key]
        if result['temperature_celsius'] is None:result['temperature_celsius']=previous.get('temperature_celsius')
        return result
    # Avoid type autodetection (which may spin up USB bridges). Only known
    # direct NVMe/ATA devices are queried; unsupported power checks fail closed.
    device_type='nvme' if '/nvme/' in str(node) else 'ata' if re.search(r'/ata[0-9]+/',str(node)) else None
    if device_type is None:return result
    device=Path('/dev')/name
    tool=shutil.which('smartctl')
    try: allowed=tool and stat.S_ISBLK(device.stat().st_mode) and os.access(device,os.R_OK)
    except OSError: allowed=False
    if not allowed: return result
    if budget[0]<=0: return result
    budget[0]-=1
    result['health_at']=now
    try:
        run=subprocess.run([tool,'-j','-d',device_type,'-n','standby,3,5','-H','-A',str(device)],capture_output=True,timeout=1,check=False)
        if len(run.stdout)>128*1024: return result
        raw=json.loads(run.stdout)
        if not isinstance(raw,dict) or not isinstance(raw.get('smartctl'),dict):return result
        # Bits 0-2 signal parse/open/command failure, not a healthy drive.
        status=raw.get('smartctl',{}).get('exit_status',run.returncode)
        if type(status) is not int or status&7: return result
        status_data=raw.get('smart_status',{})
        passed=status_data.get('passed') if isinstance(status_data,dict) else None
        if type(passed) is bool:
            result.update(health='passed' if passed else 'failed',health_at=now)
        temperature_data=raw.get('temperature',{})
        temperature=temperature_data.get('current') if isinstance(temperature_data,dict) else None
        if type(temperature) in (int,float) and math.isfinite(temperature) and -40<=temperature<=200:
            result['temperature_celsius']=temperature;result['health_at']=now
    except (OSError,ValueError,TypeError,subprocess.TimeoutExpired): pass
    return result


def _unescape(value):
    return re.sub(r'\\(040|011|012|134)',lambda m:chr(int(m[1],8)),value)


def filesystem_sample(proc, by_number, stat_reader=None):
    grouped={};limit=False
    for index,line in enumerate(_bounded(proc/'self/mountinfo').splitlines()):
        if index>=4096:limit=True;break
        fields=line.split()
        if len(fields)<10 or '-' not in fields: continue
        split=fields.index('-')
        if len(fields)<=split+2 or fields[2] not in by_number:continue
        path=_unescape(fields[4]);fstype=fields[split+1]
        if not path.startswith('/') or not path.isprintable() or len(path)>512:continue
        device=by_number[fields[2]]
        key=device['id']
        if key not in grouped:
            if len(grouped)>=MAX_MOUNTS:limit=True;break
            grouped[key]={'id':key,'device':key,'fstype':_clean(fstype,40),'mounts':[],
                          'total_bytes':None,'available_bytes':None,'used_bytes':None,'reserved_bytes':None,'used_percent':None,'state':'unavailable'}
        row=grouped[key]
        if len(row['mounts'])>=16:limit=True;continue
        if any(m['path']==path for m in row['mounts']):continue
        row['mounts'].append({'path':path,'read_only':'ro' in fields[5].split(',')})
        if row['state']=='ready':continue
        try:
            if stat_reader: values=stat_reader(path,fields[2])
            else:
                fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_NONBLOCK)
                try:
                    actual=os.fstat(fd).st_dev
                    if f'{os.major(actual)}:{os.minor(actual)}'!=fields[2]:raise ValueError('mount changed')
                    values=os.fstatvfs(fd)
                finally:os.close(fd)
            total=values.f_blocks*values.f_frsize;free=values.f_bfree*values.f_frsize;available=values.f_bavail*values.f_frsize
            if not 0<=available<=free<=total or total<=0:raise ValueError('invalid filesystem stats')
            used=total-free
            row.update(total_bytes=total,available_bytes=available,used_bytes=used,reserved_bytes=free-available,
                       used_percent=round(used/(used+available)*100,2) if used+available else 100,state='ready')
        except (OSError,ValueError,TypeError): pass
    return sorted(grouped.values(),key=lambda f:f['mounts'][0]['path']),limit


def storage_sample(sys_root=Path('/sys'), proc=Path('/proc'), previous=None, clock=None, stat_reader=None):
    previous=previous or {};clock=time.monotonic() if clock is None else clock
    counters={'boot':'','clock':clock,'devices':{},'sensors':{}}
    result=empty_storage();budget=[4]
    try:
        root=Path(sys_root).resolve(strict=True);proc=Path(proc)
        boot=_bounded(proc/'sys/kernel/random/boot_id',128).strip();counters['boot']=boot
        same_boot=bool(boot) and previous.get('boot')==boot
        elapsed=clock-previous.get('clock',clock) if same_boot else 0
        old=previous.get('devices',{}) if same_boot else {}
        cached=previous.get('sensors',{}) if same_boot else {}
        nodes={};resolved={};by_number={}
        with os.scandir(root/'class/block') as entries:
            for index,entry in enumerate(entries):
                if index>=512:result['limit_reached']=True;break
                if not NAME.fullmatch(entry.name) or entry.name.startswith(('loop','ram','zram')):continue
                try:
                    node=Path(entry.path).resolve(strict=True)
                    if not node.is_relative_to(root):raise ValueError('outside sysfs')
                    size=_numeric(node/'size',root,0,2**60)
                    if size==0:continue
                    if len(nodes)>=MAX_DEVICES:result['limit_reached']=True;break
                    number=_text(node/'dev',root)
                    if not re.fullmatch(r'[0-9]{1,6}:[0-9]{1,6}',number):raise ValueError('invalid block device')
                    identifier=hashlib.sha256(str(node.relative_to(root)).encode()).hexdigest()
                    virtual='/virtual/' in str(node)
                    kind='partition' if (node/'partition').exists() else 'lvm' if _text(node/'dm/uuid',root).startswith('LVM-') else 'virtual' if virtual else 'disk'
                    label=_clean(_text(node/'dm/name',root) or _text(node/'device/model',root) or entry.name)
                    transport='nvme' if '/nvme/' in str(node) else 'usb' if re.search(r'/usb[0-9]+/',str(node)) else 'sata' if re.search(r'/ata[0-9]+/',str(node)) else 'virtual' if virtual else 'unknown'
                    rota=_text(node/'queue/rotational',root)
                    generation=_text(node/'diskseq',root) or str(node.stat().st_ino)
                    io,counts=_io(_text(node/'stat',root),old.get(identifier),elapsed,generation)
                    if counts:counters['devices'][identifier]=counts
                    sensors=_sensors(entry.name,node,root,cached.get(identifier) if old.get(identifier,{}).get('generation')==generation else None,time.time(),budget) if kind=='disk' else {'temperature_celsius':None,'health':'unavailable','health_at':None}
                    if kind=='disk':counters['sensors'][identifier]=sensors
                    row={'id':identifier,'name':entry.name,'model':label,'kind':kind,'transport':transport,
                         'rotational':rota=='1' if rota in ('0','1') else None,'size_bytes':int(size)*512 if size is not None else None,
                         'parents':[],'io':io,**sensors}
                    nodes[entry.name]=(row,node);resolved[node]=identifier;by_number[number]=row
                except (OSError,ValueError,RuntimeError):result['unreadable_devices']+=1
        for row,node in nodes.values():
            if node.parent in resolved:row['parents'].append(resolved[node.parent])
            try:
                slaves=list(islice((node/'slaves').iterdir(),17))
                if len(slaves)>16:result['limit_reached']=True
                for slave in slaves[:16]:
                    target=slave.resolve(strict=True)
                    if target in resolved and resolved[target] not in row['parents']:row['parents'].append(resolved[target])
            except (OSError,ValueError,RuntimeError):pass
        result['devices']=sorted([v[0] for v in nodes.values()],key=lambda d:(d['kind']!='disk',d['name']))
        try:result['filesystems'],limited=filesystem_sample(proc,by_number,stat_reader);result['limit_reached']|=limited
        except (OSError,ValueError,UnicodeError):result['mounts_unavailable']=True
        partial=(result['limit_reached'] or result['unreadable_devices'] or result.get('mounts_unavailable') or any(f['state']!='ready' for f in result['filesystems']) or any(d['size_bytes'] is None for d in result['devices']))
        result['state']='partial' if partial else 'ready' if nodes else 'not-detected'
        return result,counters
    except (OSError,ValueError,TypeError,KeyError,RuntimeError):return empty_storage(),counters


def public_storage(raw):
    """Strict additive API projection, with failure isolated from CPU/RAM/GPU."""
    if raw is None:return empty_storage('not-collected')
    try:
        _require(isinstance(raw,dict) and type(raw.get('limit_reached')) is bool)
        _require(isinstance(raw.get('devices'),list) and len(raw['devices'])<=MAX_DEVICES)
        _require(isinstance(raw.get('filesystems'),list) and len(raw['filesystems'])<=MAX_MOUNTS)
        devices=[];seen=set()
        def text(v,limit=160):
            _require(isinstance(v,str) and len(v)<=limit and (not v or v.isprintable()));return v
        def numeric(v,hi=2**70,lo=0):
            _require(v is None or type(v) in (int,float) and math.isfinite(v) and lo<=v<=hi);return v
        for row in raw['devices']:
            identifier=text(row['id'],64);_require(IDENTIFIER.fullmatch(identifier) and identifier not in seen);seen.add(identifier)
            _require(row['kind'] in ('disk','partition','lvm','virtual') and row['transport'] in ('nvme','usb','sata','virtual','unknown'))
            _require(row['rotational'] is None or type(row['rotational']) is bool)
            _require(NAME.fullmatch(row['name']) and isinstance(row['parents'],list) and len(row['parents'])<=16 and all(isinstance(v,str) and IDENTIFIER.fullmatch(v) for v in row['parents']))
            io=row['io'];_require(io['state'] in ('ready','sampling','reset','unavailable'))
            safe_io={k:numeric(io.get(k),hi=100 if k=='busy_percent' else 2**70) for k in IO_FIELDS}
            safe_io.update(state=io['state'],interval_seconds=numeric(io.get('interval_seconds'),90))
            if io['state']!='ready':safe_io.update(dict.fromkeys(IO_FIELDS))
            _require(row['health'] in ('passed','failed','unavailable'))
            devices.append({'id':identifier,'name':text(row['name'],80),'model':text(row['model']),'kind':row['kind'],'transport':row['transport'],
                            'rotational':row['rotational'],'size_bytes':numeric(row['size_bytes']), 'parents':list(dict.fromkeys(row['parents'])),
                            'io':safe_io,'temperature_celsius':numeric(row.get('temperature_celsius'),200,-40),
                            'health':row['health'],'health_at':numeric(row.get('health_at'))})
        _require(all(p in seen and p!=d['id'] for d in devices for p in d['parents']))
        filesystems=[];fs_seen=set()
        for row in raw['filesystems']:
            _require(row['device'] in seen and row['id']==row['device'] and row['id'] not in fs_seen);fs_seen.add(row['id'])
            _require(row['state'] in ('ready','unavailable') and isinstance(row['mounts'],list) and 1<=len(row['mounts'])<=16)
            mounts=[]
            for m in row['mounts']:
                path=text(m['path'],512);_require(path.startswith('/') and type(m['read_only']) is bool);mounts.append({'path':path,'read_only':m['read_only']})
            safe={k:numeric(row[k],100 if k=='used_percent' else 2**70) for k in ('total_bytes','available_bytes','used_bytes','reserved_bytes','used_percent')}
            if row['state']=='ready':
                _require(all(v is not None for v in safe.values()) and safe['total_bytes']>0)
                _require(abs(safe['used_bytes']+safe['available_bytes']+safe['reserved_bytes']-safe['total_bytes'])<1)
                denominator=safe['used_bytes']+safe['available_bytes']
                expected=round(safe['used_bytes']/denominator*100,2) if denominator else 100
                _require(abs(safe['used_percent']-expected)<.02)
            else:safe=dict.fromkeys(safe)
            filesystems.append({'id':row['id'],'device':row['device'],'fstype':text(row['fstype'],40),'mounts':mounts,'state':row['state'],**safe})
        _require(raw['state'] in ('ready','partial','not-detected','unavailable','not-collected'))
        return {'state':raw['state'],'devices':devices,'filesystems':filesystems,'limit_reached':raw['limit_reached'],
                'unreadable_devices':numeric(raw.get('unreadable_devices',0)), 'mounts_unavailable':raw.get('mounts_unavailable') is True}
    except (AssertionError,KeyError,TypeError,ValueError,OverflowError):return empty_storage()
