"""Match registered projects to observed systemd services and authorize confirmed read access."""
from ipaddress import ip_address
import json

from native_services import UNIT, ServiceError, fingerprint, host_directory, read_host, require, validate_policy


def observation(runner, command, issues=None, source=None):
    try:
        result = runner(command, timeout=2, limit=256 * 1024)
        if not result['code'] and not result['truncated']:
            return result['stdout']
    except (ServiceError, OSError):
        pass
    if issues is not None:
        issues.add(source)
    return ''


def loaded_units(scope, names, runner, issues=None):
    command = ['systemctl'] + (['--user'] if scope == 'user' else [])
    output = observation(runner, command + ['show', '--no-pager', '--all',
        '--property=Id,LoadState,ControlGroup,WorkingDirectory,RootDirectory,RootImage',
        '--', *names], issues, scope)
    units = {}
    for block in output.strip().split('\n\n'):
        fields = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        unit = fields.get('Id', '')
        if UNIT.fullmatch(unit) and fields.get('LoadState') == 'loaded':
            units[scope, unit] = fields
    return units


def unit_properties(grants, runner):
    properties = {}
    for scope in ('user', 'system'):
        names = {grant['unit'] for grant in grants.values() if grant['scope'] == scope}
        if not names:
            continue
        for identity, fields in loaded_units(scope, sorted(names), runner).items():
            if identity[1] in names:
                properties[identity] = fields
    return properties


def listeners(runner, issues=None):
    # Separate families so that a wildcard IPv6 listener never proves IPv4 ownership.
    by_port = {}
    for family in (4, 6):
        output = observation(runner, ['ss', '-' + str(family), '-H', '-ltn', '--cgroup'], issues, 'listeners')
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


def local_addresses(runner, issues=None):
    output = observation(runner, ['ip', '-j', 'address', 'show'], issues, 'addresses')
    try:
        return {ip_address(addr['local']) for interface in json.loads(output)
                for addr in interface['addr_info'] if 'local' in addr}
    except (KeyError, ValueError, TypeError):
        if issues is not None:
            issues.add('addresses')
        return set()


def owns_listener(endpoint, group, sockets, addresses):
    if not group or group == '/':
        return False
    host = ip_address(endpoint['host'])
    owners = [owner for address, owner in sockets.get((host.version, endpoint['port']), [])
              if address == host or address.is_unspecified and (host.is_loopback or host in addresses)]
    # A parent manager (e.g. user@UID.service) does not own sockets of nested units.
    def belongs(owner):
        if owner == group:
            return True
        suffix = owner.removeprefix(group + '/')
        return suffix != owner and not any(part.endswith(('.service', '.scope')) for part in suffix.split('/'))
    return bool(owners) and all(belongs(owner) for owner in owners)


def registered_directories(projects):
    return {project['id']: host_directory(project.get('directory')) for project in projects}


def match_grant(alias, grant, fields, projects, endpoints, sockets, addresses, directories):
    directory = None
    if not fields.get('RootImage') and fields.get('RootDirectory', '') in ('', '/'):
        directory = host_directory(fields.get('WorkingDirectory'))
    evidence = {}
    by_dir, by_port, by_name = set(), set(), set()
    for project in projects:
        key = project['id']
        reasons = {}
        root = directories[key]
        if root is not None and directory is not None and directory.is_relative_to(root):
            by_dir.add(key)
            reasons['directory'] = str(directory)
        entries = project.get('entries') or [project]
        for entry in entries:
            endpoint = endpoints.get(entry['endpoint'])
            if endpoint and owns_listener(endpoint, fields.get('ControlGroup'), sockets, addresses):
                by_port.add(key)
                reasons['listener'] = {'host': endpoint['host'], 'port': endpoint['port']}
                if len(entries) > 1:
                    reasons['entry_domain'] = entry['domain']
                break
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


def binding_preview(listing, runner=read_host):
    catalog = listing.get('project_groups', listing['projects'])
    owned = {alias for project in catalog for alias in project['bound_services']}
    grants = {alias: grant for alias, grant in listing['services'].items() if alias not in owned}
    projects = [project for project in catalog if project['endpoint_available']]
    rows = []
    if grants and projects:
        properties = unit_properties(grants, runner) if runner else {}
        sockets = listeners(runner) if properties else {}
        addresses = local_addresses(runner) if sockets else set()
        directories = registered_directories(projects)
        for alias, grant in grants.items():
            fields = properties.get((grant['scope'], grant['unit']), {})
            rows.append(match_grant(alias, grant, fields, projects, listing['endpoints'], sockets, addresses, directories))
    return {'rows': rows, 'policy_sha256': listing['policy_sha256'],
            'projects': [{key: project[key] for key in ('id', 'revision', 'name', 'domain')} for project in projects]}

