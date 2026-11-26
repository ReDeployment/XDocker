"""Small, bounded Unix-socket Docker API client; never accepts caller API paths."""
import http.client
import json
from pathlib import PurePosixPath
import re
import socket
import struct


class DockerError(Exception):
    pass


def job_log_config(settings):
    return {'Type': 'json-file', 'Config': {
        'max-size': settings.get('XDOCKER_LOG_MAX_SIZE') or '20971520',
        'max-file': settings.get('XDOCKER_LOG_MAX_FILE') or '5',
        'compress': 'true',
    }}


def compose_log_owner(labels, host_root):
    """Match XDocker launch paths as recorded by Compose on the Docker host."""
    if not host_root or not PurePosixPath(host_root).is_absolute():
        return False
    root = PurePosixPath(host_root)
    project = labels.get('com.docker.compose.project', '')
    service = labels.get('com.docker.compose.service', '')
    directory = labels.get('com.docker.compose.project.working_dir', '')
    files = labels.get('com.docker.compose.project.config_files', '').split(',')
    if directory == str(root) and files[0] == str(root / 'compose.yml'):
        # Hosting combines the root project with enabled site/service fragments.
        return all(re.fullmatch(re.escape(str(root)) + r'/(sites|services)/[a-z0-9-]+/compose\.yml', f)
                   for f in files[1:])
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', project):
        return False
    if service in {'init', 'admin-init', 'migrate'}:
        return False
    checkout = root / 'data/standalone' / project
    # Home bundles use either the instance directory or the application's
    # Compose directory as working_dir. Accept only their own optional overlay.
    for deployment in (root, checkout):
        compose = PurePosixPath(files[0])
        app = compose.parent
        if app.parent != deployment / 'apps' or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', app.name):
            continue
        if files[0] != str(app / 'compose.yml') or files[1:] not in ([], [str(app / 'container-logging.override.yml')]):
            continue
        if directory in (str(deployment / 'instances' / project), str(app)):
            return True
    return False


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=15):
        super().__init__('localhost', timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class Docker:
    def __init__(self, path='/var/run/docker.sock'):
        self.path = path

    def request(self, method, path, body=None, raw=False, timeout=15):
        connection = UnixHTTP(self.path, timeout)
        try:
            payload = None if body is None else json.dumps(body).encode()
            connection.request(method, '/v1.44' + path, payload, {'Content-Type': 'application/json'})
            response = connection.getresponse()
            data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise DockerError('Docker response exceeds size limit')
            if response.status not in (200, 201, 204, 304):
                # Avoid echoing Docker errors that may contain paths or credentials.
                raise DockerError(f'Docker API returned HTTP {response.status}')
            return data if raw else (json.loads(data) if data else {})
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise DockerError('Docker API unavailable or invalid response') from error
        finally:
            connection.close()


def log_text(raw):
    """Decode Docker multiplexed stdout/stderr, or plain TTY log bytes."""
    chunks = []
    pos = 0
    while len(raw) - pos >= 8 and raw[pos] in (0, 1, 2) and raw[pos+1:pos+4] == b'\0\0\0':
        size = struct.unpack('>I', raw[pos+4:pos+8])[0]
        if pos + 8 + size > len(raw):
            break
        chunks.append(raw[pos+8:pos+8+size])
        pos += 8 + size
    if pos < len(raw):
        chunks.append(raw[pos:])
    return b''.join(chunks).decode('utf-8', errors='replace')
