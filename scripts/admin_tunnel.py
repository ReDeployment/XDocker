"""XDocker 私有控制台 SSH 隧道；不读取令牌或私钥，不接管已有连接。"""
import argparse
from collections import deque
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = re.compile(r'^(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9_.-]*$')
MESSAGES = {
    'connecting':'正在建立 SSH 隧道。', 'connected':'管理接口可访问。',
    'reconnecting':'SSH 连接断开，等待自动重连。',
    'port-conflict':'本机端口被占用；不会接管或停止占用进程。',
    'host-key-error':'SSH 主机身份验证失败，请人工核对 known_hosts。',
    'authentication-error':'SSH 认证失败，请在终端验证同一主机的现有密钥认证。',
    'service-error':'管理 HTTP 服务返回错误；SSH 进程仍在运行。',
    'api-unreachable':'管理接口不可达；请检查转发目标和服务器服务。',
    'unexpected-response':'目标未返回预期的管理健康响应。',
    'stopped':'本工具管理的连接已停止。', 'not-started':'尚未启动此端口的托管连接。',
}


def configuration(destination, local_port=19080, server_port=9080):
    if not isinstance(destination, str) or not DESTINATION.fullmatch(destination):
        raise ValueError('请使用 SSH 主机别名、IPv4 或域名，可带用户名；不支持命令或选项。')
    for port in (local_port, server_port):
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError('端口必须为 1–65535 的整数。')
    return {'destination':destination, 'local_port':local_port, 'server_port':server_port}


def folder(root, port):
    configuration('check', port)
    return Path(root)/'data/admin-tunnel'/str(port)


def private_directory(directory):
    directory = Path(directory)
    for parent in (directory.parent.parent, directory.parent, directory):
        if parent.is_symlink():
            raise ValueError('拒绝使用符号链接作为隧道状态目录。')
        parent.mkdir(exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    directory.parent.chmod(0o700)


def save(path, data):
    path = Path(path)
    private_directory(path.parent)
    if path.is_symlink():
        raise ValueError('拒绝使用符号链接作为隧道状态目录或文件。')
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name+'.')
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def supervisor_active(root, port):
    path = folder(root, port)/'supervisor.lock'
    if not path.exists():return False
    if path.is_symlink():raise ValueError('拒绝符号链接锁文件。')
    with path.open('r') as lock:
        try:fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:return True
    return False


def port_free(port):
    with socket.socket() as sock:
        # Closed probe connections may remain in TIME_WAIT; they are not an active listener.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def ssh_command(config, binary):
    return [str(binary), '-N', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
            '-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'RemoteCommand=none',
            '-o', 'ForkAfterAuthentication=no',
            '-o', 'LogLevel=ERROR', '-L',
            '127.0.0.1:'+str(config['local_port'])+':127.0.0.1:'+str(config['server_port']),
            config['destination']]


def ssh_failure(stderr):
    text = stderr.lower()
    if any(s in text for s in ('host key verification failed','remote host identification has changed')):
        return 'host-key-error'
    if any(s in text for s in ('permission denied','too many authentication failures','no supported authentication methods')):
        return 'authentication-error'
    if any(s in text for s in ('address already in use','cannot listen to port','could not request local forwarding')):
        return 'port-conflict'
    return 'reconnecting'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def health(port):
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with client.open('http://127.0.0.1:'+str(port)+'/healthz', timeout=2) as response:
            data = response.read(4097)
            if len(data) <= 4096 and json.loads(data).get('status') == 'ok':
                return 'connected'
            return 'unexpected-response'
    except urllib.error.HTTPError as error:
        return 'service-error' if error.code >= 500 else 'unexpected-response'
    except (OSError, urllib.error.URLError):
        return 'api-unreachable'
    except (ValueError, AttributeError):
        return 'unexpected-response'


def retry_delay(attempt):
    return min(60, 5 * 2**min(attempt, 4))


