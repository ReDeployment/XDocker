"""XDocker management API. Configuration stays read-only; operations are allowlisted."""
import hashlib
import configparser
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
import urllib.request

from flask import Flask, g, jsonify, request, send_from_directory, send_file
from werkzeug.exceptions import HTTPException
from docker_api import Docker, DockerError, log_text
from backups import InstanceBackups
from instances import InstanceReader, setup_fields, password_for
from preflight import Preflight, BusyCheck
from backup_plans import BackupPlans, BackupScheduler, PlanConflict, OperationBusy, PlanCancelled
from image_versions import ImageVersions, VersionCheckBusy
from github_watch import GitHubWatch, WatchError
from github_webhooks import WebhookInbox

PROJECT = 'xdocker-web-hosting'
KEY = re.compile(r'^[a-z0-9][a-z0-9-]*$')
DOMAIN = re.compile(r'^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$')


def now():
    return datetime.now(timezone.utc).isoformat()


def env_values(path):
    """Read simple assignment values, never evaluate shell or execute commands."""
    result = {}
    if not path.is_file():
        return result
    for line in path.read_text().splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)$', line)
        if match:
            try:
                parts = shlex.split(match[2], comments=True)
                if len(parts) <= 1:
                    result[match[1]] = parts[0] if parts else ''
            except ValueError:
                continue
    return result


def configuration(root):
    env = env_values(root / 'root.env')
    enabled = env.get('ENABLED_SITES', '').split()
    sites = []
    for directory in sorted((root / 'sites').glob('*')):
        if not KEY.fullmatch(directory.name):
            continue
        values = env_values(directory / 'site.conf') | env_values(directory / '.env')
        domain = values.get('SITE_DOMAIN', '')
        kind = values.get('SITE_KIND', 'static')
        service = ('frps' if kind == 'frp_http' else
                   values.get('SITE_INSTANCE', '')+'-web' if kind == 'app' else values.get('SITE_SERVICE', ''))
        if not DOMAIN.fullmatch(domain) or not KEY.fullmatch(service):
            continue
        sites.append({'key': directory.name, 'domain': domain, 'kind': kind,
                      'service': service, 'enabled': directory.name in enabled,
                      'certificate': values.get('SITE_CERT_NAME', domain),
                      'image': values.get('SITE_IMAGE', '') if kind == 'static' else ''})
    allowed = {'nginx', 'certbot-renew'} | set(env.get('ENABLED_SERVICES', '').split())
    allowed |= {site['service'] for site in sites if site['enabled']}
    return sites, allowed


def redact(text, values=()):
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    for value in sorted(set(values), key=len, reverse=True):
        if len(value) >= 6:
            text = text.replace(value, '[REDACTED]')
    text = re.sub(r'(?i)((?:bearer|basic)\s+)[\w.+/=-]+', r'\1[REDACTED]', text)
    text = re.sub(r'''(?i)((?:setup_token|token|password|secret|authorization|api[_-]?key)["']?\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''', r'\1[REDACTED]', text)
    text = re.sub(r'://[^\s/@]+:[^\s/@]+@', '://[REDACTED]@', text)
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[PRIVATE KEY REDACTED]', text, flags=re.S)
    return text[-65536:]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def webhook_probe(opener, domain):
    """Validate TLS and the receiver rejection path without a signature or queued event."""
    request = urllib.request.Request('https://'+domain+'/github/webhook/PowerX', data=b'{}',
                                     headers={'Content-Type':'application/json'}, method='POST')
    try:
        with opener.open(request, timeout=10) as response:
            raise ValueError('Webhook accepted an unsigned probe: HTTP '+str(response.status))
    except urllib.error.HTTPError as error:
        try:
            body = error.read(4097)
            rejected = error.code == 401 and len(body) <= 4096 and json.loads(body).get('error') == 'invalid-signature'
            if not rejected: raise ValueError('Webhook rejection did not match: HTTP '+str(error.code))
        finally: error.close()
    return 'reachable', 'HTTPS 401 · 验签入口可达，无签名请求已拒绝；未创建事件'


