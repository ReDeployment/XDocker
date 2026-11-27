"""Connect existing services: private grants, fixed operations, no application launcher."""
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import subprocess
import time
import urllib.error
import urllib.request

from host_metrics import private_directory,read_json,atomic_json
from registry_store import RegistryStore

IDENTIFIER = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')
UNIT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,180}\.service$')
DOMAIN = re.compile(r'^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')
SHA = re.compile(r'^[a-f0-9]{64}$')
WRITE_ACTIONS = {'start','stop','restart'}


class ServiceError(ValueError):
    pass


def require(ok, code):
    if not ok: raise ServiceError(code)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def identifier(value):
    require(isinstance(value,str) and IDENTIFIER.fullmatch(value), 'XD_SERVICE_ID_INVALID')


def safe_path(value):
    require(isinstance(value,str) and value.startswith('/') and not any(c in value for c in '\n\r\0$"\\;{}'), 'XD_SERVICE_PATH_INVALID')
    require('..' not in Path(value).parts, 'XD_SERVICE_PATH_INVALID')
    return value


def host_directory(value):
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        return None
    try:
        resolved = path.resolve(strict=True)
        if resolved != Path('/') and resolved.is_dir():
            return resolved
    except (OSError, RuntimeError, ValueError):
        # Missing paths, inaccessible directories and symlink loops cannot prove ownership.
        return None
    return None


def proxy_loop(endpoint,gateway):
    ep,gw=ipaddress.ip_address(endpoint['host']),ipaddress.ip_address(gateway['listen'])
    return endpoint['port'] in (gateway['http_port'],gateway['https_port']) and (ep==gw or gw.is_unspecified or ep.is_loopback and gw.is_loopback)


def default_policy():
    return {'schema':1,'revision':1,'endpoints':{},'services':{},'certificates':{},
        'gateway':{'enabled':False,'listen':'127.0.0.1','http_port':8088,'https_port':8443,'binary':'/usr/sbin/nginx'}}


def validate_policy(value):
    require(isinstance(value,dict) and set(value)=={'schema','revision','endpoints','services','certificates','gateway'} and value['schema']==1 and type(value['revision']) is int and value['revision']>=1, 'XD_SERVICE_POLICY_INVALID')
    for kind in ('endpoints','services','certificates'):
        require(isinstance(value[kind],dict) and len(value[kind])<=100,'XD_SERVICE_CAPACITY')
        for alias,row in value[kind].items():
            identifier(alias);require(isinstance(row,dict),'XD_SERVICE_POLICY_INVALID')
            if kind=='endpoints':
                require(set(row)=={'scheme','host','port','health_path','expected_status'},'XD_SERVICE_ENDPOINT_INVALID')
                require(row['scheme'] in ('http','https') and type(row['port']) is int and 1<=row['port']<=65535,'XD_SERVICE_ENDPOINT_INVALID')
                try: address=ipaddress.ip_address(row['host'])
                except (ValueError,TypeError): raise ServiceError('XD_SERVICE_LITERAL_ADDRESS_REQUIRED') from None
                require(not address.is_unspecified and not address.is_multicast and not address.is_link_local,'XD_SERVICE_ENDPOINT_INVALID')
                require(isinstance(row['health_path'],str) and re.fullmatch(r'/[A-Za-z0-9_./~-]{0,200}',row['health_path']) and '..' not in row['health_path'],'XD_SERVICE_HEALTH_PATH_INVALID')
                require(type(row['expected_status']) is int and 200<=row['expected_status']<=299,'XD_SERVICE_ENDPOINT_INVALID')
            elif kind=='services':
                require(set(row)=={'scope','unit','actions','definition_sha256'},'XD_SERVICE_GRANT_INVALID')
                require(row['scope'] in ('system','user') and isinstance(row['unit'],str) and UNIT.fullmatch(row['unit']),'XD_SERVICE_UNIT_INVALID')
                require(isinstance(row['actions'],list) and set(row['actions'])<={'status','logs',*WRITE_ACTIONS} and 'status' in row['actions'],'XD_SERVICE_GRANT_INVALID')
                if set(row['actions'])&WRITE_ACTIONS:
                    require(isinstance(row['definition_sha256'],str) and SHA.fullmatch(row['definition_sha256']),'XD_SERVICE_DEFINITION_PIN_REQUIRED')
                else:require(row['definition_sha256'] is None or isinstance(row['definition_sha256'],str) and SHA.fullmatch(row['definition_sha256']),'XD_SERVICE_GRANT_INVALID')
                require(not row['unit'].startswith(('xdocker-admin','xdocker-host-monitor')),'XD_SERVICE_SELF_CONTROL_DENIED')
            else:
                require(set(row)=={'certificate','key','domains'},'XD_SERVICE_CERTIFICATE_INVALID')
                safe_path(row['certificate']);safe_path(row['key'])
                require(isinstance(row['domains'],list) and 1<=len(row['domains'])<=100 and all(isinstance(d,str) and DOMAIN.fullmatch(d) for d in row['domains']),'XD_SERVICE_CERTIFICATE_INVALID')
    gateway=value['gateway']
    require(isinstance(gateway,dict) and set(gateway)=={'enabled','listen','http_port','https_port','binary'} and type(gateway['enabled']) is bool,'XD_SERVICE_GATEWAY_INVALID')
    try:address=ipaddress.ip_address(gateway['listen'])
    except (ValueError,TypeError):raise ServiceError('XD_SERVICE_LITERAL_ADDRESS_REQUIRED') from None
    require(not address.is_multicast and not address.is_link_local,'XD_SERVICE_GATEWAY_INVALID')
    require(all(type(gateway[k]) is int and (gateway[k]==standard or 1024<=gateway[k]<=65535)
                for k,standard in (('http_port',80),('https_port',443)))
            and gateway['http_port']!=gateway['https_port'],'XD_SERVICE_GATEWAY_INVALID')
    safe_path(gateway['binary'])
    # One exact systemd unit is represented by one grant, not conflicting aliases.
    identities=[(r['scope'],r['unit']) for r in value['services'].values()]
    require(len(set(identities))==len(identities),'XD_SERVICE_DUPLICATE_UNIT')
    return value


