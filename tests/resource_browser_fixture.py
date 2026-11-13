"""Disposable monitoring reports + real Admin, no host/process/Docker access."""
import json
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'services/admin'),str(ROOT/'tests')]
from werkzeug.serving import make_server, WSGIRequestHandler
from app import create_app
from host_metrics import atomic_json, private_directory, write_history
from test_admin import FakeDocker
from test_resource_monitor import sample_report
from test_gpu_metrics import gpu_fixture
from test_storage_metrics import storage_fixture


def main():
    class Quiet(WSGIRequestHandler):
        def log(self,*args,**kwargs):pass
    with tempfile.TemporaryDirectory(prefix='xdocker-resource-browser-') as raw:
        root=Path(raw).resolve();state=private_directory(root/'state');config=root/'config';(config/'sites').mkdir(parents=True)
        (config/'root.env').write_text('ENABLED_SITES=""\nENABLED_SERVICES="admin"\n')
        (config/'certificates.json').write_text('{"certificates":[]}')
        token='fixture-resource-only-'+'x'*40;(state/'access-token').write_text(token);(state/'access-token').chmod(0o600)
        metrics=private_directory(root/'metrics');report=sample_report();report['gpu']=gpu_fixture();report['storage']=storage_fixture();atomic_json(metrics/'report.json',report);write_history(metrics,report)
        docker=FakeDocker();app=create_app({'TESTING':True,'STATE':str(state),'CONFIG':str(config),
            'REPORTS':str(root/'reports'),'HOST_METRICS':str(metrics)},docker)
        server=make_server('127.0.0.1',0,app,threaded=True,request_handler=Quiet)
        print(json.dumps({'url':f'http://127.0.0.1:{server.server_port}/#resources','fixture_token':token}),flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:
            server.server_close()
            for name in ('release_scheduler','github_watch'):
                if app.extensions.get(name): app.extensions[name].close()
        assert all(method=='GET' for method,_,_ in docker.calls),'Monitor granted runtime mutation'


if __name__=='__main__':main()
