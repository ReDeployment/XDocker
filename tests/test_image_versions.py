"""Registered image identities, public evidence, and previews must stay read-only."""
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/admin'))
import image_versions as versions
from app import create_app

OLD='a'*40
NEW='b'*40
TOKEN='fixture-private-admin-token-'+'x'*40
CID={'backend':'a'*64,'web-admin':'b'*64}
OLD_IDS={'backend':'sha256:'+'1'*64,'web-admin':'sha256:'+'2'*64}
NEW_IDS={'backend':'sha256:'+'3'*64,'web-admin':'sha256:'+'4'*64}
NEW_REFS={role:repo+'@sha256:'+('c' if role=='backend' else 'd')*64 for role,repo in versions.REPOS.items()}


def candidate(channel='dev'):
    return {'id':'dev-example','channel':channel,'version':'sha-'+NEW if channel=='dev' else 'v1.0.0','source_revision':NEW,
            'images':dict(NEW_REFS),'evidence':{'run_id':12345,'release_tag':None if channel=='dev' else 'v1.0.0'},
            'migration':{'status':'none','from_revisions':[OLD],'notes':'fixture maintainer migration note','rollback':'compatible'}}


def image(identifier,revision,reference):
    return {'Id':identifier,'RepoDigests':[reference],'Os':'linux','Architecture':'amd64',
            'Config':{'Labels':{'org.opencontainers.image.revision':revision,'org.opencontainers.image.version':'v0.1.0','private.secret':'do-not-return'},
                      'Env':['PASSWORD=do-not-return']}}


