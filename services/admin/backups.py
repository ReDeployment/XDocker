"""Instance backups and isolated restore drills; never copy live PGDATA."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import time
from datetime import datetime, timezone
from docker_api import DockerError, log_text

INSTANCE = re.compile(r'^[a-z0-9][a-z0-9-]*$')
IDENTIFIER = re.compile(r'^[a-f0-9]{32}$')
FILES = ('database.dump', 'redis.rdb', 'config.tar.gz', 'runtime.tar.gz')


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


class InstanceBackups:
    def __init__(self, engine, root, host_root, uid=None, gid=None):
        self.engine = engine
        self.root = Path(root)
        self.host_root = PurePosixPath(host_root) if host_root else None
        self.uid = os.getuid() if uid is None else uid
        self.gid = os.getgid() if gid is None else gid

    def folder(self, instance, identifier):
        if not isinstance(instance, str) or not INSTANCE.fullmatch(instance) or not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise ValueError('Invalid backup identity')
        folder = self.root / instance / identifier
        if folder.is_symlink() or folder.parent.is_symlink():
            raise ValueError('Backup symlinks are refused')
        return folder

    def save(self, folder, record):
        temporary = folder / 'manifest.tmp'
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n')
        temporary.chmod(0o600)
        temporary.replace(folder / 'manifest.json')

    def read(self, instance, identifier):
        folder = self.folder(instance, identifier)
        record = json.loads((folder / 'manifest.json').read_text())
        if record.get('instance') != instance or record.get('id') != identifier:
            raise ValueError('Backup manifest identity mismatch')
        return folder, record

    def records(self, allowed):
        result = []
        for instance in allowed:
            for path in (self.root / instance).glob('*/manifest.json'):
                try:
                    _, record = self.read(instance, path.parent.name)
                    result.append(record)
                except (OSError, ValueError):
                    continue
        return sorted(result, key=lambda r: r.get('created', ''), reverse=True)

    def inspect(self, identifier, instance, service):
        value = self.engine.request('GET', '/containers/'+identifier+'/json')
        labels = value.get('Config', {}).get('Labels', {})
        if labels.get('com.docker.compose.project') != instance or labels.get('com.docker.compose.service') != service:
            raise ValueError('Instance container identity changed')
        return value

    def helper(self, image, command, mounts, network, job_id, timeout=600, env=None, tmpfs=None):
        body = {'Image': image, 'Entrypoint': command, 'Cmd': [],
                'Labels': {'io.xdocker.admin.job': job_id},
                'Env': env or [], 'HostConfig': {'Mounts': mounts, 'NetworkMode': network,
                    'CapDrop': ['ALL'], 'CapAdd': ['CHOWN', 'DAC_OVERRIDE', 'FOWNER'],
                    'SecurityOpt': ['no-new-privileges:true']}}
        # PostgreSQL's initialization entrypoint needs chown/setuid in the
        # isolated drill container; no host data is writable there.
        if tmpfs:
            body['HostConfig'].update(Tmpfs=tmpfs, CapDrop=[])
        created = self.engine.request('POST', '/containers/create', body)['Id']
        try:
            self.engine.request('POST', '/containers/'+created+'/start')
            deadline = time.monotonic()+timeout
            while time.monotonic() < deadline:
                state = self.engine.request('GET', '/containers/'+created+'/json')['State']
                if not state['Running']:
                    if state['ExitCode'] != 0:
                        raise ValueError('Backup helper failed, exit '+str(state['ExitCode'])+'; live instance data was not replaced.')
                    return
                time.sleep(.5)
            raise TimeoutError('Backup helper timed out; inspect job and instance state.')
        finally:
            self.engine.request('DELETE', '/containers/'+created+'?force=true&v=true')

    def create(self, instance, containers, job_id):
        folder = self.folder(instance, job_id)
        if self.host_root is None or not self.host_root.is_absolute():
            raise ValueError('Run admin.sh init to configure ADMIN_HOST_ROOT')
        if set(containers) != {'backend', 'web-admin', 'postgres', 'redis'}:
            raise ValueError('All four registered instance containers are required')
        details = {s: self.inspect(c['Id'], instance, s) for s, c in containers.items()}
        if not all(details[s]['State']['Running'] for s in ('postgres', 'redis')):
            raise ValueError('PostgreSQL and Redis must be running before backup')
        sources = {m['Destination']: m['Source'] for m in details['backend']['Mounts'] if m['Type'] == 'bind'}
        expected_config = self.host_root / 'instances' / instance / 'config'
        expected_runtime = self.host_root / 'data' / 'instances' / instance / 'runtime'
        if PurePosixPath(sources.get('/etc/powerx', '')) != expected_config or PurePosixPath(sources.get('/data', '')) != expected_runtime:
            raise ValueError('Nonstandard config/data mounts are refused; configure a supported instance first')
        folder.mkdir(parents=True, mode=0o700)
        folder.parent.chmod(0o700)
        folder.chmod(0o700)
        resume = [{'id': containers[s]['Id'], 'service': s} for s in ('backend', 'web-admin') if details[s]['State']['Running']]
        record = {'version': 1, 'id': job_id, 'instance': instance, 'created': timestamp(), 'state': 'running',
                  'scope': ['postgresql', 'redis-snapshot', 'config-and-keys', 'runtime-files'],
                  'consistency': 'application-maintenance', 'resume': resume,
                  'images': {s: {'reference': d['Config']['Image'], 'id': d['Image']} for s, d in details.items()}}
        self.save(folder, record)
        mounts = [{'Type':'bind', 'Source':str(expected_config), 'Target':'/config', 'ReadOnly':True},
                  {'Type':'bind', 'Source':str(expected_runtime), 'Target':'/runtime', 'ReadOnly':True},
                  {'Type':'bind', 'Source':str(self.host_root/'data'/'instance-backups'/instance/job_id), 'Target':'/backup', 'ReadOnly':False}]
        try:
            for s in ('web-admin', 'backend'):
                if details[s]['State']['Running']:
                    self.engine.request('POST', '/containers/'+containers[s]['Id']+'/stop?t=15', timeout=40)
            script = '''umask 077
export PGPASSWORD="$(cat /config/postgres-password)"
pg_dump -h 127.0.0.1 -U powerx -d powerx --format=custom --no-owner --no-acl -f /backup/database.dump
pg_restore --list /backup/database.dump > /backup/database.toc
tar -czf /backup/config.tar.gz -C /config .
tar --exclude=./logs --exclude=./backups --exclude=./tmp -czf /backup/runtime.tar.gz -C /runtime .
'''+f'chown -R {int(self.uid)}:{int(self.gid)} /backup\n'
            self.helper(details['postgres']['Image'], ['/bin/sh', '-ec', script], mounts, 'container:'+containers['postgres']['Id'], job_id)
            script = '''umask 077
export REDISCLI_AUTH="$(cat /config/redis-password)"
redis-cli -h 127.0.0.1 --rdb /backup/redis.rdb
redis-check-rdb /backup/redis.rdb
'''+f'chown -R {int(self.uid)}:{int(self.gid)} /backup\n'
            self.helper(details['redis']['Image'], ['/bin/sh', '-ec', script], [mounts[0], mounts[2]], 'container:'+containers['redis']['Id'], job_id)
            record['files'] = {name: {'size': (folder/name).stat().st_size, 'sha256': digest(folder/name)} for name in FILES}
            self.save(folder, record)
            portable = {k:v for k,v in record.items() if k != 'resume'}
            portable.update(state='ready', completed=timestamp())
            (folder/'bundle-manifest.json').write_text(json.dumps(portable, ensure_ascii=False, indent=2)+'\n')
            (folder/'bundle-manifest.json').chmod(0o600)
            bundle = folder / 'backup.tar.gz'
            with tarfile.open(bundle, 'w:gz') as archive:
                for name in FILES:
                    archive.add(folder/name, arcname=name, recursive=False)
                archive.add(folder/'bundle-manifest.json', arcname='manifest.json', recursive=False)
            bundle.chmod(0o600)
            record.update(state='ready', completed=timestamp(), bundle_size=bundle.stat().st_size, bundle_sha256=digest(bundle), verified=True)
            self.save(folder, record)
        except Exception:
            record.update(state='failed', completed=timestamp())
            self.save(folder, record)
            raise
        finally:
            failures = self.resume(instance, resume)
            record['resume'] = failures
            if failures:
                record['state'] = 'resume_failed'
            self.save(folder, record)
            if failures:
                raise ValueError('Backup finished but application resume failed; restart the listed services through SSH.')
        return '实例备份已完成并校验；原运行服务已恢复。备份包含私有密钥，下载后应私下保存。'

    def resume(self, instance, entries):
        failures = []
        for entry in entries:
            try:
                value = self.inspect(entry['id'], instance, entry['service'])
                if not value['State']['Running']:
                    self.engine.request('POST', '/containers/'+entry['id']+'/start')
            except Exception:
                failures.append(entry)
        return failures

    def recover(self, allowed):
        for record in self.records(allowed):
            if record.get('state') == 'running' or record.get('resume'):
                folder = self.folder(record['instance'], record['id'])
                record['resume'] = self.resume(record['instance'], record.get('resume', []))
                record.update(state='interrupted' if not record['resume'] else 'resume_failed', completed=timestamp())
                self.save(folder, record)

    def verify(self, instance, identifier):
        folder, record = self.read(instance, identifier)
        if record.get('state') != 'ready':
            raise ValueError('Only completed backups can be verified or downloaded')
        for name in FILES:
            path = folder/name
            if path.is_symlink() or digest(path) != record['files'][name]['sha256']:
                raise ValueError('Backup component checksum mismatch: '+name)
        if (folder/'backup.tar.gz').is_symlink() or digest(folder/'backup.tar.gz') != record['bundle_sha256']:
            raise ValueError('Backup bundle checksum mismatch')
        return folder, record

    def drill(self, instance, identifier, job_id):
        folder, record = self.verify(instance, identifier)
        source = str(self.host_root/'data'/'instance-backups'/instance/identifier)
        script = '''set -eu
export POSTGRES_DB=restore_verify POSTGRES_USER=powerx POSTGRES_HOST_AUTH_METHOD=trust
/usr/local/bin/docker-entrypoint.sh postgres >/tmp/postgres.log 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true' EXIT
for n in $(seq 1 60); do pg_isready -h 127.0.0.1 -U powerx -d restore_verify >/dev/null 2>&1 && break; sleep 1; done
pg_restore --exit-on-error --no-owner --no-acl -h 127.0.0.1 -U powerx -d restore_verify /backup/database.dump
psql -h 127.0.0.1 -U powerx -d restore_verify -v ON_ERROR_STOP=1 -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" > /tmp/table-count
'''
        self.helper(record['images']['postgres']['id'], ['/bin/bash','-ec',script],
                    [{'Type':'bind','Source':source,'Target':'/backup','ReadOnly':True}],
                    'none', job_id, tmpfs={'/var/lib/postgresql/data':'size=1g','/tmp':'size=64m'})
        record['last_restore_drill'] = {'at':timestamp(), 'state':'success', 'isolated':True}
        self.save(folder, record)
        return '数据库已在无网络、无公网端口、独立临时 PostgreSQL 中成功恢复。运行中实例没有被覆盖；配置与文件包已校验，业务级恢复仍需单独验收。'

    def prune(self, instance, keep):
        if not isinstance(keep, int) or isinstance(keep, bool) or not 1 <= keep <= 100:
            raise ValueError('Retention count must be between 1 and 100')
        records = [r for r in self.records([instance]) if r.get('state') == 'ready']
        import shutil
        removed = 0
        for record in records[keep:]:
            self.verify(instance, record['id'])
            shutil.rmtree(self.folder(instance, record['id']))
            removed += 1
        return f'已保留最新 {keep} 份成功备份，清理 {removed} 份旧备份；失败或中断记录保留。'
