"""Delivery invariants: checked content, both native platforms, trusted publication only."""
import ast
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('admin_image_artifact', ROOT / 'scripts/admin_image_artifact.py')
artifact = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(artifact)
SHA = 'a' * 40
IMAGE_ID = 'sha256:' + 'b' * 64


class ImageArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def receipt(self, arch):
        archive = self.root / ('image-' + arch + '.tar')
        archive.write_bytes(('disposable content ' + arch).encode())
        row = dict(schema=1, revision=SHA, arch=arch, platform='linux/' + arch,
                   image=artifact.tag(SHA, arch), image_id=IMAGE_ID,
                   archive=archive.name, archive_sha256=artifact.sha256(archive))
        (self.root / ('receipt-' + arch + '.json')).write_text(json.dumps(row))
        return row

    def info(self, arch='arm64', **changes):
        return dict(Config={'Env': ['XDOCKER_REVISION=' + SHA]}, Os='linux',
                    Architecture=arch, Id=IMAGE_ID, **changes)

    def test_incomplete_second_platform_prevents_any_load(self):
        self.receipt('amd64')
        with patch.object(artifact.subprocess, 'run') as run:
            with self.assertRaises(ValueError): artifact.load_images(self.root, SHA)
            run.assert_not_called()

    def test_tampered_second_archive_prevents_any_load(self):
        for arch in artifact.ARCHES: self.receipt(arch)
        (self.root / 'image-arm64.tar').write_bytes(b'tampered')
        with patch.object(artifact.subprocess, 'run') as run:
            with self.assertRaises(ValueError): artifact.load_images(self.root, SHA)
            run.assert_not_called()

    def test_revision_path_extra_fields_and_symlinks_rejected(self):
        good = self.receipt('arm64')
        file = self.root / 'receipt-arm64.json'
        for changes in ({'revision': 'c'*40}, {'archive': '../image.tar'}, {'token': 'not-allowed'}, {'schema': True}):
            with self.subTest(changes=changes):
                file.write_text(json.dumps({**good, **changes}))
                with self.assertRaises(ValueError): artifact.verify_archive(self.root, SHA, 'arm64')
        file.unlink();file.symlink_to(self.root / 'image-arm64.tar')
        with self.assertRaises(ValueError): artifact.verify_archive(self.root, SHA, 'arm64')

    def test_loaded_identity_checked_before_publication(self):
        for arch in artifact.ARCHES: self.receipt(arch)
        wrong = self.info('amd64');wrong['Id'] = 'sha256:' + 'c'*64
        with patch.object(artifact.subprocess, 'run') as run, patch.object(artifact, 'inspect', return_value=wrong):
            with self.assertRaises(ValueError): artifact.load_images(self.root, SHA)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][:3], ['docker', 'image', 'load'])

    def test_success_loads_exact_checked_archives_no_build_or_push(self):
        expected = [self.receipt(arch) for arch in artifact.ARCHES]
        with patch.object(artifact.subprocess, 'run') as run, patch.object(artifact, 'inspect', side_effect=[self.info(arch) for arch in artifact.ARCHES]):
            self.assertEqual(artifact.load_images(self.root, SHA), expected)
            self.assertEqual([call.args[0][:3] for call in run.call_args_list], [['docker', 'image', 'load']]*2)

    def test_runtime_revision_and_platform_not_just_tag(self):
        for field, bad in (('Architecture', 'amd64'), ('Os', 'windows'), ('Config', {'Env':['XDOCKER_REVISION='+'c'*40]}),
                           ('Config', {'Env':['XDOCKER_REVISION='+SHA]*2})):
            info = self.info();info[field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError): artifact.verify_image(info, SHA, 'arm64')
        artifact.verify_image(self.info(), SHA, 'arm64', IMAGE_ID)

    def test_export_requires_checked_tag_and_saves_exact_image(self):
        image = artifact.tag(SHA, 'arm64')
        def save(command, **kwargs): Path(command[4]).write_bytes(b'image archive')
        with patch.object(artifact, 'inspect', return_value=self.info()), patch.object(artifact.subprocess, 'run', side_effect=save) as run:
            with self.assertRaises(ValueError): artifact.export_image('latest', SHA, 'arm64', self.root)
            run.assert_not_called()
            row = artifact.export_image(image, SHA, 'arm64', self.root)
            self.assertEqual(set(row), artifact.FIELDS)
            self.assertEqual(row['image_id'], IMAGE_ID)
            self.assertEqual(artifact.verify_archive(self.root, SHA, 'arm64')[0], row)
            self.assertEqual(run.call_args.args[0][-1], image)

    def test_revision_input_is_strict_and_local_tree_honest(self):
        for bad in ('main', 'latest', 'a'*39, SHA+';push', '../revision'):
            with self.assertRaises(ValueError): artifact.tag(bad, 'arm64')
        self.assertIn('local-tree-', artifact.tag('local-tree-'+'c'*64, 'arm64'))

    def test_index_requires_both_tested_native_platforms(self):
        file = self.root / 'index.json'
        def manifest(arch): return {'digest': IMAGE_ID, 'platform': {'os':'linux', 'architecture':arch}}
        for manifests in ([manifest('arm64')], [manifest('arm64')]*2,
                          [manifest('amd64'),manifest('unknown')], [manifest('amd64'),{}]):
            file.write_text(json.dumps({'schemaVersion':2, 'manifests':manifests}))
            with self.assertRaises(ValueError): artifact.check_index(file)
        file.write_text(json.dumps({'schemaVersion':2,'manifests':[manifest(arch) for arch in artifact.ARCHES]}))
        artifact.check_index(file)


class WorkflowBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.load((ROOT / '.github/workflows/publish-admin.yml').read_text(), Loader=yaml.BaseLoader)

    def gate(self, expression, repository, ref, event):
        expression = expression.replace('&&',' and ').replace('||',' or ')
        for key, value in {'github.repository':repository, 'github.ref':ref, 'github.event_name':event}.items():
            expression = expression.replace(key, repr(value))
        tree = ast.parse(expression, mode='eval')
        allowed = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.Eq, ast.Constant, ast.Load)
        self.assertTrue(all(isinstance(node, allowed) for node in ast.walk(tree)))
        return eval(compile(tree, '<workflow gate>', 'eval'), {'__builtins__': {}})

    def test_pr_and_non_main_dispatch_cannot_publish_or_upload(self):
        self.assertIn('pull_request', self.workflow['on'])
        jobs = self.workflow['jobs']
        gates = [jobs['publish']['if']] + [step['if'] for step in jobs['smoke']['steps'] if 'if' in step]
        for expression in gates:
            for repository, ref, event, expected in (
                ('ReDeployment/XDocker','refs/heads/main','push', True),
                ('ReDeployment/XDocker','refs/heads/main','workflow_dispatch', True),
                ('ReDeployment/XDocker','refs/pull/1/merge','pull_request', False),
                ('ReDeployment/XDocker','refs/heads/topic','workflow_dispatch', False),
                ('fork/XDocker','refs/heads/main','push', False),
                ('ReDeployment/XDocker','refs/heads/main','pull_request', False)):
                with self.subTest(expression=expression, ref=ref, event=event):
                    self.assertIs(self.gate(expression, repository, ref, event), expected)

    def test_write_permissions_exist_only_in_guarded_publisher(self):
        self.assertEqual(self.workflow['permissions'], {'contents':'read'})
        for name, job in self.workflow['jobs'].items():
            if name != 'publish': self.assertNotIn('packages', job.get('permissions', {}))
            for step in job['steps']:
                if step.get('uses', '').startswith('docker/login-action'): self.assertEqual(name, 'publish')
        self.assertEqual(self.workflow['jobs']['publish']['permissions']['packages'], 'write')

    def test_both_native_smokes_block_publisher_and_no_rebuild(self):
        jobs = self.workflow['jobs']
        self.assertEqual(jobs['smoke']['needs'], 'validate')
        self.assertEqual(jobs['publish']['needs'], 'smoke')
        entries = jobs['smoke']['strategy']['matrix']['include']
        self.assertEqual({row['arch']:row['runner'] for row in entries}, {'amd64':'ubuntu-latest','arm64':'ubuntu-24.04-arm'})
        self.assertNotIn('continue-on-error', jobs['smoke'])
        smoke_runs = '\n'.join(step.get('run', '') for step in jobs['smoke']['steps'])
        for script in ('tests/admin_build_context_smoke.py','tests/admin_http_smoke.py','tests/admin_smoke.py','tests/backup_smoke.py'):
            self.assertIn('python '+script, smoke_runs)
        for step in jobs['smoke']['steps']:
            self.assertNotIn('continue-on-error', step)
            if step.get('uses', '').startswith('docker/build-push-action'):
                self.assertEqual(step['with']['push'], 'false');self.assertEqual(step['with']['load'], 'true')
        steps = jobs['publish']['steps']
        verify = next(i for i,s in enumerate(steps) if 'admin_image_artifact.py load' in s.get('run',''))
        login = next(i for i,s in enumerate(steps) if s.get('uses','').startswith('docker/login-action'))
        self.assertLess(verify, login)
        self.assertFalse(any(s.get('uses','').startswith('docker/build-push-action') for s in steps))
        self.assertNotIn('docker build ', '\n'.join(s.get('run','') for s in steps))


if __name__ == '__main__': unittest.main()
