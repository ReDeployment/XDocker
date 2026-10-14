"""Loopback-only SSH control. Reuses XDocker's scoped tunnel supervisor."""
import argparse
from collections import deque
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import secrets
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).with_name('static')
spec = importlib.util.spec_from_file_location('xdocker_admin_tunnel', ROOT/'scripts/admin_tunnel.py')
tunnel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tunnel)


def safe_file(path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('拒绝符号链接状态文件。')
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid():
            raise ValueError('本机状态文件须属于当前用户，权限为 600 或 400。')
        if info.st_size > 65536:
            raise ValueError('本机状态文件过大。')
    return path


def initialize(root):
    directory = Path(root)/'data/tunnel-manager'
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ValueError('拒绝符号链接管理目录。')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = safe_file(directory/'access-token')
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(48)+'\n')
    return directory


class Manager:
    def __init__(self, root=ROOT, control=None):
        self.root = Path(root).resolve()
        self.directory = self.root/'data/tunnel-manager'
        self.token = safe_file(self.directory/'access-token').read_text().strip()
        if len(self.token) < 32:
            raise ValueError('先执行 tunnel-manager.sh init。')
        self.control = control or tunnel.manage
        self.lock = threading.Lock()
        self.jobs = deque(maxlen=50)
        ledger = safe_file(self.directory/'jobs.json')
        if ledger.exists():
            records = json.loads(ledger.read_text())
            if not isinstance(records, list) or len(records) > 50:
                raise ValueError('操作记录格式无效，保留原文件后排查。')
            for record in records:
                if record['state'] == 'running':
                    record.update(state='unconfirmed', message='管理服务中断，先核对实际连接；操作不会自动重试。')
                self.jobs.append(record)

    def ports(self):
        directory = self.root/'data/admin-tunnel'
        if directory.is_symlink():
            raise ValueError('拒绝符号链接隧道目录。')
        if not directory.exists():
            return []
        ports = sorted(int(p.name) for p in directory.iterdir() if p.name.isdigit() and 1 <= int(p.name) <= 65535)
        if len(ports) > 30:
            raise ValueError('连接配置超过 30 项，请先在本机整理。')
        return ports

    def config(self, port):
        path = safe_file(tunnel.folder(self.root, port)/'config.json')
        if not path.exists():
            raise ValueError('没有此端口的 XDocker 配置。')
        data = json.loads(path.read_text())
        config = tunnel.configuration(**data)
        if config['local_port'] != port:
            raise ValueError('隧道配置端口不匹配。')
        return config

    def version(self, config):
        return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

    def registered(self, config):
        return sys.platform == 'darwin' and tunnel.launchctl('print', 'gui/'+str(os.getuid())+'/'+tunnel.descriptor(self.root,config)['Label']).returncode == 0

    def status(self, port):
        config = self.config(port)
        path = safe_file(tunnel.folder(self.root, port)/'status.json')
        stored = json.loads(path.read_text()) if path.exists() else {}
        active = tunnel.supervisor_active(self.root, port)
        registered = self.registered(config)
        state = stored.get('state', 'not-started')
        if not active and state in ('connecting','connected','reconnecting','api-unreachable','service-error','unexpected-response'):
            state = 'stopped'
        return dict(config, version=self.version(config), state=state,
                    message=tunnel.MESSAGES.get(state, '状态待核对。'), at=stored.get('at'),
                    supervisor_active=active, registered=registered, current_http=tunnel.health(port),
                    url='http://127.0.0.1:'+str(port), retry_seconds=stored.get('retry_seconds'))

    def snapshot(self):
        profiles = []
        for port in self.ports():
            if not (tunnel.folder(self.root, port)/'config.json').exists():
                continue
            try:
                profiles.append(self.status(port))
            except (ValueError, OSError, TypeError, json.JSONDecodeError):
                profiles.append({'local_port': port, 'state':'invalid-config', 'message':'配置不可安全读取，请在本机排查。', 'manageable':False})
        with self.lock:
            jobs = [dict(j) for j in self.jobs]
        return {'profiles':profiles, 'jobs':jobs, 'platform':sys.platform,
                'writes_supported':sys.platform == 'darwin', 'busy':any(j['state']=='running' for j in jobs)}

    def allowed_origins(self, port):
        # Only this standalone UI and registered XDocker loopback forwarding ports.
        ports = {port}
        for candidate in self.ports():
            try:self.config(candidate)
            except (ValueError,OSError,TypeError):continue
            ports.add(candidate)
        return {'http://'+host+':'+str(p) for host in ('127.0.0.1','localhost') for p in ports}

    def submit(self, body):
        if sys.platform != 'darwin':
            raise ValueError('网页启停当前支持 macOS；Linux 使用现有前台 run。')
        if set(body) != {'action','destination','local_port','server_port','version','confirmation'}:
            raise ValueError('操作字段无效。')
        action = body['action']
        if action not in ('connect','disconnect','reconnect'):
            raise ValueError('不支持此操作。')
        config = tunnel.configuration(body['destination'], body['local_port'], body['server_port'])
        if body['confirmation'] != config['destination']:
            raise ValueError('请确认完整 SSH 目标。')
        port = config['local_port']
        existing = (tunnel.folder(self.root, port)/'config.json').exists()
        current = self.config(port) if existing else None
        if body['version'] != (self.version(current) if current else None):
            raise ValueError('配置已变化，请读取状态后重新确认。')
        if current and current != config:
            raise ValueError('此端口已有另一配置；先断开，再在本机调整配置。')
        if action != 'connect' and not current:
            raise ValueError('仅管理当前仓库已登记的隧道。')
        if port == getattr(self, 'port', 19079):
            raise ValueError('本机管理服务端口不能用作 SSH 转发端口。')
        if action == 'connect' and (tunnel.supervisor_active(self.root, port) or not tunnel.port_free(port)):
            raise ValueError('端口已有连接或被占用；不会接管其他进程。')
        with self.lock:
            if any(j['state']=='running' for j in self.jobs):
                raise ValueError('已有操作执行中，请等待实际结果。')
            job = {'id':secrets.token_hex(12), 'action':action, **config, 'state':'running', 'at':time.time(), 'message':'执行中；断开会中断此端口的访问。'}
            self.jobs.appendleft(job)
            tunnel.save(self.directory/'jobs.json', list(self.jobs))
        def work():
            try:
                # Fixed functions/arguments only. Existing ownership checks remain in force.
                if action in ('disconnect','reconnect'):
                    self.control('stop', self.root, config)
                    deadline=time.monotonic()+15
                    while self.registered(config) or tunnel.supervisor_active(self.root,port) or (action=='reconnect' and not tunnel.port_free(port)):
                        if time.monotonic()>=deadline:
                            raise ValueError('原任务仍在退出或端口被占用，请核对状态后再操作。')
                        time.sleep(.2)
                if action in ('connect','reconnect'):
                    self.control('start', self.root, config)
                result = 'requested' if action != 'disconnect' else 'completed'
                message = '任务已登记，请以 SSH 状态和 HTTP 检查确认连接成功。' if action != 'disconnect' else '本工具的托管任务已停止。'
            except ValueError as error:
                result, message = 'failed', str(error)
            except Exception:
                result, message = 'unconfirmed', '操作结果未确认，请读取实际状态；不会自动重试。'
            with self.lock:
                job.update(state=result, message=message)
                tunnel.save(self.directory/'jobs.json', list(self.jobs))
        threading.Thread(target=work, daemon=True).start()
        return dict(job)