def run_fixed(command, timeout=8, limit=65536):
    """Bounded child process. No shell, stdin, inherited token variables or sudo fallback."""
    env={k:os.environ[k] for k in ('PATH','HOME','USER','LOGNAME','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS') if k in os.environ}
    env.update(LANG='C.UTF-8',LC_ALL='C.UTF-8',SYSTEMD_COLORS='0',SYSTEMD_PAGER='cat')
    try:child=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env)
    except OSError:raise ServiceError('XD_SERVICE_EXECUTOR_UNAVAILABLE') from None
    output=bytearray();truncated=False;deadline=time.monotonic()+timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout,selectors.EVENT_READ)
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise ServiceError('XD_SERVICE_COMMAND_TIMEOUT')
                ready=selector.select(min(remaining,.2))
                if ready:
                    block=os.read(child.stdout.fileno(),min(8192,limit+1-len(output)))
                    if not block:break
                    output.extend(block)
                    if len(output)>limit:truncated=True;child.kill();break
                elif child.poll() is not None:break
        code=child.wait(timeout=max(.1,deadline-time.monotonic()))
        return {'code':code,'stdout':bytes(output[:limit]).decode('utf-8','replace'),'truncated':truncated}
    except subprocess.TimeoutExpired:raise ServiceError('XD_SERVICE_COMMAND_TIMEOUT') from None
    finally:
        if child.poll() is None:child.kill();child.wait()
        child.stdout.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def probe_endpoint(endpoint):
    address=endpoint['host'];host='['+address+']' if ':' in address else address
    url=f"{endpoint['scheme']}://{host}:{endpoint['port']}{endpoint['health_path']}"
    client=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    started=time.monotonic()
    try:
        with client.open(urllib.request.Request(url,method='GET',headers={'Host':endpoint['host_header']} if endpoint.get('host_header') else {}),timeout=3) as response:status=response.status
    except urllib.error.HTTPError as exc:status=exc.code;exc.close()
    except (OSError,ValueError):return {'state':'unreachable','at':time.time(),'status':None,'elapsed_ms':round((time.monotonic()-started)*1000)}
    return {'state':'healthy' if status==endpoint['expected_status'] else 'unexpected-status','at':time.time(),
            'status':status,'elapsed_ms':round((time.monotonic()-started)*1000)}


