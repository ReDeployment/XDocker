"""Render a logging-only override for an XDocker standalone bundle to stdout."""
import argparse
import json
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/admin'))
from config_env import env_values


def render(root, project, app):
    key = re.compile(r'[a-z0-9][a-z0-9-]*')
    if not all(key.fullmatch(value) for value in (project, app)):
        raise ValueError('Invalid XDocker standalone project or application')
    directory = root / 'data/standalone' / project / 'apps' / app
    source = directory / 'compose.yml'
    if source.resolve() != source or not source.is_file():
        raise ValueError('Expected a local standalone Compose template')
    try:
        model = yaml.safe_load(source.read_text())
        policy = yaml.safe_load((root/'services/container-logging.yml').read_text())['services']['rotated-logs']['logging']
    except yaml.YAMLError:
        raise ValueError('Cannot read Compose template or shared logging policy') from None
    if not isinstance(model, dict) or 'include' in model or not isinstance(model.get('services'), dict):
        raise ValueError('Expected one standalone Compose file with explicit services')
    services = model['services']
    if not services or not all(isinstance(name, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', name) for name in services):
        raise ValueError('Invalid or empty Compose service list')
    for service in services.values():
        if not isinstance(service, dict) or 'extends' in service:
            raise ValueError('Review inherited or invalid service definitions before changing log drivers')
        existing = service.get('logging', {})
        if (not isinstance(existing, dict) or existing.get('driver', 'json-file') != 'json-file'
                or not isinstance(existing.get('options', {}), dict)
                or set(existing.get('options', {})) - set(policy['options'])):
            raise ValueError('Review the existing log driver and options before replacing them')
    settings = env_values(root / '.env')
    for option, suffix in [('max-size', 'SIZE'), ('max-file', 'FILE')]:
        variable = 'XDOCKER_LOG_MAX_' + suffix
        value = settings.get(variable)
        if value:
            pattern = r'[1-9][0-9]*[kKmMgG]?' if suffix == 'SIZE' else r'[1-9][0-9]*'
            if not re.fullmatch(pattern, value) or (suffix == 'FILE' and int(value) < 2):
                raise ValueError('Invalid shared log limit: ' + variable)
            policy['options'][option] = '${' + variable + ':-' + value + '}'
        if app == 'powerx':
            policy['options'][option] = '${POWERX_LOG_MAX_' + suffix + ':-' + policy['options'][option] + '}'
    return {'services': {name: {'logging': policy} for name in services}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project')
    parser.add_argument('app')
    args = parser.parse_args()
    try:
        override = render(ROOT, args.project, args.app)
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(override, indent=2))
