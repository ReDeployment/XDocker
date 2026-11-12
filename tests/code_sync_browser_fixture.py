"""Disposable full Git pipeline; only SSH transport and service control are replaced."""
import base64
import json
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'services/admin'),str(ROOT/'tests')]
from werkzeug.serving import make_server,WSGIRequestHandler
from app import create_app
from native_runtime import initialize
from docker_api import DockerError
from code_sync import CodeSyncWorker,default_policy
from sync_runtime import git,GitTransfer,text_git
from sync_agent import CopyAgent,agent_sha
from host_metrics import atomic_json,private_directory


def main():
    class Quiet(WSGIRequestHandler):
        def log(self,*args,**kwargs):pass
    class NoDocker:
        def __init__(self):self.calls=[]
        def request(self,method,*args,**kwargs):self.calls.append(method);assert method=='GET';raise DockerError('No Docker in fixture')
    with tempfile.TemporaryDirectory(prefix='xdocker-code-sync-browser-') as raw:
        root=Path(raw).resolve();state=root/'state';initialize(state,ROOT)
        workspace=root/'workspace';workspace.mkdir();source=workspace/'project';source.mkdir()
        git(source,'init','-b','main');git(source,'config','user.name','Fixture');git(source,'config','user.email','fixture@example.invalid')
        (source/'app.py').write_text('print("fixture")\n');(source/'.gitignore').write_text('.env\n')
        git(source,'add','.');git(source,'commit','-m','initial');git(source,'remote','add','origin','https://github.com/example/project.git')
        original_head=text_git(source,'rev-parse','HEAD');original_index=(source/'.git/index').read_bytes();(source/'app.py').write_text('print("uncommitted fixture")\n')
        (source/'scratch.tmp').write_text('private untracked scratch')
        (source/'.env').write_text('PRIVATE_FIXTURE=not-for-upload')
        remote=root/'remote.git';git(root,'init','--bare',str(remote));copies=root/'copies';copies.mkdir();copy_path=copies/'project'
        agent_root=private_directory(root/'agent');policy_file=agent_root/'policy.json'
        atomic_json(policy_file,{'schema':2,'roots':[str(copies)],'credentials':{},'targets':{'example':{'directory':str(copy_path),'repository':'example/project','branch':'main','credential':None,'service':None}}})
        resolver=lambda repo:str(remote);agent=CopyAgent(policy_file,resolver)
        docker=NoDocker();app=create_app({'TESTING':True,'NATIVE_MODE':True,'NATIVE_OPERATIONS_ENABLED':True,'STATE':str(state),'CONFIG':str(state/'native-config'),
            'REPORTS':str(state/'reports'),'BACKUPS':str(state/'backups'),'HOST_ROOT':str(ROOT),'HOST_METRICS':str(state/'host-metrics')},docker)
        hub=app.extensions['code_sync'];policy=default_policy();policy.update(execution=True,source_roots=[str(workspace)],identities={'fixture':str(root/'fixture-key')});hub.import_policy(policy)
        hub.transfer=GitTransfer(private_directory(root/'cache'),resolver);hub.transport=lambda device,body:agent.handle({**body,'expected_agent_sha':agent_sha()})
        worker=CodeSyncWorker(hub);worker.start()
        server=make_server('127.0.0.1',0,app,threaded=True,request_handler=Quiet)
        print(json.dumps({'url':f'http://127.0.0.1:{server.server_port}/#code-sync','fixture_token':(state/'access-token').read_text().strip(),
                          'directory':str(source),'copy_directory':str(copy_path),'host_key':'ssh-ed25519 '+base64.b64encode(b'x'*40).decode()}),flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:worker.close();server.server_close();app.extensions['github_watch'].close()
        assert all(m=='GET' for m in docker.calls)
        assert text_git(source,'rev-parse','HEAD')==original_head and (source/'.git/index').read_bytes()==original_index
        assert copy_path.is_dir() and not (copy_path/'.env').exists() and not (copy_path/'scratch.tmp').exists()
        assert (copy_path/'app.py').read_text()=='print("fixture")\n'
        assert text_git(copy_path,'rev-parse','HEAD')==original_head
        assert any(j['state']=='success' for j in hub.store.all('jobs'))


if __name__=='__main__':main()
