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

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException
from docker_api import Docker, DockerError, log_text

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
        service = 'frps' if kind == 'frp_http' else values.get('SITE_SERVICE', '')
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


def create_app(settings=None, docker=None):
    app = Flask(__name__, static_folder='static', static_url_path='/assets')
    app.config.update(STATE=os.getenv('ADMIN_STATE', '/state'), CONFIG=os.getenv('ADMIN_CONFIG', '/config'),
                      REPORTS=os.getenv('ADMIN_REPORTS', '/reports'), MAX_CONTENT_LENGTH=8192,
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

    def managed_containers():
        _, allowed = configuration(config)
        filters = urlencode({'all': 'true', 'filters': json.dumps({'label': ['com.docker.compose.project='+PROJECT]})})
        containers = engine.request('GET', '/containers/json?' + filters)
        return [c for c in containers if c.get('Labels', {}).get('com.docker.compose.project') == PROJECT
                and c['Labels'].get('com.docker.compose.service') in allowed
                and c['Labels'].get('com.docker.compose.oneoff', 'False').lower() != 'true']

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
        services = [{'id': c['Id'], 'service': c['Labels']['com.docker.compose.service'],
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

    def job(kind, target, action, work):
        if not operation_lock.acquire(blocking=False):
            return jsonify(error='已有操作正在执行，请等待结果。'), 409
        identifier = secrets.token_hex(16)
        try:
            with db() as connection:
                connection.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)', (identifier, kind, target, action, 'running', now(), None, ''))
            audit(action, target, 'requested')
        except Exception:
            operation_lock.release()
            raise

        def run():
            try:
                output = work(identifier)
                status = 'success'
            except Exception as error:
                output = str(error) if isinstance(error, (DockerError, ValueError, TimeoutError)) else '操作失败；请检查实际服务状态。'
                status = 'failed'
            finally:
                with db() as connection:
                    connection.execute('UPDATE jobs SET state=?,completed=?,output=? WHERE id=?', (status, now(), redact(output, [token]), identifier))
                audit(action, target, status)
                operation_lock.release()
        threading.Thread(target=run, daemon=True).start()
        return jsonify(id=identifier), 202

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
        service = container['Labels']['com.docker.compose.service']
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

    @app.get('/api/audit')
    def audits():
        with db() as connection:
            return jsonify(events=[dict(row) for row in connection.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 100')])

    return app
