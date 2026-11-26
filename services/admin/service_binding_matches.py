"""Read-only ownership suggestions for explicitly granted native units."""
from ipaddress import ip_address
import json
from pathlib import PurePosixPath

from native_services import ServiceError, run_fixed


def observation(runner, command):
    try:
        result = runner(command, timeout=2, limit=256 * 1024)
    except (ServiceError, OSError):
        return ''
    return result['stdout'] if not result['code'] and not result['truncated'] else ''


def unit_properties(grants, runner):
    properties = {}
    for scope in ('user', 'system'):
        names = {grant['unit'] for grant in grants.values() if grant['scope'] == scope}
        if not names:
            continue
        command = ['systemctl'] + (['--user'] if scope == 'user' else [])
        output = observation(runner, command + ['show', '--no-pager',
            '--property=Id,LoadState,ControlGroup,WorkingDirectory,RootDirectory,RootImage', '--', *sorted(names)])
        for block in output.strip().split('\n\n'):
            fields = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
            if fields.get('Id') in names and fields.get('LoadState') == 'loaded':
                properties[scope, fields['Id']] = fields
    return properties


def listeners(runner):
    # Separate families so that a wildcard IPv6 listener never proves IPv4 ownership.
    by_port = {}
    for family in (4, 6):
        output = observation(runner, ['ss', '-' + str(family), '-H', '-ltn', '--cgroup'])
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[0] != 'LISTEN':
                continue
            host, _, port = parts[3].rpartition(':')
            if not port.isdecimal():
                continue
            host = host.strip('[]')
            host = ('0.0.0.0' if family == 4 else '::') if host == '*' else host
            try:
                address = ip_address(host)
            except ValueError:
                continue
            group = next((v[7:] for v in parts[5:] if v.startswith('cgroup:')), '')
            by_port.setdefault((family, int(port)), []).append((address, group))
    return by_port


def local_addresses(runner):
    output = observation(runner, ['ip', '-j', 'address', 'show'])
    try:
        return {ip_address(addr['local']) for interface in json.loads(output)
                for addr in interface['addr_info'] if 'local' in addr}
    except (KeyError, ValueError, TypeError):
        return set()


def owns_listener(endpoint, group, sockets, addresses):
    if not group or group == '/':
        return False
    host = ip_address(endpoint['host'])
    owners = [owner for address, owner in sockets.get((host.version, endpoint['port']), [])
              if address == host or address.is_unspecified and (host.is_loopback or host in addresses)]
    return bool(owners) and all(owner == group or owner.startswith(group + '/') for owner in owners)


def match_grant(alias, grant, fields, projects, endpoints, sockets, addresses):
    working = fields.get('WorkingDirectory', '')
    directory = PurePosixPath(working) if working.startswith('/') and not (
        fields.get('RootImage') or fields.get('RootDirectory', '') not in ('', '/')) else None
    evidence = {}
    by_dir, by_port, by_name = set(), set(), set()
    for project in projects:
        key = project['id']
        reasons = {}
        root = project.get('directory')
        if root and PurePosixPath(root) != PurePosixPath('/') and directory and '..' not in directory.parts and directory.is_relative_to(root):
            by_dir.add(key)
            reasons['directory'] = working
        endpoint = endpoints[project['endpoint']]
        if owns_listener(endpoint, fields.get('ControlGroup'), sockets, addresses):
            by_port.add(key)
            reasons['listener'] = {'host': endpoint['host'], 'port': endpoint['port']}
        if any(name == key or name.startswith((key + '-', key + '.')) for name in (alias, grant['unit'])):
            by_name.add(key)
            reasons['name'] = True
        if reasons:
            evidence[key] = reasons

    # An observed port can distinguish two projects sharing a checkout, and vice versa.
    signals = [group for group in (by_dir, by_port) if group]
    matches = set.intersection(*signals) if signals else set()
    suggested = next(iter(matches)) if len(matches) == 1 and (not by_name or matches <= by_name) else None
    return {'alias': alias, **grant, 'suggested': suggested, 'evidence': evidence,
            'unit_observed': bool(fields)}


def binding_preview(listing, runner=run_fixed):
    owned = {alias for project in listing['projects'] for alias in project['bound_services']}
    grants = {alias: grant for alias, grant in listing['services'].items() if alias not in owned}
    projects = [project for project in listing['projects'] if project['endpoint_available']]
    rows = []
    if grants and projects:
        properties = unit_properties(grants, runner) if runner else {}
        sockets = listeners(runner) if properties else {}
        addresses = local_addresses(runner) if sockets else set()
        for alias, grant in grants.items():
            fields = properties.get((grant['scope'], grant['unit']), {})
            rows.append(match_grant(alias, grant, fields, projects, listing['endpoints'], sockets, addresses))
    return {'rows': rows, 'policy_sha256': listing['policy_sha256'],
            'projects': [{key: project[key] for key in ('id', 'revision', 'name', 'domain')} for project in projects]}
