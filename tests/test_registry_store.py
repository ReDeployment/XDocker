"""SQLite migration, transactional rollback and independent writer persistence."""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/admin'))
from host_metrics import atomic_json,private_directory
from registry_store import RegistryStore,RegistryError,LEGACY,APPLICATION_ID
from test_native_services import policy,project


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=private_directory(Path(self.temp.name).resolve()/'state')

    def legacy(self):
        values={'policy':policy(),'hub':{'schema':1,'projects':{'example':project()},'operations':[]},
            'monitors':{'schema':1,'apps':{'example':{'id':'example','revision':1,'name':'项目','host_id':'a'*64,'targets':['b'*64]}}},
            'gateway':{'fingerprint':'c'*64,'owner':{'pid':42,'started':1234},'at':1000},
            'gateway_error':{'fingerprint':'d'*64,'at':900,'rollback_verified':True,'runtime_unconfirmed':True}}
        for key,relative in LEGACY.items():
            path=self.root/relative;private_directory(path.parent);atomic_json(path,values[key])
        return values

    def test_import_preserves_sources_and_hash_backups_reopens_and_backups_database(self):
        values=self.legacy();original={k:(self.root/v).read_bytes() for k,v in LEGACY.items()}
        store=RegistryStore(self.root)
        self.assertEqual(store.read_policy(),values['policy'])
        self.assertEqual(store.read_hub()['projects']['example']['domain'],'example.home.test')
        self.assertEqual(store.read_monitors(),values['monitors'])
        self.assertEqual(store.read_gateway(),values['gateway'])
        self.assertEqual(store.read_gateway_error(),values['gateway_error'])
        for key,raw in original.items():
            self.assertEqual((self.root/LEGACY[key]).read_bytes(),raw)
            self.assertEqual((self.root/'registry-migration'/(hashlib.sha256(raw).hexdigest()+'.json')).read_bytes(),raw)
        self.assertEqual(RegistryStore(self.root).read_policy(),values['policy'])
        destination=self.root/'backups'/'consistent.sqlite3';receipt=store.backup(destination)
        self.assertEqual(receipt['sha256'],hashlib.sha256(destination.read_bytes()).hexdigest())
        self.assertEqual(destination.stat().st_mode&0o777,0o600)
        with sqlite3.connect(destination) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT count(*) FROM registry_migrations').fetchone()[0],5)
            self.assertEqual(db.execute('SELECT name FROM projects').fetchone()[0],'示例项目')
        with self.assertRaises(RegistryError):store.backup(destination)

    def test_legacy_change_after_import_is_rejected_and_database_not_replaced(self):
        self.legacy();store=RegistryStore(self.root);before=store.read_policy()
        path=self.root/LEGACY['policy'];path.write_text('{}');path.chmod(0o600)
        with self.assertRaisesRegex(RegistryError,'SOURCE_CHANGED'):RegistryStore(self.root)
        self.assertEqual(store.read_policy(),before)

    def test_invalid_import_and_duplicate_domains_rollback_all_schema(self):
        values=self.legacy();values['hub']['projects']['other']=project(id='other')
        atomic_json(self.root/LEGACY['hub'],values['hub'])
        with self.assertRaises(RegistryError):RegistryStore(self.root)
        with sqlite3.connect(self.root/'registry.sqlite3') as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0],0)
        del values['hub']['projects']['other'];atomic_json(self.root/LEGACY['hub'],values['hub'])
        self.assertEqual(len(RegistryStore(self.root).read_hub()['projects']),1)

    def test_unique_monitor_targets_rollback_and_independent_concurrent_writes(self):
        store=RegistryStore(self.root);monitor={'schema':1,'apps':{'example':{'id':'example','revision':1,'name':'项目','host_id':'a'*64,'targets':['b'*64]}}}
        hub={'schema':1,'projects':{'example':project()},'operations':[]}
        with ThreadPoolExecutor(3) as pool:
            futures=[pool.submit(store.write_policy,policy()),pool.submit(store.write_monitors,monitor),pool.submit(store.write_hub,hub)]
            for future in futures:future.result()
        duplicate=copy.deepcopy(monitor);duplicate['apps']['other']={**duplicate['apps']['example'],'id':'other'}
        with self.assertRaises(RegistryError):store.write_monitors(duplicate)
        reopened=RegistryStore(self.root)
        self.assertEqual(reopened.read_policy(),policy());self.assertEqual(reopened.read_monitors(),monitor)
        self.assertEqual(reopened.read_hub()['projects']['example']['name'],'示例项目')
        self.assertFalse((self.root/LEGACY['policy']).exists())
        self.assertEqual(store.path.stat().st_mode&0o777,0o600)

    def test_foreign_schema_and_symlink_database_are_rejected(self):
        path=self.root/'registry.sqlite3'
        with sqlite3.connect(path) as db:db.execute('CREATE TABLE unrelated(value TEXT)')
        path.chmod(0o600)
        with self.assertRaisesRegex(RegistryError,'SCHEMA_UNSUPPORTED'):RegistryStore(self.root)
        path.unlink();destination=self.root/'other';destination.write_text('preserved');path.symlink_to(destination)
        with self.assertRaisesRegex(RegistryError,'UNSAFE_FILE'):RegistryStore(self.root)
        self.assertEqual(destination.read_text(),'preserved')


if __name__=='__main__':unittest.main()