def create_app(settings=None, docker=None):
    app = Flask(__name__, static_folder='static', static_url_path='/assets')
    app.config.update(STATE=os.getenv('ADMIN_STATE', '/state'), CONFIG=os.getenv('ADMIN_CONFIG', '/config'),
                      REPORTS=os.getenv('ADMIN_REPORTS', '/reports'), MAX_CONTENT_LENGTH=8192,
                      BACKUPS=os.getenv('ADMIN_BACKUPS', '/backups'), HOST_ROOT=os.getenv('ADMIN_HOST_ROOT', ''),
                      BACKUP_SCHEDULER_ENABLED=os.getenv('ADMIN_BACKUP_SCHEDULER_ENABLED', 'false')=='true',
                      VERSION_CATALOG=os.getenv('ADMIN_VERSION_CATALOG', '/config/powerx-versions.json'),
                      GITHUB_WATCH_ENABLED=os.getenv('ADMIN_GITHUB_WATCH_ENABLED','false')=='true',
                      GITHUB_WATCH_INTERVAL=os.getenv('ADMIN_GITHUB_WATCH_INTERVAL','300'),
                      GITHUB_WEBHOOK_QUEUE=os.getenv('ADMIN_GITHUB_WEBHOOK_QUEUE','/github-inbox'),
                      GITHUB_WEBHOOK_ENABLED=os.getenv('ADMIN_GITHUB_WEBHOOK_ENABLED','false')=='true',
                      TRUSTED_HOSTS=['localhost', '127.0.0.1', '[::1]'])
    app.config.update(settings or {})
    state, config, reports = (Path(app.config[name]) for name in ('STATE', 'CONFIG', 'REPORTS'))
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = (state / 'access-token').read_text().strip()
    if len(token) < 32:
        raise RuntimeError('Initialize a strong access-token with scripts/admin.sh init')
    engine = docker or Docker()
    database = state / 'admin.sqlite3'
    operation_lock = threading.Lock()
    backup_manager = InstanceBackups(engine, app.config['BACKUPS'], app.config['HOST_ROOT'])
    instance_reader = InstanceReader(engine, app.config['HOST_ROOT'])

    try:watch_interval=int(app.config['GITHUB_WATCH_INTERVAL'])
    except (TypeError,ValueError):watch_interval=300
    watch_interval=max(60,min(3600,watch_interval))
    watch=GitHubWatch(state,bool(app.config['GITHUB_WATCH_ENABLED']),watch_interval)
    app.extensions['github_watch']=watch
    hooks=WebhookInbox(watch.store,app.config['GITHUB_WEBHOOK_QUEUE'],bool(app.config['GITHUB_WEBHOOK_ENABLED']))
    app.extensions['github_webhooks']=hooks

    def db():
        connection = sqlite3.connect(database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    with db() as connection:
        connection.executescript('''
        CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, csrf TEXT, expires REAL);
        CREATE TABLE IF NOT EXISTS attempts (at REAL);
        CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, kind TEXT, target TEXT, action TEXT,
          state TEXT, created TEXT, completed TEXT, output TEXT);
        CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at TEXT, action TEXT, target TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS probes (site TEXT PRIMARY KEY, at TEXT, status TEXT, output TEXT);
        ''')
        unfinished = {r['id'] for r in connection.execute("SELECT id FROM jobs WHERE state='running'")}
        if unfinished:
            orphan_jobs = engine.request('GET', '/containers/json?' + urlencode({'all':'true','filters':json.dumps({'label':['io.xdocker.admin.job']})}))
            for container in orphan_jobs:
                if container.get('Labels', {}).get('io.xdocker.admin.job') in unfinished:
                    engine.request('DELETE', '/containers/'+container['Id']+'?force=true&v=true')
        connection.execute("UPDATE jobs SET state='interrupted', completed=?, output='管理服务重启，任务未确认完成；请检查实际状态。' WHERE state='running'", (now(),))
    database.chmod(0o600)

    def audit(action, target, result):
        with db() as connection:
            connection.execute('INSERT INTO audit(at,action,target,result) VALUES(?,?,?,?)', (now(), action, target, result))

    @app.before_request
    def authentication():
        if request.method == 'POST':
            if request.headers.get('Origin') != request.host_url.rstrip('/'):
                return jsonify(error='请求来源不匹配，请使用当前页面操作。'), 403
            if not request.is_json:
                return jsonify(error='Only JSON requests are accepted'), 415
        if not request.path.startswith('/api/') or request.path == '/api/login':
            return None
        raw = request.cookies.get('xdocker_session', '')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with db() as connection:
            row = connection.execute('SELECT * FROM sessions WHERE hash=? AND expires>?', (digest, time.time())).fetchone()
        if not row:
            return jsonify(error='请先登录管理控制台。'), 401
        g.session_hash = digest
        g.csrf = row['csrf']
        if request.method == 'POST' and not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), g.csrf):
            return jsonify(error='操作验证已失效，请刷新页面。'), 403

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.errorhandler(Exception)
    def error_response(error):
        if isinstance(error, HTTPException):
            return jsonify(error=error.description), error.code
        if isinstance(error, DockerError):
            return jsonify(error=str(error)), 502
        app.logger.error('Management request failed: %s', type(error).__name__)
        return jsonify(error='操作未完成，请检查服务状态或管理服务日志。'), 500

    @app.get('/')
    def page():
        return send_from_directory(app.static_folder, 'index.html')

    @app.get('/healthz')
    def health():
        return jsonify(status='ok')

    @app.post('/api/login')
    def login():
        payload = request.get_json()
        value = payload.get('token', '') if isinstance(payload, dict) else ''
        if not isinstance(value, str):
            return jsonify(error='请输入管理访问令牌。'), 400
        with db() as connection:
            connection.execute('DELETE FROM attempts WHERE at<?', (time.time()-300,))
            connection.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            if connection.execute('SELECT count(*) FROM attempts').fetchone()[0] >= 10:
                return jsonify(error='尝试次数过多，请在五分钟后重试。'), 429
            if not hmac.compare_digest(hashlib.sha256(value.encode()).digest(), hashlib.sha256(token.encode()).digest()):
                connection.execute('INSERT INTO attempts VALUES(?)', (time.time(),))
                return jsonify(error='访问令牌不正确。'), 401
            raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            connection.execute('INSERT INTO sessions VALUES(?,?,?)', (hashlib.sha256(raw.encode()).hexdigest(), csrf, time.time()+28800))
        audit('login', 'admin', 'success')
        response = jsonify(csrf=csrf)
        response.set_cookie('xdocker_session', raw, httponly=True, samesite='Strict', max_age=28800, path='/')
        return response

    @app.get('/api/session')
    def session_info():
        return jsonify(csrf=g.csrf)

    @app.post('/api/logout')
    def logout():
        with db() as connection:
            connection.execute('DELETE FROM sessions WHERE hash=?', (g.session_hash,))
        response = jsonify(ok=True)
        response.delete_cookie('xdocker_session')
        return response

    def managed_projects():
        _, allowed = configuration(config)
        projects = {PROJECT: allowed}
        for file in (config / 'instances').glob('*/instance.json'):
            try:
                instance = json.loads(file.read_text())
                project = instance.get('project', '')
                if instance.get('app') == 'powerx' and instance.get('enabled') is True and KEY.fullmatch(project) and project == file.parent.name and project != PROJECT:
                    projects[project] = {'postgres', 'redis', 'backend', 'web-admin'}
            except (ValueError, OSError, TypeError):
                continue
        return projects

    # Startup recovery restores only the application services this control
    # plane itself paused; unfinished helper containers were removed above.
    backup_manager.recover([p for p in managed_projects() if p != PROJECT])

    def managed_containers():
        projects = managed_projects()
        filters = urlencode({'all': 'true', 'filters': json.dumps({'label': ['com.docker.compose.project']})})
        containers = engine.request('GET', '/containers/json?' + filters)
        return [c for c in containers if c.get('Labels', {}).get('com.docker.compose.project') in projects
                and c['Labels'].get('com.docker.compose.service') in projects[c['Labels']['com.docker.compose.project']]
                and c['Labels'].get('com.docker.compose.oneoff', 'False').lower() != 'true']

    preflight = Preflight(engine, config, state, reports, app.config['HOST_ROOT'],
                          lambda: configuration(config)[0],
                          lambda: [p for p in managed_projects() if p != PROJECT])

    @app.get('/api/preflight')
    def preflight_list():
        if request.args:return jsonify(error='检查接口不接受自定义目标或参数。'), 400
        try:return jsonify(preflight.listing())
        except (OSError, ValueError, TypeError):
            return jsonify(error='无法生成检查清单，请核对已启用的配置和宿主机报告。'), 409

    @app.get('/api/preflight/<key>')
    def preflight_check(key):
        if request.args:return jsonify(error='检查接口不接受自定义目标或参数。'), 400
        try:return jsonify(preflight.run(key))
        except KeyError:return jsonify(error='请选择当前检查清单中的项目。'), 404
        except BusyCheck:return jsonify(error='另一个只读检查正在进行，请稍后重试。'), 409
        except (OSError, ValueError, TypeError):
            return jsonify(error='检查清单不可用，请核对配置。'), 409

    def service_name(container):
        labels = container['Labels']
        service = labels['com.docker.compose.service']
        return service if labels['com.docker.compose.project'] == PROJECT else labels['com.docker.compose.project']+'/'+service

    def resolve(identifier):
        if not re.fullmatch(r'[a-f0-9]{64}', identifier):
            return None
        return next((c for c in managed_containers() if c['Id'] == identifier), None)

    def safe_logs(identifier):
        details = engine.request('GET', f'/containers/{identifier}/json')
        sensitive = [token]
        for assignment in details.get('Config', {}).get('Env', []):
            key, _, value = assignment.partition('=')
            if re.search('TOKEN|SECRET|PASS|KEY|CREDENTIAL|AUTH', key, re.I):
                sensitive.append(value)
        raw = engine.request('GET', f'/containers/{identifier}/logs?stdout=true&stderr=true&tail=200&timestamps=true', raw=True)
        return redact(log_text(raw), sensitive)

    def inventory():
        return json.loads((config / 'certificates.json').read_text())['certificates']

    def cert_summary():
        histories = []
        for file in reports.glob('certificates-*.json'):
            try:
                report = json.loads(file.read_text())
                if report.get('action') in ('check', 'renew', 'renew-test', 'issue', 'issue-test'):
                    histories.append(report)
            except (ValueError, OSError):
                continue
        rows = {item['name']: dict(item, status='UNKNOWN', checked_at=None) for item in inventory()}
        for report in sorted(histories, key=lambda x: x.get('checked_at', '')):
            for result in report.get('results', []):
                if result.get('name') in rows:
                    row = rows[result['name']]
                    for key in ('expiry', 'days_remaining', 'fingerprint'):
                        if key in result:
                            row[key] = result[key]
                    status = result.get('certificate_status') or result.get('status')
                    if status in ('LOCAL_VALID', 'EXPIRING', 'CRITICAL', 'EXPIRED', 'ERROR', 'NOT_YET_VALID'):
                        row.update(status=status, checked_at=report.get('checked_at'))
        for row in rows.values():
            if row.get('expiry'):
                try:
                    remaining = (datetime.fromisoformat(row['expiry']) - datetime.now(timezone.utc)).total_seconds()
                    row['days_remaining'] = int(remaining // 86400)
                    if remaining < 0:
                        row['status'] = 'EXPIRED'
                except ValueError:
                    row['status'] = 'UNKNOWN'
        return list(rows.values()), [{k: r.get(k) for k in ('action', 'checked_at', 'reload')} for r in histories]

    @app.get('/api/snapshot')
    def snapshot():
        sites, _ = configuration(config)
        containers = managed_containers()
        services = [{'id': c['Id'], 'service': service_name(c),
                     'name': c['Names'][0].lstrip('/'), 'image': c['Image'], 'state': c['State'],
                     'status': c['Status'], 'created': c['Created'], 'protected': c['Labels']['com.docker.compose.service'] == 'admin',
                     'ports': [{'ip': p.get('IP', ''), 'host': p.get('PublicPort'), 'container': p.get('PrivatePort')} for p in c.get('Ports', []) if p.get('PublicPort')]} for c in containers]
        certs, report_history = cert_summary()
        with db() as connection:
            probes = {row['site']: dict(row) for row in connection.execute('SELECT * FROM probes')}
        for site in sites:
            site['probe'] = probes.get(site['key'])
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(config / 'frpc-template.ini')
        for site in sites:
            if site['kind'] == 'frp_http':
                for section in parser.sections():
                    route = parser[section]
                    if route.get('subdomain') == site['domain'].split('.')[0]:
                        site['client_template'] = {'name':section, 'address':route.get('local_ip'), 'port':route.get('local_port')}
        version = engine.request('GET', '/version')
        return jsonify(at=now(), services=services, sites=sites, certificates=certs, reports=report_history,
                       engine={k: version.get(k) for k in ('Version', 'ApiVersion', 'Os', 'Arch')},
                       revision=os.getenv('ADMIN_DEPLOY_REVISION', 'development'), image_revision=os.getenv('XDOCKER_REVISION', 'development'))

    @app.get('/api/services/<identifier>/logs')
    def logs(identifier):
        if not resolve(identifier):
            return jsonify(error='该容器不属于已启用的 XDocker 服务。'), 404
        return jsonify(text=safe_logs(identifier))

    @app.get('/api/services/<identifier>/stats')
    def stats(identifier):
        container = resolve(identifier)
        if not container:
            return jsonify(error='Unknown managed service'), 404
        if container['State'] != 'running':
            return jsonify(error='容器未运行，无法读取资源。'), 409
        raw = engine.request('GET', f'/containers/{identifier}/stats?stream=false', timeout=25)
        current, previous = raw.get('cpu_stats', {}), raw.get('precpu_stats', {})
        delta = current.get('cpu_usage', {}).get('total_usage', 0)-previous.get('cpu_usage', {}).get('total_usage', 0)
        total = current.get('system_cpu_usage', 0)-previous.get('system_cpu_usage', 0)
        memory = raw.get('memory_stats', {})
        usage = max(0, memory.get('usage', 0)-memory.get('stats', {}).get('inactive_file', 0))
        return jsonify(cpu_percent=round(max(0, delta)/total*current.get('online_cpus', 1)*100, 2) if total > 0 else None,
                       memory_bytes=usage, memory_limit=memory.get('limit'), pids=raw.get('pids_stats', {}).get('current'))

    def start_job(kind, target, action, work, prepare=None, finished=None):
        if not operation_lock.acquire(blocking=False):
            raise OperationBusy('已有操作正在执行，请等待结果。')
        identifier = secrets.token_hex(16)
        try:
            with db() as connection:
                connection.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)', (identifier, kind, target, action, 'running', now(), None, ''))
            audit(action, target, 'requested')
            if prepare is not None and not prepare(identifier):
                audit(action,target,'cancelled')
                raise PlanCancelled('计划已禁用、变更或超出允许开始时间，未启动备份。')
        except Exception:
            try:
                with db() as connection:
                    connection.execute("UPDATE jobs SET state='cancelled',completed=?,output='任务未进入执行，请核对计划与时间窗口。' WHERE id=?", (now(), identifier))
            finally:operation_lock.release()
            raise

        def run():
            try:
                output = work(identifier)
                status = 'success'
            except Exception as error:
                output = str(error) if isinstance(error, (DockerError, ValueError, TimeoutError)) else '操作失败；请检查实际服务状态。'
                status = 'failed'
            finally:
                try:
                    with db() as connection:
                        connection.execute('UPDATE jobs SET state=?,completed=?,output=? WHERE id=?', (status, now(), redact(output, [token]), identifier))
                    audit(action, target, status)
                    if finished is not None:finished(identifier,status)
                finally:operation_lock.release()
        try:threading.Thread(target=run, daemon=True).start()
        except Exception:
            try:
                with db() as connection:
                    connection.execute("UPDATE jobs SET state='failed',completed=?,output='执行线程未启动，未开始业务操作。' WHERE id=?", (now(),identifier))
                audit(action,target,'failed')
                if finished is not None:finished(identifier,'failed')
            finally:operation_lock.release()
            raise
        return identifier

    def job(kind, target, action, work):
        try:return jsonify(id=start_job(kind,target,action,work)), 202
        except OperationBusy:return jsonify(error='已有操作正在执行，请等待结果。'), 409

    def command_payload():
        body = request.get_json()
        return body if isinstance(body, dict) else {}

    @app.post('/api/services/<identifier>/action')
    def service_action(identifier):
        container = resolve(identifier)
        if not container:
            return jsonify(error='Unknown managed service'), 404
        body = command_payload()
        action = body.get('action')
        service = service_name(container)
        if service == 'admin':
            return jsonify(error='管理服务自身请通过 SSH 维护。'), 409
        if action not in ('start', 'stop', 'restart') or body.get('confirmation') != service:
            return jsonify(error='请输入完整服务名确认操作。'), 400
        def work(_):
            # Resolve again to reject removed/replaced targets at execution time.
            if not resolve(identifier):
                raise ValueError('目标容器已变化，请刷新后重试。')
            engine.request('POST', f'/containers/{identifier}/{action}?t=15', timeout=40)
            details = engine.request('GET', f'/containers/{identifier}/json')
            actual = details['State']['Status']
            expected = 'exited' if action == 'stop' else 'running'
            if actual != expected:
                raise ValueError('Docker 操作结束，但实际状态为 ' + actual)
            return '实际状态：'+actual+'。请继续检查容器健康状态和站点响应。'
        return job('service', service, action, work)

    def certificate_work(action, name, identifier):
        renewal = next((c for c in managed_containers() if c['Labels']['com.docker.compose.service'] == 'certbot-renew'), None)
        if not renewal:
            raise ValueError('证书续期服务不存在，请先通过 Git 部署 TLS 服务。')
        details = engine.request('GET', f'/containers/{renewal["Id"]}/json')
        allowed = {'/etc/letsencrypt', '/var/lib/certbot-events', '/var/www/certbot', '/opt/xdocker/certbot/certificates.json', '/opt/xdocker/scripts/certificates.py'}
        mounts = [{"Type": "bind", "Source": m['Source'], "Target": m['Destination'], "ReadOnly": not m['RW']} for m in details['Mounts'] if m['Type'] == 'bind' and m['Destination'] in allowed]
        if {m['Target'] for m in mounts} != allowed:
            raise ValueError('证书工具挂载与预期不一致，已拒绝运行。')
        networks = details['NetworkSettings']['Networks']
        if len(networks) != 1:
            raise ValueError('证书网络与预期不一致。')
        # Use a unique report per job so single-certificate checks never overwrite the renewal scheduler report.
        command = ['python3', '/opt/xdocker/scripts/certificates.py', action, name,
                   '--runtime', 'container', '--reload', 'container-event', '--json',
                   '--report', '/var/lib/certbot-events/certificates-admin-'+identifier+'.json']
        body = {'Image': details['Config']['Image'], 'Entrypoint': command, 'Cmd': [],
                'Labels': {'io.xdocker.admin.job': identifier},
                'HostConfig': {'Mounts': mounts, 'NetworkMode': next(iter(networks))}}
        created = engine.request('POST', '/containers/create?name=xdocker-admin-job-'+identifier, body)
        container_id = created['Id']
        try:
            engine.request('POST', f'/containers/{container_id}/start')
            deadline = time.monotonic()+600
            while time.monotonic() < deadline:
                result = engine.request('GET', f'/containers/{container_id}/json')['State']
                if not result['Running']:
                    output = safe_logs(container_id)
                    # check exit 2 means usable but approaching expiry; show it as a warning rather than failure.
                    if result['ExitCode'] != 0 and not (action == 'check' and result['ExitCode'] == 2):
                        raise ValueError(f'证书工具退出码 {result["ExitCode"]}\n'+output)
                    return output
                time.sleep(2)
            raise TimeoutError('证书操作超过十分钟；已停止该临时任务，请检查结果。')
        finally:
            engine.request('DELETE', f'/containers/{container_id}?force=true&v=true')

    @app.post('/api/certificates/action')
    def certificate_action():
        body = command_payload()
        name, action = body.get('name'), body.get('action')
        allowed = {item['name'] for item in inventory() if item.get('enabled', True)}
        if not isinstance(name, str) or name not in allowed or not DOMAIN.fullmatch(name):
            return jsonify(error='请选择清单中已启用的证书。'), 400
        if action not in ('check', 'renew-test', 'renew') or body.get('confirmation') != name:
            return jsonify(error='请选择支持的操作并输入完整证书名。'), 400
        return job('certificate', name, action, lambda identifier: certificate_work(action, name, identifier))

    @app.post('/api/sites/probe')
    def probe_sites():
        body = command_payload()
        selected = body.get('site')
        sites, _ = configuration(config)
        sites = [s for s in sites if s['enabled'] and (selected == 'all' or s['key'] == selected)]
        if not sites:
            return jsonify(error='请选择已启用的站点。'), 400
        def work(_):
            outputs = []
            failures = 0
            opener = urllib.request.build_opener(NoRedirect)
            for site in sites:
                url = 'https://'+site['domain']+('/healthz' if site['kind'] == 'frp_http' else '/')
                try:
                    if site['kind'] == 'github_webhook':
                        status, detail = webhook_probe(opener, site['domain'])
                    else:
                        with opener.open(url, timeout=10) as response:
                            code = response.status
                        status, detail = ('reachable', f'HTTPS {code}') if code == 200 else ('failed', f'HTTPS {code}')
                except Exception as error:
                    status, detail = 'failed', type(error).__name__ + ': ' + redact(str(error), [token])[:300]
                failures += int(status == 'failed')
                with db() as connection:
                    connection.execute('INSERT OR REPLACE INTO probes VALUES(?,?,?,?)', (site['key'], now(), status, detail))
                outputs.append(site['domain']+' '+detail)
            if failures:
                raise ValueError('\n'.join(outputs))
            return '\n'.join(outputs)
        return job('probe', selected, 'probe', work)

    @app.get('/api/jobs')
    def jobs():
        with db() as connection:
            return jsonify(jobs=[dict(row) for row in connection.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 30')])

    @app.get('/api/jobs/<identifier>')
    def job_detail(identifier):
        if not re.fullmatch(r'[a-f0-9]{32}',identifier):return jsonify(error='任务身份格式不正确。'),404
        with db() as connection:
            row=connection.execute('SELECT * FROM jobs WHERE id=?',(identifier,)).fetchone()
        if row is None:return jsonify(error='任务记录不存在，请核对备份清单。'),404
        return jsonify(job=dict(row))

    @app.get('/api/audit')
    def audits():
        with db() as connection:
            return jsonify(events=[dict(row) for row in connection.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 100')])

    @app.get('/api/github')
    def github_observations():
        if set(request.args)-{'before'}:return jsonify(error='不接受自定义 GitHub 查询或目标。'),400
        cursor=request.args.get('before')
        if cursor is not None and (not re.fullmatch(r'[0-9]{1,19}',cursor) or not 0<int(cursor)<2**63):
            return jsonify(error='通知游标不合法。'),400
        try:
            data=watch.listing(int(cursor) if cursor else None);data['webhooks']=hooks.status()
            return jsonify(data)
        except (WatchError,OSError,sqlite3.DatabaseError,ValueError):return jsonify(error='GitHub 观察记录不可读；原文件保留，未启动采集。'),409

    @app.get('/api/github/summary')
    def github_observation_summary():
        if request.args:return jsonify(error='不接受自定义查询。'),400
        try:
            if not watch.store or watch.error:raise WatchError('watch-store-unavailable')
            return jsonify(watch.store.summary())
        except (WatchError,OSError,sqlite3.DatabaseError,ValueError):return jsonify(error='GitHub 通知状态未确认。'),409

    @app.post('/api/github/action')
    def github_observation_action():
        body=command_payload();action=body.get('action')
        fields={'sync':{'action','project'},'read':{'action','ids'},'consume-webhooks':{'action'},
                'configure':{'action','project','revision','enabled','branches','required_checks'}}
        if request.args or action not in fields or set(body)!=fields[action]:return jsonify(error='仅支持采集、导入 Webhook、标记已读和本地观察配置。'),400
        try:
            if not watch.store or watch.error:raise WatchError('watch-store-unavailable')
            if action=='consume-webhooks':
                result=hooks.consume();audit('github-webhook-import','inbox','success')
                return jsonify(result)
            if action=='sync':
                selected=watch.sync(body['project']);audit('github-sync',body['project'],'requested')
                return jsonify(projects=selected,running=True,github_writes=False),202
            if action=='read':watch.store.mark_read(body['ids'])
            else:watch.store.configure(body['project'],body['revision'],body['enabled'],body['branches'],body['required_checks'])
            audit('github-'+action,body.get('project','inbox'),'success')
            return jsonify(saved=True,github_writes=False)
        except (WatchError,OSError,sqlite3.DatabaseError,ValueError,TypeError) as error:
            code=str(error) if isinstance(error,WatchError) else 'watch-operation-unconfirmed'
            return jsonify(error='GitHub 观察操作未完成：'+code),409

    def backup_instances():
        return [p for p in managed_projects() if p != PROJECT]

    versions=ImageVersions(engine,config,app.config['VERSION_CATALOG'],backup_instances,managed_containers,backup_manager.records)

    @app.get('/api/image-versions')
    def image_versions_list():
        if request.args:return jsonify(error='版本接口不接受自定义 URL 或参数。'),400
        try:return jsonify(versions.listing())
        except (OSError,ValueError,TypeError):return jsonify(error='无法读取登记实例的镜像配置。'),409

    @app.get('/api/image-versions/<instance>')
    @app.get('/api/image-versions/<instance>/local/<identifier>')
    @app.get('/api/image-versions/<instance>/preview/<identifier>')
    def image_version_detail(instance,identifier=None):
        if request.args:return jsonify(error='版本接口不接受自定义目标或参数。'),400
        try:
            if identifier is None:return jsonify(versions.current(instance))
            if '/preview/' in request.path:return jsonify(versions.preview(instance,identifier))
            return jsonify(versions.local(instance,identifier))
        except KeyError:return jsonify(error='请选择已登记实例和目录中的候选版本。'),404
        except (OSError,ValueError,TypeError):return jsonify(error='镜像目录或配置异常，未生成升级预览。'),409

    @app.get('/api/image-versions/check/<identifier>/evidence')
    @app.get('/api/image-versions/check/<identifier>/registry/<role>')
    def image_version_check(identifier,role=None):
        if request.args:return jsonify(error='版本检查不接受自定义 URL 或参数。'),400
        try:return jsonify(versions.check(identifier,'registry' if role is not None else 'evidence',role))
        except VersionCheckBusy:return jsonify(error='另一个版本证据检查正在进行，请稍后重试。'),409
        except KeyError:return jsonify(error='请选择目录中的候选版本及前后端角色。'),404
        except (OSError,ValueError,TypeError):return jsonify(error='候选目录格式异常，未接受此版本。'),409

    plans = BackupPlans(state, backup_instances)

    def scheduled_backup(plan, at):
        instance=plan['instance']
        def work(identifier):
            if instance not in backup_instances():raise ValueError('实例已取消登记，未启动备份。')
            containers={c['Labels']['com.docker.compose.service']:c for c in managed_containers()
                        if c['Labels']['com.docker.compose.project']==instance}
            output=backup_manager.create(instance,containers,identifier)
            backup_manager.verify(instance,identifier)
            plans.stage(instance,identifier,backup_state='ready')
            if plan['auto_prune']:
                try:
                    cleanup=backup_manager.prune(instance,plan['keep'],require_valid_survivor=True)
                    plans.stage(instance,identifier,cleanup_state='success')
                    output+='\n'+cleanup
                except Exception:
                    plans.stage(instance,identifier,cleanup_state='failed')
                    raise ValueError('本次备份已生成并通过校验，但自动清理未完成；请查看备份及操作记录，未自动重试。') from None
            return output
        return start_job('backup',instance,'backup-scheduled',work,
            prepare=lambda identifier:plans.claim(instance,plan['revision'],plan['next_run_at'],identifier,scheduler.clock()),
            finished=lambda identifier,status:plans.finish(instance,identifier,status))

    scheduler=BackupScheduler(plans,scheduled_backup,bool(app.config['BACKUP_SCHEDULER_ENABLED']),audit,
                              clock=app.config.get('BACKUP_SCHEDULER_CLOCK'))
    app.extensions['backup_plans']=plans
    app.extensions['backup_scheduler']=scheduler
    try:plans.recover()
    except (OSError,ValueError,TypeError):scheduler.error='计划文件读取失败；不会覆盖原文件或启动计划备份。'

    @app.get('/api/backup-plans')
    def list_backup_plans():
        try:rows=plans.listing()
        except (OSError,ValueError,TypeError):return jsonify(error='无法读取备份计划；请检查私有计划文件，原文件不会被覆盖。'),409
        return jsonify(plans=rows,scheduler_enabled=scheduler.enabled,
                       scheduler_running=bool(scheduler.worker and scheduler.worker.is_alive() and scheduler.lease),
                       scheduler_error=scheduler.error,
                       scope='全实例维护备份；开启后短暂停止所选实例前后端。允许迟到只限制开始时间，不限制备份完成时长。')

    @app.post('/api/backup-plans/<instance>')
    def update_backup_plan(instance):
        body=command_payload()
        if instance not in backup_instances():return jsonify(error='请选择明确登记的 PowerX 实例。'),404
        if body.get('confirmation')!=instance:return jsonify(error='请输入完整实例名确认。'),400
        try:
            if body.get('action')=='save':
                config=body.get('plan')
                if isinstance(config,dict) and config.get('enabled') is True and body.get('maintenance_ack') is not True:
                    return jsonify(error='启用前请明确确认暂停前后端的维护影响。'),400
                if isinstance(config,dict) and config.get('auto_prune') is True and body.get('prune_ack') is not True:
                    return jsonify(error='开启自动清理前请确认删除旧成功备份的影响。'),400
                row=plans.update(instance,config,body.get('revision'))
            elif body.get('action')=='disable':row=plans.disable(instance,body.get('revision'))
            else:return jsonify(error='请选择保存或禁用计划。'),400
        except PlanConflict as error:return jsonify(error=str(error)),409
        except (ValueError,TypeError):return jsonify(error='计划字段无效或计划文件异常，请重新读取并核对时间、份数和版本。'),400
        except OSError:return jsonify(error='无法保存计划，请核对私有目录权限。'),409
        audit('backup-plan-'+body['action'],instance,'success')
        return jsonify(plan=row,scheduler_enabled=scheduler.enabled)

    def instance_config(project):
        backend = next((c for c in managed_containers()
                        if c['Labels']['com.docker.compose.project'] == project
                        and c['Labels']['com.docker.compose.service'] == 'backend'), None)
        if backend is None:
            raise ValueError('Start the instance once to generate its private Setup configuration.')
        return instance_reader.read(backend, project)

    @app.get('/api/instances')
    def list_instances():
        sites, _ = configuration(config)
        return jsonify(instances=[{'name':project,
            'url':next(('https://'+s['domain'] for s in sites if s['enabled'] and s['kind']=='app'
                       and s['service']==project+'-web'), '')}
            for project in managed_projects() if project != PROJECT])

    @app.get('/api/instances/<project>')
    def instance_details(project):
        if project not in managed_projects() or project == PROJECT:
            return jsonify(error='请选择已登记的 PowerX 实例。'), 404
        try:
            values = setup_fields(instance_config(project))
        except (ValueError, OSError, TypeError):
            return jsonify(error='无法读取该实例配置，请确认已启动过实例且配置挂载正确。'), 409
        return jsonify(instance=project, **values)

    @app.post('/api/instances/<project>/credentials')
    def instance_credentials(project):
        if project not in managed_projects() or project == PROJECT:
            return jsonify(error='请选择已登记的 PowerX 实例。'), 404
        body = command_payload()
        service = body.get('service')
        if service not in ('postgres','redis') or body.get('confirmation') != project:
            return jsonify(error='请选择数据库或缓存，并输入完整实例名确认。'), 400
        try:
            password = password_for(instance_config(project), service)
        except (ValueError, OSError, TypeError):
            return jsonify(error='无法读取该服务密码，请检查实例私有配置。'), 409
        audit('credential-view', project+'/'+service, 'success')
        return jsonify(instance=project, service=service, password=password)

    @app.get('/api/backups')
    def list_backups():
        instances = backup_instances()
        sites, _ = configuration(config)
        return jsonify(instances=instances, backups=backup_manager.records(instances),
            application_centers=[{'instance':p, 'url':'https://'+s['domain']+'/ops/backup'}
                for p in instances for s in sites if s['kind']=='app' and s['service']==p+'-web' and s['enabled']])

    @app.post('/api/backups/action')
    def backup_action():
        body = command_payload()
        instance = body.get('instance')
        if not isinstance(instance, str) or instance not in backup_instances():
            return jsonify(error='请选择明确登记的 PowerX 实例。'), 400
        action = body.get('action')
        identifier = body.get('id', '')
        target = instance if action in ('create','prune') else identifier
        if not isinstance(target, str) or body.get('confirmation') != target:
            return jsonify(error='请输入完整目标名称确认。'), 400
        if action == 'create':
            containers = {c['Labels']['com.docker.compose.service']:c for c in managed_containers()
                if c['Labels']['com.docker.compose.project']==instance}
            return job('backup', instance, 'backup-create', lambda jid: backup_manager.create(instance, containers, jid))
        if action in ('verify','drill'):
            try:backup_manager.read(instance, identifier)
            except (OSError, ValueError):return jsonify(error='备份不存在或身份不匹配。'), 404
            work = (lambda jid: '备份文件及下载包 SHA256 校验通过。' if backup_manager.verify(instance, identifier) else '') if action=='verify' else (lambda jid: backup_manager.drill(instance, identifier, jid))
            return job('backup', instance, 'backup-'+action, work)
        if action == 'prune':
            keep = body.get('keep')
            if not isinstance(keep, int) or isinstance(keep, bool) or not 1 <= keep <= 100:
                return jsonify(error='保留份数须为 1–100。'), 400
            return job('backup', instance, 'backup-prune', lambda jid: backup_manager.prune(instance, keep))
        return jsonify(error='Unsupported backup action'), 400

    @app.get('/api/backups/<instance>/<identifier>/download')
    def download_backup(instance, identifier):
        if instance not in backup_instances():
            return jsonify(error='Unknown registered instance'), 404
        try:
            folder, record = backup_manager.verify(instance, identifier)
        except (OSError, ValueError):
            return jsonify(error='备份未完成或校验失败，拒绝下载。'), 409
        audit('backup-download', instance+'/'+identifier, 'success')
        response = send_file(folder/'backup.tar.gz', as_attachment=True,
                             download_name=instance+'-'+identifier+'.tar.gz', mimetype='application/gzip')
        response.headers['X-Backup-SHA256'] = record['bundle_sha256']
        return response

    if scheduler.enabled and not app.config.get('TESTING'):
        scheduler.start()
    if watch.enabled and not app.config.get('TESTING'):watch.start()
    if hooks.enabled and not app.config.get('TESTING'):hooks.start()
    return app
