"""Fixed stdin JSON agent for approved destination copies, never a shell endpoint."""
import fcntl
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from host_metrics import private_directory,read_json,atomic_json
from native_services import SystemdServices,validate_policy,default_policy
from sync_runtime import (SyncError,require,identifier,repository,backup_branch,safe_path,inspect_copy,pull_copy,
                          digest,github_auth)


@lru_cache(maxsize=1)
def agent_sha():
    root=Path(__file__).parent
    return hashlib.sha256(b''.join((root/name).read_bytes() for name in ('sync_agent.py','sync_runtime.py','native_services.py','host_metrics.py'))).hexdigest()


def validate_agent_policy(value):
    require(isinstance(value,dict) and set(value)=={'schema','roots','targets','credentials'} and value['schema']==1,'XD_SYNC_AGENT_POLICY_INVALID')
    require(isinstance(value['roots'],list) and 1<=len(value['roots'])<=20,'XD_SYNC_AGENT_POLICY_INVALID')
    for root in value['roots']:require(isinstance(root,str) and root.startswith('/') and '..' not in Path(root).parts,'XD_SYNC_AGENT_POLICY_INVALID')
    require(isinstance(value['targets'],dict) and len(value['targets'])<=100 and isinstance(value['credentials'],dict),'XD_SYNC_AGENT_POLICY_INVALID')
    for name,grant in value['targets'].items():
        identifier(name);require(isinstance(grant,dict) and set(grant)=={'directory','repository','backup_branch','credential','service'},'XD_SYNC_AGENT_POLICY_INVALID')
        safe_path(grant['directory'],value['roots']);repository(grant['repository']);backup_branch(grant['backup_branch'])
        require(grant['credential'] is None or grant['credential'] in value['credentials'],'XD_SYNC_AGENT_CREDENTIAL_UNAVAILABLE')
        if grant['service'] is not None:
            p=default_policy();p['services']['copy']=grant['service'];validate_policy(p)
            require(set(grant['service']['actions'])=={'status','restart'},'XD_SYNC_AGENT_SERVICE_INVALID')
    for alias,path in value['credentials'].items():
        identifier(alias);require(isinstance(path,str) and path.startswith('secrets/') and len(Path(path).parts)==2 and '..' not in path,'XD_SYNC_AGENT_POLICY_INVALID')
    return value