class Server(ThreadingHTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Tokens, request bodies and SSH output never enter HTTP logs.

    def send(self, code, data, content_type='application/json; charset=utf-8'):
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        allowed = self.server.manager.allowed_origins(self.server.server_port)
        origin = self.headers.get('Origin')
        if origin in allowed:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        ancestors = ' '.join(sorted(allowed))
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors "+ancestors+"; base-uri 'none'; form-action 'self'")
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def guard(self, write=False):
        port = self.server.server_port
        if self.headers.get('Host') not in ('127.0.0.1:'+str(port),'localhost:'+str(port)):
            self.send(403, {'error':'host-rejected'}); return False
        origin = self.headers.get('Origin')
        if (write and origin is None) or (origin and origin not in self.server.manager.allowed_origins(port)):
            self.send(403, {'error':'origin-rejected'}); return False
        if self.path.startswith('/api/') and not secrets.compare_digest(self.headers.get('X-XDocker-Local-Token',''), self.server.manager.token):
            self.send(401, {'error':'本机管理令牌未被接受。'}); return False
        return True

    def do_GET(self):
        if not self.guard():return
        path = urlsplit(self.path).path
        if path == '/healthz':
            return self.send(200, {'status':'ok','service':'xdocker-ssh-tunnel-manager'})
        if path == '/api/tunnels':
            try:return self.send(200, self.server.manager.snapshot())
            except (OSError,ValueError):return self.send(503, {'error':'本机状态无法安全读取。'})
        files = {'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
        if path not in files:return self.send(404, {'error':'not-found'})
        file = STATIC/files[path]
        return self.send(200, file.read_bytes(), {'/':'text/html; charset=utf-8','/app.js':'text/javascript; charset=utf-8','/style.css':'text/css; charset=utf-8'}[path])

    def do_POST(self):
        if not self.guard(write=True):return
        if self.path != '/api/tunnels/action':return self.send(404, {'error':'not-found'})
        if self.headers.get('Content-Type') != 'application/json' or self.headers.get('Transfer-Encoding'):
            return self.send(400, {'error':'仅接受 JSON。'})
        try:
            size = int(self.headers.get('Content-Length','0'))
            if not 0 < size <= 4096:raise ValueError('请求大小无效。')
            def unique(items):
                result={}
                for key,value in items:
                    if key in result:raise ValueError('重复 JSON 字段。')
                    result[key]=value
                return result
            body = json.loads(self.rfile.read(size),object_pairs_hook=unique)
            if not isinstance(body, dict):raise ValueError('请求格式无效。')
            return self.send(202, self.server.manager.submit(body))
        except (ValueError, TypeError, KeyError):
            return self.send(409, {'error':'操作或配置校验未通过，请重新读取状态并确认目标、端口及任务归属。'})


def launch_descriptor(root, port):
    root = Path(root).resolve()
    label = 'com.redeployment.xdocker.tunnel-manager.'+hashlib.sha256((str(root)+':'+str(port)).encode()).hexdigest()[:12]
    return {'Label':label, 'ProgramArguments':[sys.executable,str(root/'clients/ssh-tunnel/manager.py'),'run','--port',str(port)],
            'WorkingDirectory':str(root),'RunAtLoad':True,'KeepAlive':{'SuccessfulExit':False},
            'StandardOutPath':'/dev/null','StandardErrorPath':'/dev/null'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init','token','run','start','stop','status'))
    parser.add_argument('--port', type=int, default=19079)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:raise ValueError('无效端口。')
    if args.action == 'init':
        initialize(ROOT);print('已初始化本机私有令牌。使用 token 在自己的终端读取，然后启动本机服务。');return
    if args.action == 'token':
        print(safe_file(ROOT/'data/tunnel-manager/access-token').read_text().strip());return
    if args.action == 'status':
        with __import__('urllib.request',fromlist=['urlopen']).urlopen('http://127.0.0.1:'+str(args.port)+'/healthz',timeout=2) as response:
            print(response.read(4096).decode())
        return
    if args.action in ('start','stop'):
        if sys.platform != 'darwin':raise ValueError('后台托管当前支持 macOS；其他系统使用 run。')
        agent=launch_descriptor(ROOT,args.port)
        target='gui/'+str(os.getuid())+'/'+agent['Label']
        plist=Path.home()/'Library/LaunchAgents'/(agent['Label']+'.plist')
        if plist.exists():
            saved=plistlib.loads(plist.read_bytes()) if not plist.is_symlink() else {}
            if saved.get('ProgramArguments') != agent['ProgramArguments'] or saved.get('WorkingDirectory') != str(ROOT):
                raise ValueError('托管文件不属于本仓库，拒绝操作。')
        running=tunnel.launchctl('print',target).returncode==0
        if running and not plist.exists():raise ValueError('拒绝接管同名任务。')
        if args.action=='stop':
            if running:tunnel.launchctl('bootout',target,check=True)
            plist.unlink(missing_ok=True);print('已停止本机管理服务；现有 SSH 隧道继续由各自监督任务运行。');return
        if len(safe_file(ROOT/'data/tunnel-manager/access-token').read_text().strip())<32:
            raise ValueError('先初始化本机令牌。')
        if running or not tunnel.port_free(args.port):raise ValueError('任务已登记或端口占用。')
        lockfile=safe_file(ROOT/'data/tunnel-manager/manager.lock')
        if lockfile.exists():
            with lockfile.open('r') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise ValueError('已有本机管理器运行，先停止原管理器。') from None
        plist.parent.mkdir(parents=True,exist_ok=True)
        with plist.open('wb') as stream:
            os.fchmod(stream.fileno(),0o600);plistlib.dump(agent,stream)
        tunnel.launchctl('bootstrap','gui/'+str(os.getuid()),str(plist),check=True)
        print('已登记本机 SSH 管理服务，请用 status 核对实际 HTTP 状态。');return
    lock=safe_file(ROOT/'data/tunnel-manager/manager.lock').open('a')
    os.fchmod(lock.fileno(),0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    manager=Manager();manager.port=args.port
    server=Server(('127.0.0.1',args.port),Handler);server.manager=manager
    print('XDocker 本机 SSH 管理：http://127.0.0.1:'+str(args.port),flush=True)
    try:server.serve_forever()
    finally:server.server_close();lock.close()


if __name__ == '__main__':
    try:main()
    except (ValueError,OSError,subprocess.CalledProcessError):
        print('本机管理操作失败：请检查私有配置、端口和托管任务；不会接管其他进程。',file=sys.stderr)
        sys.exit(1)
