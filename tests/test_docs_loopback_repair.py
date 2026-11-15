"""The one-off Docs recovery preserves content/config and rejects broader targets."""
from copy import deepcopy
import importlib.util
import io
from pathlib import Path
import tarfile
import unittest

spec=importlib.util.spec_from_file_location('docs_repair',Path(__file__).resolve().parents[1]/'scripts/repair-powerxdocs-loopback.py')
repair=importlib.util.module_from_spec(spec);spec.loader.exec_module(repair)
ID='a'*64
IMAGE='sha256:'+'b'*64


def original():
    return {'Id':ID,'Image':IMAGE,'Name':'/powerxdocs-local','State':{'Status':'exited'},'Mounts':[],
            'Config':{'Image':'powerxdocs:local-fixture','Cmd':['nginx','-g','daemon off;'],'Entrypoint':['/docker-entrypoint.sh'],
                      'Env':['FIXTURE=preserve-me'],'Labels':{'fixture':'preserved'},'Volumes':None,'WorkingDir':''},
            'HostConfig':{'NetworkMode':'bridge','Privileged':False,'AutoRemove':False,'PublishAllPorts':False,'ContainerIDFile':'',
                          'Binds':None,'VolumesFrom':None,'PortBindings':{'80/tcp':[{'HostIp':'100.100.0.3','HostPort':'8080'}]},
                          'RestartPolicy':{'Name':'unless-stopped','MaximumRetryCount':0},'Memory':1000000}}


class DocsRepairTests(unittest.TestCase):
    def test_exact_image_config_environment_and_limits_are_preserved_without_mutating_original(self):
        before=original();unchanged=deepcopy(before)
        result=repair.create_spec(before,ID,IMAGE)
        self.assertEqual(before,unchanged);self.assertEqual(result['Image'],IMAGE)
        self.assertEqual(result['Cmd'],before['Config']['Cmd']);self.assertEqual(result['Env'],before['Config']['Env'])
        self.assertEqual(result['Labels'],before['Config']['Labels']);self.assertEqual(result['HostConfig']['Memory'],1000000)
        self.assertEqual(result['HostConfig']['PortBindings'],repair.BINDING)
        self.assertEqual(result['HostConfig']['RestartPolicy']['Name'],'no')
        self.assertNotIn('100.100.0.3',str(result['HostConfig']['PortBindings']))

    def test_running_wrong_identity_preview_mounted_privileged_or_external_network_is_rejected(self):
        for key in ('running','id','image','preview','mount','volume','privileged','network','autoremove','allports','cidfile','binding','restart'):
            value=original()
            if key=='running':value['State']['Status']='running'
            if key=='id':value['Id']='c'*64
            if key=='image':value['Image']='sha256:'+'c'*64
            if key=='preview':value['Name']='/powerxdocs-merged-preview'
            if key=='mount':value['Mounts']=[{'Source':'/business/data'}]
            if key=='volume':value['Config']['Volumes']={'/data':{}}
            if key=='privileged':value['HostConfig']['Privileged']=True
            if key=='network':value['HostConfig']['NetworkMode']='host'
            if key=='autoremove':value['HostConfig']['AutoRemove']=True
            if key=='allports':value['HostConfig']['PublishAllPorts']=True
            if key=='cidfile':value['HostConfig']['ContainerIDFile']='/private/existing'
            if key=='binding':value['HostConfig']['PortBindings']['443/tcp']=[]
            if key=='restart':value['HostConfig']['RestartPolicy']['Name']='always'
            with self.assertRaises(ValueError,msg=key):repair.create_spec(value,ID,IMAGE)

    def test_modified_document_tree_is_not_silently_replaced_by_image_content(self):
        repair.validate_docs_changes([{'Path':'/etc/nginx/conf.d/default.conf','Kind':0}])
        for path in ('/usr/share/nginx/html','/usr/share/nginx/html/index.html'):
            with self.assertRaisesRegex(ValueError,'CONTENT_LAYER_CHANGED'):
                repair.validate_docs_changes([{'Path':path,'Kind':1}])

    def test_config_archive_does_not_extract_links_traversal_or_oversized_data(self):
        def archive(name='default.conf',kind=tarfile.REGTYPE,size=3):
            stream=io.BytesIO()
            with tarfile.open(fileobj=stream,mode='w') as tar:
                entry=tarfile.TarInfo(name);entry.type=kind;entry.size=size if kind==tarfile.REGTYPE else 0;entry.linkname='/private/secret' if kind==tarfile.SYMTYPE else ''
                tar.addfile(entry,io.BytesIO(b'x'*entry.size) if entry.isfile() else None)
            return stream.getvalue()
        self.assertEqual(repair.config_bytes(archive()),b'xxx')
        for raw in (archive('../default.conf'),archive(kind=tarfile.SYMTYPE),archive(size=65537)):
            with self.assertRaises(ValueError):repair.config_bytes(raw)


if __name__=='__main__':unittest.main()