def read_host(command, **bounds):
    # A system service can run as the unit owner without inheriting their login session.
    if command[:2] == ['systemctl', '--user'] and not (
            os.environ.get('XDG_RUNTIME_DIR') or os.environ.get('DBUS_SESSION_BUS_ADDRESS')):
        command = ['env', f'XDG_RUNTIME_DIR=/run/user/{os.geteuid()}', *command]
    return run_fixed(command, **bounds)


class SystemdServices:
    def __init__(self,runner=read_host):self.runner=runner

    def status(self,grant):
        args=['systemctl']+(['--user'] if grant['scope']=='user' else [])
        result=self.runner([*args,'show','--no-pager','--property=Id,LoadState,ActiveState,SubState,MainPID,ExecMainStatus,FragmentPath,DropInPaths,NeedDaemonReload','--',grant['unit']])
        require(result['code']==0 and not result['truncated'],'XD_SERVICE_STATUS_UNAVAILABLE')
        fields=dict(line.split('=',1) for line in result['stdout'].splitlines() if '=' in line)
        require(fields.get('Id')==grant['unit'] and fields.get('LoadState')=='loaded','XD_SERVICE_UNIT_NOT_LOADED')
        paths=[fields.get('FragmentPath',''),*shlex.split(fields.get('DropInPaths',''))]
        definitions=[]
        for name in paths:
            path=Path(name);require(name and path.is_absolute() and path.is_file() and path.stat().st_size<=1024*1024,'XD_SERVICE_DEFINITION_UNAVAILABLE')
            definitions.append((str(path.resolve()),hashlib.sha256(path.read_bytes()).hexdigest()))
        definition=fingerprint(definitions)
        safe={name:fields.get(name,'unknown') for name in ('ActiveState','SubState','MainPID','ExecMainStatus','NeedDaemonReload')}
        return {'scope':grant['scope'],'unit':grant['unit'],'state':safe['ActiveState'],'substate':safe['SubState'],
            'pid':int(safe['MainPID']) if safe['MainPID'].isdecimal() else None,'at':time.time(),
            'definition_sha256':definition,'definition_matches':definition==grant['definition_sha256'],
            'reload_required':safe['NeedDaemonReload']!='no','observation':fingerprint({'grant':grant,'runtime':safe,'definition':definition})}

    def logs(self,grant):
        require('logs' in grant['actions'],'XD_SERVICE_ACTION_NOT_GRANTED')
        flag='--user-unit=' if grant['scope']=='user' else '--unit='
        value=self.runner(['journalctl',flag+grant['unit'],'--no-pager','--lines=100','--output=cat'])
        require(value['code']==0 or value['truncated'],'XD_SERVICE_LOGS_UNAVAILABLE')
        return {'text':value['stdout'],'truncated':value['truncated']}

    def action(self,grant,action,observation):
        require(action in WRITE_ACTIONS and action in grant['actions'],'XD_SERVICE_ACTION_NOT_GRANTED')
        before=self.status(grant)
        require(before['observation']==observation,'XD_SERVICE_OBSERVATION_CHANGED')
        require(before['definition_matches'] and not before['reload_required'],'XD_SERVICE_DEFINITION_CHANGED')
        args=['systemctl']+(['--user'] if grant['scope']=='user' else [])
        result=self.runner([*args,'--no-ask-password',action,'--',grant['unit']])
        require(result['code']==0 and not result['truncated'],'XD_SERVICE_ACTION_REJECTED')
        after=self.status(grant)
        require(after['definition_matches'] and not after['reload_required'],'XD_SERVICE_DEFINITION_CHANGED')
        expected='inactive' if action=='stop' else 'active'
        require(after['state']==expected,'XD_SERVICE_RUNTIME_UNCONFIRMED')
        return after


