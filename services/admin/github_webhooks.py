"""Import the isolated signed-event queue; never reads webhook secrets or calls GitHub."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading

from github_watch import PROJECTS, WatchError, safe_text, stamp

FIELDS = {'kind', 'event', 'action', 'title', 'url', 'sha', 'binding', 'actor', 'body', 'conclusion',
          'status', 'number', 'object_id', 'attempt', 'path', 'line', 'source', 'publish_authorized'}
EVENTS = {'ping', 'push', 'pull_request', 'pull_request_review', 'issue_comment',
          'pull_request_review_comment', 'commit_comment', 'workflow_run', 'workflow_job',
          'check_run', 'check_suite', 'status'}
GUID = re.compile(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}')


class WebhookInbox:
    def __init__(self, store, directory, enabled=False, interval=5):
        self.store = store; self.file = Path(directory) / 'queue.sqlite3'
        self.enabled = enabled; self.interval = interval; self.error = None
        self.stop_event = threading.Event(); self.worker = None; self.last_import = None
        if store:
            # Additive, owned by the observer, never touches administration or business databases.
            with store.db() as db:
                db.execute('''CREATE TABLE IF NOT EXISTS hook_receipts(delivery TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL, project TEXT NOT NULL, at TEXT NOT NULL, notification INTEGER NOT NULL)''')

    @contextmanager
    def queue(self, write=False):
        if any(p.is_symlink() for p in (self.file, *self.file.parents)) or not self.file.is_file():
            raise WatchError('webhook-queue-unavailable')
        db = sqlite3.connect(self.file.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            if db.execute('PRAGMA user_version').fetchone()[0] != 1: raise WatchError('webhook-queue-schema-unknown')
            with db: yield db
        finally: db.close()

    def status(self):
        result = {'auto_import_enabled': self.enabled, 'consumer_running': bool(self.worker and self.worker.is_alive()),
                  'last_import_at': self.last_import, 'error': self.error, 'ready': False,
                  'pending': None, 'receipts': None, 'receipt_limit': 100000, 'last_received_at': None, 'github_writes': False}
        try:
            with self.queue() as db:
                result.update(ready=True, pending=db.execute('SELECT count(*) FROM events').fetchone()[0],
                              last_received_at=db.execute('SELECT max(at) FROM receipts').fetchone()[0],
                              receipts=db.execute('SELECT count(*) FROM receipts').fetchone()[0])
                if result['pending']>=10000 or result['receipts']>=100000: result['error']=result['error'] or 'webhook-queue-full'
        except (WatchError, OSError, sqlite3.DatabaseError): result['error'] = self.error or 'webhook-queue-unavailable'
        return result

    def validate(self, row):
        if not GUID.fullmatch(row['delivery']) or row['project'] not in PROJECTS or row['event'] not in EVENTS:
            raise WatchError('webhook-record-invalid')
        if not re.fullmatch('[a-f0-9]{64}', row['fingerprint']) or len(row['payload'].encode()) > 65536:
            raise WatchError('webhook-record-invalid')
        payload = json.loads(row['payload'])
        if not isinstance(payload, dict) or set(payload) != FIELDS or payload['kind'] != 'webhook' or payload['event'] != row['event'] or payload['source'] != 'signed-webhook' or payload['publish_authorized'] is not False:
            raise WatchError('webhook-record-invalid')
        if payload['sha'] is not None and (not isinstance(payload['sha'], str) or not re.fullmatch('[a-f0-9]{40}', payload['sha'])):
            raise WatchError('webhook-record-invalid')
        for key in ('number', 'object_id', 'attempt', 'line'):
            if payload[key] is not None and (type(payload[key]) is not int or not 0 < payload[key] < 2**63): raise WatchError('webhook-record-invalid')
        base = 'https://github.com/' + PROJECTS[row['project']]
        if not isinstance(payload['url'], str) or not (payload['url'] == base or payload['url'].startswith(base + '/')):
            raise WatchError('webhook-record-invalid')
        if not isinstance(row['at'], str) or len(row['at']) > 80: raise WatchError('webhook-record-invalid')
        for key in ('action', 'title', 'binding', 'actor', 'body', 'conclusion', 'status', 'path'):
            if not isinstance(payload[key], str): raise WatchError('webhook-record-invalid')
            payload[key] = safe_text(payload[key], limit=8000 if key == 'body' else 2000)
        payload.update(delivery=row['delivery'], received_at=row['at'], binding='event-sha-needs-refresh' if payload['sha'] else 'unbound')
        return payload

    def import_one(self, row):
        payload = self.validate(row)
        # Hash the normalized record too. Changing queued content under the same delivery must fail closed.
        fingerprint = hashlib.sha256((row['fingerprint'] + '\0' + row['project'] + '\0' + row['event'] + '\0' + row['at'] + '\0' + row['payload']).encode()).hexdigest()
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT fingerprint FROM hook_receipts WHERE delivery=?', (row['delivery'],)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint: raise WatchError('webhook-import-conflict')
                return False
            db.execute('DELETE FROM notifications WHERE read_at IS NOT NULL AND id NOT IN (SELECT id FROM notifications ORDER BY id DESC LIMIT 2000)')
            if db.execute('SELECT count(*) FROM notifications').fetchone()[0] >= 10000 or db.execute('SELECT count(*) FROM hook_receipts').fetchone()[0] >= 100000:
                raise WatchError('webhook-inbox-full')
            cursor = db.execute('INSERT INTO notifications(project,kind,at,payload) VALUES(?,?,?,?)',
                                (row['project'], 'webhook', row['at'], json.dumps(payload, ensure_ascii=False)))
            db.execute('INSERT INTO hook_receipts VALUES(?,?,?,?,?)', (row['delivery'], fingerprint, row['project'], stamp(), cursor.lastrowid))
            if row['event'] != 'ping':
                db.execute("UPDATE projects SET status='webhook-needs-refresh',revision=? WHERE id=?", (hashlib.sha256((row['delivery']+row['fingerprint']).encode()).hexdigest(), row['project']))
        return True

    def consume(self):
        if not self.store: raise WatchError('watch-store-unavailable')
        imported = 0; acknowledged = 0
        try:
            with self.queue() as db:
                rows = db.execute('SELECT e.*,r.fingerprint FROM events e JOIN receipts r USING(delivery) ORDER BY e.at,e.delivery LIMIT 50').fetchall()
            for row in rows:
                inserted = self.import_one(row)  # Commit notification BEFORE deleting queue payload.
                with self.queue(write=True) as db:
                    acknowledged += db.execute('DELETE FROM events WHERE delivery=? AND payload=?', (row['delivery'], row['payload'])).rowcount
                imported += int(inserted)
            self.last_import = stamp(); self.error = None
            return {'imported': imported, 'acknowledged': acknowledged, 'github_writes': False}
        except (WatchError, OSError, sqlite3.DatabaseError, ValueError, TypeError, KeyError) as error:
            self.error = str(error) if isinstance(error, WatchError) else 'webhook-import-unconfirmed'
            raise WatchError(self.error) from None

    def start(self):
        if not self.enabled or not self.store or self.worker: return
        def loop():
            while not self.stop_event.wait(self.interval):
                try: self.consume()
                except WatchError: continue
        self.worker = threading.Thread(target=loop, daemon=True); self.worker.start()

    def close(self): self.stop_event.set()
