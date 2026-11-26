"""Exercise private deployment overrides and the log policy on Docker API jobs."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/admin'))
from backups import InstanceBackups
from docker_api import compose_log_owner

spec = importlib.util.spec_from_file_location('standalone_logs', ROOT/'scripts/standalone-log-policy.py')
override = importlib.util.module_from_spec(spec)
spec.loader.exec_module(override)


class ContainerLogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root/'services').mkdir()
        shutil.copy(ROOT/'services/container-logging.yml', self.root/'services')
        self.app = self.root/'data/standalone/demo/apps/marketplace'
        self.app.mkdir(parents=True)
        self.template = self.app/'compose.yml'
        self.template.write_text('services:\n  storage:\n    image: fixture\n  postgres:\n    image: fixture\n')

    @unittest.skipUnless(shutil.which('docker'), 'Docker Compose CLI required')
    def test_standalone_merge_changes_only_logging(self):
        (self.root/'.env').write_text('XDOCKER_LOG_MAX_SIZE=3145728\nXDOCKER_LOG_MAX_FILE=3\n')
        extra = self.app/'container-logging.override.yml'
        extra.write_text(json.dumps(override.render(self.root, 'demo', 'marketplace')))
        command = ['docker', 'compose', '-p', 'demo', '-f', str(self.template)]
        def model(files):
            return json.loads(subprocess.check_output(command + files + ['config','--format','json'], cwd=self.app))
        before, after = model([]), model(['-f', str(extra)])
        for service in after['services'].values():
            self.assertEqual(service.pop('logging'), {'driver':'json-file','options':{
                'max-size':'3145728','max-file':'3','compress':'true'}})
        self.assertEqual(before, after)
        labels = {'com.docker.compose.project':'demo', 'com.docker.compose.service':'storage',
                  'com.docker.compose.project.working_dir':str(self.app),
                  'com.docker.compose.project.config_files':str(self.template)+','+str(extra)}
        self.assertTrue(compose_log_owner(labels, self.root))
        labels['com.docker.compose.project'] = 'other'
        self.assertFalse(compose_log_owner(labels, self.root))

    def test_rejects_ambiguous_templates_and_nonlocal_targets(self):
        for project in ('../demo', '', '/tmp'):
            with self.assertRaises(ValueError):
                override.render(self.root, project, 'marketplace')
        for template in ('include: [other.yml]\nservices: {}', 'services: {web: {extends: base}}',
                         'services: {web: {logging: {driver: syslog}}}'):
            self.template.write_text(template)
            with self.assertRaises(ValueError):
                override.render(self.root, 'demo', 'marketplace')
        self.template.unlink()
        self.template.symlink_to(self.root/'services/container-logging.yml')
        with self.assertRaises(ValueError):
            override.render(self.root, 'demo', 'marketplace')

    def test_backup_helper_sends_shared_limits_to_engine(self):
        engine = Mock()
        engine.request.side_effect = [{'Id':'fixture'}, {}, {'State':{'Running':False,'ExitCode':0}}, {}]
        backups = InstanceBackups(engine, self.root, str(self.root),
                                  log_settings=lambda: {'XDOCKER_LOG_MAX_SIZE':'8388608','XDOCKER_LOG_MAX_FILE':'3'})
        backups.helper('fixture', ['true'], [], 'none', 'job')
        method, path, payload = engine.request.call_args_list[0].args
        self.assertEqual((method,path), ('POST','/containers/create'))
        self.assertEqual(payload['HostConfig']['LogConfig'], {'Type':'json-file','Config':{
            'max-size':'8388608','max-file':'3','compress':'true'}})
