"""Multi-site lifecycle checks; no services or real certificates are created."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REAL_DOCKER = shutil.which("docker")


class HostingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xdocker-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ("scripts", "nginx", "sites", "certbot"):
            shutil.copytree(ROOT / directory, self.root / directory,
                            ignore=shutil.ignore_patterns(".env"))
        for file in (".env.example", "compose.yml"):
            shutil.copy(ROOT / file, self.root / file)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text('''#!/bin/sh
printf '%s | %s\\n' "$COMPOSE_FILE" "$*" >> "$DOCKER_CALLS"
case "$*" in
  *'run --rm --entrypoint /bin/sh certbot'*) [ "${NO_CERT:-0}" = 0 ] || exit 1 ;;
  *'exec -T nginx nginx -t'*) [ "${INVALID_CONFIG:-0}" = 0 ] || exit 1 ;;
  *'certificates.py check'*) [ "${BAD_CERT:-0}" = 0 ] || exit 1 ;;
  *'certificates.py acme-config'*) [ "${ACME_FAIL:-0}" = 0 ] || exit 1 ;;
esac
''')
        docker.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        DOCKER_CALLS=str(self.root / "calls"))
        self.run_action("init")
        self.edit(self.root / ".env", "replace-with-your-email@example.com", "operator@example.com")

    def edit(self, path, old, new):
        path.write_text(path.read_text().replace(old, new))

    def enable_three(self):
        self.edit(self.root / ".env", 'ENABLED_SITES="powerxdoc"',
                  'ENABLED_SITES="powerxdoc powerwechat artisancloud-home"')
        for key in ("powerwechat", "artisancloud-home"):
            path = self.root / f"sites/{key}/.env"
            path.write_text(path.read_text().split("SITE_IMAGE=")[0] + "SITE_IMAGE=nginx:alpine\n")

    def run_action(self, *args, success=True, **extra):
        result = subprocess.run(["sh", "scripts/hosting.sh", *args],
                                cwd=self.root, env=dict(self.env, **extra),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def calls(self):
        path = self.root / "calls"
        return path.read_text() if path.exists() else ""

    def config_path(self, key):
        return self.root / f"data/nginx/{key}.conf"

    def test_init_preserves_global_and_site_settings(self):
        self.edit(self.root / "sites/powerxdoc/.env", "powerx-doc.artisan-cloud.com", "docs.example.com")
        originals = {p: p.read_text() for p in (self.root / ".env", self.root / "sites/powerxdoc/.env")}
        self.run_action("init")
        for path, original in originals.items():
            self.assertEqual(path.read_text(), original)

    def test_http_starts_only_selected_site_and_gateway(self):
        self.enable_three()
        self.run_action("http", "powerwechat")
        config = self.config_path("powerwechat").read_text()
        self.assertIn("server_name powerwechat.artisan-cloud.com", config)
        self.assertIn("http://powerwechat-docs:80", config)
        self.assertNotIn("__SERVICE__", config)
        self.assertIn("compose up -d --wait --wait-timeout 120 powerwechat-docs", self.calls())
        self.assertNotIn("120 powerx-doc", self.calls())
        self.assertFalse(self.config_path("powerxdoc").exists())

    def test_local_image_skips_pull(self):
        self.run_action("http", "powerxdoc", "--no-pull")
        self.assertNotIn("compose pull", self.calls())

    def test_disabled_site_cannot_be_started(self):
        self.run_action("http", "powerwechat", success=False)
        self.assertEqual(self.calls(), "")

    def test_unpublished_placeholder_cannot_be_enabled(self):
        self.edit(self.root / ".env", '"powerxdoc"', '"powerxdoc powerwechat"')
        (self.root / "sites/powerwechat/.env").write_text(
            "SITE_DOMAIN=powerwechat.artisan-cloud.com\n"
            "SITE_IMAGE=ghcr.io/example/docs:REPLACE_WITH_PUBLISHED_TAG\n")
        result = self.run_action("compose", "config", success=False)
        self.assertIn("published SITE_IMAGE", result.stderr)
        self.assertEqual(self.calls(), "")

    def test_issue_targets_requested_domain_and_dry_run(self):
        self.enable_three()
        self.run_action("issue-test", "artisancloud-home")
        self.assertIn("certificates.py issue-test artisan-cloud.com", self.calls())
        self.assertIn("--email operator@example.com", self.calls())
        self.assertNotIn("-d powerx-doc", self.calls())

    def test_missing_certificate_preserves_http(self):
        self.run_action("http", "powerxdoc")
        original = self.config_path("powerxdoc").read_text()
        self.run_action("https", "powerxdoc", success=False, NO_CERT="1")
        self.assertEqual(self.config_path("powerxdoc").read_text(), original)
        self.assertNotIn("--profile tls", self.calls())

    def test_failed_second_site_tls_preserves_first_site(self):
        self.enable_three()
        self.run_action("http", "powerxdoc")
        self.run_action("https", "powerxdoc")
        self.run_action("http", "powerwechat")
        originals = {key: self.config_path(key).read_text() for key in ("powerxdoc", "powerwechat")}
        self.run_action("https", "powerwechat", success=False, INVALID_CONFIG="1")
        for key, original in originals.items():
            self.assertEqual(self.config_path(key).read_text(), original)

    def test_http_cannot_downgrade_selected_tls_site(self):
        self.run_action("http", "powerxdoc")
        self.run_action("https", "powerxdoc")
        config = self.config_path("powerxdoc").read_text()
        self.assertIn("listen 443 ssl", config)
        self.assertIn("compose --profile tls up -d certbot-renew", self.calls())
        self.run_action("http", "powerxdoc", success=False)
        self.assertEqual(self.config_path("powerxdoc").read_text(), config)

    def test_bad_certificate_refuses_tls_activation(self):
        self.run_action("http", "powerxdoc")
        original = self.config_path("powerxdoc").read_text()
        self.run_action("https", "powerxdoc", success=False, BAD_CERT="1")
        self.assertEqual(self.config_path("powerxdoc").read_text(), original)

    def test_shared_san_certificate_uses_its_lineage_name(self):
        with (self.root / "sites/powerxdoc/.env").open("a") as stream:
            stream.write("SITE_CERT_NAME=shared.example.com\n")
        self.run_action("http", "powerxdoc")
        self.run_action("issue-test", "powerxdoc")
        self.run_action("https", "powerxdoc")
        self.assertIn("certificates.py issue-test shared.example.com", self.calls())
        self.assertIn("/live/shared.example.com/fullchain.pem", self.config_path("powerxdoc").read_text())

    def test_acme_generation_failure_preserves_existing_config(self):
        self.run_action("http", "powerxdoc")
        original = self.config_path("powerxdoc").read_text()
        acme = self.root / "data/nginx/10-certificates-acme.conf"
        acme.write_text("# Previous ACME configuration\n")
        self.run_action("https", "powerxdoc", success=False, ACME_FAIL="1")
        self.assertEqual(self.config_path("powerxdoc").read_text(), original)
        self.assertEqual(acme.read_text(), "# Previous ACME configuration\n")

    def test_renew_test_is_scoped_to_one_certificate(self):
        self.enable_three()
        self.run_action("renew-test", "powerwechat")
        self.assertIn("certificates.py renew-test powerwechat.artisan-cloud.com", self.calls())

    def test_update_only_recreates_selected_service(self):
        self.enable_three()
        self.run_action("update", "artisancloud-home")
        self.assertIn("compose pull artisan-cloud-home", self.calls())
        self.assertNotIn("up -d nginx", self.calls())

    def test_duplicate_domains_rejected_before_docker(self):
        self.enable_three()
        self.edit(self.root / "sites/powerwechat/.env", "powerwechat.artisan-cloud.com", "powerx-doc.artisan-cloud.com")
        result = self.run_action("http", "powerxdoc", success=False)
        self.assertIn("Duplicate domain", result.stderr)
        self.assertEqual(self.calls(), "")

    def test_invalid_domain_and_site_path_rejected(self):
        self.run_action("http", "../powerxdoc", success=False)
        self.edit(self.root / "sites/powerxdoc/.env", "powerx-doc.artisan-cloud.com", "invalid/path")
        self.run_action("http", "powerxdoc", success=False)
        self.assertEqual(self.calls(), "")

    @unittest.skipUnless(REAL_DOCKER, "Docker Compose CLI is required for config checks")
    def test_real_compose_includes_only_enabled_sites(self):
        env = dict(self.env, PATH=os.environ["PATH"])
        def config():
            result = subprocess.run(["sh", "scripts/hosting.sh", "compose", "--profile", "tls", "config", "--format", "json"],
                                    cwd=self.root, env=env, capture_output=True, text=True, check=True)
            return json.loads(result.stdout)["services"]
        one = config()
        self.assertEqual(set(one), {"nginx", "certbot-renew", "powerx-doc"})
        self.enable_three()
        three = config()
        self.assertEqual(set(three), {"nginx", "certbot-renew", "powerx-doc", "powerwechat-docs", "artisan-cloud-home"})
        self.assertEqual(three["powerwechat-docs"]["image"], "nginx:alpine")
        for key in ("powerx-doc", "powerwechat-docs", "artisan-cloud-home"):
            self.assertNotIn("ports", three[key])
        self.assertNotIn("depends_on", three["nginx"])


if __name__ == "__main__":
    unittest.main()
