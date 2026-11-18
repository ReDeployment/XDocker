"""Fixed SMART reads and a sanitized cache shared with the unprivileged sampler.

The root oneshot runs a root-owned standalone copy with Python isolated mode.
It never imports the writable checkout and never accepts smartctl arguments.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time

CACHE=Path('/var/lib/xdocker-smart/report.json')
CONFIG=Path('/etc/xdocker-smart/devices.json')
MAX_DEVICES=8
STATES=('ready','sleeping','idle-paused','transport-blocked','unsupported','permission-denied','timeout','read-error','tool-missing','stale','not-collected','not-applicable')
ALERTS=('past-temperature-threshold','past-attribute-threshold','error-log','self-test-log')


def sensor(state,now=None):
    return {'temperature_celsius':None,'health':'unavailable','health_at':now,
            'sensor':{'state':state,'source':'smart','alerts':[]}}


def parse_smart(raw,returncode,now):
    result=sensor('read-error',now)
    if not isinstance(raw,dict) or not isinstance(raw.get('smartctl'),dict):return result
    status=raw['smartctl'].get('exit_status',returncode)
    if type(status) is not int or not 0<=status<=255:return result
    # These custom exit codes are meaningful only with the matching power state.
    messages=raw['smartctl'].get('messages',[])
    messages=[m.get('string','') for m in messages if isinstance(m,dict) and isinstance(m.get('string'),str)] if isinstance(messages,list) else []
    if status==3 and (raw.get('power_mode') in ('SLEEP','STANDBY') or any(re.fullmatch(r'Device is in (?:SLEEP|STANDBY) mode, exit\(3\)',m) for m in messages)):
        return sensor('sleeping',now)
    if status==5 and (raw.get('power_mode')=='UNKNOWN' or any('CHECK POWER MODE' in m for m in messages)):return sensor('unsupported',now)
    if status&7:return result
    result['sensor']['state']='ready'
    health=raw.get('smart_status',{})
    passed=health.get('passed') if isinstance(health,dict) else None
    if type(passed) is bool:result['health']='passed' if passed else 'failed'
    if status&24:result['health']='failed'
    alerts=[]
    if status&32:
        attributes=raw.get('ata_smart_attributes',{})
        rows=attributes.get('table',[]) if isinstance(attributes,dict) else []
        temperature=any(isinstance(r,dict) and r.get('id') in (190,194) and r.get('when_failed')=='past' for r in rows) if isinstance(rows,list) else False
        alerts.append('past-temperature-threshold' if temperature else 'past-attribute-threshold')
    if status&64:alerts.append('error-log')
    if status&128:alerts.append('self-test-log')
    if alerts and result['health']!='failed':result['health']='warning'
    result['sensor']['alerts']=alerts
    temp=raw.get('temperature',{})
    current=temp.get('current') if isinstance(temp,dict) else None
    if type(current) in (int,float) and math.isfinite(current) and -40<=current<=200:result['temperature_celsius']=current
    return result


def probe(device,kind,now,tool='/usr/sbin/smartctl'):
    if kind not in ('nvme','ata','sat'):raise ValueError('XD_SMART_TYPE_INVALID')
    args=[tool,'-j','-d',kind]
    if kind!='nvme':args+=['-n','standby,3,5']
    args+=['-H','-A',str(device)]
    try:
        run=subprocess.run(args,capture_output=True,timeout=2,check=False,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C'})
        if len(run.stdout)>128*1024:return sensor('read-error',now)
        return parse_smart(json.loads(run.stdout),run.returncode,now)
    except FileNotFoundError:return sensor('tool-missing',now)
    except PermissionError:return sensor('permission-denied',now)
    except subprocess.TimeoutExpired:return sensor('timeout',now)
    except (OSError,ValueError,TypeError):return sensor('read-error',now)


def validate_config(raw):
    if not isinstance(raw,dict) or set(raw)!={'schema','devices'} or raw['schema']!=1 or not isinstance(raw['devices'],list) or not 1<=len(raw['devices'])<=MAX_DEVICES:raise ValueError('XD_SMART_CONFIG_INVALID')
    seen=set()
    for row in raw['devices']:
        if not isinstance(row,dict) or not {'path','type'}<=set(row)<={'path','type','idle_pause_seconds'} or row['type'] not in ('nvme','ata','sat'):raise ValueError('XD_SMART_CONFIG_INVALID')
        pause=row.get('idle_pause_seconds')
        if pause is not None and (type(pause) is not int or not 60<=pause<=1800 or row['type']=='nvme'):raise ValueError('XD_SMART_CONFIG_INVALID')
        path=row['path']
        if not isinstance(path,str) or not re.fullmatch(r'/dev/disk/by-id/[a-zA-Z0-9_.:+-]{1,200}',path) or '-part' in path or path in seen:raise ValueError('XD_SMART_CONFIG_INVALID')
        seen.add(path)
    return raw


def trusted_json(path,owner=0):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=owner or info.st_mode&0o027 or info.st_size>128*1024:raise ValueError('XD_SMART_UNTRUSTED_FILE')
        raw=stream.read(128*1024+1)
    if len(raw)>128*1024:raise ValueError('XD_SMART_INPUT_LIMIT')
    return json.loads(raw)


def device_identity(alias,kind,sys_root=Path('/sys'),dev_root=Path('/dev')):
    root=Path(sys_root).resolve(strict=True);dev_root=Path(dev_root).resolve(strict=True)
    device=Path(alias).resolve(strict=True)
    if not device.is_relative_to(dev_root):raise ValueError('XD_SMART_DEVICE_INVALID')
    expected=r'nvme[0-9]+n[0-9]+' if kind=='nvme' else r'sd[a-z]{1,4}'
    if not re.fullmatch(expected,device.name):raise ValueError('XD_SMART_DEVICE_INVALID')
    info=device.stat();node=(root/'class/block'/device.name).resolve(strict=True)
    if not stat.S_ISBLK(info.st_mode) or not node.is_relative_to(root) or (node/'partition').exists():raise ValueError('XD_SMART_DEVICE_INVALID')
    if (node/'dev').read_text().strip()!=f'{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}':raise ValueError('XD_SMART_DEVICE_CHANGED')
    identity=hashlib.sha256(str(node.relative_to(root)).encode()).hexdigest()
    generation=(node/'diskseq').read_text().strip() if (node/'diskseq').exists() else str(node.stat().st_ino)
    if not re.fullmatch(r'[0-9]{1,30}',generation):raise ValueError('XD_SMART_DEVICE_INVALID')
    return device,identity,generation


def last_reading(row):
    if not isinstance(row,dict):return None
    info=public_sensor(row.get('sensor'))
    if not info:return None
    if info.get('last') is not None:return info['last']
    if info['state']!='ready' or row.get('health_at') is None:return None
    return public_last({'at':row['health_at'],'temperature_celsius':row.get('temperature_celsius'),
                        'health':row.get('health'),'alerts':info['alerts']})


def io_activity(device,sys_root):
    # Kernel counters only: no command/open to the physical disk or mountpoint.
    values=[int(v) for v in (Path(sys_root)/'class/block'/device.name/'stat').read_text().split()]
    if len(values)<11 or any(v<0 for v in values):raise ValueError('XD_SMART_IO_UNAVAILABLE')
    return [values[i] if i<len(values) else 0 for i in (0,2,4,6,11,13,15)],values[8]>0


def transport_blocked(device,sys_root=Path('/sys')):
    """Known Ultra Touch UAS ATA filter, detected without querying the disk.

    Linux uas-detect.h sets US_FL_NO_ATA_1X for Seagate USB devices. Scope
    this exception to the exact USB model validated on the deployed host.
    An explicit matching module-parameter override defers to normal probing.
    Never publish USB identifiers, driver paths, or the private configuration.
    """
    root=Path(sys_root).resolve(strict=True)
    node=(root/'class/block'/device.name).resolve(strict=True)
    if not node.is_relative_to(root):raise ValueError('XD_SMART_DEVICE_INVALID')
    uas=False;usb=None
    for parent in (node,*node.parents):
        if not parent.is_relative_to(root):break
        driver=parent/'driver'
        if driver.is_symlink():
            target=driver.resolve(strict=True)
            if target.is_relative_to(root) and target.name=='uas':uas=True
        if (parent/'idVendor').exists() and (parent/'idProduct').exists():
            usb=((parent/'idVendor').read_text().strip().lower(),(parent/'idProduct').read_text().strip().lower())
            break
    if not uas or usb!=('0bc2','2065'):return False
    try:quirks=(root/'module/usb_storage/parameters/quirks').read_text()
    except FileNotFoundError:quirks=''
    for entry in quirks.strip().split(','):
        fields=entry.split(':')
        if len(fields)==3 and tuple(p.lower().zfill(4) for p in fields[:2])==usb:
            return 't' in fields[2]
    return True


def collect_smart(config,sys_root=Path('/sys'),dev_root=Path('/dev'),proc=Path('/proc'),previous=None,clock=None):
    validate_config(config);devices={};errors=[];activity={}
    clock=time.monotonic() if clock is None else clock
    boot=(proc/'sys/kernel/random/boot_id').read_text().strip();previous=previous or {}
    old=previous.get('_activity',{}) if isinstance(previous,dict) and previous.get('boot')==boot else {}
    old=old if isinstance(old,dict) else {}
    previous_rows=previous.get('devices',{}) if isinstance(previous,dict) and previous.get('boot')==boot else {}
    previous_rows=previous_rows if isinstance(previous_rows,dict) else {}
    for row in config['devices']:
        try:
            device,identity,generation=device_identity(row['path'],row['type'],sys_root,dev_root)
            now=time.time();pause=row.get('idle_pause_seconds');idle=False
            if pause is not None:
                counts,busy=io_activity(device,sys_root);before=old.get(identity,{})
                last_io=before.get('last_io') if isinstance(before,dict) else None
                same=isinstance(before,dict) and before.get('generation')==generation and before.get('counts')==counts and type(last_io) in (int,float) and math.isfinite(last_io) and 0<=last_io<=clock
                last_io=last_io if same and not busy else clock
                activity[identity]={'generation':generation,'counts':counts,'last_io':last_io}
                idle=not busy and clock-last_io>=pause
            # The affected UAS transport rejects ATA IDENTIFY and may make
            # CHECK POWER MODE look like SLEEP even after real reads. This is
            # not power-state evidence; do not query it or declare it healthy.
            blocked=row['type']=='sat' and transport_blocked(device,sys_root)
            value=sensor('transport-blocked') if blocked else sensor('idle-paused') if idle else probe(device,row['type'],now)
            if pause is not None and not idle:
                # Exclude the SMART command's own block-counter increments.
                # Any application I/O during this read remains conservatively
                # active for this interval; the next idle window starts here.
                counts,busy=io_activity(device,sys_root)
                activity[identity]['counts']=counts
                if busy:activity[identity]['last_io']=clock
            old_row=previous_rows.get(identity)
            last=last_reading(old_row) if isinstance(old_row,dict) and old_row.get('generation')==generation else None
            if value['sensor']['state']!='ready' and last is not None:value['sensor']['last']=last
            if device_identity(row['path'],row['type'],sys_root,dev_root)!=(device,identity,generation):raise ValueError('XD_SMART_DEVICE_CHANGED')
            if identity in devices:raise ValueError('XD_SMART_DUPLICATE_DEVICE')
            devices[identity]={'generation':generation,'observed_at':now,**value}
        except (OSError,ValueError,RuntimeError):errors.append('device-unavailable')
    return {'schema':1,'boot':boot,'at':time.time(),'devices':devices,'errors':errors,'_activity':activity}


def read_cache(boot,now,path=CACHE,owner=0):
    try:
        raw=trusted_json(path,owner)
        if not isinstance(raw,dict) or raw.get('schema')!=1 or not isinstance(raw.get('devices'),dict) or len(raw['devices'])>MAX_DEVICES:return {},'read-error'
        at=raw.get('at')
        if type(at) not in (int,float) or not math.isfinite(at) or not -5<=now-at<=180 or raw.get('boot')!=boot:return {},'stale'
        return raw['devices'],'ready'
    except FileNotFoundError:return {},'not-collected'
    except (OSError,ValueError,TypeError):return {},'read-error'


def cached_sensor(devices,identity,generation,now):
    row=devices.get(identity)
    if not isinstance(row,dict):return None
    if row.get('generation')!=generation:return sensor('stale')
    value=public_sensor(row.get('sensor'))
    at=row.get('health_at');observed=row.get('observed_at',at);temp=row.get('temperature_celsius');health=row.get('health')
    if value is None or type(observed) not in (int,float) or not math.isfinite(observed) or not -5<=now-observed<=180:return sensor('stale')
    if at is None:
        if value['state'] not in ('idle-paused','transport-blocked'):return sensor('read-error')
    elif type(at) not in (int,float) or not math.isfinite(at) or not -5<=now-at<=180:return sensor('stale')
    if health not in ('passed','warning','failed','unavailable'):return sensor('read-error')
    if temp is not None and (type(temp) not in (int,float) or not math.isfinite(temp) or not -40<=temp<=200):return sensor('read-error')
    if value['state']!='ready' and (temp is not None or health!='unavailable'):return sensor('read-error')
    return {'temperature_celsius':temp,'health':health,'health_at':at,'sensor':value}


def public_last(raw):
    if not isinstance(raw,dict):return None
    at=raw.get('at');temp=raw.get('temperature_celsius');health=raw.get('health');alerts=raw.get('alerts')
    if type(at) not in (int,float) or not math.isfinite(at) or not 0<=at<=2**52:return None
    if temp is not None and (type(temp) not in (int,float) or not math.isfinite(temp) or not -40<=temp<=200):return None
    if health not in ('passed','warning','failed','unavailable') or not isinstance(alerts,list) or len(alerts)>len(ALERTS) or any(a not in ALERTS for a in alerts):return None
    return {'at':at,'temperature_celsius':temp,'health':health,'alerts':list(dict.fromkeys(alerts))}


def public_sensor(raw):
    if raw is None:return {'state':'not-collected','source':'none','alerts':[]}
    if not isinstance(raw,dict) or raw.get('state') not in STATES or raw.get('source') not in ('smart','hwmon','none'):return None
    alerts=raw.get('alerts')
    if not isinstance(alerts,list) or len(alerts)>len(ALERTS) or any(a not in ALERTS for a in alerts):return None
    result={'state':raw['state'],'source':raw['source'],'alerts':list(dict.fromkeys(alerts))}
    if raw.get('last') is not None:
        last=public_last(raw['last'])
        if last is None:return None
        result['last']=last
    return result


def publish(path,value):
    # The deployment creates this root-owned, group-readable directory first.
    path=Path(path);directory=path.parent
    info=directory.stat()
    if directory.is_symlink() or info.st_uid!=0 or info.st_mode&0o027:raise ValueError('XD_SMART_OUTPUT_UNSAFE')
    raw=json.dumps(value,allow_nan=False).encode()
    if len(raw)>128*1024:raise ValueError('XD_SMART_OUTPUT_LIMIT')
    fd,temporary=tempfile.mkstemp(prefix='.smart-',dir=directory)
    try:
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),0o640);stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
    code=Path(__file__).resolve();info=code.stat()
    if os.getuid()!=0 or info.st_uid!=0 or info.st_mode&0o022 or Path(args.config)!=CONFIG or Path(args.output)!=CACHE:raise ValueError('XD_SMART_INSTALLATION_REQUIRED')
    config=validate_config(trusted_json(CONFIG))
    try:previous=trusted_json(CACHE)
    except (OSError,ValueError,TypeError):previous={}
    publish(CACHE,collect_smart(config,previous=previous))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,TypeError,RuntimeError):raise SystemExit('XD_SMART_COLLECTION_FAILED: check the root-owned configuration and service') from None
