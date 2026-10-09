import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote, urlsplit
import yaml

spec = importlib.util.spec_from_file_location('powerx_db_config', Path(__file__).resolve().parents[1]/'scripts/powerx-db-config.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PasswordSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'database': {'driver':'postgres', 'host':'postgres', 'port':5432,
            'database':'powerx', 'username':'powerx', 'password':'old-fixture',
            'dsn':'postgres://powerx:old-fixture@postgres:5432/powerx?sslmode=disable'},
            'install': {'status':'installed'}, 'auth': {'jwt_secret':'keep-fixture'}}
        (self.root/'config.yaml').write_text(yaml.safe_dump(self.config))
        (self.root/'postgres-password').write_text('old-fixture\n')

    def test_changes_password_and_uri_without_reset_or_key_rotation(self):
        password = "new @:/#' fixture"
        draft = {'database': {'host':'postgres','name':'powerx','username':'powerx','password':'old-fixture'},
                 'admin': {'password':'keep-admin-fixture'}}
        (self.root/'setup.wizard.config.json').write_text(json.dumps(draft))
        calls = []
        backup = module.sync(self.root, password, lambda db,pw:calls.append(pw))
        config = yaml.safe_load((self.root/'config.yaml').read_text())
        self.assertEqual(unquote(urlsplit(config['database']['dsn']).password),password)
        self.assertEqual(config['install'],self.config['install'])
        self.assertEqual(config['auth'],self.config['auth'])
        self.assertEqual((self.root/'postgres-password').read_text(),password+'\n')
        self.assertEqual(json.loads((self.root/'setup.wizard.config.json').read_text())['admin'],draft['admin'])
        self.assertEqual(calls,[password])
        self.assertEqual((backup/'postgres-password').read_text(),'old-fixture\n')
        self.assertEqual((self.root/'config.yaml').stat().st_mode&0o777,0o600)
        self.assertEqual(backup.stat().st_mode&0o777,0o700)

    def test_authentication_failure_leaves_all_files_unchanged(self):
        before = {p:p.read_bytes() for p in self.root.iterdir()}
        def fail(*args):raise ValueError('authentication failed')
        with self.assertRaises(ValueError):module.sync(self.root,'wrong-fixture',fail)
        self.assertEqual({p:p.read_bytes() for p in self.root.iterdir()},before)

    def test_dsn_mismatch_rejected_without_files_changed(self):
        self.config['database']['dsn']='postgres://powerx:old-fixture@other-host:5432/powerx'
        original=yaml.safe_dump(self.config)
        (self.root/'config.yaml').write_text(original)
        with self.assertRaises(ValueError):module.sync(self.root,'new-fixture',lambda *args:None)
        self.assertEqual((self.root/'config.yaml').read_text(),original)

    def test_different_setup_draft_not_overwritten(self):
        draft=json.dumps({'database':{'host':'external','name':'another','username':'another','password':'keep-fixture'}})
        (self.root/'setup.wizard.config.json').write_text(draft)
        module.sync(self.root,'new-fixture',lambda *args:None)
        self.assertEqual((self.root/'setup.wizard.config.json').read_text(),draft)

    def test_invalid_password_rejected(self):
        for password in ('','a\nb','a\0b'):
            with self.assertRaises(ValueError):module.sync(self.root,password,lambda *args:None)


if __name__ == '__main__':unittest.main()
