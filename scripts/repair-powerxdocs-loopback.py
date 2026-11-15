#!/usr/bin/env python3
"""Explicit one-off recovery of the approved PowerXDocs main container.

Not a web API or automatic project launcher. Preserve the stopped original and
immutable image, including its customized Nginx config. Never touches preview.
"""
import argparse
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
from urllib.parse import quote

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/admin'))
from docker_api import Docker
from host_metrics import atomic_json,read_json,private_directory
from native_services import ServiceHub,fingerprint
from gateway_runtime import GatewayRuntime

NAME='powerxdocs-local'
CONFIG='/etc/nginx/conf.d/default.conf'
BINDING={'80/tcp':[{'HostIp':'127.0.0.1','HostPort':'8080'}]}


def require(condition,code):
    if not condition:raise ValueError(code)


def create_spec(original, expected_id, expected_image):
    require(re.fullmatch(r'[a-f0-9]{64}',expected_id) is not None,'XD_DOCS_ID_INVALID')
    require(re.fullmatch(r'sha256:[a-f0-9]{64}',expected_image) is not None,'XD_DOCS_IMAGE_INVALID')
    require(original['Id']==expected_id and original['Image']==expected_image,'XD_DOCS_SOURCE_CHANGED')
    require(original['Name']=='/'+NAME and original['State']['Status']=='exited','XD_DOCS_SOURCE_NOT_STOPPED')
    host=original['HostConfig']
    require(not original.get('Mounts') and not original['Config'].get('Volumes') and not host.get('Binds') and not host.get('VolumesFrom'),'XD_DOCS_HAS_PERSISTENT_MOUNTS')
    require(host['NetworkMode']=='bridge' and not host.get('Privileged') and not host.get('AutoRemove') and not host.get('PublishAllPorts') and not host.get('ContainerIDFile'),'XD_DOCS_UNEXPECTED_RUNTIME')
    require(set(host['PortBindings'])=={'80/tcp'} and host['RestartPolicy']['Name']=='unless-stopped','XD_DOCS_UNEXPECTED_RUNTIME')
    spec=deepcopy(original['Config']);spec['Image']=expected_image
    spec['HostConfig']=deepcopy(host);spec['HostConfig']['PortBindings']=deepcopy(BINDING)
    # Validation candidates must not acquire automatic startup before cutover.
    spec['HostConfig']['RestartPolicy']={'Name':'no','MaximumRetryCount':0}
    return spec


def config_bytes(raw):
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        members=archive.getmembers()
        require(len(members)==1 and members[0].name=='default.conf' and members[0].isfile() and 0<members[0].size<=65536,'XD_DOCS_CONFIG_ARCHIVE_INVALID')
        data=archive.extractfile(members[0]).read(65537)
        require(len(data)==members[0].size,'XD_DOCS_CONFIG_ARCHIVE_INVALID')
        return data


def get_config(engine,identifier):
    return config_bytes(engine.request('GET',f'/containers/{identifier}/archive?path='+quote(CONFIG,safe=''),raw=True))


def validate_docs_changes(changes):
    require(isinstance(changes,list) and len(changes)<=4096,'XD_DOCS_CHANGES_INVALID')
    require(not any(r.get('Path','').startswith('/usr/share/nginx/html') for r in changes),'XD_DOCS_CONTENT_LAYER_CHANGED')


def check_http():
    client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with client.open('http://127.0.0.1:8080/',timeout=2) as response:
        require(response.status==200,'XD_DOCS_HTTP_UNHEALTHY')
        body=response.read(1024*1024+1)
    require(len(body)<=1024*1024 and b'<html' in body.lower(),'XD_DOCS_HTTP_INVALID')
    return hashlib.sha256(body).hexdigest()


def prepare(engine,state,backup,expected_id,expected_image):
    original=engine.request('GET','/containers/'+NAME+'/json')
    spec=create_spec(original,expected_id,expected_image)
    validate_docs_changes(engine.request('GET',f'/containers/{expected_id}/changes'))
    conf=get_config(engine,expected_id)
    hub=ServiceHub(state);gateway=GatewayRuntime(hub);preview=gateway.preview()
    require(preview['fingerprint']==preview['applied_fingerprint'],'XD_DOCS_GATEWAY_HAS_PENDING_CHANGES')
    require(preview['configuration']==(gateway.root/'active.conf').read_text(),'XD_DOCS_GATEWAY_CONFIG_CHANGED')
    policy=hub.policy()
    require(policy['endpoints']['powerx-docs']=={'scheme':'http','host':'100.100.0.3','port':8080,'health_path':'/','expected_status':200},'XD_DOCS_ENDPOINT_CHANGED')
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind(('127.0.0.1',8080));sock.listen(1)
    backup=Path(backup).absolute()
    require(backup.parent.resolve()==(state/'backups').resolve() and not backup.exists(),'XD_DOCS_BACKUP_PATH_INVALID')
    backup.mkdir(mode=0o700);private_directory(backup)
    hub.store.backup(backup/'registry.sqlite3')
    atomic_json(backup/'original-container.json',original)
    atomic_json(backup/'original-policy.json',policy)
    atomic_json(backup/'original-projects.json',hub.data()['projects'])
    (backup/'default.conf').write_bytes(conf);(backup/'default.conf').chmod(0o600)
    (backup/'gateway-active.conf').write_text(preview['configuration']);(backup/'gateway-active.conf').chmod(0o600)
    suffix=time.strftime('%Y%m%dT%H%M%S',time.gmtime())
    plan={'schema':1,'original_id':expected_id,'image_id':expected_image,'candidate_name':NAME+'-recovery-'+suffix,
          'retired_name':NAME+'-retired-'+suffix,'config_sha256':hashlib.sha256(conf).hexdigest(),
          'gateway_fingerprint':preview['fingerprint'],'stage':'creating'}
    atomic_json(backup/'plan.json',plan)
    result=engine.request('POST','/containers/create?name='+plan['candidate_name'],body=spec)
    candidate=result['Id'];require(re.fullmatch(r'[a-f0-9]{64}',candidate) is not None,'XD_DOCS_CANDIDATE_ID_INVALID')
    plan.update(candidate_id=candidate,stage='created');atomic_json(backup/'plan.json',plan)
    subprocess.run(['docker','--host','unix:///var/run/docker.sock','cp',str(backup/'default.conf'),candidate+':'+CONFIG],check=True,capture_output=True,timeout=15)
    require(get_config(engine,candidate)==conf,'XD_DOCS_CONFIG_COPY_MISMATCH')
    engine.request('POST',f'/containers/{candidate}/start')
    deadline=time.monotonic()+20
    while True:
        try:plan['homepage_sha256']=check_http();break
        except (OSError,ValueError):
            if time.monotonic()>=deadline:raise ValueError('XD_DOCS_CANDIDATE_HTTP_FAILED') from None
            time.sleep(.25)
    current=engine.request('GET',f'/containers/{candidate}/json')
    require(current['Image']==expected_image and current['HostConfig']['PortBindings']==BINDING,'XD_DOCS_CANDIDATE_CHANGED')
    require(get_config(engine,candidate)==conf,'XD_DOCS_CONFIG_CHANGED_ON_START')
    validate_docs_changes(engine.request('GET',f'/containers/{candidate}/changes'))
    plan['stage']='prepared';atomic_json(backup/'plan.json',plan)
    return {k:plan[k] for k in ('stage','original_id','candidate_id','image_id','candidate_name','homepage_sha256')}


