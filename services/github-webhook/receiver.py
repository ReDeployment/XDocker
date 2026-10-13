"""Signed repository events only. No Docker, administration or GitHub API access."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from flask import Flask, jsonify, request

PROJECTS = json.loads(Path(__file__).with_name('projects.json').read_text())
EVENTS = {'ping', 'push', 'pull_request', 'pull_request_review', 'issue_comment',
          'pull_request_review_comment', 'commit_comment', 'workflow_run', 'workflow_job',
          'check_run', 'check_suite', 'status'}
GUID = re.compile(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}')
LIMIT = 2 * 1024 * 1024


def clean(value, secret='', limit=2000):
    value = value if isinstance(value, str) else ''
    if secret: value = value.replace(secret, '[REDACTED]')
    value = re.sub(r'(?i)((?:bearer|basic)\s+)[\w.+/=-]+', r'\1[REDACTED]', value)
    value = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b', '[REDACTED]', value)
    value = re.sub(r'''(?i)((?:token|password|secret|authorization|api[_-]?key)["']?\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''', r'\1[REDACTED]', value)
    value = re.sub(r'://[^\s/@]+:[^\s/@]+@', '://[REDACTED]@', value)
    value = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[PRIVATE KEY REDACTED]', value, flags=re.S)
    return ''.join(c for c in value if ord(c) >= 32 or c in '\n\t')[:limit]


def positive(value):
    return value if type(value) is int and 0 < value < 2**63 else None


def object_value(data, key):
    value = data.get(key, {})
    if not isinstance(value, dict): raise ValueError('invalid-payload')
    return value


def normalize(project, event, data, secret):
    repo = PROJECTS[project]
    if not isinstance(data, dict) or object_value(data, 'repository').get('full_name') != repo:
        raise ValueError('repository-mismatch')
    primary = {'pull_request': 'pull_request', 'pull_request_review': 'review',
               'issue_comment': 'comment', 'pull_request_review_comment': 'comment',
               'commit_comment': 'comment', 'workflow_run': 'workflow_run',
               'workflow_job': 'workflow_job', 'check_run': 'check_run', 'check_suite': 'check_suite'}
    item = object_value(data, primary[event]) if event in primary else data
    if event in primary and not item: raise ValueError('invalid-payload')
    pr = object_value(data, 'pull_request')
    number = positive(pr.get('number')) or positive(object_value(data, 'issue').get('number')) or positive(data.get('number'))
    raw_sha = item.get('head_sha') or item.get('commit_id') or object_value(pr, 'head').get('sha') or data.get('after') or data.get('sha')
    sha = raw_sha if isinstance(raw_sha, str) and re.fullmatch('[a-f0-9]{40}', raw_sha) else None
    identifier = positive(item.get('id'))
    base = 'https://github.com/' + repo
    # URLs are constructed, never fetched from an event or interpreted as commands.
    url = base
    if number and event.startswith(('pull_request', 'issue_comment')): url += '/pull/' + str(number) if pr or 'pull_request' in object_value(data, 'issue') else '/issues/' + str(number)
    elif event == 'workflow_run' and identifier: url += '/actions/runs/' + str(identifier)
    elif sha: url += '/commit/' + sha
    details = item.get('body', '')
    if event == 'check_run': details = object_value(item, 'output').get('summary', '')
    if event == 'workflow_job':
        steps = item.get('steps', [])
        if not isinstance(steps, list): raise ValueError('invalid-payload')
        details = '\n'.join(clean(step.get('name', ''), secret, 240) for step in steps[:100] if isinstance(step, dict) and step.get('conclusion') in ('failure', 'timed_out', 'cancelled'))
    if event == 'push': details = clean(data.get('ref', ''), secret, 240) + (' · deleted' if data.get('deleted') is True else '')
    body = clean(details, secret, 8000)
    title = clean(item.get('title') or item.get('name') or item.get('context') or event, secret, 240)
    action = clean(data.get('action', ''), secret, 80)
    return {'kind': 'webhook', 'event': event, 'action': action, 'title': event + (' · ' + action if action else '') + ' · ' + title,
            'url': url, 'sha': sha, 'binding': 'event-sha-needs-refresh' if sha else 'unbound',
            'actor': clean(object_value(data, 'sender').get('login', ''), secret, 100),
            'body': body, 'conclusion': clean(item.get('conclusion') or item.get('state', ''), secret, 80),
            'status': clean(item.get('status', ''), secret, 80), 'number': number, 'object_id': identifier,
            'attempt': positive(item.get('run_attempt')), 'path': clean(item.get('path', ''), secret, 300),
            'line': positive(item.get('line')), 'source': 'signed-webhook', 'publish_authorized': False}


def secret_for(directory, project):
    path = Path(directory) / (project + '.secret')
    # No symlink traversal; secrets never enter the event database or response.
    if any(p.is_symlink() for p in (path, *path.parents)): raise ValueError('secret-unavailable')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in (0o400, 0o600) or info.st_size > 257:
        raise ValueError('secret-unavailable')
    secret = path.read_text().strip()
    if not re.fullmatch('[A-Za-z0-9_-]{32,256}', secret): raise ValueError('secret-unavailable')
    return secret


class Queue:
    def __init__(self, root, pending_limit=10000, receipt_limit=100000):
        self.root = Path(root); self.file = self.root / 'queue.sqlite3'
        self.pending_limit = pending_limit; self.receipt_limit = receipt_limit
        if any(p.is_symlink() for p in (self.root, *self.root.parents, self.file)):
            raise ValueError('queue-unsafe')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700); self.root.chmod(0o700)
        with self.db() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1): raise ValueError('queue-schema-unknown')
            db.executescript('''CREATE TABLE IF NOT EXISTS receipts(delivery TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(delivery TEXT PRIMARY KEY REFERENCES receipts(delivery), project TEXT NOT NULL,
                    event TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL);
                PRAGMA user_version=1;''')
        self.file.chmod(0o600)

    @contextmanager
    def db(self):
        if any(p.is_symlink() for p in (self.file, *self.file.parents)): raise ValueError('queue-unsafe')
        db = sqlite3.connect(self.file, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def accept(self, delivery, fingerprint, project, event, payload):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT fingerprint FROM receipts WHERE delivery=?', (delivery,)).fetchone()
            if old:
                if not hmac.compare_digest(old['fingerprint'], fingerprint): raise ValueError('delivery-conflict')
                return False
            if db.execute('SELECT count(*) FROM events').fetchone()[0] >= self.pending_limit or db.execute('SELECT count(*) FROM receipts').fetchone()[0] >= self.receipt_limit:
                raise ValueError('queue-full')
            at = datetime.now(timezone.utc).isoformat()
            db.execute('INSERT INTO receipts VALUES(?,?,?)', (delivery, fingerprint, at))
            db.execute('INSERT INTO events VALUES(?,?,?,?,?)', (delivery, project, event, at, json.dumps(payload, ensure_ascii=False)))
        return True


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate-json-key')
        result[key] = value
    return result


def create_app(settings=None):
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=LIMIT, QUEUE=os.getenv('WEBHOOK_QUEUE', '/inbox'), SECRETS=os.getenv('WEBHOOK_SECRETS', '/secrets'))
    app.config.update(settings or {})
    queue = Queue(app.config['QUEUE']); app.extensions['queue'] = queue

    @app.get('/healthz')
    def health():
        try:
            with queue.db() as db: db.execute('SELECT 1 FROM receipts LIMIT 1')
        except (OSError, ValueError, sqlite3.DatabaseError): return jsonify(error='queue-unavailable'), 503
        return jsonify(status='ready')

    @app.post('/github/webhook/<project>')
    def receive(project):
        if project not in PROJECTS: return jsonify(error='unknown-project'), 404
        if request.args or request.mimetype != 'application/json': return jsonify(error='invalid-request'), 400
        try: secret = secret_for(app.config['SECRETS'], project)
        except (OSError, ValueError): return jsonify(error='secret-unavailable'), 503
        signature = request.headers.get('X-Hub-Signature-256', '')
        if not re.fullmatch(r'sha256=[a-f0-9]{64}', signature): return jsonify(error='invalid-signature'), 401
        raw = request.get_data(cache=False)
        if not hmac.compare_digest(signature, 'sha256=' + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()):
            return jsonify(error='invalid-signature'), 401
        delivery = request.headers.get('X-GitHub-Delivery', '')
        event = request.headers.get('X-GitHub-Event', '')
        if not GUID.fullmatch(delivery): return jsonify(error='invalid-delivery'), 400
        if event not in EVENTS: return jsonify(error='unsupported-event'), 422
        try:
            data = json.loads(raw.decode('utf-8'), object_pairs_hook=strict_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid-json')))
            payload = normalize(project, event, data, secret)
        except (ValueError, TypeError, RecursionError): return jsonify(error='invalid-payload'), 400
        fingerprint = hashlib.sha256(project.encode() + b'\0' + event.encode() + b'\0' + raw).hexdigest()
        try: inserted = queue.accept(delivery, fingerprint, project, event, payload)
        except ValueError as error:
            return jsonify(error=str(error)), 409 if str(error) == 'delivery-conflict' else 503
        except (OSError, sqlite3.DatabaseError): return jsonify(error='queue-unavailable'), 503
        return jsonify(accepted=True, duplicate=not inserted, delivery=delivery), 202 if inserted else 200

    @app.errorhandler(413)
    def oversized(_): return jsonify(error='payload-too-large', limit_bytes=LIMIT), 413
    return app
