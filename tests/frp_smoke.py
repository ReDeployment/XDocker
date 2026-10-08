"""Real FRP authentication, TLS and HTTP routing; optional native Nginx gateway."""
import argparse
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Backend(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/ws":
            key = self.headers["Sec-WebSocket-Key"]
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.wfile.write(b"\x81\x02ok")
            self.wfile.flush()
            self.close_connection = True
            return
        if self.path == "/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            time.sleep(1.5)
            self.close_connection = True
            return
        body = json.dumps({"host": self.headers.get("Host"), "path": self.path,
                           "proto": self.headers.get("X-Forwarded-Proto")}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def smoke(frps, frpc, nginx=None):
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    with tempfile.TemporaryDirectory(prefix="xdocker-frp-smoke-") as directory:
        root = Path(directory)
        for name in ("scripts", "services", "clients", "sites", "nginx"):
            shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns(".env"))
        shutil.copy(ROOT / ".env.example", root / ".env.example")
        shutil.copy(ROOT / "compose.yml", root / "compose.yml")
        subprocess.run(["sh", "scripts/frp.sh", "init"], cwd=root, env=env, check=True, capture_output=True)
        control, vhost, gateway = port(), port(), port()
        config = root / "data/frp/frps.ini"
        text = config.read_text().replace("bind_port = 7000", f"bind_addr = 127.0.0.1\nbind_port = {control}")
        text = text.replace("vhost_http_port = 8080", f"vhost_http_port = {vhost}")
        text = text.replace("/etc/frp/", str(root / "data/frp") + "/")
        config.write_text(text)
        subprocess.run([frps, "verify", "-c", str(config)], check=True, capture_output=True)
        backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        backend.daemon_threads = True
        threading.Thread(target=backend.serve_forever, daemon=True).start()
        processes = []
        logs = []

        def start(command, label):
            log = (root / (label + ".log")).open("wb")
            logs.append(log)
            process = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=log)
            processes.append(process)
            return process

        try:
            start([frps, "-c", str(config)], "server")
            token = (root / "data/frp/token").read_text().strip()
            client = root / "client.ini"
            common = (f"[common]\nserver_addr=127.0.0.1\nserver_port={control}\ntoken={token}\n"
                      f"tls_enable=true\ntls_server_name=xdocker-frps\ntls_trusted_ca_file={root}/data/frp/server.crt\nlogin_fail_exit=true\n")
            proxies = "".join(f"\n[{name}]\ntype=http\nlocal_ip=127.0.0.1\nlocal_port={backend.server_port}\ncustom_domains={name}.example.test\n" for name in ("one", "two"))
            client.write_text(common + proxies)
            time.sleep(.3)
            start([frpc, "-c", str(client)], "client")
            for attempt in range(60):
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", vhost, timeout=.5)
                    connection.request("GET", "/ready", headers={"Host": "one.example.test"})
                    response = connection.getresponse()
                    if response.status == 200:
                        assert json.loads(response.read())["path"] == "/ready"
                        connection.close()
                        break
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(.1)
            else:
                raise AssertionError("Authenticated FRP tunnel did not become ready")
            for name in ("one", "two"):
                connection = http.client.HTTPConnection("127.0.0.1", vhost, timeout=3)
                connection.request("GET", "/probe?value=1", headers={"Host": name + ".example.test"})
                response = connection.getresponse()
                data = json.loads(response.read())
                assert response.status == 200 and data["host"] == name + ".example.test" and data["path"] == "/probe?value=1"
                connection.close()
            for label, invalid in (("wrong-token", common.replace(token, "invalid-token")),
                                   ("wrong-identity", common.replace("tls_server_name=xdocker-frps", "tls_server_name=wrong-server")),
                                   ("plaintext", f"[common]\nserver_addr=127.0.0.1\nserver_port={control}\ntoken={token}\ntls_enable=false\nlogin_fail_exit=true\n")):
                path = root / (label + ".ini")
                path.write_text(invalid + f"\n[rejected]\ntype=http\nlocal_ip=127.0.0.1\nlocal_port={backend.server_port}\ncustom_domains=rejected.example.test\n")
                rejected = subprocess.run([frpc, "-c", str(path)], env=env, capture_output=True, timeout=12)
                output = (rejected.stdout + rejected.stderr).decode(errors="replace").lower()
                # FRPC 0.52.3 logs a failed login but can exit with code zero.
                # This version's mux layer can report TLS verification failures
                # as "session shutdown" rather than expose the x509 error.
                expected = {"wrong-token": "token", "wrong-identity": "login to server failed", "plaintext": "login to server failed"}
                assert expected[label] in output, label + ": " + output.replace(token, "<redacted>")[-1600:]
                connection = http.client.HTTPConnection("127.0.0.1", vhost, timeout=3)
                connection.request("GET", "/probe", headers={"Host": "rejected.example.test"})
                response = connection.getresponse()
                assert response.status == 404, label
                response.read(); connection.close()
            print("FRP: TLS verified; wrong token, wrong server identity and plaintext rejected; multi-host routing passed")
            if nginx:
                server = (ROOT / "nginx/frp-http.conf.template").read_text().replace("__DOMAIN__", "one.example.test two.example.test")
                server = server.replace("listen 80;", f"listen 127.0.0.1:{gateway};").replace("http://frps:8080", f"http://127.0.0.1:{vhost}")
                headers = (ROOT / "nginx/frp-headers.conf").read_text()
                nginx_config = root / "nginx.conf"
                nginx_config.write_text(f"pid {root}/nginx.pid; error_log {root}/nginx-error.log; events {{}} http {{ access_log off; {headers} {server} }}")
                subprocess.run([nginx, "-t", "-p", str(root), "-c", str(nginx_config)], check=True, capture_output=True)
                start([nginx, "-p", str(root), "-c", str(nginx_config), "-g", "daemon off;"], "gateway")
                time.sleep(.2)
                connection = http.client.HTTPConnection("127.0.0.1", gateway, timeout=3)
                connection.request("GET", "/probe?x=1", headers={"Host": "two.example.test"})
                response = connection.getresponse()
                data = json.loads(response.read())
                assert data == {"host": "two.example.test", "path": "/probe?x=1", "proto": "http"}
                connection.close()
                connection = http.client.HTTPConnection("127.0.0.1", gateway, timeout=3)
                started = time.monotonic()
                connection.request("GET", "/sse", headers={"Host": "one.example.test"})
                response = connection.getresponse()
                assert response.readline() == b"data: first\n" and time.monotonic() - started < 1.2
                connection.close()
                with socket.create_connection(("127.0.0.1", gateway), timeout=3) as sock:
                    sock.sendall(b"GET /ws HTTP/1.1\r\nHost: one.example.test\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
                    received = b""
                    while b"\x81\x02ok" not in received:
                        chunk = sock.recv(4096)
                        assert chunk
                        received += chunk
                    assert received.startswith(b"HTTP/1.1 101")
                print("Nginx + FRP: Host/path/header preservation, SSE streaming and WebSocket upgrade passed")
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired: process.kill(); process.wait()
            for log in logs: log.close()
            backend.shutdown(); backend.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--frps", required=True)
    parser.add_argument("--frpc", required=True)
    parser.add_argument("--nginx")
    args = parser.parse_args()
    smoke(args.frps, args.frpc, args.nginx)
