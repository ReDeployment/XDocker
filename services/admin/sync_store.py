"""Private SQLite configuration, leases and bounded durable code-sync receipts."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time

from host_metrics import private_directory,read_json
from sync_runtime import require,SyncError

SCHEMA='''
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,uid TEXT NOT NULL,project TEXT,
 name TEXT NOT NULL,directory TEXT NOT NULL,repository TEXT NOT NULL,branch TEXT NOT NULL,
 credential TEXT,enabled INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,name TEXT NOT NULL,host TEXT NOT NULL,
 port INTEGER NOT NULL,username TEXT NOT NULL,identity TEXT NOT NULL,host_key TEXT NOT NULL,enabled INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS targets(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,source TEXT NOT NULL REFERENCES sources(id),
 device TEXT NOT NULL REFERENCES devices(id),remote_target TEXT NOT NULL,directory TEXT NOT NULL,restart INTEGER NOT NULL,enabled INTEGER NOT NULL,grant_sha TEXT);
CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,source TEXT NOT NULL REFERENCES sources(id),
 mode TEXT NOT NULL,hour INTEGER NOT NULL,minute INTEGER NOT NULL,timezone TEXT NOT NULL,enabled INTEGER NOT NULL,next_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,source TEXT NOT NULL,kind TEXT NOT NULL,at REAL NOT NULL,updated REAL NOT NULL,
 state TEXT NOT NULL,config_sha TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,sha TEXT,snapshot TEXT,result TEXT,error TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_running_source ON jobs(source) WHERE state IN ('queued','running','reconcile-queued');
CREATE TABLE IF NOT EXISTS archived_jobs(idempotency_key TEXT PRIMARY KEY,archive TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credentials(id TEXT PRIMARY KEY,name TEXT NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
'''
TABLES={'sources','devices','targets','schedules'}
JSON_COLUMNS={'snapshot','result'}
BOOL_COLUMNS={'enabled','restart'}


def row_dict(row):
    value=dict(row)
    for key in value:
        if key in JSON_COLUMNS and value[key] is not None:value[key]=json.loads(value[key])
        if key in BOOL_COLUMNS:value[key]=bool(value[key])
    return value


class SyncStore:
    def __init__(self,state):
        self.root=private_directory(Path(state).resolve()/'code-sync');self.path=self.root/'control.sqlite3'
        require(not self.path.is_symlink(),'XD_SYNC_UNSAFE_DATABASE')
        if not self.path.exists():fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(fd)
        require(not self.path.stat().st_mode&0o077,'XD_SYNC_UNSAFE_DATABASE')
        with self.db(write=True) as db:
            version=db.execute('PRAGMA user_version').fetchone()[0];require(version in (0,1,2),'XD_SYNC_SCHEMA_UNSUPPORTED')
            appid=db.execute('PRAGMA application_id').fetchone()[0]
            tables=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            require((version==0 and appid==0 and not tables) or (version in (1,2) and appid==0x5853594E),'XD_SYNC_SCHEMA_UNSUPPORTED')
            if version==1:
                # Preserve history; invalidate every old snapshot grant/job before switching modes.
                require(sqlite3.sqlite_version_info>=(3,35,0),'XD_SYNC_SQLITE_UPGRADE_REQUIRED')
                for column in ('backup_branch','includes','excludes'):db.execute('ALTER TABLE sources DROP COLUMN '+column)
                for table in ('sources','targets','schedules'):db.execute('UPDATE '+table+' SET enabled=0,revision=revision+1')
                db.execute('UPDATE targets SET grant_sha=NULL')
                db.execute("UPDATE jobs SET state=CASE WHEN sha IS NULL THEN 'failed' ELSE 'interrupted' END,error='XD_SYNC_POLICY_REVIEW_REQUIRED',updated=? WHERE state IN ('queued','running','reconcile-queued')",(time.time(),))
                db.execute("INSERT INTO metadata VALUES('mode-migration','committed-only-v2') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            for statement in SCHEMA.split(';'):
                if statement.strip():db.execute(statement)
            db.execute('PRAGMA user_version=2')
            db.execute('PRAGMA application_id=1481857358')

    @contextmanager
    def db(self,write=False):
        require(not self.path.is_symlink() and self.path.stat().st_size<=32*1024*1024,'XD_SYNC_UNSAFE_DATABASE')
        connection=sqlite3.connect(self.path,timeout=3,isolation_level=None);connection.row_factory=sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys=ON');connection.execute('PRAGMA max_page_count=8192')
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield connection;connection.execute('COMMIT')
        except sqlite3.Error:
            if connection.in_transaction:connection.execute('ROLLBACK')
            raise SyncError('XD_SYNC_DATABASE_UNAVAILABLE') from None
        except BaseException:
            if connection.in_transaction:connection.execute('ROLLBACK')
            raise
        finally:connection.close()

    def all(self,table):
        require(table in TABLES|{'jobs','credentials'},'XD_SYNC_TABLE_INVALID')
        with self.db() as db:
            order='at DESC' if table=='jobs' else 'id'
            return [row_dict(r) for r in db.execute('SELECT * FROM '+table+' ORDER BY '+order+' LIMIT 300')]

    def get(self,table,identifier):
        require(table in TABLES|{'jobs','credentials'},'XD_SYNC_TABLE_INVALID')
        with self.db() as db:row=db.execute('SELECT * FROM '+table+' WHERE id=?',(identifier,)).fetchone()
        return row_dict(row) if row else None

    def save(self,table,value):
        require(table in TABLES,'XD_SYNC_TABLE_INVALID')
        with self.db(write=True) as db:
            old=db.execute('SELECT revision FROM '+table+' WHERE id=?',(value['id'],)).fetchone()
            require(value['revision']==(old[0]+1 if old else 1),'XD_SYNC_REVISION_CONFLICT')
            require(old or db.execute('SELECT count(*) FROM '+table).fetchone()[0]<100,'XD_SYNC_CONFIGURATION_CAPACITY')
            fields=list(value);values=[json.dumps(value[k],ensure_ascii=False) if k in JSON_COLUMNS else int(value[k]) if k in BOOL_COLUMNS else value[k] for k in fields]
            if old:
                updates=[k for k in fields if k!='id']
                db.execute('UPDATE '+table+' SET '+','.join(k+'=?' for k in updates)+' WHERE id=?',[values[fields.index(k)] for k in updates]+[value['id']])
            else:db.execute('INSERT INTO '+table+' ('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',values)
        return value

    def queue(self,job):
        with self.db(write=True) as db:
            old=db.execute('SELECT * FROM jobs WHERE idempotency_key=?',(job['idempotency_key'],)).fetchone()
            archived=db.execute('SELECT archive FROM archived_jobs WHERE idempotency_key=?',(job['idempotency_key'],)).fetchone()
            if archived:
                receipt=read_json(self.root/'archives'/archived[0])
                old=next((row for row in receipt['jobs'] if row['idempotency_key']==job['idempotency_key']),None)
                require(old is not None,'XD_SYNC_ARCHIVE_INVALID')
            if old:
                old=row_dict(old);require((old['source'],old['kind'],old['config_sha'])==(job['source'],job['kind'],job['config_sha']),'XD_SYNC_IDEMPOTENCY_CONFLICT');return old
            require(db.execute('SELECT count(*) FROM jobs').fetchone()[0]<300,'XD_SYNC_HISTORY_CAPACITY')
            require(not db.execute("SELECT 1 FROM jobs WHERE source=? AND state IN ('queued','running','reconcile-queued','upload-unconfirmed','interrupted','partial')",(job['source'],)).fetchone(),'XD_SYNC_SOURCE_BUSY_OR_UNCONFIRMED')
            fields=list(job);db.execute('INSERT INTO jobs('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',list(job.values()))
        return job

    def patch_job(self,identifier,**changes):
        require(set(changes)<={'state','sha','snapshot','result','error'},'XD_SYNC_JOB_FIELD_INVALID')
        changes['updated']=time.time()
        with self.db(write=True) as db:
            fields=list(changes);values=[json.dumps(changes[k],ensure_ascii=False) if k in JSON_COLUMNS and changes[k] is not None else changes[k] for k in fields]
            db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',values+[identifier])

    def recover(self):
        with self.db(write=True) as db:
            db.execute("UPDATE jobs SET state=CASE WHEN sha IS NULL THEN 'failed' ELSE 'interrupted' END,error='XD_SYNC_INTERRUPTED',updated=? WHERE state IN ('queued','running','reconcile-queued')",(time.time(),))

    def request_reconcile(self,identifier):
        with self.db(write=True) as db:
            row=db.execute('SELECT * FROM jobs WHERE id=?',(identifier,)).fetchone()
            require(row is not None and row['state'] in ('interrupted','upload-unconfirmed') and row['sha'],'XD_SYNC_JOB_NOT_RECONCILABLE')
            db.execute("UPDATE jobs SET state='reconcile-queued',updated=? WHERE id=?",(time.time(),identifier))

    def metadata(self,key,value=None):
        with self.db(write=value is not None) as db:
            if value is not None:db.execute('INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))
            row=db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
            return row[0] if row else None

    def credential(self,identifier,name):
        with self.db(write=True) as db:db.execute('INSERT INTO credentials VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,updated=excluded.updated',(identifier,name,time.time()))

    def due(self,now):
        with self.db() as db:return [row_dict(r) for r in db.execute('SELECT * FROM schedules WHERE enabled=1 AND next_at<=? ORDER BY next_at LIMIT 10',(now,))]

    def advance(self,identifier,old,next_at):
        with self.db(write=True) as db:return db.execute('UPDATE schedules SET next_at=? WHERE id=? AND next_at=?',(next_at,identifier,old)).rowcount==1
