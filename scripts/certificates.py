#!/usr/bin/env python3
"""Certificate inventory, preserved-lineage renewal and live TLS verification.

Runs inside the Certbot image or on a legacy host. No renewal options are
silently rewritten; no private keys or credentials are emitted in reports.
"""
import argparse
import configparser
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.request

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
DNS = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")


def load_inventory(path):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("Inventory must be a JSON object")
    if data.get("version") != 1:
        raise ValueError("Inventory version must be 1")
    warning, critical = data.get("warning_days", 30), data.get("critical_days", 7)
    if type(warning) is not int or type(critical) is not int or not 0 <= critical < warning:
        raise ValueError("Require 0 <= critical_days < warning_days")
    if not isinstance(data.get("certificates"), list) or not data["certificates"]:
        raise ValueError("Certificate inventory cannot be empty")
    if not isinstance(data.get("acme_directory", "https://acme-v02.api.letsencrypt.org/directory"), str):
        raise ValueError("acme_directory must be a URL")
    names, domains = set(), set()
    for item in data["certificates"]:
        if not isinstance(item, dict):
            raise ValueError("Certificate entries must be JSON objects")
        name = item.get("name", "")
        if not isinstance(name, str) or not DNS.fullmatch(name) or name in names:
            raise ValueError("Invalid or duplicate certificate name")
        names.add(name)
        values = item.get("domains")
        if not isinstance(values, list) or not values or not all(isinstance(value, str) for value in values) or len(set(values)) != len(values):
            raise ValueError(f"Invalid domain list for {name}")
        for domain in values:
            if not isinstance(domain, str) or not DNS.fullmatch(domain) or domain in domains:
                raise ValueError(f"Invalid or duplicate domain in {name}")
            domains.add(domain)
        if type(item.get("enabled", True)) is not bool:
            raise ValueError(f"enabled must be a boolean for {name}")
    return data


def selected(data, name):
    items = [item for item in data["certificates"] if item.get("enabled", True)]
    if name:
        items = [item for item in items if item["name"] == name]
    if not items:
        raise ValueError("No enabled certificate matches this selection")
    return items


def acme_configuration(items, nginx_dir):
    claimed = set()
    for path in Path(nginx_dir).glob("*.conf"):
        if path.name == "10-certificates-acme.conf":
            continue
        # XDocker's generated server_name directives contain literal hostnames.
        for match in re.finditer(r"(?m)^\s*server_name\s+([^;]+);", path.read_text()):
            claimed.update(match.group(1).split())
    domains = sorted({domain for item in items for domain in item["domains"]} - claimed)
    if not domains:
        return "# All inventory domains already have website ingress configurations.\n"
    return ("# Certificate validation only; business application routes are not configured here.\n"
            "server {\n    listen 80;\n    server_name " + " ".join(domains) + ";\n"
            "    location ^~ /.well-known/acme-challenge/ {\n"
            "        root /var/www/certbot;\n        default_type text/plain;\n"
            "        try_files $uri =404;\n    }\n    location / { return 404; }\n}\n")


def openssl(*args, input_data=None):
    return subprocess.run(["openssl", *args], input=input_data, capture_output=True, check=True).stdout


