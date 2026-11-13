"""Linux host sampler. Reads procfs and fixed Docker GETs; never inspects argv/env."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import stat
import tempfile
import time

from docker_api import Docker, DockerError
from gpu_metrics import gpu_sample

MAX_PROCESSES = 4096
MAX_GROUPS = 128
MAX_CONTAINERS = 32
MAX_REPORT = 4 * 1024 * 1024
MAX_AGE = 90
HISTORY_SECONDS = 86400


def key(kind, identity):
    return hashlib.sha256((kind + '\0' + identity).encode()).hexdigest()


def clean(value, limit=512):
    return ''.join(c for c in str(value) if c.isprintable())[:limit]


def private_directory(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('XD_METRICS_UNSAFE_PATH')
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.stat().st_mode & 0o077:
        raise ValueError('XD_METRICS_PRIVATE_DIRECTORY_REQUIRED')
    return path


def read_json(path, limit=MAX_REPORT):
    # O_NOFOLLOW also protects the interval between checking and opening.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > limit:
            raise ValueError('XD_METRICS_UNSAFE_FILE')
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('XD_METRICS_REPORT_TOO_LARGE')
    return json.loads(raw)


def atomic_json(path, value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    if len(raw) > MAX_REPORT:
        raise ValueError('XD_METRICS_REPORT_TOO_LARGE')
    fd, temporary = tempfile.mkstemp(prefix='.metrics-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def proc_sample(proc=Path('/proc'), previous=None, clock=None):
    """Process identities include start time, so reused PIDs never inherit CPU time."""
    clock = time.monotonic() if clock is None else clock
    previous = previous or {}
    boot = (proc/'sys/kernel/random/boot_id').read_text().strip()
    same_boot = previous.get('boot') == boot
    elapsed = clock - previous.get('clock', clock) if same_boot else 0
    old_processes = previous.get('process_ticks', {}) if same_boot else {}
    hz, page_size = os.sysconf('SC_CLK_TCK'), os.sysconf('SC_PAGE_SIZE')
    ticks = [int(v) for v in (proc/'stat').read_text().splitlines()[0].split()[1:9]]
    old_ticks = previous.get('cpu_ticks', []) if same_boot else []
    total = sum(ticks) - sum(old_ticks) if len(old_ticks) == len(ticks) else 0
    idle = ticks[3] + ticks[4] - old_ticks[3] - old_ticks[4] if total > 0 else 0
    memory = {}
    for line in (proc/'meminfo').read_text().splitlines():
        name, _, raw = line.partition(':')
        if name in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
            memory[name] = int(raw.split()[0]) * 1024
    groups, processes, counters = {}, [], {}
    skipped, container_processes, scanned = 0, 0, 0
    paths = sorted((p for p in proc.iterdir() if p.name.isdecimal()), key=lambda p: int(p.name))
    for directory in paths[:MAX_PROCESSES]:
        scanned += 1
        try:
            raw = (directory/'stat').read_text()
            start, end = raw.index('('), raw.rindex(')')
            tail = raw[end+2:].split()  # field 3 onward
            pid, ppid, started = int(directory.name), int(tail[1]), int(tail[19])
            used, rss = int(tail[11])+int(tail[12]), max(0, int(tail[21]))*page_size
            identity = f'{pid}:{started}'
            counters[identity] = used
            cpu = round(max(0, used-old_processes[identity])/hz/elapsed*100, 2) if elapsed > 0 and identity in old_processes else None
            cgroup = (directory/'cgroup').read_text()
            if re.search(r'(?:docker[-/]|libpod-)[a-f0-9]{12,64}|/kubepods[/.]', cgroup):
                container_processes += 1
                continue
            uid = directory.stat().st_uid
            units = [v for v in re.split(r'[/\n]', cgroup) if v.endswith('.service') and not v.startswith('user@')]
            unit = units[-1] if units else None
            exe, cwd = ('', '') if unit else (os.readlink(directory/'exe'), os.readlink(directory/'cwd'))
            again = (directory/'stat').read_text()
            if int(again[again.rindex(')')+2:].split()[19]) != started:
                skipped += 1
                continue
            kind = 'systemd' if unit else 'process'
            # Same executable + working directory + uid survives ordinary restarts.
            selector = f'{uid}:{unit}' if unit else f'{uid}:{exe}\0{cwd}'
            identifier = key(kind, selector)
            if identifier not in groups and len(groups) >= MAX_GROUPS:
                skipped += 1
                continue
            row = groups.setdefault(identifier, {'id':identifier, 'kind':kind,
                'name':clean(unit or Path(exe).name), 'description':clean(unit or cwd),
                'memory_kind':'rss', 'cpu_percent':0, 'memory_bytes':0, 'pids':0,
                'state':'running', 'complete':True})
            row['memory_bytes'] += rss; row['pids'] += 1
            row['cpu_percent'] = None if cpu is None or row['cpu_percent'] is None else round(row['cpu_percent']+cpu, 2)
            processes.append({'pid':pid, 'ppid':ppid, 'name':clean(raw[start+1:end], 80),
                'target':identifier, 'cpu_percent':cpu, 'memory_bytes':rss})
        except (OSError, ValueError, IndexError):
            skipped += 1  # includes exited, kernel and inaccessible processes
    try: machine = Path('/etc/machine-id').read_text().strip()
    except OSError: machine = socket.gethostname()
    host = {'id':key('host', machine), 'hostname':clean(socket.gethostname(), 128), 'cpu_logical':os.cpu_count(),
        'cpu_percent':round(max(0, min(100, (total-idle)/total*100)), 2) if total > 0 else None,
        'memory_total':memory.get('MemTotal'), 'memory_available':memory.get('MemAvailable'),
        'swap_used':memory.get('SwapTotal', 0)-memory.get('SwapFree', 0),
        'load':list(os.getloadavg()), 'disk_total':shutil.disk_usage('/').total,
        'disk_free':shutil.disk_usage('/').free}
    return {'host':host, 'targets':list(groups.values()), 'processes':processes,
        'coverage':{'processes_scanned':scanned, 'processes_unreadable':skipped,
                    'container_processes_excluded':container_processes,
                    'process_limit_reached':len(paths)>MAX_PROCESSES,
                    'group_limit_reached':len(groups)>=MAX_GROUPS}}, \
        {'boot':boot, 'clock':clock, 'cpu_ticks':ticks, 'process_ticks':counters}


def docker_sample(engine, previous=None):
    previous = previous or {}
    rows = engine.request('GET', '/containers/json?all=true', timeout=2)
    if not isinstance(rows, list): raise DockerError('Invalid container list')
    targets, counters = [], {}
    deadline = time.monotonic()+10

    def sample(container):
        identifier = container.get('Id', '')
        if not re.fullmatch('[a-f0-9]{64}', identifier): return None
        labels = container.get('Labels') or {}
        project, service = labels.get('com.docker.compose.project'), labels.get('com.docker.compose.service')
        # Compose labels bind identity across container recreation; standalone uses exact ID.
        stable = f'{project}/{service}' if project and service and labels.get('com.docker.compose.oneoff','false').lower()!='true' else identifier
        target = key('docker', stable)
        row = {'id':target, 'kind':'docker', 'name':clean(stable if stable!=identifier else '/'.join(container.get('Names', []))),
            'description':clean(container.get('Image', '')), 'state':clean(container.get('State', 'unknown'), 32),
            'memory_kind':'working-set', 'cpu_percent':None, 'memory_bytes':None, 'pids':None,
            'complete':False, 'container_id':identifier}
        if row['state'] != 'running': return row, None
        if time.monotonic() >= deadline: return row, None
        try:
            raw = engine.request('GET', f'/containers/{identifier}/stats?stream=false&one-shot=true', timeout=1)
            current = raw.get('cpu_stats', {}); old = previous.get(identifier, {})
            used = current.get('cpu_usage', {}).get('total_usage', 0); system = current.get('system_cpu_usage', 0)
            delta = system-old.get('system', system)
            row['cpu_percent'] = round(max(0, used-old.get('used', used))/delta*current.get('online_cpus',1)*100,2) if delta>0 else None
            mem = raw.get('memory_stats', {})
            row.update(memory_bytes=max(0, mem.get('usage',0)-mem.get('stats',{}).get('inactive_file',0)),
                       pids=raw.get('pids_stats',{}).get('current'), complete=True)
            return row, {'used':used, 'system':system}
        except DockerError:
            return row, None
    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(sample, rows[:MAX_CONTAINERS]):
            if result:
                row, counter = result; targets.append(row)
                if counter is not None: counters[row['container_id']] = counter
    # Duplicate Compose identities (e.g. replicas) cannot be silently assigned to one app.
    duplicates = {r['id'] for r in targets if sum(x['id']==r['id'] for x in targets)>1}
    for row in targets:
        if row['id'] in duplicates:
            row['id'] = key('docker', row['container_id'])
            row['name'] += ' · '+row['container_id'][:12]
    return targets, counters, {'docker_limit_reached':len(rows)>MAX_CONTAINERS,
        'docker_incomplete':sum(r['state']=='running' and not r['complete'] for r in targets)}


def write_history(root, report):
    path = root/'history.sqlite3'
    if path.is_symlink(): raise ValueError('XD_METRICS_UNSAFE_FILE')
    if path.exists() and path.stat().st_mode & 0o077: raise ValueError('XD_METRICS_UNSAFE_FILE')
    with sqlite3.connect(path, timeout=2) as db:
        path.chmod(0o600)
        db.execute('PRAGMA max_page_count=16384')  # <= 64 MiB at default 4 KiB pages
        db.execute('CREATE TABLE IF NOT EXISTS samples (at INTEGER, target TEXT, cpu REAL, memory INTEGER, pids INTEGER, PRIMARY KEY(target,at))')
        db.execute('CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT)')
        machine = db.execute("SELECT value FROM metadata WHERE name='host_id'").fetchone()
        if machine and machine[0]!=report['host']['id']:
            raise ValueError('XD_METRICS_HISTORY_HOST_CHANGED')
        db.execute("INSERT OR IGNORE INTO metadata VALUES('host_id',?)", (report['host']['id'],))
        at = int(report['at']//60)*60
        # Once per minute, with a rolling 24-hour window. No raw process metadata in history.
        db.execute('DELETE FROM samples WHERE at < ?', (at-HISTORY_SECONDS,))
        host = report['host']
        values = [('host', host['cpu_percent'], host['memory_total']-host['memory_available'], None)]
        values += [(r['id'],r['cpu_percent'],r['memory_bytes'],r['pids']) for r in report['targets'] if r['state']=='running' and r['complete']]
        db.executemany('INSERT OR IGNORE INTO samples VALUES(?,?,?,?,?)', [(at,*r) for r in values])


def collect(output, proc=Path('/proc'), engine=None, include_docker=True, sys_root=Path('/sys')):
    root = private_directory(output)
    fd = os.open(root/'collector.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('XD_METRICS_COLLECTOR_BUSY') from None
        try: previous = read_json(root/'report.json').get('_counters', {})
        except FileNotFoundError: previous = {}
        report, counters = proc_sample(proc, previous)
        report.update(schema=1, at=time.time(), errors=[], gpu=gpu_sample(sys_root))
        if include_docker:
            try:
                targets, docker_counters, coverage = docker_sample(engine or Docker(), previous.get('docker', {}))
                report['targets'] += targets; report['coverage'].update(coverage); counters['docker'] = docker_counters
            except DockerError:
                report['errors'].append('XD_METRICS_DOCKER_UNAVAILABLE')
        report['_counters'] = counters
        try: write_history(root, report)
        except (OSError, ValueError, sqlite3.Error): report['errors'].append('XD_METRICS_HISTORY_UNAVAILABLE')
        atomic_json(root/'report.json', report)
        return {'sampled':True, 'targets':len(report['targets']), 'errors':report['errors']}
    finally:
        os.close(fd)
