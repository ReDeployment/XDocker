"""Small, bounded Unix-socket Docker API client; never accepts caller API paths."""
import http.client
import json
import socket
import struct


class DockerError(Exception):
    pass


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