def read_leaf(path):
    """Return public leaf metadata. Cryptography exists in the Certbot image;
    OpenSSL is a fallback for legacy Ubuntu hosts without that Python package.
    """
    pem = Path(path).read_bytes()
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        details = openssl("x509", "-in", str(path), "-noout", "-startdate", "-enddate", "-ext", "subjectAltName").decode()
        dates = dict(line.split("=", 1) for line in details.splitlines() if line.startswith(("notBefore=", "notAfter=")))
        before = datetime.strptime(dates["notBefore"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
        after = datetime.strptime(dates["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
        domains = set(re.findall(r"DNS:([^,\s]+)", details))
        der = openssl("x509", "-in", str(path), "-outform", "DER")
        pub = openssl("x509", "-in", str(path), "-noout", "-pubkey")
        public_key = openssl("pkey", "-pubin", "-outform", "DER", input_data=pub)
    else:
        cert = x509.load_pem_x509_certificate(pem)
        before = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before.replace(tzinfo=UTC)
        after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(tzinfo=UTC)
        try:
            domains = set(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName))
        except x509.ExtensionNotFound:
            domains = set()
        der = cert.public_bytes(serialization.Encoding.DER)
        public_key = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return {"before": before, "after": after, "domains": domains,
            "fingerprint": hashlib.sha256(der).hexdigest(), "public_key": public_key}


def key_public(path):
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        return openssl("pkey", "-in", str(path), "-passin", "pass:", "-pubout", "-outform", "DER")
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    return key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def renewal_options(config_dir, name):
    path = Path(config_dir) / "renewal" / f"{name}.conf"
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string("[lineage]\n" + path.read_text())
    return dict(parser["renewalparams"])


def inspect(item, config_dir, data, now=None):
    now = now or datetime.now(UTC)
    result = {"name": item["name"], "domains": item["domains"], "status": "ERROR"}
    try:
        folder = Path(config_dir) / "live" / item["name"]
        leaf = read_leaf(folder / "fullchain.pem")
        if leaf["public_key"] != key_public(folder / "privkey.pem"):
            raise ValueError("Certificate/private-key mismatch")
        if leaf["domains"] != set(item["domains"]):
            raise ValueError("Certificate SAN differs from inventory; refusing to shrink or alter domains")
        result.update(expiry=leaf["after"].isoformat(), days_remaining=math.floor((leaf["after"] - now).total_seconds() / 86400),
                      fingerprint=leaf["fingerprint"])
        if now < leaf["before"]:
            result["status"] = "NOT_YET_VALID"
        elif now >= leaf["after"]:
            result["status"] = "EXPIRED"
        elif leaf["after"] <= now + timedelta(days=data["critical_days"]):
            result["status"] = "CRITICAL"
        elif leaf["after"] <= now + timedelta(days=data["warning_days"]):
            result["status"] = "EXPIRING"
        else:
            result["status"] = "LOCAL_VALID"
        options = renewal_options(config_dir, item["name"])
        result["authenticator"] = options.get("authenticator", "unknown")
        result["installer"] = options.get("installer", "None")
    except (OSError, ValueError, TypeError, configparser.Error, subprocess.CalledProcessError) as error:
        result.update(status="ERROR", error=str(error))
    return result


def network_preflight(data, items, config_dir, timeout, staging=False):
    urls = set()
    for item in items:
        try:
            server = renewal_options(config_dir, item["name"]).get("server")
        except (OSError, ValueError, configparser.Error):
            server = None
        urls.add(server or data["acme_directory"])
    if staging:
        urls.add("https://acme-staging-v02.api.letsencrypt.org/directory")
    results = []
    for url in sorted(urls):
        if not url.startswith("https://"):
            raise ValueError("ACME directory must use verified HTTPS")
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                document = json.load(response)
            if not all(key in document for key in ("newNonce", "newAccount", "newOrder")):
                raise ValueError("Response is not an ACME directory")
            results.append({"url": url, "status": "REACHABLE"})
        except (OSError, ValueError) as error:
            results.append({"url": url, "status": "UNREACHABLE", "error": str(error)})
    return results


def container_compatible(options):
    # Imported nginx/apache/standalone/DNS plugin configurations cannot silently
    # become webroot renewals inside this stock Certbot container.
    return options.get("authenticator") == "webroot" and options.get("installer", "None").lower() in ("none", "")


def renew_item(item, args, data):
    info = inspect(item, args.config_dir, data)
    if info["status"] in ("ERROR", "NOT_YET_VALID"):
        return dict(info, status="REFUSED", error=info.get("error", "Certificate is not yet valid"))
    options = renewal_options(args.config_dir, item["name"])
    if args.runtime == "container" and not container_compatible(options):
        return dict(info, status="REFUSED", error="Saved renewal requires a host/plugin. Use legacy host renewal or migrate to a separate webroot lineage; options were not rewritten.")
    command = [args.certbot_bin, "renew", "--config-dir", args.config_dir,
               "--cert-name", item["name"], "--non-interactive"]
    if args.action == "renew-test":
        # Operator-triggered validation should not wait up to eight minutes for
        # Certbot's scheduled-renewal jitter. Normal renewals retain that jitter.
        command.extend(["--dry-run", "--no-random-sleep-on-renew"])
    # Inherit Certbot stdout on stderr, keeping JSON report stdout parseable.
    completed = subprocess.run(command, stdout=sys.stderr, stderr=sys.stderr)
    if completed.returncode:
        return dict(info, status="FAILED", error=f"Certbot exited {completed.returncode}")
    if args.action == "renew-test":
        return dict(info, status="DRY_RUN_OK")
    after = inspect(item, args.config_dir, data)
    if after["status"] in ("ERROR", "EXPIRED", "NOT_YET_VALID"):
        return dict(after, status="FAILED", error=after.get("error", "No usable certificate after renewal"))
    changed = after["fingerprint"] != info["fingerprint"]
    return dict(after, certificate_status=after["status"], status="RENEWED" if changed else "UNCHANGED")


def reload_updated(args):
    if args.reload == "container-event":
        target = Path(args.reload_event)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(str(time.time_ns()) + "\n")
        temporary.replace(target)
        return "EVENT_WRITTEN"
    if args.reload == "host-nginx":
        if args.runtime != "host":
            raise ValueError("host-nginx reload requires host runtime")
        subprocess.run(["nginx", "-t"], check=True, stdout=sys.stderr, stderr=sys.stderr)
        subprocess.run(["systemctl", "reload", "nginx"], check=True, stdout=sys.stderr, stderr=sys.stderr)
        return "HOST_NGINX_RELOADED"
    return "NOT_REQUESTED"


def issue_item(item, args, data):
    if not args.email or "@" not in args.email or args.email.startswith("replace-with-"):
        raise ValueError("Set a real --email or CERTBOT_EMAIL for issuance")
    folder = Path(args.config_dir) / "live" / item["name"]
    previous = None
    if folder.exists():
        previous = inspect(item, args.config_dir, data)
        if previous["status"] in ("ERROR", "NOT_YET_VALID"):
            raise ValueError("Existing lineage differs from inventory or is invalid; inspect it before issuance")
        if not container_compatible(renewal_options(args.config_dir, item["name"])):
            raise ValueError("Existing lineage uses a different renewal method. Renew on its original host; do not silently replace its options.")
    command = [args.certbot_bin, "certonly", "--config-dir", args.config_dir,
               "--webroot", "--webroot-path", args.webroot, "--cert-name", item["name"],
               "--email", args.email, "--agree-tos", "--non-interactive"]
    for domain in item["domains"]:
        command.extend(["-d", domain])
    if args.action == "issue-test":
        command.append("--dry-run")
    completed = subprocess.run(command, stdout=sys.stderr, stderr=sys.stderr)
    if completed.returncode:
        return {"name": item["name"], "status": "FAILED", "error": f"Certbot exited {completed.returncode}"}
    if args.action == "issue-test":
        return {"name": item["name"], "domains": item["domains"], "status": "DRY_RUN_OK"}
    after = inspect(item, args.config_dir, data)
    if after["status"] in ("ERROR", "EXPIRED", "NOT_YET_VALID"):
        return dict(after, status="FAILED", error=after.get("error", "No usable certificate after issuance"))
    changed = previous is None or previous["fingerprint"] != after["fingerprint"]
    return dict(after, certificate_status=after["status"], status="ISSUED" if changed else "UNCHANGED")


def verify_domain(domain, timeout, local_fingerprint=None, warning_days=30, critical_days=7):
    try:
        context = ssl.create_default_context()
        with socket.create_connection((domain, 443), timeout=timeout) as connection:
            with context.wrap_socket(connection, server_hostname=domain) as secure:
                fingerprint = hashlib.sha256(secure.getpeercert(binary_form=True)).hexdigest()
                expiry = datetime.fromtimestamp(ssl.cert_time_to_seconds(secure.getpeercert()["notAfter"]), UTC)
        if local_fingerprint is not None and fingerprint != local_fingerprint:
            raise ValueError("Public endpoint serves a different leaf certificate than local disk")
        now = datetime.now(UTC)
        status = "TLS_VALID"
        if expiry <= now + timedelta(days=critical_days):
            status = "TLS_CRITICAL"
        elif expiry <= now + timedelta(days=warning_days):
            status = "TLS_EXPIRING"
        return {"domain": domain, "status": status, "expiry": expiry.isoformat(), "fingerprint": fingerprint,
                "days_remaining": math.floor((expiry - now).total_seconds() / 86400)}
    except (OSError, ValueError) as error:
        return {"domain": domain, "status": "TLS_FAILED", "error": str(error)}


def output(report, args):
    rendered = json.dumps(report, indent=2)
    if args.report:
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(rendered + "\n")
        temporary.replace(target)
    if args.json:
        print(rendered)
    else:
        for row in report.get("results", []):
            label = row.get("name", row.get("domain", row.get("url", "")))
            remaining = f" days={row['days_remaining']}" if "days_remaining" in row else ""
            expiry = f" expiry={row['expiry']}" if "expiry" in row else ""
            print(f"{row['status']:14} {label}{remaining}{expiry}")
            if row.get("error"):
                print("  " + row["error"])
            if row.get("domains"):
                print("  domains: " + " ".join(row["domains"]))
        if report.get("reload"):
            print("reload: " + report["reload"])


def run(args):
    data = load_inventory(args.inventory)
    # Fill explicit defaults once so inspection and policy use the same values.
    data.setdefault("warning_days", 30)
    data.setdefault("critical_days", 7)
    data.setdefault("acme_directory", "https://acme-v02.api.letsencrypt.org/directory")
    items = selected(data, args.name)
    report = {"action": args.action, "checked_at": datetime.now(UTC).isoformat(), "results": []}
    code = 0
    if args.action == "list":
        report["results"] = [dict(item, status="REGISTERED") for item in items]
    elif args.action == "acme-config":
        if args.json or args.report:
            raise ValueError("acme-config emits Nginx configuration, not a JSON report")
        print(acme_configuration(items, args.nginx_dir), end="")
        return 0
    elif args.action == "check":
        report["results"] = [inspect(item, args.config_dir, data) for item in items]
        if any(r["status"] in ("ERROR", "EXPIRED", "CRITICAL", "NOT_YET_VALID") for r in report["results"]):
            code = 1
        elif any(r["status"] == "EXPIRING" for r in report["results"]):
            code = 2
        if args.require_usable and all(r["status"] not in ("ERROR", "EXPIRED", "NOT_YET_VALID") for r in report["results"]):
            code = 0
    elif args.action in ("preflight", "renew-test", "renew", "issue-test", "issue"):
        if args.action in ("issue-test", "issue") and not args.name:
            raise ValueError("Issuance requires one explicit Certificate Name")
        network = network_preflight(data, items, args.config_dir, args.timeout, staging=args.action in ("renew-test", "issue-test"))
        report["network"] = network
        if args.action == "preflight":
            report["results"] = network
            code = int(any(row["status"] != "REACHABLE" for row in network))
        elif any(row["status"] != "REACHABLE" for row in network):
            failures = "; ".join(row.get("error", row["url"]) for row in network if row["status"] != "REACHABLE")
            report["results"] = [{"name": item["name"], "status": "BLOCKED_NETWORK",
                                  "error": "ACME unreachable; no Certbot operation attempted: " + failures} for item in items]
            code = 1
        else:
            for item in items:
                try:
                    operation = issue_item if args.action in ("issue-test", "issue") else renew_item
                    report["results"].append(operation(item, args, data))
                except (OSError, ValueError, configparser.Error) as error:
                    report["results"].append({"name": item["name"], "status": "FAILED", "error": str(error)})
            code = int(any(r["status"] in ("REFUSED", "FAILED") for r in report["results"]))
            if not code:
                if any(r.get("certificate_status") == "CRITICAL" for r in report["results"]):
                    code = 1
                elif any(r.get("certificate_status") == "EXPIRING" for r in report["results"]):
                    code = 2
            if args.action in ("renew", "issue") and any(r["status"] in ("RENEWED", "ISSUED") for r in report["results"]):
                try:
                    report["reload"] = reload_updated(args)
                except (OSError, ValueError, subprocess.CalledProcessError) as error:
                    report["reload"] = "FAILED: " + str(error)
                    code = 1
    elif args.action == "verify":
        for item in items:
            fingerprint = None
            if args.match_local:
                local = inspect(item, args.config_dir, data)
                if "fingerprint" not in local or local["status"] == "ERROR":
                    report["results"].append({"name": item["name"], "status": "TLS_FAILED", "error": local.get("error", "Missing local certificate")})
                    continue
                fingerprint = local["fingerprint"]
            report["results"].extend(verify_domain(domain, args.timeout, fingerprint, data["warning_days"], data["critical_days"]) for domain in item["domains"])
        if any(r["status"] in ("TLS_FAILED", "TLS_CRITICAL") for r in report["results"]):
            code = 1
        elif any(r["status"] == "TLS_EXPIRING" for r in report["results"]):
            code = 2
    output(report, args)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["list", "check", "preflight", "renew-test", "renew", "verify", "issue-test", "issue", "acme-config"])
    parser.add_argument("name", nargs="?", help="Certificate Name, not an arbitrary SAN domain; omitted = all enabled")
    parser.add_argument("--inventory", default=str(ROOT / "certbot/certificates.json"))
    parser.add_argument("--config-dir", default="/etc/letsencrypt")
    parser.add_argument("--runtime", choices=["host", "container"], default="host")
    parser.add_argument("--certbot-bin", default="certbot")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--email", default=os.environ.get("CERTBOT_EMAIL"))
    parser.add_argument("--webroot", default="/var/www/certbot")
    parser.add_argument("--nginx-dir", default="/etc/nginx/conf.d")
    parser.add_argument("--require-usable", action="store_true", help="check: accept still-valid near-expiry certificates for HTTPS activation")
    parser.add_argument("--reload", choices=["none", "host-nginx", "container-event"], default="none")
    parser.add_argument("--reload-event", default="/var/lib/certbot-events/reload")
    parser.add_argument("--match-local", action="store_true", help="Verify public leaf equals local leaf; omit behind a CDN")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--report", help="Write an atomic JSON report; keep it outside Git")
    args = parser.parse_intermixed_args(argv)
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    if args.reload == "host-nginx" and args.runtime != "host":
        parser.error("host-nginx reload requires host runtime")
    if args.require_usable and args.action != "check":
        parser.error("--require-usable applies only to check")
    try:
        return run(args)
    except (OSError, ValueError, KeyError, configparser.Error) as error:
        print("Certificate tool error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