class Engine:
    def __init__(self):
        self.calls=[];self.offline=False;self.images={};self.details={}
        for role,repo in versions.REPOS.items():
            config=repo+':sha-'+OLD
            old=image(OLD_IDS[role],OLD,repo+'@sha256:'+('e' if role=='backend' else 'f')*64)
            self.images[OLD_IDS[role]]=old;self.images[config]=old
            new=image(NEW_IDS[role],NEW,NEW_REFS[role]);self.images[NEW_REFS[role]]=new;self.images[NEW_IDS[role]]=new
            self.details[CID[role]]={'Image':OLD_IDS[role],'Config':{'Image':config,'Env':['TOKEN=do-not-return'],
                'Labels':{'com.docker.compose.project':'powerx-dev','com.docker.compose.service':role}},
                'State':{'Status':'running','Health':{'Status':'healthy'}}}
    def containers(self):
        return [{'Id':CID[role],'Labels':{'com.docker.compose.project':'powerx-dev','com.docker.compose.service':role,'com.docker.compose.oneoff':'False'}} for role in versions.REPOS]
    def request(self,method,path,body=None,**kwargs):
        self.calls.append((method,path,body))
        if self.offline:raise OSError('fixture-private-error')
        if method!='GET':raise AssertionError('Docker mutation '+method+' '+path)
        if path=='/version':return {'Os':'linux','Arch':'amd64','Version':'29'}
        if path.startswith('/containers/json'):return self.containers()
        if path.startswith('/containers/'):return self.details[path.split('/')[2]]
        if path.startswith('/images/'):
            key=unquote(path[len('/images/'):-len('/json')])
            if key not in self.images:raise OSError('Image not cached')
            return self.images[key]
        raise AssertionError(path)


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        for part in ('state','config/instances/powerx-dev','config/sites','reports'):(self.root/part).mkdir(parents=True)
        (self.root/'state/access-token').write_text(TOKEN)
        (self.root/'config/root.env').write_text('ENABLED_SITES=""\n')
        (self.root/'config/instances/powerx-dev/instance.json').write_text(json.dumps({'app':'powerx','project':'powerx-dev','enabled':True}))
        self.env=self.root/'config/instances/powerx-dev/.env'
        self.env.write_text('DEPLOYMENT_ENV=dev\nPASSWORD=do-not-return\n'+''.join(key+'='+versions.REPOS[role]+':sha-'+OLD+'\n' for role,key in versions.ENV_KEYS.items()))
        self.catalog=self.root/'catalog.json';self.write_catalog([candidate()]);self.engine=Engine()
        self.viewer=versions.ImageVersions(self.engine,self.root/'config',self.catalog,lambda:['powerx-dev'],self.engine.containers,lambda _:[])
        self.app=create_app({'TESTING':True,'STATE':str(self.root/'state'),'CONFIG':str(self.root/'config'),'REPORTS':str(self.root/'reports'),
                             'BACKUPS':str(self.root/'backups'),'HOST_ROOT':str(self.root),'VERSION_CATALOG':str(self.catalog)},self.engine)
        self.client=self.app.test_client()

    def write_catalog(self,rows):self.catalog.write_text(json.dumps({'schema':1,'versions':rows}))
    def login(self):self.client.post('/api/login',json={'token':TOKEN},headers={'Origin':'http://localhost'})
    def verified_evidence(self):
        with patch.object(versions,'isolated_evidence',return_value={'state':'verified','detail':'fixture verified'}):self.viewer.check('dev-example','evidence')

    def test_auth_allowlisted_instances_targets_and_no_configuration_writes(self):
        self.assertEqual(self.client.get('/api/image-versions').status_code,401);self.login()
        before=self.env.read_text()
        self.assertEqual(self.client.get('/api/image-versions').status_code,200)
        self.assertEqual(self.client.get('/api/image-versions/unrelated').status_code,404)
        self.assertEqual(self.client.get('/api/image-versions/powerx-dev/preview/unknown').status_code,404)
        self.assertEqual(self.client.get('/api/image-versions/check/dev-example/evidence?url=http://localhost').status_code,400)
        self.assertEqual(self.client.post('/api/image-versions/powerx-dev/preview/dev-example',json={},headers={'Origin':'http://localhost'}).status_code,403)
        response=self.client.get('/api/image-versions/powerx-dev');self.assertEqual(response.status_code,200)
        self.assertNotIn('do-not-return',response.text);self.assertNotIn(TOKEN,response.text)
        self.assertEqual(self.env.read_text(),before);self.assertTrue(all(c[0]=='GET' for c in self.engine.calls))

    def test_runtime_image_id_is_not_substituted_for_registry_digest(self):
        row=self.viewer.current('powerx-dev');self.assertEqual(row['pair']['state'],'matched')
        for role in versions.REPOS:
            self.assertEqual(row['runtime'][role]['image_id'],OLD_IDS[role])
            self.assertNotIn(OLD_IDS[role],row['runtime'][role]['repo_digests'])
        self.engine.images[OLD_IDS['backend']]['RepoDigests']=[]
        self.assertEqual(self.viewer.current('powerx-dev')['runtime']['backend']['repo_digests'],[])

    def test_tag_repointing_config_drift_and_mixed_actual_sources_are_detected(self):
        self.engine.images[versions.REPOS['backend']+':sha-'+OLD]=self.engine.images[NEW_IDS['backend']]
        row=self.viewer.current('powerx-dev');self.assertEqual(row['runtime']['backend']['configuration_match'],'different')
        self.engine.images[OLD_IDS['web-admin']]['Config']['Labels']['org.opencontainers.image.revision']=NEW
        self.assertEqual(self.viewer.current('powerx-dev')['pair']['state'],'different')

    def test_missing_oci_revision_is_not_inferred_from_tag_or_container_environment(self):
        del self.engine.images[OLD_IDS['backend']]['Config']['Labels']['org.opencontainers.image.revision']
        row=self.viewer.current('powerx-dev');self.assertEqual(row['pair']['state'],'unknown')
        self.assertEqual(row['configured']['backend']['tag_revision'],OLD);self.assertIsNone(row['runtime']['backend']['source_revision'])

    def test_container_identity_replacement_or_duplicate_is_not_accepted(self):
        self.engine.details[CID['backend']]['Config']['Labels']['com.docker.compose.project']='other'
        self.assertEqual(self.viewer.current('powerx-dev')['runtime']['backend']['state'],'unknown')
        self.viewer.containers=lambda:self.engine.containers()+[self.engine.containers()[0]]
        self.assertEqual(self.viewer.current('powerx-dev')['runtime']['backend']['state'],'unknown')

    def test_catalog_missing_invalid_or_symlink_does_not_fake_latest_version(self):
        self.write_catalog([]);self.assertEqual(self.viewer.listing()['candidates'],[])
        self.catalog.unlink();self.assertEqual(self.viewer.listing()['candidates'],[])
        self.catalog.mkdir();self.assertIsNotNone(self.viewer.listing()['catalog_error']);self.catalog.rmdir()
        self.catalog.write_text('{"schema":true,"versions":[]}');self.assertIsNotNone(self.viewer.listing()['catalog_error'])
        self.catalog.write_text('{"secret":"do-not-return"}')
        listing=self.viewer.listing();self.assertIsNotNone(listing['catalog_error']);self.assertNotIn('do-not-return',json.dumps(listing))
        self.catalog.unlink();target=self.root/'other';target.write_text('private');self.catalog.symlink_to(target)
        self.assertEqual(self.viewer.listing()['candidates'],[]);self.assertEqual(target.read_text(),'private')

    def test_candidates_require_full_source_and_role_specific_digest_pins(self):
        for mutate in (lambda c:c['images'].pop('web-admin'),lambda c:c['images'].update(backend=versions.REPOS['backend']+':latest'),
                       lambda c:c['images'].update(backend=NEW_REFS['web-admin']),lambda c:c.update(source_revision='short'),
                       lambda c:c['evidence'].update(run_id='123'),lambda c:c.update(channel='release')):
            row=candidate();mutate(row)
            with self.assertRaises((ValueError,TypeError)):versions.validate_candidate(row)
        row=candidate('stable');self.assertEqual(versions.validate_candidate(row)['version'],'v1.0.0')
        row['version']='v1.0.0-rc1';row['evidence']['release_tag']=row['version']
        with self.assertRaises(ValueError):versions.validate_candidate(row)

    def test_local_digest_revision_and_host_platform_must_all_match(self):
        row=self.viewer.local('powerx-dev','dev-example');self.assertTrue(all(r['state']=='verified' for r in row['images'].values()))
        self.engine.images[NEW_REFS['backend']]['Architecture']='arm64'
        self.assertEqual(self.viewer.local('powerx-dev','dev-example')['images']['backend']['state'],'mismatch')
        self.engine.images[NEW_REFS['backend']]['Architecture']='amd64';self.engine.images[NEW_REFS['backend']]['RepoDigests']=[]
        self.assertFalse(self.viewer.local('powerx-dev','dev-example')['images']['backend']['digest_match'])

    def test_readonly_preview_separates_technical_review_from_backup_and_apply(self):
        self.verified_evidence();before=self.env.read_text()
        row=self.viewer.preview('powerx-dev','dev-example');self.assertEqual(row['technical_preview'],'reviewable')
        self.assertFalse(row['applied']);self.assertTrue(row['backup_required']);self.assertIsNone(row['backup'])
        self.assertEqual(len(row['config_preview'].splitlines()),2);self.assertEqual(self.env.read_text(),before)
        self.assertEqual(row['deployment'],'not-confirmed');self.assertTrue(all(c[0]=='GET' for c in self.engine.calls))

    def test_preview_exposes_only_valid_backup_summary_without_private_manifest_details(self):
        self.viewer.backup_records=lambda _: [{'id':'a'*32,'state':'ready','created':datetime.now(timezone.utc).isoformat(),'verified':True,
            'config_password':'do-not-return','last_restore_drill':{'private':'do-not-return'}}]
        row=self.viewer.preview('powerx-dev','dev-example');self.assertEqual(row['backup']['id'],'a'*32)
        self.assertNotIn('do-not-return',json.dumps(row));self.assertTrue(row['backup_required'])

    def test_missing_evidence_or_migration_scope_blocks_review(self):
        self.assertEqual(self.viewer.preview('powerx-dev','dev-example')['technical_preview'],'blocked')
        self.verified_evidence();row=candidate();row['migration']['from_revisions']=[];self.write_catalog([row])
        blockers=self.viewer.preview('powerx-dev','dev-example')['blockers'];self.assertTrue(any('迁移' in b for b in blockers))

    def test_production_development_target_and_expired_evidence_are_blocked(self):
        self.env.write_text(self.env.read_text().replace('DEPLOYMENT_ENV=dev','DEPLOYMENT_ENV=prod'))
        self.verified_evidence();self.assertTrue(any('正式实例' in b for b in self.viewer.preview('powerx-dev','dev-example')['blockers']))
        self.viewer.checks['dev-example']['evidence']['at']=(datetime.now(timezone.utc)-timedelta(minutes=16)).isoformat()
        self.assertTrue(any('证据' in b for b in self.viewer.preview('powerx-dev','dev-example')['blockers']))

    def test_catalog_change_invalidates_cached_evidence(self):
        self.verified_evidence();row=candidate();row['evidence']['run_id']=67890;self.write_catalog([row])
        self.assertIsNone(self.viewer.listing()['checks'][0]['evidence'])

    def test_engine_failure_keeps_config_and_never_claims_deployment(self):
        self.engine.offline=True;row=self.viewer.current('powerx-dev')
        self.assertEqual(row['pair']['state'],'unknown');self.assertIsNotNone(row['engine_error'])
        preview=self.viewer.preview('powerx-dev','dev-example');self.assertEqual(preview['technical_preview'],'blocked');self.assertEqual(preview['deployment'],'not-confirmed')

    def test_matching_containers_are_distinguished_from_running_or_business_acceptance(self):
        for role in versions.REPOS:
            self.engine.details[CID[role]]['Image']=NEW_IDS[role]
        self.assertEqual(self.viewer.preview('powerx-dev','dev-example')['deployment'],'matching-running')
        self.engine.details[CID['backend']]['State']['Status']='exited'
        self.assertEqual(self.viewer.preview('powerx-dev','dev-example')['deployment'],'matching-stopped')


