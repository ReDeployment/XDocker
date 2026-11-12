"""Project code backup, approved SSH copies, durable jobs and daily schedules."""
from datetime import datetime,timedelta
import base64
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import threading
import time
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError

from host_metrics import private_directory,read_json,atomic_json
from registry_store import RegistryStore
from sync_store import SyncStore
from sync_runtime import (SyncError,require,identifier,repository,backup_branch,safe_path,scan_source,canonical,
                          text_git,source_branch,GitTransfer,github_auth,digest,run,SHA,DENY_PARTS,DENY_SUFFIXES,RUNTIME_ROOTS)
from sync_agent import agent_sha


def default_policy():
    return {'schema':1,'execution':False,'source_roots':[],'identities':{},
            'agent_path':'/data/workspace/XDocker/scripts/code-sync-agent.py',
            'agent_policy':'/data/workspace/XDocker/data/code-sync-agent/policy.json'}


def validate_policy(value):
    require(isinstance(value,dict) and set(value)==set(default_policy()) and value['schema']==1 and type(value['execution']) is bool,'XD_SYNC_POLICY_INVALID')
    require(isinstance(value['source_roots'],list) and len(value['source_roots'])<=20 and isinstance(value['identities'],dict) and len(value['identities'])<=20,'XD_SYNC_POLICY_INVALID')
    for path in [*value['source_roots'],value['agent_path'],value['agent_policy'],*value['identities'].values()]:
        require(isinstance(path,str) and path.startswith('/') and '..' not in Path(path).parts and not any(c in path for c in '\n\r\0'),'XD_SYNC_POLICY_INVALID')
    for alias in value['identities']:identifier(alias)
    return value


def next_daily(hour,minute,timezone,now=None):
    try:zone=ZoneInfo(timezone)
    except (ValueError,ZoneInfoNotFoundError):raise SyncError('XD_SYNC_TIMEZONE_INVALID') from None
    current=datetime.fromtimestamp(time.time() if now is None else now,zone)
    at=current.replace(hour=hour,minute=minute,second=0,microsecond=0)
    if at<=current:at+=timedelta(days=1)
    return at.timestamp()


