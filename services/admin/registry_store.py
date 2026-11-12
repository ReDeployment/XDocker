"""Typed SQLite registry and one-time, fail-closed migration of legacy private JSON."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from host_metrics import private_directory

APPLICATION_ID=0x58445247
LEGACY={'policy':'service-hub/policy.json','hub':'service-hub/projects.json',
        'monitors':'resources/apps.json','gateway':'service-hub/gateway/runtime.json',
        'gateway_error':'service-hub/gateway/last-error.json'}
SCHEMA='''
CREATE TABLE registry_meta(name TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE endpoints(alias TEXT PRIMARY KEY,scheme TEXT NOT NULL,host TEXT NOT NULL,port INTEGER NOT NULL,
 health_path TEXT NOT NULL,expected_status INTEGER NOT NULL);
CREATE TABLE service_grants(alias TEXT PRIMARY KEY,scope TEXT NOT NULL,unit TEXT NOT NULL,actions TEXT NOT NULL,definition_sha256 TEXT,
 UNIQUE(scope,unit));
CREATE TABLE certificates(alias TEXT PRIMARY KEY,certificate TEXT NOT NULL,key_path TEXT NOT NULL,domains TEXT NOT NULL);
CREATE TABLE gateway_settings(singleton INTEGER PRIMARY KEY CHECK(singleton=1),revision INTEGER NOT NULL,
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),listen TEXT NOT NULL,http_port INTEGER NOT NULL,https_port INTEGER NOT NULL,binary TEXT NOT NULL);
CREATE TABLE projects(id TEXT PRIMARY KEY,revision INTEGER NOT NULL CHECK(revision>0),name TEXT NOT NULL,
 endpoint TEXT NOT NULL,domain TEXT NOT NULL UNIQUE,service TEXT UNIQUE,monitor TEXT,delivery TEXT NOT NULL,tls TEXT,
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),directory TEXT);
CREATE TABLE monitor_apps(id TEXT PRIMARY KEY,revision INTEGER NOT NULL CHECK(revision>0),name TEXT NOT NULL,host_id TEXT NOT NULL);
CREATE TABLE monitor_bindings(app_id TEXT NOT NULL REFERENCES monitor_apps(id) ON DELETE CASCADE,
 target_id TEXT NOT NULL UNIQUE,PRIMARY KEY(app_id,target_id));
CREATE TABLE native_operations(id TEXT PRIMARY KEY,project TEXT NOT NULL,action TEXT NOT NULL,at REAL NOT NULL,
 state TEXT NOT NULL,policy_sha256 TEXT,idempotency_key TEXT UNIQUE,request_sha256 TEXT,result TEXT,error TEXT);
CREATE TABLE gateway_runtime(singleton INTEGER PRIMARY KEY CHECK(singleton=1),fingerprint TEXT NOT NULL,
 pid INTEGER NOT NULL,started INTEGER NOT NULL,at REAL NOT NULL);
CREATE TABLE gateway_errors(singleton INTEGER PRIMARY KEY CHECK(singleton=1),at REAL NOT NULL,fingerprint TEXT NOT NULL,
 rollback_verified INTEGER NOT NULL,runtime_unconfirmed INTEGER NOT NULL);
CREATE TABLE registry_migrations(source TEXT PRIMARY KEY,relative_path TEXT NOT NULL,sha256 TEXT,at REAL NOT NULL);
'''


class RegistryError(ValueError):pass


def require(ok,code):
    if not ok:raise RegistryError(code)


def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)


def source_bytes(path):
    if not path.exists() and not path.is_symlink():return None
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and not info.st_mode&0o077 and info.st_size<=4*1024*1024,'XD_REGISTRY_UNSAFE_LEGACY_FILE')
        raw=stream.read(4*1024*1024+1)
    require(len(raw)<=4*1024*1024,'XD_REGISTRY_LEGACY_CAPACITY')
    return raw


class RegistryStore:
    def __init__(self,state):
        # Existing Admin state roots predate private subdirectories and may be 0755.
        # Keep that contract; sensitive database, locks and backups remain private.
        require(not Path(state).is_symlink(),'XD_REGISTRY_UNSAFE_FILE')
        self.root=Path(state).resolve();self.root.mkdir(parents=True,mode=0o700,exist_ok=True)
        self.path=self.root/'registry.sqlite3'
        self.initialize()

    def check_file(self):
        require(not self.path.is_symlink(),'XD_REGISTRY_UNSAFE_FILE')
        if self.path.exists():
            info=self.path.stat()
            require(stat.S_ISREG(info.st_mode) and not info.st_mode&0o077 and info.st_size<=64*1024*1024,'XD_REGISTRY_UNSAFE_FILE')

    @contextmanager
    def connection(self,write=False):
        self.check_file()
        try:
            db=sqlite3.connect(self.path,timeout=3,isolation_level=None)
            db.row_factory=sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA busy_timeout=3000')
            db.execute('PRAGMA max_page_count=16384')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                yield db
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:db.execute('ROLLBACK')
                raise
            finally:db.close()
        except sqlite3.Error:raise RegistryError('XD_REGISTRY_DATABASE_UNAVAILABLE') from None

    def sources(self):
        values={}
        for key,relative in LEGACY.items():
            path=self.root/relative
            require(not any(p.is_symlink() for p in (path,*path.parents)),'XD_REGISTRY_UNSAFE_LEGACY_FILE')
            values[key]=source_bytes(path)
        return values

    def initialize(self):
        fd=os.open(self.root/'registry-migration.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        locks=[]
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            self.check_file()
            if not self.path.exists():
                created=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(created)
            with self.connection() as db:
                version=db.execute('PRAGMA user_version').fetchone()[0]
                appid=db.execute('PRAGMA application_id').fetchone()[0]
                tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                if version==1 and appid==APPLICATION_ID:
                    rows={r['source']:r['sha256'] for r in db.execute('SELECT source,sha256 FROM registry_migrations')}
                    require(set(rows)==set(LEGACY),'XD_REGISTRY_MIGRATION_RECEIPT_INVALID')
                    for key,raw in self.sources().items():
                        require(rows[key]==(hashlib.sha256(raw).hexdigest() if raw is not None else None),'XD_REGISTRY_LEGACY_SOURCE_CHANGED')
                    require(db.execute('PRAGMA quick_check').fetchone()[0]=='ok','XD_REGISTRY_INTEGRITY_FAILED')
                    return
                require(version==0 and appid==0 and not tables,'XD_REGISTRY_SCHEMA_UNSUPPORTED')
            # Old writers use these locks; stop the old Admin before upgrading this state.
            for relative in ('service-hub/hub.lock','resources/registry.lock'):
                path=self.root/relative
                if path.parent.exists():
                    require(not path.parent.is_symlink(),'XD_REGISTRY_UNSAFE_LEGACY_FILE')
                    lock=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);locks.append(lock)
                    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:raise RegistryError('XD_REGISTRY_LEGACY_WRITER_BUSY') from None
            original=self.sources();values={k:json.loads(v) if v is not None else None for k,v in original.items()}
            from native_services import validate_policy,default_policy,ServiceHub
            policy=validate_policy(default_policy() if original['policy'] is None else values['policy'])
            hub={'schema':1,'projects':{},'operations':[]} if original['hub'] is None else values['hub']
            require(isinstance(hub,dict) and set(hub)=={'schema','projects','operations'} and hub['schema']==1 and isinstance(hub['projects'],dict) and len(hub['projects'])<=100 and isinstance(hub['operations'],list) and len(hub['operations'])<=200,'XD_REGISTRY_LEGACY_INVALID')
            for key,row in hub['projects'].items():
                require(isinstance(row,dict) and row.get('id')==key,'XD_REGISTRY_LEGACY_INVALID');ServiceHub.validate_project(None,row)
            from resource_monitor import validate_monitor_registry
            monitors={'schema':1,'apps':{}} if original['monitors'] is None else values['monitors']
            validate_monitor_registry(monitors)
            # Immutable backups precede the transaction; failures preserve both old sources and receipts.
            backup=private_directory(self.root/'registry-migration')
            for key,raw in original.items():
                if raw is None:continue
                digest=hashlib.sha256(raw).hexdigest();target=backup/(digest+'.json')
                if target.exists():require(source_bytes(target)==raw,'XD_REGISTRY_BACKUP_CONFLICT')
                else:
                    output=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
                    with os.fdopen(output,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            directory=os.open(backup,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
            with self.connection(write=True) as db:
                for statement in SCHEMA.split(';'):
                    if statement.strip():db.execute(statement)
                db.execute(f'PRAGMA application_id={APPLICATION_ID}');db.execute('PRAGMA user_version=1')
                db.execute("INSERT INTO registry_meta VALUES('schema','1')")
                self._save_policy(db,policy);self._save_hub(db,hub);self._save_monitors(db,monitors)
                if original['gateway'] is not None:self._save_gateway(db,values['gateway'])
                if original['gateway_error'] is not None:self._save_gateway_error(db,values['gateway_error'])
                require(original==self.sources(),'XD_REGISTRY_LEGACY_SOURCE_CHANGED')
                db.executemany('INSERT INTO registry_migrations VALUES(?,?,?,?)',[(k,LEGACY[k],hashlib.sha256(v).hexdigest() if v is not None else None,time.time()) for k,v in original.items()])
        finally:
            for lock in locks:os.close(lock)
            os.close(fd)

    def _save_policy(self,db,value):
        for table in ('endpoints','service_grants','certificates','gateway_settings'):db.execute('DELETE FROM '+table)
        db.executemany('INSERT INTO endpoints VALUES(?,?,?,?,?,?)',[(k,v['scheme'],v['host'],v['port'],v['health_path'],v['expected_status']) for k,v in value['endpoints'].items()])
        db.executemany('INSERT INTO service_grants VALUES(?,?,?,?,?)',[(k,v['scope'],v['unit'],encoded(v['actions']),v['definition_sha256']) for k,v in value['services'].items()])
        db.executemany('INSERT INTO certificates VALUES(?,?,?,?)',[(k,v['certificate'],v['key'],encoded(v['domains'])) for k,v in value['certificates'].items()])
        g=value['gateway'];db.execute('INSERT INTO gateway_settings VALUES(1,?,?,?,?,?,?)',(value['revision'],int(g['enabled']),g['listen'],g['http_port'],g['https_port'],g['binary']))

    def read_policy(self):
        with self.connection() as db:
            row=db.execute('SELECT * FROM gateway_settings WHERE singleton=1').fetchone()
            require(row is not None,'XD_REGISTRY_POLICY_MISSING')
            endpoints={r['alias']:{k:r[k] for k in ('scheme','host','port','health_path','expected_status')} for r in db.execute('SELECT * FROM endpoints ORDER BY alias')}
            services={r['alias']:{'scope':r['scope'],'unit':r['unit'],'actions':json.loads(r['actions']),'definition_sha256':r['definition_sha256']} for r in db.execute('SELECT * FROM service_grants ORDER BY alias')}
            certs={r['alias']:{'certificate':r['certificate'],'key':r['key_path'],'domains':json.loads(r['domains'])} for r in db.execute('SELECT * FROM certificates ORDER BY alias')}
            return {'schema':1,'revision':row['revision'],'endpoints':endpoints,'services':services,'certificates':certs,
                    'gateway':{'enabled':bool(row['enabled']),**{k:row[k] for k in ('listen','http_port','https_port','binary')}}}

    def write_policy(self,value):
        with self.connection(write=True) as db:
            current=db.execute('SELECT revision FROM gateway_settings WHERE singleton=1').fetchone()[0]
            require(value['revision']==current+1,'XD_SERVICE_POLICY_REVISION_CONFLICT')
            self._save_policy(db,value)

    def _save_hub(self,db,data):
        db.execute('DELETE FROM projects');db.execute('DELETE FROM native_operations')
        db.executemany('INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?,?)',[(r['id'],r['revision'],r['name'],r['endpoint'],r['domain'],r['service'],r['monitor'],r['delivery'],r['tls'],int(r['enabled']),r.get('directory')) for r in data['projects'].values()])
        for r in data['operations']:
            db.execute('INSERT INTO native_operations VALUES(?,?,?,?,?,?,?,?,?,?)',(r['id'],r['project'],r['action'],r['at'],r['state'],r.get('policy_sha256'),r.get('idempotency_key'),r.get('request_sha256'),encoded(r['result']) if 'result' in r else None,r.get('error')))

    def read_hub(self):
        with self.connection() as db:
            projects={}
            for row in db.execute('SELECT * FROM projects ORDER BY id'):
                value=dict(row);value['enabled']=bool(value['enabled']);projects[value['id']]=value
            operations=[]
            for row in db.execute('SELECT * FROM native_operations ORDER BY at,id'):
                value={k:v for k,v in dict(row).items() if v is not None}
                if 'result' in value:value['result']=json.loads(value['result'])
                operations.append(value)
            return {'schema':1,'projects':projects,'operations':operations}

    def write_hub(self,data):
        with self.connection(write=True) as db:self._save_hub(db,data)

    def _save_monitors(self,db,data):
        db.execute('DELETE FROM monitor_bindings');db.execute('DELETE FROM monitor_apps')
        for r in data['apps'].values():
            db.execute('INSERT INTO monitor_apps VALUES(?,?,?,?)',(r['id'],r['revision'],r['name'],r['host_id']))
            db.executemany('INSERT INTO monitor_bindings VALUES(?,?)',[(r['id'],v) for v in r['targets']])

    def read_monitors(self):
        with self.connection() as db:
            apps={r['id']:{**dict(r),'targets':[]} for r in db.execute('SELECT * FROM monitor_apps ORDER BY id')}
            for r in db.execute('SELECT app_id,target_id FROM monitor_bindings ORDER BY target_id'):apps[r['app_id']]['targets'].append(r['target_id'])
            return {'schema':1,'apps':apps}

    def write_monitors(self,data):
        with self.connection(write=True) as db:self._save_monitors(db,data)

    def _save_gateway(self,db,value):
        require(set(value)=={'fingerprint','owner','at'} and isinstance(value['owner'],dict),'XD_REGISTRY_GATEWAY_INVALID')
        db.execute('INSERT OR REPLACE INTO gateway_runtime VALUES(1,?,?,?,?)',(value['fingerprint'],value['owner']['pid'],value['owner']['started'],value['at']))

    def read_gateway(self):
        with self.connection() as db:
            row=db.execute('SELECT * FROM gateway_runtime WHERE singleton=1').fetchone()
            return {'fingerprint':row['fingerprint'],'owner':{'pid':row['pid'],'started':row['started']},'at':row['at']} if row else None

    def write_gateway(self,value):
        with self.connection(write=True) as db:self._save_gateway(db,value)

    def _save_gateway_error(self,db,value):
        require(set(value)=={'at','fingerprint','rollback_verified','runtime_unconfirmed'},'XD_REGISTRY_GATEWAY_INVALID')
        db.execute('INSERT OR REPLACE INTO gateway_errors VALUES(1,?,?,?,?)',(value['at'],value['fingerprint'],int(value['rollback_verified']),int(value['runtime_unconfirmed'])))

    def read_gateway_error(self):
        with self.connection() as db:
            row=db.execute('SELECT * FROM gateway_errors WHERE singleton=1').fetchone()
            return {'at':row['at'],'fingerprint':row['fingerprint'],'rollback_verified':bool(row['rollback_verified']),'runtime_unconfirmed':bool(row['runtime_unconfirmed'])} if row else None

    def write_gateway_error(self,value):
        with self.connection(write=True) as db:self._save_gateway_error(db,value)

    def backup(self,destination):
        destination=Path(destination).absolute();private_directory(destination.parent)
        require(not destination.exists() and not destination.is_symlink(),'XD_REGISTRY_BACKUP_EXISTS')
        fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(fd)
        with self.connection() as source:
            with sqlite3.connect(destination) as target:source.backup(target)
        return {'file':str(destination),'sha256':hashlib.sha256(destination.read_bytes()).hexdigest()}
