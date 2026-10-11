"""Check Docker's actual context filtering with disposable fake private files."""
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ('Dockerfile', 'requirements.txt', 'app.py', 'docker_api.py', 'backups.py',
          'instances.py', 'preflight.py', 'backup_plans.py', 'image_versions.py',
          'static/index.html', 'static/app.js', 'static/style.css')


def main():
    source = ROOT / 'services/admin'
    with tempfile.TemporaryDirectory(prefix='xdocker-context-') as temp:
        context = Path(temp)
        for name in (*PUBLIC, '.dockerignore'):
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
            assert paths == set(PUBLIC), 'Docker context differs from the public runtime allowlist'
            print('Actual Docker context passed: public runtime only; root and nested fake private files excluded.')
        finally:
            if identifier: subprocess.run(['docker','rm',identifier], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(['docker','image','rm',image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__': main()