def supervise(root, config, binary, stop=None, poll_interval=2):
    stop = stop or threading.Event()
    directory = folder(root, config['local_port'])
    private_directory(directory)
    lock_path = directory/'supervisor.lock'
    if lock_path.is_symlink():
        raise ValueError('拒绝符号链接锁文件。')
    with lock_path.open('a') as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('此端口已有 XDocker 隧道托管进程。') from None
        save(directory/'config.json', config)

        def state(code, **values):
            save(directory/'status.json', {'state':code, 'message':MESSAGES[code],
                 'at':datetime.now(timezone.utc).isoformat(), 'supervisor_pid':os.getpid(), **values})

        process = None
        terminal_state = False
        final_state = 'stopped'
        attempt = 0
        try:
            while not stop.is_set():
                if not port_free(config['local_port']):
                    state('port-conflict');terminal_state=True;final_state='port-conflict';break
                state('connecting')
                errors = deque(maxlen=20)
                process = subprocess.Popen(ssh_command(config, binary), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors='replace')
                def drain(stream):
                    while True:
                        line = stream.readline(1024)
                        if not line:break
                        errors.append(line)
                    stream.close()
                reader = threading.Thread(target=drain, args=(process.stderr,), daemon=True)
                reader.start()
                started = time.monotonic()
                while process.poll() is None and not stop.is_set():
                    state(health(config['local_port']), ssh_pid=process.pid)
                    stop.wait(poll_interval)
                if stop.is_set():
                    break
                reader.join(timeout=1)
                failure = ssh_failure(''.join(errors))
                if failure != 'reconnecting':
                    state(failure);terminal_state=True;final_state=failure;break
                if time.monotonic()-started >= 30:
                    attempt = 0
                delay = retry_delay(attempt)
                state('reconnecting', retry_seconds=delay)
                stop.wait(delay)
                attempt += 1
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()
            if not terminal_state:
                state('stopped')
            # 不持久化 SSH 原始 stderr；只保存预定义诊断，避免代理输出泄露凭据。
        return final_state


def descriptor(root, config, python=sys.executable):
    identity = hashlib.sha256((str(Path(root).resolve())+':'+str(config['local_port'])).encode()).hexdigest()[:12]
    root = Path(root).resolve()
    directory = folder(root, config['local_port'])
    return {'Label':'com.redeployment.xdocker.admin-tunnel.'+identity,
            'ProgramArguments':[str(Path(python).resolve()), str(Path(root)/'scripts/admin_tunnel.py'),
                                'supervise', '--config', str(directory/'config.json')],
            'WorkingDirectory':str(root), 'RunAtLoad':True,
            'KeepAlive':{'SuccessfulExit':False}, 'ThrottleInterval':30,
            'StandardOutPath':'/dev/null', 'StandardErrorPath':'/dev/null'}


def launchctl(*args, check=False):
    return subprocess.run(['launchctl', *args], capture_output=True, text=True, check=check)


