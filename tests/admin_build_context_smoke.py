"""Check Docker's actual context filtering with disposable fake private files."""
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import argparse
import sys
import os
import re

def local_engine():
    """Check only local configuration before allowing the disposable Docker test."""
    if not shutil.which('docker'):
        raise SystemExit('Docker test not run: docker is unavailable')
    override=os.environ.get('DOCKER_HOST','')
    host=subprocess.check_output(['docker','context','inspect','--format','{{.Endpoints.docker.Host}}'],text=True).strip()
    if not host.startswith('unix://') or override and not override.startswith('unix://'):
        raise SystemExit('Docker test not run: a local Unix-socket context is required')
    result=subprocess.run(['docker','info','--format','{{.ServerVersion}}'],capture_output=True,text=True)
    if result.returncode or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9._-]+)?',result.stdout.strip()):
        raise SystemExit('Docker test not run: local daemon is unavailable')

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ('Dockerfile', 'requirements.txt', 'app.py', 'sync_scan.py', 'sync_runtime.py', 'sync_store.py', 'sync_agent.py', 'code_sync.py', 'code_sync_api.py', 'host_metrics.py', 'resource_monitor.py', 'registry_store.py', 'native_services.py', 'gateway_runtime.py', 'service_hub_api.py', 'native_runtime.py', 'docker_api.py', 'backups.py',
          'instances.py', 'preflight.py', 'backup_plans.py', 'image_versions.py', 'github_watch.py', 'github_webhooks.py', 'github_triage.py', 'github_auth.py',
          'static/index.html', 'static/code_sync.js', 'static/code_sync.css', 'static/code-sync-guide.html', 'static/app.js', 'static/resources.js', 'static/resources.css', 'static/service_hub.js', 'static/service_hub.css', 'static/github.js', 'static/style.css')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--receiver-only",action="store_true")
    parser.add_argument('--static-only',action='store_true')
    args=parser.parse_args()
    source = ROOT / ("services/github-webhook" if args.receiver_only else "services/admin")
    public = ("Dockerfile","requirements.txt","receiver.py","projects.json") if args.receiver_only else PUBLIC
    if args.static_only:
        with tempfile.TemporaryDirectory(prefix='xdocker-runtime-') as temp:
            for name in public:
                p=Path(temp)/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source/name,p)
                if name.endswith('.py'):compile(p.read_text(),name,'exec')
            module='receiver' if args.receiver_only else 'app'
            subprocess.run([sys.executable,'-B','-c','import '+module],cwd=temp,env={**os.environ,'PYTHONPATH':temp},check=True)
        print('Static public runtime imports passed; Docker not used.')
        return
    local_engine()
    with tempfile.TemporaryDirectory(prefix='xdocker-context-') as temp:
        context = Path(temp)
        for name in (*public, '.dockerignore'):
            destination = context / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, destination)
        for name in ('.env', 'static/private-sentinel.txt', 'static/nested/private.key'):
            file = context / name;file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('disposable fake private content; must be excluded')
        dockerfile = context / 'Dockerfile.context-test'
        dockerfile.write_text('FROM scratch\nCOPY . /context\n')
        image = 'xdocker-context-acceptance:' + secrets.token_hex(4)
        identifier = None
        try:
            subprocess.run(['docker','build','-q','-f',str(dockerfile),'-t',image,str(context)], check=True, stdout=subprocess.DEVNULL)
            identifier = subprocess.check_output(['docker','create',image,'/not-run'], text=True).strip()
            copied = context / 'copied'
            subprocess.run(['docker','cp',identifier+':/context',str(copied)], check=True)
            paths = {file.relative_to(copied).as_posix() for file in copied.rglob('*') if file.is_file()}
            assert paths == set(public), 'Docker context differs from the public runtime allowlist'
            print('Actual Docker context passed: public runtime only; root and nested fake private files excluded.')
        finally:
            if identifier: subprocess.run(['docker','rm',identifier], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(['docker','image','rm',image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__': main()
