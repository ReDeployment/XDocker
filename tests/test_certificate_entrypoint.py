import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class EntryPointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xdocker-cert-entry-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for folder in ("scripts", "certbot", "nginx"):
            shutil.copytree(ROOT / folder, self.root / folder)
        (self.root / ".env").write_text('ENABLED_SITES="unknown-site-with-no-image"\n')
        binary = self.root / "bin"
        binary.mkdir()
        docker = binary / "docker"
        docker.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_CALLS"
case "$*" in
  *'certificates.py acme-config'*)
    exec python3 scripts/certificates.py acme-config --nginx-dir data/nginx ;;
  *'exec -T nginx nginx -t'*) [ "${INVALID_CONFIG:-0}" = 0 ] || exit 1 ;;
esac
''')
        docker.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{binary}:{os.environ['PATH']}", DOCKER_CALLS=str(self.root / "calls"))

    def run_action(self, *args, success=True, **extra):
        completed = subprocess.run(["sh", "scripts/certificates.sh", *args], cwd=self.root,
                                   env=dict(self.env, **extra), capture_output=True, text=True)
        self.assertEqual(completed.returncode == 0, success, completed.stdout + completed.stderr)
        return completed

    def test_container_checks_do_not_load_or_start_business_sites(self):
        self.run_action("check")
        calls = (self.root / "calls").read_text()
        self.assertIn("compose -f compose.yml run --rm --no-deps", calls)
        self.assertNotIn("sites/", calls)
        self.assertNotIn("up -d", calls)

    def test_legacy_host_list_does_not_use_docker_or_source_env(self):
        (self.root / ".env").write_text('exit 99\n')
        result = self.run_action("--host", "list")
        self.assertIn("debug-scrm.artisan-cloud.com", result.stdout)
        self.assertFalse((self.root / "calls").exists())

    def test_acme_only_start_does_not_create_application_routes(self):
        self.run_action("acme-http")
        calls = (self.root / "calls").read_text()
        config = (self.root / "data/nginx/10-certificates-acme.conf").read_text()
        self.assertIn("shopify.artisan-cloud.com", config)
        self.assertIn("debug.artisan-cloud.com", config)
        self.assertNotIn("proxy_pass", config)
        self.assertIn("up -d nginx", calls)
        self.assertNotIn("up -d powerx", calls)

    def test_acme_config_rejection_restores_previous_file(self):
        directory = self.root / "data/nginx"
        directory.mkdir(parents=True)
        config = directory / "10-certificates-acme.conf"
        config.write_text("# Previous configuration\n")
        self.run_action("acme-http", success=False, INVALID_CONFIG="1")
        self.assertEqual(config.read_text(), "# Previous configuration\n")


if __name__ == "__main__":
    unittest.main()
