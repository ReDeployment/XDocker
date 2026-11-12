"""XDocker's own native Nginx prefix. Never adopts the machine's existing Nginx."""
import os
from pathlib import Path
import socket
import stat
import tempfile
import time
import urllib.request

from host_metrics import private_directory
from native_services import ServiceError, fingerprint, require, run_fixed, proxy_loop


def atomic_text(path,text):
    require(len(text.encode())<=1024*1024,'XD_GATEWAY_CONFIG_CAPACITY')
    fd,name=tempfile.mkstemp(prefix='.gateway-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:stream.write(text);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def quote(path):
    require(not any(c in str(path) for c in '\n\r\0$"\\'),'XD_GATEWAY_PATH_INVALID')
    return '"'+str(path)+'"'


def listen(address,port):return ('['+address+']' if ':' in address else address)+':'+str(port)


class GatewayRuntime:
    def __init__(self,hub,runner=run_fixed,owner_reader=None,marker_reader=None):
        self.hub=hub;self.root=private_directory(hub.root/'gateway');self.runner=runner
        self.owner_reader=owner_reader or self.owner;self.marker_reader=marker_reader or self.marker

    def preview(self):
        policy=self.hub.policy();data=self.hub.data();projects=sorted(data['projects'].values(),key=lambda r:r['id'])
        require(len(projects)<=100,'XD_SERVICE_CAPACITY')
        certificate_pins={}
        for row in projects:
            require(row['endpoint'] in policy['endpoints'],'XD_SERVICE_BINDING_UNAVAILABLE')
            require(row['domain'] not in self.hub.domains(),'XD_SERVICE_DOMAIN_CONFLICT')
            if row['tls'] is not None:
                require(row['tls'] in policy['certificates'] and row['domain'] in policy['certificates'][row['tls']]['domains'],'XD_SERVICE_CERTIFICATE_DOMAIN_MISMATCH')
                cert=policy['certificates'][row['tls']]
                # Paths are private policy references. Nginx -t verifies the actual key pair.
                for kind in ('certificate','key'):
                    path=Path(cert[kind]);require(path.is_file() and path.stat().st_size<=1024*1024,'XD_GATEWAY_CERTIFICATE_UNAVAILABLE')
                    import hashlib
                    certificate_pins[cert[kind]]=hashlib.sha256(path.read_bytes()).hexdigest()
        plan=fingerprint({'policy':policy,'projects':projects,'certificates':certificate_pins})
        gateway=policy['gateway'];http=listen(gateway['listen'],gateway['http_port'])
        lines=['worker_processes 1;',f'pid {quote(self.root/"nginx.pid")};',f'error_log {quote(self.root/"error.log")} warn;',
            'events { worker_connections 512; }','http {',
            '  access_log off;','  map $http_upgrade $xdocker_connection { default upgrade; "" close; }',
            f'  client_body_temp_path {quote(self.root/"client_temp")};',f'  proxy_temp_path {quote(self.root/"proxy_temp")};',
            f'  fastcgi_temp_path {quote(self.root/"fastcgi_temp")};',f'  uwsgi_temp_path {quote(self.root/"uwsgi_temp")};',
            f'  scgi_temp_path {quote(self.root/"scgi_temp")};',
            f'  server {{ listen {http} default_server; server_name _;',
            f'    location = /__xdocker_gateway_status {{ add_header X-XDocker-Plan {plan} always; return 204; }}',
            '    location / { return 404; }','  }']
        for row in projects:
            if not row['enabled']:continue
            endpoint=policy['endpoints'][row['endpoint']];address=listen(endpoint['host'],endpoint['port'])
            require(not proxy_loop(endpoint,gateway),'XD_SERVICE_PROXY_LOOP')
            lines+=[f'  server {{ listen {http}; server_name {row["domain"]};']
            if row['tls'] is not None:
                cert=policy['certificates'][row['tls']]
                lines += ['    location / { return 301 https://$host:'+str(gateway['https_port'])+'$request_uri; }','  }',
                    f'  server {{ listen {listen(gateway["listen"],gateway["https_port"])} ssl; server_name {row["domain"]};',
                    f'    ssl_certificate {quote(cert["certificate"])};',f'    ssl_certificate_key {quote(cert["key"])};',
                    '    ssl_protocols TLSv1.2 TLSv1.3;']
            lines += ['    location / {',f'      proxy_pass {endpoint["scheme"]}://{address};',
                '      proxy_http_version 1.1;','      proxy_set_header Host $host;',
                '      proxy_set_header X-Real-IP $remote_addr;','      proxy_set_header X-Forwarded-For $remote_addr;',
                '      proxy_set_header X-Forwarded-Proto $scheme;','      proxy_set_header Upgrade $http_upgrade;',
                '      proxy_set_header Connection $xdocker_connection;','      proxy_read_timeout 300s;',
                f'      add_header X-XDocker-Plan {plan} always;','    }','  }']
            if endpoint['scheme']=='https':
                lines[-2:-2]=['      proxy_ssl_verify on;','      proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;']
        lines+=['}'];text='\n'.join(lines)+'\n'
        require(len(text.encode())<=1024*1024,'XD_GATEWAY_CONFIG_CAPACITY')
        runtime=self.hub.store.read_gateway()
        last_error=self.hub.store.read_gateway_error()
        return {'fingerprint':plan,'configuration':text,'routes':sum(r['enabled'] for r in projects),
            'enabled':gateway['enabled'],'applied_fingerprint':runtime.get('fingerprint') if runtime else None,
            'runtime_verified':bool(runtime and runtime.get('owner') and self.owner_reader(policy,runtime)==runtime.get('owner')),
            'last_error':last_error if last_error and (not runtime or last_error['at']>runtime['at']) else None,
            'address':http,'http_port':gateway['http_port'],'https_port':gateway['https_port'],'policy_sha256':fingerprint(policy)}

    def owner(self,policy,runtime=None):
        try:
            pid_file=self.root/'nginx.pid'
            if pid_file.is_symlink():return None
            pid=int(pid_file.read_text().strip());process=Path('/proc')/str(pid)
            raw=(process/'stat').read_text();started=int(raw[raw.rindex(')')+2:].split()[19])
            executable=os.readlink(process/'exe')
            command=(process/'cmdline').read_bytes().replace(b'\0',b' ').decode('utf-8','replace')
            expected=f' -p {self.root}/ -c {self.root}/active.conf'
            if Path(executable).resolve()!=Path(policy['gateway']['binary']).resolve() or expected not in command:return None
            return {'pid':pid,'started':started}
        except (OSError,ValueError,IndexError):return None

    def marker(self,policy,plan):
        address=policy['gateway']['listen']
        if address=='0.0.0.0':address='127.0.0.1'
        if address=='::':address='::1'
        host='['+address+']' if ':' in address else address
        client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url=f'http://{host}:{policy["gateway"]["http_port"]}/__xdocker_gateway_status'
        for _ in range(6):
            try:
                with client.open(url,timeout=.5) as response:
                    if response.status==204 and response.headers.get('X-XDocker-Plan')==plan:return True
            except OSError:pass
            time.sleep(.1)
        return False

    def command(self,policy,*args):
        return [policy['gateway']['binary'],'-p',str(self.root)+'/', '-c',str(self.root/'active.conf'),*args]

    def empty_pid_file(self):
        # nginx -t creates a zero-byte pid file, including on a failed config test.
        # It carries no process identity; starting still requires both ports free.
        try:
            info=(self.root/'nginx.pid').lstat()
            return stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid() and info.st_nlink==1 and info.st_size==0
        except OSError:return False

    def restore(self):
        """Only restore the last explicitly applied plan, never promote pending edits."""
        with self.hub.locked():
            runtime=self.hub.store.read_gateway()
            if runtime is None:return {'state':'not-configured'}
            plan=self.preview()
            require(plan['fingerprint']==runtime['fingerprint'],'XD_GATEWAY_PENDING_CONFIGURATION')
            require((self.root/'active.conf').read_text()==plan['configuration'],'XD_GATEWAY_ACTIVE_CONFIGURATION_CHANGED')
            pid_file=self.root/'nginx.pid'
            if pid_file.exists() and not self.empty_pid_file() and self.owner_reader(self.hub.policy(),runtime) is None:
                require(not pid_file.is_symlink() and int(pid_file.read_text().strip())==runtime['owner']['pid'],'XD_GATEWAY_OWNER_UNCONFIRMED')
                # A stale receipt is removable only after the recorded process is gone.
                require(not (Path('/proc')/str(runtime['owner']['pid'])).exists(),'XD_GATEWAY_OWNER_UNCONFIRMED')
                pid_file.unlink()
        return self.apply({'fingerprint':plan['fingerprint'],'confirmation':'xdocker-gateway'})

    def apply(self,body):
        require(isinstance(body,dict) and set(body)=={'fingerprint','confirmation'} and body['confirmation']=='xdocker-gateway','XD_GATEWAY_CONFIRMATION_REQUIRED')
        with self.hub.locked():
            plan=self.preview();policy=self.hub.policy();require(plan['enabled'],'XD_GATEWAY_APPLY_DISABLED')
            require(body['fingerprint']==plan['fingerprint'],'XD_GATEWAY_PLAN_CHANGED')
            runtime=self.hub.store.read_gateway()
            owner=self.owner_reader(policy,runtime)
            require(owner is None or runtime is not None and runtime.get('owner')==owner,'XD_GATEWAY_OWNER_UNCONFIRMED')
            require(not (self.root/'nginx.pid').exists() or owner is not None or self.empty_pid_file(),'XD_GATEWAY_OWNER_UNCONFIRMED')
            if runtime and owner and runtime['fingerprint']==plan['fingerprint'] and self.marker_reader(policy,plan['fingerprint']):
                return {'state':'applied','fingerprint':plan['fingerprint'],'owner':owner,'routes':plan['routes'],'at':runtime['at'],'reused':True}
            if owner is None:
                address=policy['gateway']['listen'];family=socket.AF_INET6 if ':' in address else socket.AF_INET
                for port in (policy['gateway']['http_port'],policy['gateway']['https_port']):
                    with socket.socket(family) as sock:
                        try:sock.bind((address,port))
                        except OSError:raise ServiceError('XD_GATEWAY_PORT_IN_USE') from None
            candidate=self.root/'candidate.conf';atomic_text(candidate,plan['configuration'])
            checked=self.runner([policy['gateway']['binary'],'-p',str(self.root)+'/', '-c',str(candidate),'-t'])
            require(checked['code']==0 and not checked['truncated'],'XD_GATEWAY_CONFIG_CHECK_FAILED')
            require(self.preview()['fingerprint']==plan['fingerprint'],'XD_GATEWAY_PLAN_CHANGED')
            active=self.root/'active.conf';require(not active.is_symlink(),'XD_GATEWAY_UNSAFE_PATH')
            previous=active.read_text() if active.exists() else None
            atomic_text(active,plan['configuration'])
            started_owner=None
            try:
                result=self.runner(self.command(policy,*(['-s','reload'] if owner else [])))
                require(result['code']==0 and not result['truncated'],'XD_GATEWAY_RELOAD_FAILED')
                started_owner=self.owner_reader(policy,runtime)
                require(started_owner is not None,'XD_GATEWAY_OWNER_UNCONFIRMED')
                require(self.marker_reader(policy,plan['fingerprint']),'XD_GATEWAY_RUNTIME_UNCONFIRMED')
                require(self.preview()['fingerprint']==plan['fingerprint'],'XD_GATEWAY_PLAN_CHANGED')
                self.hub.store.write_gateway({'fingerprint':plan['fingerprint'],'owner':started_owner,'at':time.time()})
                return {'state':'applied','fingerprint':plan['fingerprint'],'owner':started_owner,'routes':plan['routes'],'at':time.time()}
            except (OSError,ValueError):
                if previous is not None:atomic_text(active,previous)
                elif active.exists():active.unlink()
                rollback=False
                try:
                    if owner and previous is not None and self.owner_reader(policy,runtime)==owner:
                        restored=self.runner(self.command(policy,'-s','reload'))
                        rollback=restored['code']==0 and runtime is not None and self.marker_reader(policy,runtime['fingerprint'])
                    elif owner is None and started_owner is not None and self.owner_reader(policy,runtime)==started_owner:
                        # Only the new master verified above can be stopped during failed first apply.
                        if previous is None:atomic_text(active,plan['configuration'])
                        stopped=self.runner(self.command(policy,'-s','quit'));rollback=stopped['code']==0
                except (OSError,ValueError):rollback=False
                self.hub.store.write_gateway_error({'at':time.time(),'fingerprint':plan['fingerprint'],'rollback_verified':rollback,'runtime_unconfirmed':True})
                raise