class CodeSync:
    def __init__(self,state,operations=False,transport=None,transfer=None):
        self.state=Path(state).resolve();self.store=SyncStore(state);self.root=self.store.root
        self.operations=operations;self.registry=RegistryStore(state);self._inventory=None
        self.credentials=private_directory(self.root/'secrets');self.transient=private_directory(self.root/'transient')
        self.transfer=transfer or GitTransfer(private_directory(self.root/'repositories'))
        self.transport=transport or self.ssh
        if not (self.root/'policy.json').exists():atomic_json(self.root/'policy.json',default_policy())

    def policy(self):return validate_policy(read_json(self.root/'policy.json'))

    def import_policy(self,value):
        validate_policy(value);atomic_json(self.root/'policy.json',value);self._inventory=None
        return {'execution':value['execution'],'source_roots':value['source_roots'],'identities':list(value['identities'])}

    def inventory(self,refresh=False):
        if self._inventory and not refresh and time.monotonic()-self._inventory[0]<20:return self._inventory[1]
        registered=self.registry.read_hub()['projects'];by_path={p['directory']:p for p in registered.values() if p.get('directory')}
        found=[];visited=0
        for root in self.policy()['source_roots']:
            path=Path(root)
            if not path.is_dir() or path.is_symlink():continue
            for directory,dirs,files in os.walk(path,followlinks=False):
                visited+=1
                if visited>1500 or len(found)>=64:break
                p=Path(directory);depth=len(p.relative_to(path).parts)
                dirs[:]=[d for d in dirs if d not in DENY_PARTS and not d.startswith('.') and 'autobackup' not in d.lower() and not (p/d).is_symlink()]
                git_dir=p/'.git'
                if git_dir.is_dir() or git_dir.is_file():
                    dirs[:]=[]
                    if git_dir.is_file() and str(p) not in by_path:continue
                    try:
                        repo=canonical(text_git(p,'remote','get-url','origin'));branch=source_branch(p)
                        project=by_path.get(str(p))
                        found.append({'directory':str(p),'repository':repo,'branch':branch,'name':project['name'] if project else p.name,'project':project['id'] if project else None})
                    except (SyncError,OSError,UnicodeError):continue
                if depth>=6:dirs[:]=[]
        # A temporary clone can have its own .git directory; deduplicate by origin,
        # preferring registered primaries, then the shortest deterministic path.
        unique={}
        for item in sorted(found,key=lambda row:(not bool(row['project']),len(row['directory']),row['directory'])):
            unique.setdefault(item['repository'].lower(),item)
        found=sorted(unique.values(),key=lambda row:row['directory'])
        self._inventory=(time.monotonic(),found);return found

    def listing(self):
        policy=self.policy()
        with self.store.db() as db:heartbeat=db.execute("SELECT value FROM metadata WHERE key='worker' ").fetchone()
        value={table:self.store.all(table) for table in ('sources','devices','targets','schedules','jobs','credentials')}
        for row in value['schedules']:row['last_error']=self.store.metadata('schedule:'+row['id'])
        return {**value,
                'archives':[p.name for p in sorted((self.root/'archives').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:1000]],
                'inventory':self.inventory(),'projects':list(self.registry.read_hub()['projects'].values()),
                'policy':{'execution':policy['execution'],'source_roots':policy['source_roots'],'identities':list(policy['identities']),
                          'agent_path':policy['agent_path'],'agent_policy':policy['agent_policy'],'agent_sha':agent_sha()},
                'archive_error':self.store.metadata('archive-error'),
                'worker':{'connected':bool(heartbeat and time.time()-float(heartbeat[0])<45)},
                'execution_enabled':bool(self.operations and policy['execution']),'configuration_writable':True,
                'builtin_excludes':sorted(DENY_PARTS)+[v+'/' for v in sorted(RUNTIME_ROOTS)]+list(DENY_SUFFIXES)+['.env*'],'backup_namespace':'xdocker-backup/'}

    def save_credential(self,body):
        require(isinstance(body,dict) and set(body)=={'id','name','token'},'XD_SYNC_INPUT_INVALID');identifier(body['id'])
        require(isinstance(body['name'],str) and 1<=len(body['name'])<=80,'XD_SYNC_NAME_INVALID')
        token=body['token'];require(isinstance(token,str) and 16<=len(token)<=2048 and re.fullmatch(r'[A-Za-z0-9_]+',token),'XD_SYNC_CREDENTIAL_INVALID')
        path=self.credentials/body['id'];require(not path.is_symlink(),'XD_SYNC_UNSAFE_CREDENTIAL')
        fd,name=__import__('tempfile').mkstemp(dir=self.credentials,prefix='.token-')
        try:
            with os.fdopen(fd,'w') as stream:stream.write(token);stream.flush();os.fsync(stream.fileno())
            os.replace(name,path)
        finally:
            if os.path.exists(name):os.unlink(name)
        self.store.credential(body['id'],body['name']);return {'id':body['id'],'name':body['name'],'configured':True}

    def secret(self,alias):
        require(isinstance(alias,str),'XD_SYNC_CREDENTIAL_REQUIRED');identifier(alias);p=self.credentials/alias
        require(p.is_file() and not p.is_symlink() and not p.stat().st_mode&0o077 and p.stat().st_size<=2048,'XD_SYNC_CREDENTIAL_REQUIRED')
        return p.read_text().strip()

    def save(self,kind,body):
        require(kind in ('sources','devices','targets','schedules') and isinstance(body,dict),'XD_SYNC_INPUT_INVALID')
        value=dict(body);confirmation=value.pop('confirmation',None)
        fields={'sources':{'id','revision','project','name','directory','repository','branch','backup_branch','includes','excludes','credential','enabled'},
                'devices':{'id','revision','name','host','port','username','identity','host_key','enabled'},
                'targets':{'id','revision','source','device','remote_target','directory','restart','enabled'},
                'schedules':{'id','revision','source','mode','hour','minute','timezone','enabled'}}[kind]
        require(set(value)==fields,'XD_SYNC_INPUT_INVALID');identifier(value['id'])
        require(type(value['revision']) is int and value['revision']>=1 and type(value['enabled']) is bool,'XD_SYNC_INPUT_INVALID')
        if kind in ('sources','devices'):require(isinstance(value['name'],str) and 1<=len(value['name'])<=80 and all(c.isprintable() for c in value['name']),'XD_SYNC_NAME_INVALID')
        if kind=='sources':
            safe_path(value['directory'],self.policy()['source_roots']);repository(value['repository']);backup_branch(value['backup_branch'])
            require(any(row['directory']==value['directory'] and row['repository'].lower()==value['repository'].lower() and row['branch']==value['branch'] for row in self.inventory(True)),'XD_SYNC_SOURCE_NOT_DISCOVERED')
            require(value['project'] is None or value['project'] in self.registry.read_hub()['projects'],'XD_SYNC_PROJECT_UNAVAILABLE')
            for key in ('includes','excludes'):
                require(isinstance(value[key],list) and 1<=len(value[key])<=30 if key=='includes' else isinstance(value[key],list) and len(value[key])<=30,'XD_SYNC_SELECTION_INVALID')
                for item in value[key]:require(isinstance(item,str) and 1<=len(item)<=200 and not item.startswith('/') and '..' not in Path(item).parts and not any(c in item for c in '\n\r\0'),'XD_SYNC_SELECTION_INVALID')
            require(value['credential'] is None or self.store.get('credentials',value['credential']) is not None,'XD_SYNC_CREDENTIAL_REQUIRED')
            if value['enabled']:require(value['credential'] is not None and confirmation==value['name'],'XD_SYNC_CONFIRMATION_REQUIRED')
            old=self.store.get(kind,value['id']);value['uid']=old['uid'] if old else secrets.token_hex(16)
        elif kind=='devices':
            try:address=ipaddress.ip_address(value['host'])
            except (ValueError,TypeError):raise SyncError('XD_SYNC_PRIVATE_DEVICE_ADDRESS_REQUIRED') from None
            require((address.is_private or address in ipaddress.ip_network('100.64.0.0/10')) and not address.is_loopback and not address.is_link_local and not address.is_unspecified and not address.is_multicast,'XD_SYNC_PRIVATE_DEVICE_ADDRESS_REQUIRED')
            require(type(value['port']) is int and 1<=value['port']<=65535 and isinstance(value['username'],str) and re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',value['username']),'XD_SYNC_DEVICE_INVALID')
            require(value['identity'] in self.policy()['identities'],'XD_SYNC_IDENTITY_NOT_GRANTED')
            require(isinstance(value['host_key'],str) and re.fullmatch(r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]{40,2048}',value['host_key']),'XD_SYNC_HOST_KEY_REQUIRED')
            try:base64.b64decode(value['host_key'].split()[1],validate=True)
            except ValueError:raise SyncError('XD_SYNC_HOST_KEY_REQUIRED') from None
        elif kind=='targets':
            source=self.source(value['source']);device=self.store.get('devices',value['device']);require(device is not None,'XD_SYNC_DEVICE_UNAVAILABLE');identifier(value['remote_target'])
            require(isinstance(value['directory'],str) and value['directory'].startswith('/') and '..' not in Path(value['directory']).parts and not any(c in value['directory'] for c in '\r\n\0') and type(value['restart']) is bool,'XD_SYNC_TARGET_INVALID')
            if value['enabled']:
                require(confirmation==source['name'],'XD_SYNC_CONFIRMATION_REQUIRED');value['grant_sha']=self.inspect_target(value)['grant_sha']
            else:value['grant_sha']=None
        else:
            source=self.source(value['source']);require(value['mode'] in ('upload','distribute'),'XD_SYNC_SCHEDULE_INVALID')
            require(type(value['hour']) is int and 0<=value['hour']<=23 and type(value['minute']) is int and 0<=value['minute']<=59,'XD_SYNC_SCHEDULE_INVALID')
            require(isinstance(value['timezone'],str) and len(value['timezone'])<=80,'XD_SYNC_SCHEDULE_INVALID')
            value['next_at']=next_daily(value['hour'],value['minute'],value['timezone'])
            if value['enabled']:require(source['enabled'] and confirmation==source['name'],'XD_SYNC_CONFIRMATION_REQUIRED')
        return self.store.save(kind,value)

    def source(self,identifier):
        value=self.store.get('sources',identifier);require(value is not None,'XD_SYNC_SOURCE_NOT_FOUND');return value

    def preview(self,name):return scan_source(self.source(name),self.policy()['source_roots'])

    def configuration(self,name):
        source=self.source(name);targets=[t for t in self.store.all('targets') if t['source']==name and t['enabled']]
        devices=[self.store.get('devices',t['device']) for t in targets]
        return digest({'source':source,'targets':targets,'devices':devices,'policy':self.policy(),'credentials':self.store.all('credentials')})

    def enqueue(self,body):
        require(isinstance(body,dict) and set(body)=={'source','kind','confirmation','idempotency_key'},'XD_SYNC_INPUT_INVALID')
        source=self.source(body['source']);require(source['enabled'] and self.operations and self.policy()['execution'],'XD_SYNC_EXECUTION_DISABLED')
        require(body['confirmation']==source['name'] and body['kind'] in ('upload','distribute'),'XD_SYNC_CONFIRMATION_REQUIRED')
        require(isinstance(body['idempotency_key'],str) and re.fullmatch(r'[a-f0-9-]{32,80}',body['idempotency_key']),'XD_SYNC_IDEMPOTENCY_INVALID')
        now=time.time();job={'id':secrets.token_hex(16),'source':source['id'],'kind':body['kind'],'at':now,'updated':now,'state':'queued','config_sha':self.configuration(source['id']),'idempotency_key':body['idempotency_key']}
        return self.store.queue(job)

    def ssh(self,device,body):
        policy=self.policy();require(device['enabled'],'XD_SYNC_DEVICE_DISABLED')
        key=Path(policy['identities'].get(device['identity'],''));require(key.is_file() and not key.is_symlink() and not key.stat().st_mode&0o077,'XD_SYNC_IDENTITY_NOT_GRANTED')
        known=self.root/('known-host-'+device['id']);require(not known.is_symlink(),'XD_SYNC_UNSAFE_HOST_KEY')
        alias='xdocker-sync-'+device['id'];known.write_text(alias+' '+device['host_key']+'\n');known.chmod(0o600)
        command=['ssh','-F','/dev/null','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','CheckHostIP=no','-o','HostKeyAlias='+alias,
                 '-o','UserKnownHostsFile='+str(known),'-o','GlobalKnownHostsFile=/dev/null','-o','IdentitiesOnly=yes','-o','IdentityAgent=none',
                 '-o','ConnectTimeout=8','-o','ServerAliveInterval=10','-o','ServerAliveCountMax=3','-i',str(key),'-p',str(device['port']),'-l',device['username'],
                 '--',device['host'],shlex.join(['/usr/bin/python3',policy['agent_path'],'--policy',policy['agent_policy']])]
        raw=run(command,input=json.dumps({**body,'expected_agent_sha':agent_sha()}).encode(),timeout=120,limit=65536,allowed=(0,1))[1]
        try:value=json.loads(raw)
        except ValueError:raise SyncError('XD_SYNC_AGENT_RESPONSE_INVALID') from None
        if isinstance(value,dict) and isinstance(value.get('error'),str) and re.fullmatch(r'XD_SYNC_[A-Z0-9_]{1,100}',value['error']):raise SyncError(value['error'])
        require(isinstance(value,dict) and 'result' in value and isinstance(value['result'],dict),'XD_SYNC_AGENT_RESPONSE_INVALID')
        return value['result']

    def check_device(self,name):
        device=self.store.get('devices',name);require(device is not None,'XD_SYNC_DEVICE_UNAVAILABLE')
        result=self.transport(device,{'operation':'hello'});require(result.get('agent_sha')==agent_sha(),'XD_SYNC_AGENT_VERSION_MISMATCH');return result

    def inspect_target(self,target):
        source=self.source(target['source']);device=self.store.get('devices',target['device']);require(device is not None,'XD_SYNC_DEVICE_UNAVAILABLE')
        result=self.transport(device,{'operation':'inspect','target':target['remote_target']})
        require(result.get('agent_sha')==agent_sha(),'XD_SYNC_AGENT_VERSION_MISMATCH')
        require(not target.get('grant_sha') or result.get('grant_sha')==target['grant_sha'],'XD_SYNC_TARGET_GRANT_CHANGED')
        require((result.get('repository','').lower(),result.get('backup_branch'),result.get('directory'))==(source['repository'].lower(),source['backup_branch'],target['directory']),'XD_SYNC_TARGET_GRANT_MISMATCH')
        require(result.get('clean') and (not target['restart'] or result.get('restart_available')),'XD_SYNC_TARGET_DIRTY_OR_RESTART_NOT_GRANTED')
        return result

    def execute(self,job):
        source=self.source(job['source']);require(self.operations and self.policy()['execution'] and source['enabled'],'XD_SYNC_EXECUTION_DISABLED')
        require(job['config_sha']==self.configuration(source['id']),'XD_SYNC_CONFIGURATION_CHANGED')
        self.store.patch_job(job['id'],state='running')
        try:
            with github_auth(self.transient,self.secret(source['credential'])) as env:
                if job['kind']=='upload':
                    def intent(sha,snapshot):
                        require(job['config_sha']==self.configuration(source['id']),'XD_SYNC_CONFIGURATION_CHANGED')
                        self.store.patch_job(job['id'],sha=sha,snapshot=snapshot)
                    published=self.transfer.publish(source,self.policy()['source_roots'],env,intent);sha=published['sha']
                    self.store.patch_job(job['id'],sha=sha,snapshot=published['snapshot'])
                else:
                    raw=self.store.metadata('published:'+source['id']);require(raw is not None,'XD_SYNC_NO_CONFIRMED_UPLOAD');previous=json.loads(raw)
                    require(previous['source']==digest({k:source[k] for k in ('repository','backup_branch','uid')}),'XD_SYNC_NO_CONFIRMED_UPLOAD');sha=previous['sha']
                    require(self.transfer.verify(source,sha,env),'XD_SYNC_REMOTE_MOVED');self.store.patch_job(job['id'],sha=sha)
            self.store.metadata('published:'+source['id'],json.dumps({'sha':sha,'source':digest({k:source[k] for k in ('repository','backup_branch','uid')})}))
            self.distribute(job,sha)
        except (OSError,ValueError,KeyError,TypeError) as error:
            code=str(error) if isinstance(error,SyncError) else 'XD_SYNC_OPERATION_FAILED'
            self.store.patch_job(job['id'],state='upload-unconfirmed' if code=='XD_SYNC_UPLOAD_UNCONFIRMED' else 'failed',error=code)

    def distribute(self,job,sha):
        require(job['config_sha']==self.configuration(job['source']),'XD_SYNC_CONFIGURATION_CHANGED')
        results=[]
        for target in self.store.all('targets'):
            if target['source']!=job['source'] or not target['enabled']:continue
            try:
                before=self.inspect_target(target);device=self.store.get('devices',target['device'])
                require(job['config_sha']==self.configuration(job['source']),'XD_SYNC_CONFIGURATION_CHANGED')
                result=self.transport(device,{'operation':'sync','target':target['remote_target'],'sha':sha,'observation':before['observation'],
                                             'grant_sha':before['grant_sha'],'request_id':digest({'job':job['id'],'target':target['id']}),'restart':target['restart']})
                require(result.get('sha')==sha and result.get('state') in ('observed','unconfirmed'),'XD_SYNC_TARGET_UNCONFIRMED')
                safe={k:result[k] for k in ('state','sha','updated','clean','restart') if k in result}
                if result['state']!='observed':
                    error=result.get('error');safe['error']=error if isinstance(error,str) and re.fullmatch(r'XD_SYNC_[A-Z0-9_]{1,100}',error) else 'XD_SYNC_TARGET_UNCONFIRMED'
                results.append({'target':target['id'],**safe})
            except (OSError,ValueError,TypeError,KeyError) as error:
                results.append({'target':target['id'],'state':'unconfirmed','error':str(error) if isinstance(error,SyncError) else 'XD_SYNC_TARGET_UNAVAILABLE'})
            self.store.patch_job(job['id'],result=results)
        self.store.patch_job(job['id'],state='success' if all(r['state']=='observed' for r in results) else 'partial',result=results,error=None)

    def reconcile(self,identifier):
        job=self.store.get('jobs',identifier);require(job is not None and job['state'] in ('upload-unconfirmed','interrupted','reconcile-queued'),'XD_SYNC_JOB_NOT_RECONCILABLE')
        require(job['sha'] is not None,'XD_SYNC_NO_UPLOAD_INTENT');source=self.source(job['source'])
        require(self.operations and self.policy()['execution'] and job['config_sha']==self.configuration(source['id']),'XD_SYNC_CONFIGURATION_CHANGED')
        self.store.patch_job(job['id'],state='running')
        with github_auth(self.transient,self.secret(source['credential'])) as env:require(self.transfer.verify(source,job['sha'],env),'XD_SYNC_UPLOAD_NOT_CONFIRMED')
        self.store.metadata('published:'+source['id'],json.dumps({'sha':job['sha'],'source':digest({k:source[k] for k in ('repository','backup_branch','uid')})}))
        self.distribute(job,job['sha']);return self.store.get('jobs',identifier)

    def close_job(self,identifier,body):
        require(isinstance(body,dict) and set(body)=={'confirmation'},'XD_SYNC_INPUT_INVALID')
        job=self.store.get('jobs',identifier);require(job is not None and job['state'] in ('interrupted','upload-unconfirmed','partial'),'XD_SYNC_JOB_NOT_CLOSABLE')
        require(body['confirmation']==self.source(job['source'])['name'],'XD_SYNC_CONFIRMATION_REQUIRED')
        self.store.patch_job(identifier,state='closed-unconfirmed',error='XD_SYNC_FOLLOWUP_CLOSED_BY_OPERATOR')

    def archive(self):
        directory=private_directory(self.root/'archives');require(len(list(directory.glob('*.json')))<1000,'XD_SYNC_ARCHIVE_CAPACITY')
        with self.store.db(write=True) as db:
            rows=[dict(row) for row in db.execute("SELECT * FROM jobs WHERE state IN ('success','failed','closed-unconfirmed') ORDER BY at LIMIT 10")]
            require(rows,'XD_SYNC_NOTHING_TO_ARCHIVE')
            value={'schema':1,'at':time.time(),'jobs':rows};name=digest(value)+'.json';atomic_json(directory/name,value)
            db.executemany('INSERT OR IGNORE INTO archived_jobs VALUES(?,?)',[(row['idempotency_key'],name) for row in rows])
            db.executemany('DELETE FROM jobs WHERE id=?',[(row['id'],) for row in rows])
        return {'archive':name,'count':len(rows),'unconfirmed_retained':True}


    def read_archive(self,name):
        require(bool(re.fullmatch(r'[a-f0-9]{64}\.json',name)),'XD_SYNC_ARCHIVE_INVALID')
        value=read_json(self.root/'archives'/name)
        require(digest(value)+'.json'==name,'XD_SYNC_ARCHIVE_INVALID')
        return value


class CodeSyncWorker:
    def __init__(self,hub):self.hub=hub;self.stopping=threading.Event();self.thread=None;self.lock=None

    def start(self):
        self.lock=os.open(self.hub.root/'worker.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:os.close(self.lock);self.lock=None;raise SyncError('XD_SYNC_WORKER_ALREADY_RUNNING') from None
        self.hub.store.recover();self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()

    def tick(self,now=None):
        now=time.time() if now is None else now
        with self.hub.store.db(write=True) as db:db.execute("INSERT INTO metadata VALUES('worker',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(now),))
        if len(self.hub.store.all('jobs'))>=270:
            try:self.hub.archive();self.hub.store.metadata('archive-error','')
            except (ValueError,OSError) as error:self.hub.store.metadata('archive-error',str(error) if isinstance(error,SyncError) else 'XD_SYNC_ARCHIVE_FAILED')
        if self.hub.operations and self.hub.policy()['execution']:
            for schedule in self.hub.store.due(now):
                if not self.hub.store.advance(schedule['id'],schedule['next_at'],next_daily(schedule['hour'],schedule['minute'],schedule['timezone'],now)):continue
                try:
                    source=self.hub.source(schedule['source'])
                    self.hub.enqueue({'source':source['id'],'kind':schedule['mode'],'confirmation':source['name'],'idempotency_key':digest({'schedule':schedule['id'],'slot':schedule['next_at']})})
                    self.hub.store.metadata('schedule:'+schedule['id'],'')
                except (ValueError,OSError) as error:
                    self.hub.store.metadata('schedule:'+schedule['id'],str(error) if isinstance(error,SyncError) else 'XD_SYNC_SCHEDULE_FAILED')
        for job in reversed(self.hub.store.all('jobs')):
            if job['state'] in ('queued','reconcile-queued'):
                try:
                    if job['state']=='reconcile-queued':self.hub.reconcile(job['id'])
                    else:self.hub.execute(job)
                except (ValueError,OSError) as error:
                    self.hub.store.patch_job(job['id'],state='upload-unconfirmed' if job.get('sha') else 'failed',error=str(error) if isinstance(error,SyncError) else 'XD_SYNC_OPERATION_FAILED')
                break

    def loop(self):
        while not self.stopping.is_set():
            try:self.tick()
            except (ValueError,OSError):pass
            self.stopping.wait(5)

    def close(self):
        self.stopping.set()
        if self.thread:self.thread.join(timeout=2)
        if self.lock is not None and (not self.thread or not self.thread.is_alive()):os.close(self.lock);self.lock=None
