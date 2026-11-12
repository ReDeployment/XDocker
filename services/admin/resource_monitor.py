"""Private app-to-metric bindings. Monitoring never grants runtime control."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from flask import jsonify, request
from registry_store import RegistryStore, RegistryError
from host_metrics import MAX_AGE, MAX_REPORT, atomic_json, number, private_directory, read_json

ID = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')
TARGET = re.compile(r'^[a-f0-9]{64}$')


class MonitorError(ValueError):
    pass


def require(condition, code):
    if not condition: raise MonitorError(code)


def report_read(root):
    root = Path(root).absolute()
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise MonitorError('XD_METRICS_UNSAFE_PATH')
    raw = read_json(root/'report.json')
    require(isinstance(raw, dict) and raw.get('schema')==1 and number(raw.get('at')), 'XD_METRICS_REPORT_INVALID')
    require(-5 <= time.time()-raw['at'], 'XD_METRICS_REPORT_FUTURE')
    host = raw.get('host', {})
    require(isinstance(host, dict) and isinstance(host.get('hostname'),str), 'XD_METRICS_REPORT_INVALID')
    require(isinstance(host.get('id'),str) and TARGET.fullmatch(host['id']), 'XD_METRICS_REPORT_INVALID')
    for name in ('memory_total','memory_available','disk_total','disk_free','swap_used','cpu_logical'):
        require(number(host.get(name)) and host[name]>=0, 'XD_METRICS_REPORT_INVALID')
    require(host['memory_total']>0 and host['cpu_logical']>0, 'XD_METRICS_REPORT_INVALID')
    require(host.get('cpu_percent') is None or number(host['cpu_percent']) and 0<=host['cpu_percent']<=100, 'XD_METRICS_REPORT_INVALID')
    require(isinstance(host.get('load'), list) and len(host['load'])==3 and all(number(v) and v>=0 for v in host['load']), 'XD_METRICS_REPORT_INVALID')
    host = {k:host[k] for k in ('id','hostname','cpu_logical','cpu_percent','memory_total','memory_available','swap_used','load','disk_total','disk_free')}
    targets, seen = [], set()
    require(isinstance(raw.get('targets'),list) and len(raw['targets'])<=160, 'XD_METRICS_REPORT_INVALID')
    for row in raw['targets']:
        require(isinstance(row,dict) and isinstance(row.get('id'),str) and TARGET.fullmatch(row['id']) and row['id'] not in seen, 'XD_METRICS_REPORT_INVALID')
        require(row.get('kind') in ('docker','process','systemd') and type(row.get('complete')) is bool, 'XD_METRICS_REPORT_INVALID')
        require(row.get('memory_kind') in ('rss','working-set'), 'XD_METRICS_REPORT_INVALID')
        for field in ('cpu_percent','memory_bytes','pids'):
            require(row.get(field) is None or number(row[field]) and row[field]>=0, 'XD_METRICS_REPORT_INVALID')
        for field in ('name','description','state'):
            require(isinstance(row.get(field),str) and len(row[field])<=512, 'XD_METRICS_REPORT_INVALID')
        seen.add(row['id'])
        targets.append({k:row[k] for k in ('id','kind','name','description','state','memory_kind','complete','cpu_percent','memory_bytes','pids')})
    processes = raw.get('processes', [])
    require(isinstance(processes,list) and len(processes)<=4096, 'XD_METRICS_REPORT_INVALID')
    safe_processes = []
    for row in processes:
        require(isinstance(row,dict) and row.get('target') in seen and isinstance(row.get('name'),str) and len(row['name'])<=80, 'XD_METRICS_REPORT_INVALID')
        require(all(number(row.get(k)) and row[k]>=0 for k in ('pid','ppid','memory_bytes')), 'XD_METRICS_REPORT_INVALID')
        require(row.get('cpu_percent') is None or number(row['cpu_percent']) and row['cpu_percent']>=0, 'XD_METRICS_REPORT_INVALID')
        safe_processes.append({k:row[k] for k in ('pid','ppid','name','target','cpu_percent','memory_bytes')})
    coverage = raw.get('coverage', {})
    require(isinstance(coverage,dict), 'XD_METRICS_REPORT_INVALID')
    safe_coverage = {k:v for k,v in coverage.items() if
        (k in ('processes_scanned','processes_unreadable','container_processes_excluded','docker_incomplete') and number(v) and v>=0)
        or (k in ('process_limit_reached','group_limit_reached','docker_limit_reached') and type(v) is bool)}
    errors = raw.get('errors', [])
    require(isinstance(errors,list) and len(errors)<=10, 'XD_METRICS_REPORT_INVALID')
    return {'at':raw['at'], 'host':host, 'targets':targets, 'processes':safe_processes,
        'coverage':safe_coverage, 'errors':[v for v in errors if v in ('XD_METRICS_DOCKER_UNAVAILABLE','XD_METRICS_HISTORY_UNAVAILABLE')],
        'fresh':time.time()-raw['at']<=MAX_AGE}


def validate_monitor_registry(data):
    require(isinstance(data,dict) and data.get('schema')==1 and isinstance(data.get('apps'),dict) and len(data['apps'])<=100, 'XD_MONITOR_REGISTRY_INVALID')
    for identifier, row in data['apps'].items():
        require(isinstance(identifier,str) and ID.fullmatch(identifier) and isinstance(row,dict) and row.get('id')==identifier, 'XD_MONITOR_REGISTRY_INVALID')
        require(type(row.get('revision')) is int and row['revision']>=1 and isinstance(row.get('name'),str) and 1<=len(row['name'])<=80, 'XD_MONITOR_REGISTRY_INVALID')
        require(isinstance(row.get('host_id'),str) and TARGET.fullmatch(row['host_id']), 'XD_MONITOR_REGISTRY_INVALID')
        require(isinstance(row.get('targets'),list) and 1<=len(row['targets'])<=16 and all(isinstance(v,str) and TARGET.fullmatch(v) for v in row['targets']), 'XD_MONITOR_REGISTRY_INVALID')


class ResourceMonitor:
    def __init__(self, state, reports):
        self.root = private_directory(Path(state).resolve()/'resources')
        self.reports = Path(reports).absolute()
        self.store = RegistryStore(Path(state).resolve())

    @contextmanager
    def locked(self):
        fd = os.open(self.root/'registry.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = self.store.read_monitors()
            validate_monitor_registry(data)
            yield data
        finally:
            os.close(fd)

    def report(self):
        try: return report_read(self.reports)
        except FileNotFoundError: raise MonitorError('XD_METRICS_NOT_CONNECTED') from None
        except (OSError,ValueError,TypeError,KeyError): raise MonitorError('XD_METRICS_REPORT_UNAVAILABLE') from None

    def listing(self):
        with self.locked() as data: apps = list(data['apps'].values())
        try: report = self.report(); error = None
        except MonitorError as exc: report = None; error = str(exc)
        targets = {r['id']:r for r in report['targets']} if report else {}
        for app in apps:
            rows = [targets.get(k) for k in app['targets']]
            complete = bool(report and report['fresh'] and app['host_id']==report['host']['id'] and all(r and r['state']=='running' and r['complete'] for r in rows))
            complete = complete and not any(report['coverage'].get(k) for k in ('process_limit_reached','group_limit_reached')) if report else False
            app['status'] = 'observed' if complete else 'unconfirmed'
            app['reason'] = ('指标完整' if complete else '等待采集' if not report else
                '采集已过期' if not report['fresh'] else '服务器身份变化，请重新核对绑定' if app['host_id']!=report['host']['id'] else
                '有组件暂未发现' if any(r is None for r in rows) else '有组件未运行' if any(r['state']!='running' for r in rows) else
                '部分指标未完整读取' if any(not r['complete'] for r in rows) else '发现范围受限')
            for metric in ('cpu_percent','memory_bytes','pids'):
                app[metric] = round(sum(r[metric] for r in rows),2) if complete and all(r[metric] is not None for r in rows) else None
            app['missing_targets'] = [k for k in app['targets'] if k not in targets]
        return {'report':report, 'apps':apps, 'error':error, 'runtime_control':False, 'configuration_writable':True}

    def save(self, body):
        require(isinstance(body,dict) and set(body)=={'id','name','revision','targets'}, 'XD_MONITOR_INPUT_INVALID')
        identifier, name, revision, targets = (body.get(k) for k in ('id','name','revision','targets'))
        require(isinstance(identifier,str) and identifier!='host' and ID.fullmatch(identifier), 'XD_MONITOR_ID_INVALID')
        require(isinstance(name,str) and name.strip()==name and 1<=len(name)<=80 and all(c.isprintable() for c in name), 'XD_MONITOR_NAME_INVALID')
        require(type(revision) is int and revision>=1 and isinstance(targets,list) and 1<=len(targets)<=16 and all(isinstance(v,str) and TARGET.fullmatch(v) for v in targets) and len(set(targets))==len(targets), 'XD_MONITOR_TARGET_INVALID')
        report = self.report()
        require(report['fresh'], 'XD_METRICS_STALE')
        require(set(targets)<={r['id'] for r in report['targets']}, 'XD_MONITOR_TARGET_NOT_DISCOVERED')
        with self.locked() as data:
            old = data['apps'].get(identifier)
            require(revision==(old['revision']+1 if old else 1), 'XD_MONITOR_REVISION_CONFLICT')
            require(old is not None or len(data['apps'])<100, 'XD_MONITOR_CAPACITY')
            require(not any(set(targets)&set(row['targets']) for key,row in data['apps'].items() if key!=identifier), 'XD_MONITOR_TARGET_ALREADY_BOUND')
            row = {**body, 'targets':sorted(targets), 'host_id':report['host']['id']}
            data['apps'][identifier] = row
            self.store.write_monitors(data)
        return row

    def remove(self, identifier, body):
        require(isinstance(body,dict) and set(body)=={'revision','confirmation'}, 'XD_MONITOR_INPUT_INVALID')
        with self.locked() as data:
            old = data['apps'].get(identifier)
            require(old is not None, 'XD_MONITOR_APP_NOT_FOUND')
            require(type(body['revision']) is int and body['revision']==old['revision'], 'XD_MONITOR_REVISION_CONFLICT')
            require(body['confirmation']==old['name'], 'XD_MONITOR_CONFIRMATION_REQUIRED')
            del data['apps'][identifier]
            self.store.write_monitors(data)

    def history(self, identifier):
        with self.locked() as data:
            if identifier=='host': keys = ['host']
            else:
                require(identifier in data['apps'], 'XD_MONITOR_APP_NOT_FOUND')
                require(data['apps'][identifier]['host_id']==self.report()['host']['id'], 'XD_MONITOR_HOST_CHANGED')
                keys = data['apps'][identifier]['targets']
        path = self.reports/'history.sqlite3'
        require(not any(p.is_symlink() for p in (path,*path.parents)), 'XD_METRICS_UNSAFE_PATH')
        if not path.exists(): return {'points':[], 'available':False}
        require(path.is_file() and not path.stat().st_mode & 0o077 and path.stat().st_size<=64*1024*1024, 'XD_METRICS_UNSAFE_FILE')
        placeholders = ','.join('?' for _ in keys)
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1) as db:
            machine = db.execute("SELECT value FROM metadata WHERE name='host_id'").fetchone()
            require(machine is not None and machine[0]==self.report()['host']['id'], 'XD_MONITOR_HOST_CHANGED')
            db.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
            deadline = time.monotonic()+2
            rows = db.execute(f'''SELECT CAST(at/300 AS INTEGER)*300, AVG(cpu), MAX(memory), MAX(pids), COUNT(*) FROM
                (SELECT at, CASE WHEN COUNT(cpu)=? THEN SUM(cpu) END AS cpu, SUM(memory) AS memory, SUM(pids) AS pids
                 FROM samples WHERE target IN ({placeholders}) AND at>=? GROUP BY at HAVING COUNT(*)=?)
                 GROUP BY CAST(at/300 AS INTEGER) ORDER BY 1 LIMIT 289''',
                [len(keys), *keys, int(time.time())-86400, len(keys)]).fetchall()
        return {'available':True, 'points':[{'at':r[0], 'cpu_percent':r[1], 'memory_bytes':r[2], 'pids':r[3], 'samples':r[4]} for r in rows],
                'resolution_seconds':300, 'window_seconds':86400}


def register_resources(app, state, audit):
    monitor = ResourceMonitor(state, app.config['HOST_METRICS'])
    app.extensions['resource_monitor'] = monitor

    @app.get('/api/resources')
    def resource_list():
        try: return jsonify(monitor.listing())
        except (OSError,ValueError,TypeError,KeyError): return jsonify(error='XD_MONITOR_STATE_UNAVAILABLE'),409

    @app.post('/api/resources/apps')
    def resource_save():
        try:
            result = monitor.save(request.get_json())
            audit('monitor-register', result['id'], 'success')
            return jsonify(app=result, runtime_control=False)
        except (OSError,ValueError,TypeError,KeyError) as exc:
            return jsonify(error=str(exc) if isinstance(exc,MonitorError) else 'XD_MONITOR_STATE_UNAVAILABLE'),409

    @app.post('/api/resources/apps/<identifier>/remove')
    def resource_remove(identifier):
        try:
            monitor.remove(identifier, request.get_json())
            audit('monitor-remove', identifier, 'success')
            return jsonify(removed=True, runtime_control=False)
        except (OSError,ValueError,TypeError,KeyError) as exc:
            return jsonify(error=str(exc) if isinstance(exc,MonitorError) else 'XD_MONITOR_STATE_UNAVAILABLE'),409

    @app.get('/api/resources/history/<identifier>')
    def resource_history(identifier):
        try: return jsonify(monitor.history(identifier))
        except (OSError,ValueError,TypeError,KeyError,sqlite3.Error) as exc:
            return jsonify(error=str(exc) if isinstance(exc,MonitorError) else 'XD_METRICS_HISTORY_UNAVAILABLE'),409
