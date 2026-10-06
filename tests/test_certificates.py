import contextlib
import builtins
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("certificates", ROOT / "scripts/certificates.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)
UTC = timezone.utc
REACHABLE = [{"url": "https://acme.example/directory", "status": "REACHABLE"}]


class CertificateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xdocker-certificates-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_dir = self.root / "letsencrypt"
        self.registry = self.root / "inventory.json"
        self.data = {"version": 1, "warning_days": 30, "critical_days": 7,
                     "acme_directory": "https://acme.example/directory",
                     "certificates": [{"name": "docs.example.com", "domains": ["docs.example.com"], "enabled": True}]}
        self.registry.write_text(json.dumps(self.data))
        self.key = ec.generate_private_key(ec.SECP256R1())

    def grouped(self):
        self.data["certificates"] = [{"name": "debug-ecommerce.example.com",
                                     "domains": ["debug-ecommerce.example.com", "debug-scrm.example.com", "debug.example.com"]}]
        self.registry.write_text(json.dumps(self.data))

    def certificate(self, days=90, authenticator="webroot", installer="None", domains=None, before=-90):
        item = self.data["certificates"][0]
        domains = domains if domains is not None else item["domains"]
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domains[0])])
        now = datetime.now(UTC)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(self.key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now + timedelta(days=before))
                .not_valid_after(now + timedelta(days=days))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain) for domain in domains]), critical=False)
                .sign(self.key, hashes.SHA256()))
        folder = self.config_dir / "live" / item["name"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "fullchain.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (folder / "privkey.pem").write_bytes(self.key.private_bytes(serialization.Encoding.PEM,
                                                                  serialization.PrivateFormat.PKCS8,
                                                                  serialization.NoEncryption()))
        renewal = self.config_dir / "renewal"
        renewal.mkdir(exist_ok=True)
        (renewal / (item["name"] + ".conf")).write_text(
            "version = 5.8.0\n[renewalparams]\n" + f"authenticator = {authenticator}\ninstaller = {installer}\nserver = https://acme.example/directory\n")
        return cert

    def invoke(self, action, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        args = [action, "--inventory", str(self.registry), "--config-dir", str(self.config_dir), "--json", *extra]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = tool.main(args)
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else {"error": stderr.getvalue()}

    def test_inventory_preserves_eight_lineages_and_ten_domains(self):
        data = tool.load_inventory(ROOT / "certbot/certificates.json")
        self.assertEqual(len(data["certificates"]), 8)
        self.assertEqual(sum(len(item["domains"]) for item in data["certificates"]), 10)
        debug = next(item for item in data["certificates"] if item["name"] == "debug-ecommerce.artisan-cloud.com")
        self.assertEqual(len(debug["domains"]), 3)

    def test_acme_only_config_excludes_active_sites_and_its_previous_output(self):
        data = tool.load_inventory(ROOT / "certbot/certificates.json")
        nginx = self.root / "nginx"
        nginx.mkdir()
        (nginx / "powerxdoc.conf").write_text("server {\n server_name powerx-doc.artisan-cloud.com;\n}\n")
        (nginx / "10-certificates-acme.conf").write_text("server_name artisan-cloud.com;")
        rendered = tool.acme_configuration(data["certificates"], nginx)
        self.assertNotIn("powerx-doc.artisan-cloud.com", rendered)
        self.assertIn("artisan-cloud.com", rendered)
        self.assertIn("debug-scrm.artisan-cloud.com", rendered)
        self.assertNotIn("proxy_pass", rendered)
        self.assertIn("return 404", rendered)

    def test_require_usable_accepts_critical_but_refuses_expired(self):
        self.certificate(days=5)
        self.assertEqual(self.invoke("check", "--require-usable")[0], 0)
        self.certificate(days=-2)
        self.assertEqual(self.invoke("check", "--require-usable")[0], 1)

    def test_expiry_policy_and_key_validation(self):
        for days, status, expected_code in [(90, "LOCAL_VALID", 0), (20, "EXPIRING", 2), (5, "CRITICAL", 1), (-2, "EXPIRED", 1)]:
            with self.subTest(days=days):
                self.certificate(days=days)
                code, report = self.invoke("check")
                self.assertEqual((code, report["results"][0]["status"]), (expected_code, status))
        other = ec.generate_private_key(ec.SECP256R1())
        key_path = self.config_dir / "live/docs.example.com/privkey.pem"
        key_path.write_bytes(other.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        code, report = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertIn("mismatch", report["results"][0]["error"])

    def test_missing_certificate_is_a_failure(self):
        code, report = self.invoke("check")
        self.assertEqual((code, report["results"][0]["status"]), (1, "ERROR"))

    def test_legacy_host_openssl_fallback(self):
        self.grouped()
        self.certificate()
        original_import = builtins.__import__
        def without_crypto(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ImportError("Test legacy host without cryptography")
            return original_import(name, *args, **kwargs)
        with patch.object(builtins, "__import__", side_effect=without_crypto):
            code, report = self.invoke("check")
        self.assertEqual((code, report["results"][0]["status"]), (0, "LOCAL_VALID"))

    def test_network_failure_never_invokes_certbot(self):
        self.certificate(days=5)
        unreachable = [{"url": "https://acme.example/directory", "status": "UNREACHABLE", "error": "timed out"}]
        with patch.object(tool, "network_preflight", return_value=unreachable), patch.object(tool.subprocess, "run") as run:
            code, report = self.invoke("renew")
        self.assertEqual((code, report["results"][0]["status"]), (1, "BLOCKED_NETWORK"))
        run.assert_not_called()

    def test_host_renewal_preserves_saved_nginx_and_san_group(self):
        self.grouped()
        self.certificate(authenticator="nginx", installer="nginx")
        with patch.object(tool, "network_preflight", return_value=REACHABLE), patch.object(tool.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            code, _ = self.invoke("renew-test", "debug-ecommerce.example.com")
        self.assertEqual(code, 0)
        command = run.call_args.args[0]
        self.assertIn("--dry-run", command)
        self.assertEqual(command[command.index("--cert-name") + 1], "debug-ecommerce.example.com")
        self.assertNotIn("--webroot", command)
        self.assertNotIn("-d", command)

    def test_container_refuses_legacy_nginx_renewal(self):
        self.certificate(authenticator="nginx", installer="nginx")
        with patch.object(tool, "network_preflight", return_value=REACHABLE), patch.object(tool.subprocess, "run") as run:
            code, report = self.invoke("renew", "--runtime", "container")
        self.assertEqual((code, report["results"][0]["status"]), (1, "REFUSED"))
        run.assert_not_called()

    def test_san_mismatch_refuses_renewal(self):
        self.grouped()
        self.certificate(domains=["debug-ecommerce.example.com"])
        with patch.object(tool, "network_preflight", return_value=REACHABLE), patch.object(tool.subprocess, "run") as run:
            code, report = self.invoke("renew")
        self.assertEqual((code, report["results"][0]["status"]), (1, "REFUSED"))
        run.assert_not_called()

    def test_group_issuance_passes_every_domain_without_force(self):
        self.grouped()
        with patch.object(tool, "network_preflight", return_value=REACHABLE), patch.object(tool.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            code, report = self.invoke("issue-test", "debug-ecommerce.example.com", "--email", "operator@example.com")
        self.assertEqual((code, report["results"][0]["status"]), (0, "DRY_RUN_OK"))
        command = run.call_args.args[0]
        domains = [command[i + 1] for i, value in enumerate(command) if value == "-d"]
        self.assertEqual(domains, self.data["certificates"][0]["domains"])
        self.assertNotIn("--force-renewal", command)
        self.assertIn("--dry-run", command)

    def test_new_issuance_writes_reload_event(self):
        def issued(*args, **kwargs):
            self.certificate()
            return SimpleNamespace(returncode=0)
        event = self.root / "events/reload"
        with patch.object(tool, "network_preflight", return_value=REACHABLE), patch.object(tool.subprocess, "run", side_effect=issued):
            code, report = self.invoke("issue", "docs.example.com", "--email", "operator@example.com", "--runtime", "container", "--reload", "container-event", "--reload-event", str(event))
        self.assertEqual((code, report["results"][0]["status"]), (0, "ISSUED"))
        self.assertTrue(event.exists())

    def test_noop_renewal_does_not_reload_or_claim_renewed(self):
        self.certificate()
        event = self.root / "events/reload"
        with patch.object(tool, "network_preflight", return_value=REACHABLE), patch.object(tool.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            code, report = self.invoke("renew", "--reload", "container-event", "--reload-event", str(event))
        self.assertEqual((code, report["results"][0]["status"]), (0, "UNCHANGED"))
        self.assertFalse(event.exists())

    def test_success_exit_with_expired_certificate_is_not_success(self):
        self.certificate(days=-2)
        with patch.object(tool, "network_preflight", return_value=REACHABLE), patch.object(tool.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            code, report = self.invoke("renew")
        self.assertEqual((code, report["results"][0]["status"]), (1, "FAILED"))

    def test_unchanged_critical_certificate_is_nonzero(self):
        self.certificate(days=5)
        with patch.object(tool, "network_preflight", return_value=REACHABLE), patch.object(tool.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            code, report = self.invoke("renew")
        self.assertEqual((code, report["results"][0]["status"]), (1, "UNCHANGED"))
        self.assertEqual(report["results"][0]["certificate_status"], "CRITICAL")

    def test_actual_changed_leaf_triggers_reload_event(self):
        self.certificate(days=20)
        event = self.root / "events/reload"
        def renewed(*args, **kwargs):
            self.certificate(days=90)
            return SimpleNamespace(returncode=0)
        with patch.object(tool, "network_preflight", return_value=REACHABLE), patch.object(tool.subprocess, "run", side_effect=renewed):
            code, report = self.invoke("renew", "--reload", "container-event", "--reload-event", str(event))
        self.assertEqual((code, report["results"][0]["status"]), (0, "RENEWED"))
        self.assertEqual(report["reload"], "EVENT_WRITTEN")
        self.assertTrue(event.exists())

    def test_nginx_validation_failure_never_reloads_host(self):
        self.certificate(days=20, authenticator="nginx", installer="nginx")
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if command[:2] == ["nginx", "-t"]:
                raise subprocess.CalledProcessError(1, command)
            self.certificate(days=90, authenticator="nginx", installer="nginx")
            return SimpleNamespace(returncode=0)
        with patch.object(tool, "network_preflight", return_value=REACHABLE), patch.object(tool.subprocess, "run", side_effect=run):
            code, report = self.invoke("renew", "--reload", "host-nginx")
        self.assertEqual(code, 1)
        self.assertTrue(report["reload"].startswith("FAILED:"))
        self.assertNotIn(["systemctl", "reload", "nginx"], commands)

    def test_verify_covers_every_san_domain(self):
        self.grouped()
        def verified(domain, *args):
            return {"domain": domain, "status": "TLS_VALID"}
        with patch.object(tool, "verify_domain", side_effect=verified) as verify:
            code, report = self.invoke("verify")
        self.assertEqual(code, 0)
        self.assertEqual([row["domain"] for row in report["results"]], self.data["certificates"][0]["domains"])
        self.assertEqual(verify.call_count, 3)

    def test_real_tls_and_stale_leaf_detection(self):
        cert = self.certificate()
        folder = self.config_dir / "live/docs.example.com"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(folder / "fullchain.pem", folder / "privkey.pem")
        trusted = ssl.create_default_context(cafile=str(folder / "fullchain.pem"))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0)); listener.listen(); listener.settimeout(3)
        self.addCleanup(listener.close)
        def serve():
            for _ in range(2):
                raw, _ = listener.accept()
                with context.wrap_socket(raw, server_side=True) as secure:
                    secure.settimeout(2)
                    secure.recv(1)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        connect = socket.create_connection
        def local_connect(address, timeout):
            return connect(("127.0.0.1", listener.getsockname()[1]), timeout=timeout)
        fingerprint = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
        with patch.object(tool.ssl, "create_default_context", return_value=trusted), patch.object(tool.socket, "create_connection", side_effect=local_connect):
            self.assertEqual(tool.verify_domain("docs.example.com", 2, fingerprint)["status"], "TLS_VALID")
            self.assertEqual(tool.verify_domain("docs.example.com", 2, "stale")["status"], "TLS_FAILED")
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())

    def test_atomic_report_contains_no_private_key(self):
        self.certificate()
        path = self.root / "reports/certificates.json"
        code, report = self.invoke("check", "--report", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(path.read_text()), report)
        self.assertNotIn("PRIVATE KEY", path.read_text())


if __name__ == "__main__":
    unittest.main()