def activate(engine,state,backup):
    backup=Path(backup).resolve();require(backup.parent==(state/'backups').resolve(),'XD_DOCS_BACKUP_PATH_INVALID')
    plan=read_json(backup/'plan.json');require(plan['stage']=='prepared','XD_DOCS_NOT_PREPARED')
    old=engine.request('GET',f'/containers/{plan["original_id"]}/json')
    original=read_json(backup/'original-container.json')
    require(old['Name']=='/'+NAME and old['State']['Status']=='exited' and old['Config']==original['Config'] and old['HostConfig']==original['HostConfig'],'XD_DOCS_SOURCE_CHANGED')
    new=engine.request('GET',f'/containers/{plan["candidate_id"]}/json')
    require(new['Name']=='/'+plan['candidate_name'] and new['State']['Running'] and new['Image']==plan['image_id'] and new['HostConfig']['PortBindings']==BINDING,'XD_DOCS_CANDIDATE_CHANGED')
    require(hashlib.sha256(get_config(engine,plan['candidate_id'])).hexdigest()==plan['config_sha256'],'XD_DOCS_CONFIG_CHANGED')
    require(check_http()==plan['homepage_sha256'],'XD_DOCS_CANDIDATE_HTTP_CHANGED')
    hub=ServiceHub(state);old_policy=read_json(backup/'original-policy.json');old_projects=read_json(backup/'original-projects.json')
    require(hub.policy()==old_policy and hub.data()['projects']==old_projects,'XD_DOCS_REGISTRY_CHANGED')
    engine.request('POST',f'/containers/{plan["original_id"]}/rename?name='+plan['retired_name'])
    plan['stage']='original-retired';atomic_json(backup/'plan.json',plan)
    engine.request('POST',f'/containers/{plan["original_id"]}/update',body={'RestartPolicy':{'Name':'no'}})
    engine.request('POST',f'/containers/{plan["candidate_id"]}/rename?name='+NAME)
    engine.request('POST',f'/containers/{plan["candidate_id"]}/update',body={'RestartPolicy':original['HostConfig']['RestartPolicy']})
    plan['stage']='container-active';atomic_json(backup/'plan.json',plan)
    policy=deepcopy(old_policy);policy['revision']+=1;policy['endpoints']['powerx-docs']['host']='127.0.0.1'
    hub.import_policy(policy)
    plan.update(stage='gateway-apply-required',policy_sha256=fingerprint(policy));atomic_json(backup/'plan.json',plan)
    return {k:plan[k] for k in ('stage','original_id','candidate_id','retired_name','image_id','policy_sha256')}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','activate'))
    parser.add_argument('--state',required=True);parser.add_argument('--backup',required=True)
    parser.add_argument('--expected-container-id');parser.add_argument('--expected-image-id')
    args=parser.parse_args();state=Path(args.state).resolve()
    require(read_json(state/'native-runtime.json')=={'schema':1,'checkout':str(ROOT)},'XD_DOCS_WRONG_CHECKOUT')
    try:
        if args.action=='prepare':
            if not args.expected_container_id or not args.expected_image_id:parser.error('prepare requires exact container and image IDs')
            result=prepare(Docker(),state,args.backup,args.expected_container_id,args.expected_image_id)
        else:result=activate(Docker(),state,args.backup)
        print(json.dumps(result))
    except Exception as exc:
        # Full inspect/config stays in the private backup; never echo Docker/env.
        code=str(exc) if isinstance(exc,ValueError) and re.fullmatch(r'XD_DOCS_[A-Z_]+',str(exc)) else 'XD_DOCS_REPAIR_STOPPED'
        print(code+': inspect the private plan checkpoint before retrying',file=sys.stderr)
        raise SystemExit(1)
