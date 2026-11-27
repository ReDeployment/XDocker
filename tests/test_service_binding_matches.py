from ipaddress import ip_address
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/admin'))
from service_binding_matches import binding_preview, match_grant, owns_listener, project_services, read_host, registered_directories


class BindingMatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / 'shared' / 'backend').mkdir(parents=True)
        (self.root / 'shared-old').mkdir()
        (self.root / 'shared-copy').mkdir()
        self.projects = [{'id': key, 'endpoint': key, 'directory': str(self.root / 'shared'),
                          'name': key, 'revision': 1, 'domain': key + '.example.test',
                          'bound_services': [], 'endpoint_available': True} for key in ('web', 'api')]
        self.endpoints = {key: {'host': '127.0.0.1', 'port': port} for key, port in [('web', 3000), ('api', 8080)]}
        self.grant = {'scope': 'user', 'unit': 'demo.service', 'actions': ['status']}
        self.fields = {'WorkingDirectory': str(self.root / 'shared' / 'backend'), 'ControlGroup': '/units/demo.service'}
        self.sockets = {(4, 8080): [(ip_address('127.0.0.1'), '/units/demo.service/worker')]}

    def match(self, alias='demo'):
        return match_grant(alias, self.grant, self.fields, self.projects, self.endpoints, self.sockets, set(),
                           registered_directories(self.projects))

    def test_system_service_can_observe_its_own_login_manager(self):
        command = ['systemctl', '--user', 'show', '--', 'example.service']
        with patch.dict('os.environ', {}, clear=True), patch('os.geteuid', return_value=1234), \
                patch('native_services.run_fixed') as run:
            read_host(command, timeout=2)
            run.assert_called_once_with(['env', 'XDG_RUNTIME_DIR=/run/user/1234', *command], timeout=2)
            with patch.dict('os.environ', {'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/test/bus'}):
                read_host(command)
                run.assert_called_with(command)
            read_host(['systemctl', 'show'])
            run.assert_called_with(['systemctl', 'show'])

    def test_port_distinguishes_shared_directory(self):
        row = self.match()
        self.assertEqual(row['suggested'], 'api')
        self.assertIn('directory', row['evidence']['web'])
        self.assertEqual(row['evidence']['api']['listener'], self.endpoints['api'])
        self.sockets.clear()
        self.assertIsNone(self.match()['suggested'])

    def test_conflicting_hints_need_a_choice(self):
        self.assertIsNone(self.match('web-worker')['suggested'])
        self.projects[1]['directory'] = str(self.root / 'shared-copy')
        self.assertIsNone(self.match()['suggested'])

    def test_directory_boundary_and_name_only(self):
        self.sockets.clear()
        self.projects[0]['directory'] = str(self.root / 'shared-old')
        self.assertEqual(self.match()['suggested'], 'api')
        self.fields['WorkingDirectory'] = str(self.root / 'shared-copy')
        row = self.match('api-worker')
        self.assertIsNone(row['suggested'])
        self.assertEqual(row['evidence'], {'api': {'name': True}})
        self.fields.update(WorkingDirectory=str(self.root / 'shared'), RootDirectory='/chroot')
        self.assertFalse(self.match()['evidence'])

    def test_worktree_path_alias_and_port_can_identify_a_deployment(self):
        link = self.root / 'workspace'
        link.symlink_to(self.root / 'shared', target_is_directory=True)
        worktree = self.root / 'shared' / 'backend'
        self.projects[0]['directory'] = str(self.root / 'shared-old')
        self.fields['WorkingDirectory'] = str(link / 'backend')
        self.sockets.clear()
        row = self.match()
        self.assertEqual(row['suggested'], 'api')
        self.assertEqual(row['evidence']['api']['directory'], str(worktree))

        # A second registration of the same directory is ambiguous until a port distinguishes it.
        self.projects[0]['directory'] = str(link)
        self.assertIsNone(self.match()['suggested'])
        self.sockets[(4, 8080)] = [(ip_address('127.0.0.1'), self.fields['ControlGroup'])]
        self.assertEqual(self.match()['suggested'], 'api')

    def test_missing_or_outside_directory_does_not_count_as_project_evidence(self):
        shared = self.root / 'shared'
        outside = shared / 'outside'
        outside.symlink_to(self.root / 'shared-copy', target_is_directory=True)
        broken = shared / 'broken'
        broken.symlink_to(self.root / 'absent', target_is_directory=True)
        loop = shared / 'loop'
        loop.symlink_to(loop, target_is_directory=True)
        self.sockets.clear()
        for path in (outside, broken, loop, shared / 'missing'):
            with self.subTest(path=path):
                self.fields['WorkingDirectory'] = str(path)
                self.assertEqual(self.match()['evidence'], {})

    def test_project_group_matches_an_api_listener_without_a_second_project_choice(self):
        web, api = self.projects
        web['entries'] = [{'id': p['id'], 'endpoint': p['endpoint'], 'domain': p['domain']} for p in self.projects]
        self.projects = [web]
        row = self.match('web-backend')
        self.assertEqual(row['suggested'], 'web')
        self.assertEqual(row['evidence']['web']['entry_domain'], api['domain'])
        self.assertEqual(row['evidence']['web']['listener'], self.endpoints['api'])

    def test_local_wildcard_scope_and_shared_port(self):
        wildcard = ip_address('0.0.0.0')
        group = self.fields['ControlGroup']
        self.sockets[(4, 8080)] = [(wildcard, group)]
        endpoint = {'host': '192.0.2.14', 'port': 8080}
        self.assertFalse(owns_listener(endpoint, group, self.sockets, set()))
        self.assertTrue(owns_listener(endpoint, group, self.sockets, {ip_address(endpoint['host'])}))
        self.assertFalse(owns_listener({'host': '::1', 'port': 8080}, group, self.sockets, set()))
        self.sockets[(4, 8080)].append((wildcard, group + '-other'))
        self.assertFalse(owns_listener(self.endpoints['api'], group, self.sockets, set()))

    def test_only_exact_unbound_grants_and_whitelisted_evidence(self):
        self.projects[0]['bound_services'] = ['bound']
        grants = {'demo': self.grant, 'bound': {**self.grant, 'unit': 'bound.service'}}
        listing = {'projects': self.projects, 'services': grants, 'endpoints': self.endpoints, 'policy_sha256': 'fixture'}
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            output = ''
            if command[0] == 'systemctl':
                self.assertEqual(command[command.index('--') + 1:], ['demo.service'])
                self.assertIn('--user', command)
                output = 'Id=demo.service\nLoadState=loaded\nControlGroup=/units/demo.service'
            elif '-4' in command:
                output = 'LISTEN 0 64 *:8080 *:* cgroup:/units/demo.service\nLISTEN 0 64 *:9999 *:* cgroup:/private/service'
            elif command[0] == 'ip':
                output = '[]'
            return {'code': 0, 'stdout': output, 'truncated': False}

        preview = binding_preview(listing, runner)
        row, = preview['rows']
        self.assertEqual(row['suggested'], 'api')
        self.assertNotIn('/private', str(preview))
        self.assertEqual(len(calls), 4)
        for bad in [{'code': 0, 'truncated': True}, {'code': 1, 'truncated': False}]:
            row, = binding_preview(listing, lambda *a, **kw: {**bad, 'stdout': 'partial'})['rows']
            self.assertIsNone(row['suggested'])
            self.assertFalse(row['unit_observed'])

    def test_discovery_groups_grants_and_candidates_without_returning_unrelated_units(self):
        self.projects[0]['bound_services'] = ['existing']
        grants = {'existing': {**self.grant, 'unit': 'web.service'},
                  'api-worker': {**self.grant, 'unit': 'api-worker.service'}}
        listing = {'projects': self.projects, 'services': grants, 'endpoints': self.endpoints}
        def runner(command, **kwargs):
            if command[0] == 'systemctl':
                self.assertEqual(command[-1], '*.service')
                units = ['web.service', 'api-worker.service', 'unknown.service', 'private.service',
                         'user@1000.service'] if '--user' in command else []
                blocks = []
                for unit in units:
                    group = '/parent' if unit.startswith('user@') else '/parent/' + unit
                    blocks.append(f'Id={unit}\nLoadState=loaded\nControlGroup={group}')
                out = '\n\n'.join(blocks)
            elif '-4' in command:
                out = 'LISTEN 0 5 127.0.0.1:8080 *:* cgroup:/parent/unknown.service'
            else:
                out = '[]' if command[0] == 'ip' else ''
            return {'stdout': out, 'code': 0, 'truncated': False}
        result = project_services(listing, runner)
        self.assertEqual(result['unavailable'], [])
        self.assertEqual(result['candidates']['web'], [])
        candidates = {row['unit']: row for row in result['candidates']['api']}
        self.assertEqual(set(candidates), {'api-worker.service', 'unknown.service'})
        self.assertIsNone(candidates['unknown.service']['alias'])
        self.assertTrue(candidates['unknown.service']['suggested'])
        self.assertEqual(candidates['api-worker.service']['alias'], 'api-worker')
        self.assertFalse(candidates['api-worker.service']['suggested'])
        self.assertNotIn('private.service', str(result))
        self.assertTrue(candidates['unknown.service']['can_grant_read'])
        self.assertFalse(candidates['api-worker.service']['can_grant_read'])
        def system_only(command, **kwargs):
            if '--user' in command:
                return {'stdout': '', 'code': 0, 'truncated': False}
            return runner([*command[:1], '--user', *command[1:]] if command[0] == 'systemctl' else command, **kwargs)
        system = project_services(listing, system_only)
        match = next(c for c in system['candidates']['api'] if c['unit'] == 'unknown.service')
        self.assertEqual((match['scope'], match['can_grant_read']), ('system', True))

    def test_incomplete_host_observations_and_non_native_are_not_reported_as_no_match(self):
        listing = {'projects': self.projects, 'services': {}, 'endpoints': {}}
        self.assertEqual(project_services(listing, None)['unavailable'], ['native'])
        result = project_services(listing, lambda *a, **kw: {'code': 1, 'truncated': True, 'stdout': 'partial'})
        self.assertEqual(set(result['unavailable']), {'system', 'user', 'listeners'})
        self.assertEqual(result['candidates'], {'api': [], 'web': []})
