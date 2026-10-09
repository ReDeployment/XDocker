"""Read only whitelisted Setup fields from a registered backend's fixed mount."""
import io
from pathlib import PurePosixPath
import tarfile

import yaml


class InstanceReader:
    def __init__(self, engine, host_root):
        self.engine = engine
        self.host_root = PurePosixPath(host_root) if host_root else None

    def read(self, container, instance):
        details = self.engine.request('GET', '/containers/'+container['Id']+'/json')
        labels = details.get('Config', {}).get('Labels', {})
        expected = str(self.host_root/'instances'/instance/'config') if self.host_root else None
        if (labels.get('com.docker.compose.project') != instance
                or labels.get('com.docker.compose.service') != 'backend'
                or not expected
                or not any(m.get('Type') == 'bind' and m.get('Destination') == '/etc/powerx'
                           and m.get('Source') == expected for m in details.get('Mounts', []))):
            raise ValueError('Instance configuration mount does not match the registered deployment.')
        archive = self.engine.request('GET', '/containers/'+container['Id']+'/archive?path=%2Fetc%2Fpowerx%2Fconfig.yaml', raw=True)
        try:
            with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
                files = contents.getmembers()
                if len(files) != 1 or files[0].name not in ('config.yaml', './config.yaml') or not files[0].isfile() or files[0].size > 1024*1024:
                    raise ValueError('Unsupported private configuration archive.')
                config = yaml.safe_load(contents.extractfile(files[0]).read())
            if not isinstance(config, dict):
                raise ValueError('Private configuration is not a mapping.')
            return config
        except (tarfile.TarError, yaml.YAMLError, UnicodeError):
            raise ValueError('Private configuration could not be read.') from None


def setup_fields(config):
    for section in ('database', 'cache', 'storage', 'install', 'deployment'):
        if not isinstance(config.get(section, {}), dict):
            raise ValueError('Invalid Setup configuration section.')
    def fields(value, definitions):
        source = value if isinstance(value, dict) else {}
        result = {}
        for key, default in definitions.items():
            value = source.get(key, default)
            result[key] = value if isinstance(value, (str, int)) and not isinstance(value, bool) else default
        result['password_available'] = isinstance(source.get('password'), str) and bool(source['password'])
        return result
    database = fields(config.get('database'), {'host':'postgres','port':5432,'database':'powerx','username':'powerx','ssl_mode':'disable'})
    cache = fields(config.get('cache'), {'host':'redis','port':6379,'db':0})
    storage = config.get('storage', {}).get('local', {})
    if not isinstance(storage, dict):
        raise ValueError('Invalid local storage configuration.')
    def text(value, default):
        return value if isinstance(value, str) else default
    return {'postgres':database, 'redis':cache,
            'storage':{'path':text(storage.get('base_path'),'/data/uploads'), 'public_url':text(storage.get('public_base_url'),'')},
            'install_status':text(config.get('install', {}).get('status'),'unknown'),
            'environment':text(config.get('deployment', {}).get('env'),'unknown')}


def password_for(config, service):
    key = {'postgres':'database', 'redis':'cache'}.get(service)
    if key is None:
        raise ValueError('Unsupported credential service.')
    section = config.get(key, {})
    password = section.get('password') if isinstance(section, dict) else None
    if not isinstance(password, str) or not password:
        raise ValueError('No password is configured for this service.')
    return password