def manage(action, root, config, home=Path.home()):
    if sys.platform != 'darwin':
        raise ValueError('start/stop 托管当前仅支持 macOS；其他 POSIX 系统请使用 run 前台模式。')
    root = Path(root).resolve()
    agent = descriptor(root, config)
    target = 'gui/'+str(os.getuid())+'/'+agent['Label']
    plist = Path(home)/'Library/LaunchAgents'/(agent['Label']+'.plist')
    running = launchctl('print', target).returncode == 0
    if plist.exists():
        if plist.is_symlink():raise ValueError('拒绝符号链接托管文件。')
        saved = plistlib.loads(plist.read_bytes())
        if saved.get('ProgramArguments') != agent['ProgramArguments'] or saved.get('WorkingDirectory') != str(root):
            raise ValueError('此托管任务不属于当前 XDocker 配置，拒绝操作。')
    elif running:
        raise ValueError('存在未由本工具登记的同名任务，拒绝接管。')
    if action == 'stop':
        if not running and supervisor_active(root, config['local_port']):
            raise ValueError('当前连接由前台 run 托管，请在其终端按 Ctrl+C 停止；不会按 PID 杀进程。')
        if running:launchctl('bootout', target, check=True)
        if plist.exists():plist.unlink()
        print('已停止本工具的托管任务；未操作其他 SSH 或 FRPC。')
        return
    if running:
        raise ValueError('托管任务已登记；请先查看 status，修改目标时先 stop。')
    if supervisor_active(root, config['local_port']):
        raise ValueError('此端口已有前台托管进程，请在原终端停止后再 start。')
    if not port_free(config['local_port']):
        raise ValueError(MESSAGES['port-conflict'])
    save(folder(root, config['local_port'])/'config.json', config)
    plist.parent.mkdir(parents=True, exist_ok=True)
    with plist.open('wb') as stream:
        os.fchmod(stream.fileno(),0o600);plistlib.dump(agent,stream)
    launchctl('bootstrap','gui/'+str(os.getuid()),str(plist),check=True)
    print('已登记本机自动重连任务。请运行 status 查看实际 SSH/HTTP 状态。')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('start','run','status','stop','supervise'))
    parser.add_argument('destination',nargs='?')
    parser.add_argument('--local-port',type=int,default=19080)
    parser.add_argument('--server-port',type=int,default=9080)
    parser.add_argument('--config',type=Path)
    args=parser.parse_args()
    configuration('check',args.local_port,args.server_port)
    if args.action=='supervise':
        if args.config is None:raise ValueError('缺少托管配置。')
        if args.config.is_symlink():raise ValueError('拒绝符号链接配置文件。')
        stored=json.loads(args.config.read_text());config=configuration(**stored)
        if args.config.resolve()!= (folder(ROOT,config['local_port'])/'config.json').resolve():
            raise ValueError('托管配置不属于当前 XDocker。')
    elif args.action in ('status','stop'):
        file=folder(ROOT,args.local_port)/'config.json'
        if not file.is_file():
            print(json.dumps({'state':'not-started','message':MESSAGES['not-started']},ensure_ascii=False));return
        config=configuration(**json.loads(file.read_text()))
        if config['local_port']!=args.local_port:
            raise ValueError('配置端口与当前状态目录不匹配，拒绝操作。')
    else:
        config=configuration(args.destination,args.local_port,args.server_port)
    if args.action=='status':
        file=folder(ROOT,config['local_port'])/'status.json'
        status=json.loads(file.read_text()) if file.is_file() else {'state':'not-started'}
        status.update(url='http://127.0.0.1:'+str(config['local_port']),current_http=health(config['local_port']))
        status['supervisor_active']=supervisor_active(ROOT,config['local_port'])
        if sys.platform=='darwin':
            registered=launchctl('print','gui/'+str(os.getuid())+'/'+descriptor(ROOT,config)['Label']).returncode==0
            status['registered']=registered
        if not status['supervisor_active'] and status.get('state') in ('connecting','connected','reconnecting','api-unreachable','service-error','unexpected-response'):
            status.update(state='stopped',message=MESSAGES['stopped'])
        print(json.dumps(status,ensure_ascii=False,indent=2));return
    if args.action in ('start','stop'):
        manage(args.action,ROOT,config);return
    stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    binary=shutil.which('ssh')
    if not binary:raise ValueError('系统 SSH 客户端不可用。')
    result=supervise(ROOT,config,binary,stop)
    if args.action=='run':
        print(MESSAGES[result])
        if result!='stopped':sys.exit(1)


if __name__=='__main__':
    try:main()
    except ValueError as error:
        print('隧道操作失败：'+str(error),file=sys.stderr)
        sys.exit(1)
    except (OSError,subprocess.CalledProcessError,TypeError):
        print('隧道操作失败：请检查参数、端口占用、任务身份或系统 SSH；不会修改现有连接。',file=sys.stderr)
        sys.exit(1)