def project_services(listing, runner=read_host):
    """Return only project-related candidates. Discovery never issues a grant or a binding."""
    projects = listing.get('project_groups', listing['projects'])
    candidates = {project['id']: [] for project in projects}
    issues = set()
    if not runner or not projects:
        return {'candidates': candidates, 'unavailable': [] if runner else ['native']}

    units = {}
    for scope in ('user', 'system'):
        units.update(loaded_units(scope, ['*.service'], runner, issues))
    grants = {(grant['scope'], grant['unit']): alias for alias, grant in listing['services'].items()}
    owned = {alias for project in projects for alias in project['bound_services']}
    sockets = listeners(runner, issues)
    addresses = local_addresses(runner, issues) if sockets else set()
    directories = registered_directories(projects)
    for scope, unit in sorted(units.keys() | grants.keys()):
        alias = grants.get((scope, unit))
        if alias in owned:
            continue
        result = match_grant(alias or unit, {'scope': scope, 'unit': unit}, units.get((scope, unit), {}),
                             projects, listing['endpoints'], sockets, addresses, directories)
        for key, reasons in result['evidence'].items():
            if result['suggested'] and key != result['suggested']:
                continue
            suggested = key == result['suggested']
            eligible = (not issues and not alias and (scope, unit) != ('system', 'docker.service')
                        and not unit.startswith(('xdocker-admin', 'xdocker-native', 'xdocker-host-monitor')))
            inside_project = scope == 'user' and 'directory' in reasons
            # Manual review relies on a directory match for the current user's services.
            # A port can describe a reverse proxy or other shared infrastructure.
            can_review = eligible and result['suggested'] is None and inside_project
            can_grant = eligible and suggested and ('listener' in reasons or inside_project)
            candidates[key].append({'scope': scope, 'unit': unit, 'alias': alias, 'suggested': suggested,
                                    'can_grant_read': bool(can_grant), 'can_review_read': bool(can_review),
                                    'reasons': reasons})
    return {'candidates': candidates, 'unavailable': sorted(issues)}


def grant_project_reads(hub, body, runner, *, bind_selected=False):
    require(isinstance(body, dict) and set(body) == {'grants', 'policy_sha256'}, 'XD_SERVICE_GRANT_INVALID')
    entries = body['grants']
    require(isinstance(entries, list) and 1 <= len(entries) <= 100, 'XD_SERVICE_GRANT_INVALID')
    require(not bind_selected or len(entries) == 1, 'XD_SERVICE_GRANT_INVALID')
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) == {'project', 'revision', 'scope', 'unit'}
                and all(isinstance(entry[key], str) for key in ('project', 'scope', 'unit')), 'XD_SERVICE_GRANT_INVALID')
    with hub.locked():
        listing = hub.listing()
        require(body['policy_sha256'] == listing['policy_sha256'], 'XD_SERVICE_CONFIGURATION_CHANGED')
        projects = {p['id']: p for p in listing['project_groups']}
        for entry in entries:
            project = projects.get(entry['project'])
            require(project and project['endpoint_available'], 'XD_SERVICE_BINDING_UNAVAILABLE')
            require(type(entry['revision']) is int and project['revision'] == entry['revision'], 'XD_SERVICE_REVISION_CONFLICT')

        candidates = project_services(listing, runner)['candidates']
        capability = 'can_review_read' if bind_selected else 'can_grant_read'
        eligible = {(pid, c['scope'], c['unit']) for pid, rows in candidates.items() for c in rows if c[capability]}
        policy = hub.policy()
        reserved = set(policy['services']) | {a for p in projects.values() for a in p['bound_services']}
        additions = {}
        results = []
        for entry in entries:
            pid, scope, unit = entry['project'], entry['scope'], entry['unit']
            require((pid, scope, unit) in eligible, 'XD_SERVICE_READ_MATCH_REQUIRED')
            alias = 'read-' + fingerprint([scope, unit])[:24]
            require(alias not in additions, 'XD_SERVICE_GRANT_INVALID')
            require(alias not in reserved, 'XD_SERVICE_BINDING_UNAVAILABLE')
            grant = {'scope': scope, 'unit': unit, 'actions': ['status', 'logs']}
            additions[alias] = {**grant, 'definition_sha256': None}
            results.append({'project': pid, 'alias': alias, **grant})
        validate_policy({**policy, 'services': {**policy['services'], **additions}})
        bindings = [(r['alias'], r['project']) for r in results] if bind_selected else []
        hub.store.add_read_grants(additions, policy['revision'], bindings=bindings)
        return results
