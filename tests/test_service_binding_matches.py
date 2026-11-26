from ipaddress import ip_address
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/admin'))
from service_binding_matches import binding_preview, match_grant, owns_listener, project_services, read_host


class BindingMatchTests(unittest.TestCase):
    def setUp(self):
        self.projects = [{'id': key, 'endpoint': key, 'directory': '/apps/shared',
                          'name': key, 'revision': 1, 'domain': key + '.example.test',
                          'bound_services': [], 'endpoint_available': True} for key in ('web', 'api')]
        self.endpoints = {key: {'host': '127.0.0.1', 'port': port} for key, port in [('web', 3000), ('api', 8080)]}
        self.grant = {'scope': 'user', 'unit': 'demo.service', 'actions': ['status']}
        self.fields = {'WorkingDirectory': '/apps/shared/backend', 'ControlGroup': '/units/demo.service'}
        self.sockets = {(4, 8080): [(ip_address('127.0.0.1'), '/units/demo.service/worker')]}

    def match(self, alias='demo'):
        return match_grant(alias, self.grant, self.fields, self.projects, self.endpoints, self.sockets, set())

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
        self.projects[1]['directory'] = '/apps/other'
        self.assertIsNone(self.match()['suggested'])

    def test_directory_boundary_and_name_only(self):
        self.sockets.clear()
        self.projects[0]['directory'] = '/apps/shared-old'
        self.assertEqual(self.match()['suggested'], 'api')
        self.fields['WorkingDirectory'] = '/apps/shared-copy'
        row = self.match('api-worker')
        self.assertIsNone(row['suggested'])
        self.assertEqual(row['evidence'], {'api': {'name': True}})
        self.fields.update(WorkingDirectory='/apps/shared', RootDirectory='/chroot')
        self.assertFalse(self.match()['evidence'])

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
