"""Exercise the PowerX Nginx templates against isolated loopback upstreams."""

from contextlib import closing
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
NGINX = shutil.which("nginx")
OPENSSL = shutil.which("openssl")


class UpstreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "upstream": self.server.name,
            "path": self.path,
            "upgrade": self.headers.get("Upgrade"),
            "connection": self.headers.get("Connection"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


@unittest.skipUnless(NGINX and OPENSSL, "requires Nginx and OpenSSL")
class PowerXGatewayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xdocker-gateway-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.frontend = self.start_upstream("frontend")
        self.backend = self.start_upstream("backend")

    def start_upstream(self, name):
        server = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        server.name = name
        thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"127.0.0.1:{server.server_port}"

    def loopback_port(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def start_gateway(self, template, tls=False):
        http_port = self.loopback_port()
        config = (ROOT / template).read_text()
        standalone = template.startswith("apps/")
        frontend = "web-admin:3000" if standalone else "__INSTANCE__-web:3000"
        backend = "backend:8080" if standalone else "__INSTANCE__-backend:8080"
        config = (config.replace("listen 80;", f"listen 127.0.0.1:{http_port};")
                  .replace("__DOMAIN__", "powerx.example.test")
                  .replace(f"http://{frontend}", f"http://{self.frontend}")
                  .replace(f"http://{backend}", f"http://{self.backend}"))
        if tls:
            certificate = self.root / "certificate.pem"
            key = self.root / "private-key.pem"
            subprocess.run([
                OPENSSL, "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "1",
                "-subj", "/CN=powerx.example.test", "-addext", "subjectAltName=IP:127.0.0.1",
                "-out", str(certificate), "-keyout", str(key),
            ], check=True, capture_output=True, timeout=10)
            tls_port = self.loopback_port()
            self.ssl_context = ssl.create_default_context(cafile=str(certificate))
            config = (config.replace("listen 443 ssl;", f"listen 127.0.0.1:{tls_port} ssl;")
                      .replace("/etc/letsencrypt/live/__CERT_NAME__/fullchain.pem", str(certificate))
                      .replace("/etc/letsencrypt/live/__CERT_NAME__/privkey.pem", str(key)))
        if not standalone:
            config = 'map $http_upgrade $powerx_connection_upgrade { default upgrade; "" close; }\n' + config

        path = self.root / "nginx.conf"
        temp_paths = "\n".join(f"    {name}_temp_path {name}_temp;"
                               for name in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
        path.write_text(
            "pid nginx.pid;\nerror_log stderr;\nevents { worker_connections 64; }\n"
            f"http {{\n    access_log off;\n{temp_paths}\n{config}\n}}\n"
        )
        log = self.root / "nginx.log"
        with log.open("w") as output:
            process = subprocess.Popen(
                [NGINX, "-p", str(self.root) + "/", "-c", str(path), "-g", "daemon off;"],
                stdout=output, stderr=subprocess.STDOUT,
            )
        self.addCleanup(self.stop_gateway, process)
        for _ in range(100):
            if process.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", http_port), timeout=0.2):
                    return (http_port, tls_port) if tls else http_port
            except OSError:
                time.sleep(0.02)
        self.fail(log.read_text())

    def stop_gateway(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def request(self, port, path, tls=False, upgrade=None):
        if tls:
            conn = HTTPSConnection("127.0.0.1", port, context=self.ssl_context, timeout=3)
        else:
            conn = HTTPConnection("127.0.0.1", port, timeout=3)
        with closing(conn):
            headers = {"Host": "powerx.example.test"}
            if upgrade:
                headers["Upgrade"] = upgrade
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            return response.status, response.getheader("Location"), response.read()

    def assert_routes(self, port, tls=False):
        cases = {
            "/api/_nuxt_icon/lucide.json?icons=eye,plus": "frontend",
            "/api/_nuxt_icon/heroicons.json?icons=eye": "frontend",
            "/_nuxt/app.css": "frontend",
            "/customers": "frontend",
            "/api/v1/health": "backend",
            "/api/_nuxt_icon_other/status": "backend",
            "/healthz": "backend",
            "/media/example.png": "backend",
            "/ws": "backend",
        }
        for path, upstream in cases.items():
            with self.subTest(path=path):
                upgrade = "websocket" if path == "/ws" else None
                status, _, response = self.request(port, path, tls, upgrade)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(response), {
                    "upstream": upstream, "path": path, "upgrade": upgrade,
                    "connection": "upgrade" if upgrade else "close",
                })

    def test_instance_gateway(self):
        self.assert_routes(self.start_gateway("apps/powerx/gateway.conf"))

    def test_shared_http_gateway(self):
        self.assert_routes(self.start_gateway("nginx/powerx-http.conf.template"))

    def test_shared_https_gateway(self):
        http_port, tls_port = self.start_gateway("nginx/powerx-https.conf.template", tls=True)
        status, redirect, _ = self.request(http_port, "/api/_nuxt_icon/lucide.json?icons=eye")
        self.assertEqual((status, redirect),
                         (301, "https://powerx.example.test/api/_nuxt_icon/lucide.json?icons=eye"))
        self.assert_routes(tls_port, tls=True)