def workflow():
    return {'head_sha':NEW,'status':'completed','conclusion':'success','head_branch':'develop',
            'repository':{'full_name':'ArtisanCloud/PowerX'},'path':'.github/workflows/publish-container.yml@develop'}


def jobs():
    return {'total_count':1,'jobs':[{'status':'completed','conclusion':'success',
        'steps':[{'name':name,'status':'completed','conclusion':'success'} for name in versions.STEPS]}]}


class EvidenceTests(unittest.TestCase):
    def test_completed_whole_workflow_and_required_steps_have_same_source(self):
        with patch.object(versions,'github_json',side_effect=[(200,workflow()),(200,jobs())]) as query:
            result=versions.verify_evidence(12345,NEW,'dev');self.assertEqual(result['state'],'verified')
            self.assertEqual(query.call_args_list[0].args[0],'actions/runs/12345')
        run=workflow();run['head_sha']=OLD
        with patch.object(versions,'github_json',return_value=(200,run)):
            self.assertEqual(versions.verify_evidence(12345,NEW,'dev')['state'],'failed')

    def test_failed_pending_skipped_smoke_other_branch_or_incomplete_jobs_cannot_pass(self):
        for field,value,state in [('status','in_progress','unknown'),('conclusion','failure','failed'),('head_branch','main','failed')]:
            run=workflow();run[field]=value
            with patch.object(versions,'github_json',return_value=(200,run)):
                self.assertEqual(versions.verify_evidence(12345,NEW,'dev')['state'],state)
        data=jobs();data['jobs'][0]['steps'][0]['conclusion']='skipped'
        with patch.object(versions,'github_json',side_effect=[(200,workflow()),(200,data)]):
            self.assertEqual(versions.verify_evidence(12345,NEW,'dev')['state'],'unknown')
        data=jobs();data['total_count']=101
        with patch.object(versions,'github_json',side_effect=[(200,workflow()),(200,data)]):
            self.assertEqual(versions.verify_evidence(12345,NEW,'dev')['state'],'unknown')

    def test_403_404_and_timeout_remain_unconfirmed_without_reading_credentials(self):
        for code in (403,404):
            with patch.object(versions,'github_json',return_value=(code,None)):
                self.assertEqual(versions.verify_evidence(12345,NEW,'dev')['state'],'unknown')
        with patch.object(versions.subprocess,'run',side_effect=subprocess.TimeoutExpired('fixture',25)):
            self.assertEqual(versions.isolated_evidence(candidate())['state'],'unknown')

    def test_stable_release_must_be_final_have_bundle_and_point_to_source(self):
        release={'tag_name':'v1.0.0','draft':False,'prerelease':False,'assets':[{'name':n,'state':'uploaded','size':100} for n in ('powerx-docker.tar.gz','powerx-docker.tar.gz.sha256')]}
        reference={'object':{'type':'commit','sha':NEW}}
        with patch.object(versions,'github_json',side_effect=[(200,workflow()),(200,jobs()),(200,release),(200,reference)]):
            self.assertEqual(versions.verify_evidence(12345,NEW,'stable','v1.0.0')['state'],'verified')
        release['prerelease']=True
        with patch.object(versions,'github_json',side_effect=[(200,workflow()),(200,jobs()),(200,release)]):
            self.assertEqual(versions.verify_evidence(12345,NEW,'stable','v1.0.0')['state'],'failed')
        release['prerelease']=False;reference['object']['sha']=OLD
        with patch.object(versions,'github_json',side_effect=[(200,workflow()),(200,jobs()),(200,release),(200,reference)]):
            self.assertEqual(versions.verify_evidence(12345,NEW,'stable','v1.0.0')['state'],'failed')

    def test_annotated_release_tag_is_dereferenced_without_custom_urls(self):
        release={'tag_name':'v1.0.0','draft':False,'prerelease':False,'assets':[{'name':n,'state':'uploaded','size':100} for n in ('powerx-docker.tar.gz','powerx-docker.tar.gz.sha256')]}
        reference={'object':{'type':'tag','sha':'c'*40}}
        with patch.object(versions,'github_json',side_effect=[(200,workflow()),(200,jobs()),(200,release),(200,reference),(200,{'object':{'type':'commit','sha':NEW}})]) as query:
            self.assertEqual(versions.verify_evidence(12345,NEW,'stable','v1.0.0')['state'],'verified')
            self.assertEqual(query.call_args_list[-1].args[0],'git/tags/'+'c'*40)

    def test_github_requests_use_fixed_repo_without_authorization_or_redirect(self):
        with patch.object(versions,'http_read',return_value=(200,{},b'{"head_sha":"fixture"}')) as fetch:
            versions.github_json('actions/runs/12345')
            url,headers=fetch.call_args.args;self.assertEqual(url,'https://api.github.com/repos/ArtisanCloud/PowerX/actions/runs/12345')
            self.assertNotIn('Authorization',headers)


if __name__=='__main__':unittest.main()