class ServiceHub:
    def __init__(self,state,adapter=None,probe=probe_endpoint,domains=None,monitors=None):
        self.root=private_directory(Path(state).resolve()/'service-hub')
        self.adapter=adapter or SystemdServices();self.probe=probe;self.domains=domains or (lambda:set())
        self.store=RegistryStore(Path(state).resolve())
        self.observations={}
        self.monitors=monitors or (lambda:set())

    @contextmanager
    def locked(self):
        fd=os.open(self.root/'hub.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ServiceError('XD_SERVICE_BUSY') from None
            yield
        finally:os.close(fd)

    def policies(self):
        effective, imported = self.store.read_policies()
        return validate_policy(effective), imported

    def policy(self):
        return self.policies()[0]

    def import_policy(self,value):
        validate_policy(value)
        with self.locked():
            old=self.policy()
            require(value['revision']==old['revision']+1,'XD_SERVICE_POLICY_REVISION_CONFLICT')
            self.store.write_policy(value)
        return {'revision':value['revision'],'sha256':fingerprint(value)}

    def data(self):
        data=self.store.read_hub()
        require(isinstance(data,dict) and set(data)=={'schema','projects','operations'} and data['schema']==1 and isinstance(data['projects'],dict) and len(data['projects'])<=100 and isinstance(data['operations'],list) and len(data['operations'])<=200,'XD_SERVICE_STATE_INVALID')
        for name,row in data['projects'].items():
            require(isinstance(row,dict) and row.get('id')==name,'XD_SERVICE_STATE_INVALID')
            self.validate_project(row)
        return data

    def validate_project(self,row):
        require(isinstance(row,dict) and set(row)-{'directory'}=={'id','revision','name','endpoint','domain','service','monitor','delivery','tls','enabled'},'XD_SERVICE_PROJECT_INVALID')
        identifier(row['id']);identifier(row['endpoint'])
        if row.get('directory') is not None:safe_path(row['directory'])
        require(type(row['revision']) is int and row['revision']>=1 and isinstance(row['name'],str) and 1<=len(row['name'])<=80 and all(c.isprintable() for c in row['name']),'XD_SERVICE_PROJECT_INVALID')
        require(isinstance(row['domain'],str) and DOMAIN.fullmatch(row['domain']),'XD_SERVICE_DOMAIN_INVALID')
        for field in ('service','monitor','tls'):
            if row[field] is not None:identifier(row[field])
        require(row['delivery'] in ('external','source','binary','image') and type(row['enabled']) is bool,'XD_SERVICE_PROJECT_INVALID')

    def save(self, row):
        with self.locked():
            self._save_project(row, self.data())
        return row

    def _save_project(self, row, data, *, entry_owner=None):
        self.validate_project(row)
        policy=self.policy();old=data['projects'].get(row['id'])
        require(row['revision']==(old['revision']+1 if old else 1),'XD_SERVICE_REVISION_CONFLICT')
        require(old is not None or len(data['projects'])<100,'XD_SERVICE_CAPACITY')
        require(row['endpoint'] in policy['endpoints'] and (row['service'] is None or row['service'] in policy['services']),'XD_SERVICE_BINDING_UNAVAILABLE')
        require(row['domain'] not in self.domains() and not any(r['id']!=row['id'] and r['domain']==row['domain'] for r in data['projects'].values()),'XD_SERVICE_DOMAIN_CONFLICT')
        require(row['tls'] is None or row['tls'] in policy['certificates'] and row['domain'] in policy['certificates'][row['tls']]['domains'],'XD_SERVICE_CERTIFICATE_DOMAIN_MISMATCH')
        endpoint=policy['endpoints'][row['endpoint']];gateway=policy['gateway']
        require(not proxy_loop(endpoint,gateway),'XD_SERVICE_PROXY_LOOP')
        require(row['monitor'] is None or row['monitor'] in self.monitors(),'XD_SERVICE_MONITOR_UNAVAILABLE')
        require(row['service'] not in self.store.additional_bindings(),'XD_SERVICE_UNIT_ALREADY_BOUND')
        # One service belongs to one project; observing it never grants other aliases.
        require(row['service'] is None or not any(r['id']!=row['id'] and r['service']==row['service'] for r in data['projects'].values()),'XD_SERVICE_UNIT_ALREADY_BOUND')
        data['projects'][row['id']]=row
        self.store.write_hub(data, entry_owner=entry_owner, new_entry=row['id'])


    def add_entry(self, body):
        require(isinstance(body, dict) and set(body) == {'project', 'endpoint', 'domain', 'tls', 'enabled'},
                'XD_SERVICE_PROJECT_INVALID')
        project = body['project']
        identifier(project)
        require(isinstance(body['domain'], str), 'XD_SERVICE_DOMAIN_INVALID')
        with self.locked():
            data = self.data()
            parent = data['projects'].get(project)
            require(parent and project not in self.store.entry_owners(), 'XD_SERVICE_PROJECT_NOT_FOUND')
            row = {**parent, **{k: body[k] for k in ('endpoint', 'domain', 'tls', 'enabled')},
                   'id': 'entry-' + fingerprint(body['domain'])[:32], 'revision': 1,
                   'service': None}
            self._save_project(row, data, entry_owner=project)
        return row

    def project_merge_preview(self, target, source):
        identifier(target)
        identifier(source)
        require(target != source, 'XD_SERVICE_PROJECT_MERGE_INVALID')
        listing = self.listing()
        groups = {row['id']: row for row in listing['project_groups']}
        project, other = groups.get(target), groups.get(source)
        require(project and other and source in project['merge_candidates'], 'XD_SERVICE_PROJECT_MERGE_INVALID')
        domains = {row['id']: row for row in listing['projects']}
        entries = [domains[entry['id']] for group in (project, other) for entry in group['entries']]
        result = {
            'source': source, 'project': {'id': target, 'name': project['name']},
            'entries': [
                {'id': row['id'], 'domain': row['domain'], 'revision': row['revision'], 'enabled': row['enabled'],
                 'target': listing['endpoints'].get(row['endpoint']),
                 'services': [listing['services'].get(alias, {}).get('unit', alias) for alias in row['bound_services']]}
                for row in entries
            ],
        }
        return {**result, 'fingerprint': fingerprint({**result, 'policy': listing['policy_sha256']})}

    def merge_projects(self, target, body):
        require(isinstance(body, dict) and set(body) == {'source', 'fingerprint'}, 'XD_SERVICE_PROJECT_MERGE_INVALID')
        with self.locked():
            preview = self.project_merge_preview(target, body['source'])
            require(body['fingerprint'] == preview['fingerprint'], 'XD_SERVICE_CONFIGURATION_CHANGED')
            entries = [row['id'] for row in preview['entries'] if row['id'] != target]
            self.store.assign_entries(target, entries)
        return {'project': target, 'entries': entries}

    def bind_services(self,body):
        require(isinstance(body,dict) and set(body)=={'policy_sha256','bindings'},'XD_SERVICE_BATCH_INVALID')
        entries=body['bindings']
        require(isinstance(entries,list) and 1<=len(entries)<=100,'XD_SERVICE_BATCH_INVALID')
        with self.locked():
            policy=self.policy()
            require(body['policy_sha256']==fingerprint(policy),'XD_SERVICE_CONFIGURATION_CHANGED')
            projects=self.data()['projects']
            entry_owners = self.store.entry_owners()
            owners=self.store.additional_bindings()
            owners.update({p['service']:p['id'] for p in projects.values() if p['service']})
            seen=set()
            additions=[]
            for entry in entries:
                require(isinstance(entry,dict) and set(entry)=={'project','alias','revision'},'XD_SERVICE_BATCH_INVALID')
                alias=entry['alias'];name=entry['project']
                identifier(alias);identifier(name)
                require(alias not in seen,'XD_SERVICE_BATCH_INVALID')
                seen.add(alias)
                row=projects.get(name)
                require(row is not None,'XD_SERVICE_PROJECT_NOT_FOUND')
                require(type(entry['revision']) is int and row['revision']==entry['revision'],'XD_SERVICE_REVISION_CONFLICT')
                endpoint_available = any(p['endpoint'] in policy['endpoints'] for p in projects.values()
                                         if p['id'] == name or entry_owners.get(p['id']) == name)
                require(alias in policy['services'] and endpoint_available,'XD_SERVICE_BINDING_UNAVAILABLE')
                owner = owners.get(alias)
                require(owner is None or owner == name or entry_owners.get(owner) == name,'XD_SERVICE_UNIT_ALREADY_BOUND')
                if owner is None:additions.append((alias,name))
            if additions:self.store.add_bindings(additions)
            return {'added':len(additions),'already_bound':len(entries)-len(additions)}

    def remove(self,identifier,body):
        with self.locked():
            data=self.data();row=data['projects'].get(identifier)
            require(row is not None and isinstance(body,dict) and set(body)=={'revision','confirmation'},'XD_SERVICE_PROJECT_INVALID')
            require(body['revision']==row['revision'] and type(body['revision']) is int,'XD_SERVICE_REVISION_CONFLICT')
            require(body['confirmation']==row['name'],'XD_SERVICE_CONFIRMATION_REQUIRED')
            require(identifier not in self.store.entry_owners().values(),'XD_SERVICE_PROJECT_HAS_ENTRIES')
            del data['projects'][identifier];self.store.write_hub(data)

    def bound(self,identifier,alias=None,*,policy=None):
        data=self.data();require(identifier in data['projects'],'XD_SERVICE_PROJECT_NOT_FOUND')
        row=data['projects'][identifier]
        if policy is None:policy=self.policy()
        rows = data['projects']
        endpoint_available = row['endpoint'] in policy['endpoints']
        if alias is not None:
            entry_owners = self.store.entry_owners()
            owner = next((p['id'] for p in rows.values() if p['service'] == alias), None)
            if owner is None:
                owner = self.store.additional_bindings().get(alias)
            require(owner is not None and (owner == identifier or entry_owners.get(owner) == identifier),
                    'XD_SERVICE_BINDING_UNAVAILABLE')
            row = {**rows[owner], 'service': alias}
            endpoint_available = any(p['endpoint'] in policy['endpoints'] for p in rows.values()
                                     if p['id'] == identifier or entry_owners.get(p['id']) == identifier)
        require(endpoint_available and (row['service'] is None or row['service'] in policy['services']),
                'XD_SERVICE_BINDING_UNAVAILABLE')
        return row,policy

    def health(self,identifier):
        def target():
            effective, imported = self.policies()
            row, _ = self.bound(identifier, policy=effective)
            endpoint = {**effective['endpoints'][row['endpoint']], 'host_header': row['domain']}
            return endpoint, fingerprint({'row': row, 'policy': imported})
        endpoint, expected = target()
        value = self.probe(endpoint)
        require(expected == target()[1], 'XD_SERVICE_CONFIGURATION_CHANGED')
        self.observations[identifier]={'fingerprint':expected,**value}
        atomic_json(private_directory(self.root/'health')/(identifier+'.json'),self.observations[identifier])
        return value

    def status(self,identifier,alias=None):
        row,policy=self.bound(identifier,alias);require(row['service'] is not None,'XD_SERVICE_NOT_SUPERVISED')
        value=self.adapter.status(policy['services'][row['service']])
        return {**value,'revision':row['revision'],'policy_sha256':fingerprint(policy),
            'actions':policy['services'][row['service']]['actions']}

    def action(self,identifier,body,alias=None):
        require(isinstance(body,dict) and set(body)=={'action','revision','confirmation','observation','policy_sha256','idempotency_key'},'XD_SERVICE_ACTION_INVALID')
        require(isinstance(body['idempotency_key'],str) and re.fullmatch(r'[a-f0-9-]{32,36}',body['idempotency_key']),'XD_SERVICE_ACTION_INVALID')
        with self.locked():
            data=self.data()
            request={'project':identifier,'body':body}
            if alias is not None:request['service']=alias
            request_sha=fingerprint(request)
            old=next((o for o in data['operations'] if o.get('idempotency_key')==body['idempotency_key']),None)
            if old:
                require(old.get('request_sha256')==request_sha,'XD_SERVICE_IDEMPOTENCY_CONFLICT')
                return old
            row,policy=self.bound(identifier,alias)
            require(row['service'] is not None,'XD_SERVICE_NOT_SUPERVISED')
            require(type(body['revision']) is int and row['revision']==body['revision'] and fingerprint(policy)==body['policy_sha256'],'XD_SERVICE_CONFIGURATION_CHANGED')
            grant=policy['services'][row['service']]
            require(body['confirmation']==(grant['unit'] if alias is not None else row['name']),'XD_SERVICE_CONFIRMATION_REQUIRED')
            require(body['action'] in WRITE_ACTIONS and body['action'] in grant['actions'],'XD_SERVICE_ACTION_NOT_GRANTED')
            require(len(data['operations'])<200,'XD_SERVICE_OPERATION_CAPACITY')
            operation={'id':os.urandom(16).hex(),'project':identifier,'action':body['action'],'at':time.time(),'state':'unconfirmed','policy_sha256':fingerprint(policy),
                       'idempotency_key':body['idempotency_key'],'request_sha256':request_sha}
            if alias is not None:operation['result']={'unit':grant['unit'],'scope':grant['scope']}
            data['operations'].append(operation);self.store.write_hub(data)
            try:
                result=self.adapter.action(grant,body['action'],body['observation'])
                require(fingerprint(policy)==fingerprint(self.policy()),'XD_SERVICE_CONFIGURATION_CHANGED')
                operation.update(state='observed',result=result)
            except (OSError,ValueError) as exc:
                operation['error']=str(exc) if isinstance(exc,ServiceError) else 'XD_SERVICE_OPERATION_UNCONFIRMED'
                self.store.write_hub(data)
                raise
            self.store.write_hub(data)
            return operation

    def listing(self):
        policy,imported=self.policies();data=self.data();rows=[];monitors=self.monitors()
        additional={}
        for alias,project in self.store.additional_bindings().items():
            additional.setdefault(project,[]).append(alias)
        for row in data['projects'].values():
            aliases=([row['service']] if row['service'] else [])+additional.get(row['id'],[])
            snapshot=self.observations.get(row['id'])
            if snapshot is None and (self.root/'health'/(row['id']+'.json')).exists():
                snapshot=read_json(self.root/'health'/(row['id']+'.json'))
            matches=bool(snapshot and snapshot.get('fingerprint')==fingerprint({'row':row,'policy':imported}))
            fresh=bool(matches and time.time()-snapshot['at']<=30)
            rows.append({**row,'bound_services':aliases,'health':snapshot if fresh else None,'last_health':snapshot if matches else None,'endpoint_available':row['endpoint'] in policy['endpoints'],
                         'service_available':all(a in policy['services'] for a in aliases),
                         'monitor_available':row['monitor'] is None or row['monitor'] in monitors})
        entry_owners = self.store.entry_owners()
        groups = {row['id']: [] for row in rows if row['id'] not in entry_owners}
        for row in rows:
            groups[entry_owners.get(row['id'], row['id'])].append(row)
        directories = {row['id']: host_directory(row.get('directory')) for row in rows if row['id'] in groups}
        project_groups = []
        for root in rows:
            if root['id'] not in groups:
                continue
            owner = root['id']
            entries = [root] + [row for row in groups[owner] if row['id'] != owner]
            directory = directories[owner]
            project_groups.append({
                **root, 'entries': [{k: row[k] for k in ('id', 'domain', 'endpoint')} for row in entries],
                'endpoint_available': any(row['endpoint_available'] for row in entries),
                'bound_services': [alias for row in entries for alias in row['bound_services']],
                'service_available': all(row['service_available'] for row in entries),
                'merge_candidates': [pid for pid, path in directories.items()
                                     if pid != owner and directory is not None and path == directory],
            })
        return {'projects':rows,'project_groups':project_groups,'entry_owners':entry_owners,'endpoints':policy['endpoints'],
            'services':{k:{'unit':v['unit'],'scope':v['scope'],'actions':v['actions']} for k,v in policy['services'].items()},
            'certificates':{k:{'domains':v['domains']} for k,v in policy['certificates'].items()},
            'policy_sha256':fingerprint(policy),'policy_revision':policy['revision'],'gateway_enabled':policy['gateway']['enabled'],
            'operations':data['operations'],'configuration_writable':True,'batch_binding_available':True}