class CopyAgent:
    def __init__(self,policy_file,remote_resolver=None,services=None):
        self.file=Path(policy_file).absolute();self.root=private_directory(self.file.parent)
        self.policy=validate_agent_policy(read_json(self.file));self.remotes=remote_resolver;self.services=services or SystemdServices()
        self.database=self.root/'agent.sqlite3';require(not self.database.is_symlink(),'XD_SYNC_UNSAFE_DATABASE')
        if not self.database.exists():fd=os.open(self.database,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(fd)
        require(not self.database.stat().st_mode&0o077 and self.database.stat().st_size<=16*1024*1024,'XD_SYNC_UNSAFE_DATABASE')
        with sqlite3.connect(self.database) as db:db.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,input_sha TEXT NOT NULL,at REAL,state TEXT NOT NULL,result TEXT)')

    def inspect(self,name):
        identifier(name);require(name in self.policy['targets'],'XD_SYNC_AGENT_TARGET_NOT_GRANTED')
        grant=self.policy['targets'][name];snapshot=inspect_copy(grant,self.policy['roots'])
        return {'agent_sha':agent_sha(),'grant_sha':digest(grant),'observation':digest(snapshot),**snapshot,
                'directory':grant['directory'],'repository':grant['repository'],'backup_branch':grant['backup_branch'],
                'restart_available':grant['service'] is not None}

    def handle(self,body):
        require(isinstance(body,dict) and body.get('expected_agent_sha')==agent_sha(),'XD_SYNC_AGENT_VERSION_MISMATCH')
        require(body.get('operation') in ('hello','inspect','sync'),'XD_SYNC_AGENT_OPERATION_INVALID')
        if body['operation']=='hello':
            require(set(body)=={'operation','expected_agent_sha'},'XD_SYNC_AGENT_INPUT_INVALID')
            return {'agent_sha':agent_sha(),'targets':list(self.policy['targets'])}
        expected={'operation','expected_agent_sha','target'}
        if body['operation']=='sync':expected|={'sha','observation','grant_sha','request_id','restart'}
        require(set(body)==expected,'XD_SYNC_AGENT_INPUT_INVALID')
        name=body['target'];identifier(name)
        if body['operation']=='inspect':return self.inspect(name)
        require(isinstance(body['request_id'],str) and len(body['request_id'])==64 and all(c in '0123456789abcdef' for c in body['request_id']) and type(body['restart']) is bool,'XD_SYNC_AGENT_INPUT_INVALID')
        lock=os.open(self.root/('target-'+name+'.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise SyncError('XD_SYNC_AGENT_BUSY') from None
            self.policy=validate_agent_policy(read_json(self.file));snapshot=self.inspect(name);grant=self.policy['targets'][name]
            input_sha=digest(body)
            with sqlite3.connect(self.database) as db:
                old=db.execute('SELECT input_sha,result,state FROM runs WHERE id=?',(body['request_id'],)).fetchone()
                archive=self.root/'receipts'/(body['request_id']+'.json')
                if not old and archive.exists():
                    receipt=read_json(archive);require(receipt['input_sha']==input_sha,'XD_SYNC_IDEMPOTENCY_CONFLICT');return receipt['result']
                if old:
                    require(old[0]==input_sha,'XD_SYNC_IDEMPOTENCY_CONFLICT')
                    return json.loads(old[1]) if old[1] else {'state':'unconfirmed','error':'XD_SYNC_AGENT_PREVIOUS_OPERATION_UNCONFIRMED'}
                if db.execute('SELECT count(*) FROM runs').fetchone()[0]>=270:
                    directory=private_directory(self.root/'receipts')
                    require(len(list(directory.glob('*.json')))<10000,'XD_SYNC_ARCHIVE_CAPACITY')
                    for row in db.execute("SELECT id,input_sha,result FROM runs WHERE state='observed' ORDER BY at LIMIT 10").fetchall():
                        atomic_json(directory/(row[0]+'.json'),{'input_sha':row[1],'result':json.loads(row[2])})
                        db.execute('DELETE FROM runs WHERE id=?',(row[0],))
                require(db.execute('SELECT count(*) FROM runs').fetchone()[0]<300,'XD_SYNC_HISTORY_CAPACITY')
                require(snapshot['grant_sha']==body['grant_sha'] and snapshot['observation']==body['observation'],'XD_SYNC_TARGET_CHANGED')
                require(not body['restart'] or grant['service'] is not None,'XD_SYNC_AGENT_RESTART_NOT_GRANTED')
                db.execute('INSERT INTO runs VALUES(?,?,?,?,NULL)',(body['request_id'],input_sha,time.time(),'unconfirmed'))
            result={'state':'unconfirmed','sha':body['sha'],'restart':'not-requested'}
            try:
                service_before=self.services.status(grant['service']) if body['restart'] else None
                if body['restart']:require(service_before['definition_matches'] and not service_before['reload_required'],'XD_SYNC_SERVICE_DEFINITION_CHANGED')
                if grant['credential']:
                    file=self.root/self.policy['credentials'][grant['credential']]
                    require(not file.is_symlink() and file.is_file() and not file.stat().st_mode&0o077 and file.stat().st_size<=2048,'XD_SYNC_CREDENTIAL_UNAVAILABLE')
                    with github_auth(self.root,file.read_text().strip()) as env:
                        result.update(pull_copy(grant,self.policy['roots'],body['sha'],body['observation'],env,self.remotes))
                else:result.update(pull_copy(grant,self.policy['roots'],body['sha'],body['observation'],{},self.remotes))
                if body['restart'] and result['updated']:
                    require(digest(validate_agent_policy(read_json(self.file))['targets'][name])==body['grant_sha'],'XD_SYNC_AGENT_POLICY_CHANGED')
                    self.services.action(grant['service'],'restart',service_before['observation']);result['restart']='observed'
                elif body['restart']:result['restart']='unchanged-copy'
                final=inspect_copy(grant,self.policy['roots'])
                require(final['head']==body['sha'] and final['clean'],'XD_SYNC_TARGET_CHANGED')
                require(digest(validate_agent_policy(read_json(self.file))['targets'][name])==body['grant_sha'],'XD_SYNC_AGENT_POLICY_CHANGED')
                result['state']='observed'
            except (ValueError,OSError) as error:
                result['error']=str(error) if isinstance(error,SyncError) else 'XD_SYNC_AGENT_OPERATION_UNCONFIRMED'
            with sqlite3.connect(self.database) as db:db.execute('UPDATE runs SET state=?,result=? WHERE id=?',(result['state'],json.dumps(result),body['request_id']))
            return result
        finally:os.close(lock)
