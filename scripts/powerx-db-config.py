"""Sync an already changed PostgreSQL password into one private instance.

Run in the instance's init container; the password never appears in arguments.
This does not alter database roles or initialize the application.
"""
import getpass
import json
import os
from pathlib import Path
import secrets
import subprocess
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import yaml


def changed_config(config, password):
    database = config['database']
    if database.get('driver', 'postgres') not in ('postgres', 'postgresql'):
        raise ValueError('Only PostgreSQL instances are supported.')
    dsn = database.get('dsn', '')
    if dsn:
        parsed = urlsplit(dsn)
        if (parsed.scheme not in ('postgres', 'postgresql')
                or parsed.hostname != database['host']
                or (parsed.port or 5432) != int(database['port'])
                or unquote(parsed.path.lstrip('/')) != database['database']
                or unquote(parsed.username or '') != database['username']):
            raise ValueError('DSN and database fields differ; reconcile them before syncing.')
        host = parsed.netloc.rsplit('@', 1)[-1]
        database['dsn'] = urlunsplit(parsed._replace(
            netloc=quote(database['username'], safe='')+':'+quote(password, safe='')+'@'+host))
    database['password'] = password
    return config


def verify(database, password):
    environment = os.environ.copy()
    environment.update(PGPASSWORD=password, PGCONNECT_TIMEOUT='5',
                       PGSSLMODE=database.get('ssl_mode') or 'disable')
    result = subprocess.run(['psql', '-X', '-w', '-v', 'ON_ERROR_STOP=1',
                             '-h', database['host'], '-p', str(database['port']),
                             '-U', database['username'], '-d', database['database'],
                             '-Atc', 'SELECT 1'], env=environment, capture_output=True, timeout=15)
    if result.returncode or result.stdout.strip() != b'1':
        raise ValueError('New password failed TCP authentication; private files were not changed.')


def sync(directory, password, check=verify):
    if not password or '\n' in password or '\r' in password or '\0' in password:
        raise ValueError('Password must be nonempty and contain no newline or NUL.')
    directory = Path(directory)
    paths = [directory/'config.yaml', directory/'postgres-password']
    draft_path = directory/'setup.wizard.config.json'
    if draft_path.exists():
        paths.append(draft_path)
    if directory.is_symlink() or any(p.is_symlink() for p in paths):
        raise ValueError('Private configuration symlinks are not supported.')
    originals = {p: p.read_bytes() for p in paths}
    config = changed_config(yaml.safe_load(originals[paths[0]]), password)
    updates = {paths[0]: yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode(),
               paths[1]: (password+'\n').encode()}
    if draft_path in originals:
        draft = json.loads(originals[draft_path])
        db = draft.get('database', {})
        current = config['database']
        if (db.get('host'), db.get('name'), db.get('username')) == (
                current['host'], current['database'], current['username']):
            db['password'] = password
            updates[draft_path] = (json.dumps(draft, ensure_ascii=False, indent=2)+'\n').encode()
    check(config['database'], password)
    backup_root = directory/'password-change-backups'
    if backup_root.is_symlink():
        raise ValueError('Backup symlinks are not supported.')
    backup = backup_root/secrets.token_hex(12)
    backup.mkdir(parents=True, mode=0o700)
    backup_root.chmod(0o700)
    for path, content in originals.items():
        target = backup/path.name
        target.write_bytes(content)
        target.chmod(0o600)

    def replace(path, content):
        temporary = path.with_name('.'+path.name+'.'+secrets.token_hex(8))
        try:
            temporary.write_bytes(content)
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    try:
        for path, content in updates.items():
            replace(path, content)
    except Exception:
        for path, content in originals.items():
            replace(path, content)
        raise
    return backup


if __name__ == '__main__':
    try:
        first = getpass.getpass('Already changed PostgreSQL password: ')
        if first != getpass.getpass('Repeat password: '):
            raise ValueError('Passwords do not match; files were not changed.')
        backup = sync('/etc/powerx', first)
        print('TCP authentication passed. Private config, DSN and password file synchronized.')
        print('Original private files preserved in '+str(backup))
    except (ValueError, OSError, subprocess.TimeoutExpired):
        raise SystemExit('Synchronization failed. Keep the application paused, check the database password and configuration, then retry; do not reset the database.')
