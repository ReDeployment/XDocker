import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('local_tunnel_manager',ROOT/'clients/ssh-tunnel/manager.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        m.initialize(self.root);self.control=Mock();self.manager=m.Manager(self.root,self.control)
        launcher=patch.object(m.tunnel,'launchctl',return_value=Mock(returncode=1))
        launcher.start();self.addCleanup(launcher.stop)
        self.server=m.Server(('127.0.0.1',0),m.Handler);self.server.manager=self.manager
        self.manager.port=self.server.server_port
        self.origin='http://127.0.0.1:'+str(self.server.server_port)
        self.worker=threading.Thread(target=self.server.serve_forever,daemon=True);self.worker.start()
        self.client=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.worker.join();self.temp.cleanup()

    def request(self,path='/api/tunnels',body=None,token=True,origin=None,host=None,raw=None):
        headers={'Origin':origin or self.origin}
        if token:headers['X-XDocker-Local-Token']=self.manager.token
        if body is not None or raw is not None:headers['Content-Type']='application/json'
        if host:headers['Host']=host
        data=raw if raw is not None else json.dumps(body).encode() if body is not None else None
        try:
            response=self.client.open(urllib.request.Request(self.origin+path,data=data,headers=headers),timeout=3)
        except urllib.error.HTTPError as error:response=error
        with response:return response.status,response.read(),response.headers

    def body(self,action='connect',port=19181,version=None):
        return {'action':action,'destination':'test-alias','local_port':port,'server_port':9080,'version':version,'confirmation':'test-alias'}

    def wait(self):
        for _ in range(100):
            if self.manager.jobs and self.manager.jobs[0]['state']!='running':return
            time.sleep(.01)
        self.fail('operation did not finish')

    def test_readonly_authentication_host_and_origin_boundaries(self):
        self.assertEqual(self.request(token=False)[0],401)
        self.assertEqual(self.request(origin='https://evil.example')[0],403)
        self.assertEqual(self.request(host='evil.example:'+str(self.server.server_port))[0],403)
        self.assertEqual(self.request('/healthz',token=False)[0],200)
        self.assertEqual(self.request()[0],200)
        self.control.assert_not_called()

    def test_static_page_and_frame_ancestors_are_local_only(self):
        code,body,headers=self.request('/',token=False)
        self.assertEqual(code,200);self.assertIn('SSH'.encode(),body)
        self.assertIn(self.origin,headers['Content-Security-Policy'])
        self.assertNotIn('https://evil.example',headers['Content-Security-Policy'])
        self.assertEqual(self.request('/../manager.py',token=False)[0],404)

    def test_input_rejection_never_runs_control(self):
        with patch.object(m.sys,'platform','darwin'),patch.object(m.tunnel,'port_free',return_value=True):
            for change in [{'action':'shell'},{'destination':'-oProxyCommand=evil'},{'local_port':True},{'local_port':0},{'confirmation':'wrong'},{'version':'stale'},{'shell':'evil'},{'local_port':self.server.server_port}]:
                body=self.body();body.update(change)
                self.assertEqual(self.request('/api/tunnels/action',body)[0],409,change)
            self.assertEqual(self.request('/api/tunnels/action',raw=b'{"action":"connect","action":"disconnect"}')[0],409)
        self.control.assert_not_called()

    def test_connect_and_reconnect_use_fixed_owned_configuration(self):
        def control(action,root,config):
            if action=='start':m.tunnel.save(m.tunnel.folder(root,config['local_port'])/'config.json',config)
        self.control.side_effect=control
        with patch.object(m.sys,'platform','darwin'),patch.object(m.tunnel,'port_free',return_value=True),patch.object(m.tunnel,'supervisor_active',return_value=False):
            self.assertEqual(self.request('/api/tunnels/action',self.body())[0],202);self.wait()
            cfg=self.manager.config(19181)
            self.control.assert_called_once_with('start',self.root,cfg)
            self.assertEqual(self.request('/api/tunnels/action',self.body())[0],409)
            self.control.reset_mock()
            body=self.body('reconnect',version=self.manager.version(cfg))
            self.assertEqual(self.request('/api/tunnels/action',body)[0],202);self.wait()
            self.assertEqual([c.args[0] for c in self.control.call_args_list],['stop','start'])
            self.assertEqual(self.manager.jobs[0]['state'],'requested')

    def test_registered_foreground_or_foreign_tasks_are_not_killed(self):
        cfg=m.tunnel.configuration('test-alias',19181);m.tunnel.save(m.tunnel.folder(self.root,19181)/'config.json',cfg)
        self.control.side_effect=ValueError('前台连接由原终端停止。')
        with patch.object(m.sys,'platform','darwin'):
            self.assertEqual(self.request('/api/tunnels/action',self.body('disconnect',version=self.manager.version(cfg)))[0],202);self.wait()
        self.assertEqual(self.manager.jobs[0]['state'],'failed')
        self.assertEqual(self.control.call_count,1)

    def test_one_running_mutation_and_no_automatic_replay(self):
        gate=threading.Event();self.control.side_effect=lambda *args:gate.wait(2)
        with patch.object(m.sys,'platform','darwin'),patch.object(m.tunnel,'port_free',return_value=True),patch.object(m.tunnel,'supervisor_active',return_value=False):
            self.assertEqual(self.request('/api/tunnels/action',self.body())[0],202)
            self.assertEqual(self.request('/api/tunnels/action',self.body(port=19182))[0],409)
            self.assertEqual(self.request()[0],200)
            gate.set();self.wait()
        self.assertEqual(self.control.call_count,1)

    def test_reconnect_waits_for_launchagent_removal_before_start(self):
        cfg=m.tunnel.configuration('test-alias',19181)
        m.tunnel.save(m.tunnel.folder(self.root,19181)/'config.json',cfg)
        removed=threading.Event()
        def registered(config):return not removed.is_set()
        def control(action,root,config):
            if action=='start':self.assertTrue(removed.is_set())
        self.control.side_effect=control
        with patch.object(m.sys,'platform','darwin'),patch.object(m.tunnel,'port_free',return_value=True),patch.object(m.tunnel,'supervisor_active',return_value=False),patch.object(self.manager,'registered',side_effect=registered):
            self.assertEqual(self.request('/api/tunnels/action',self.body('reconnect',version=self.manager.version(cfg)))[0],202)
            time.sleep(.05)
            self.assertEqual([c.args[0] for c in self.control.call_args_list],['stop'])
            removed.set();self.wait()
        self.assertEqual([c.args[0] for c in self.control.call_args_list],['stop','start'])

    def test_unsafe_config_and_token_fail_closed(self):
        p=m.tunnel.folder(self.root,19181);p.mkdir(parents=True)
        outside=self.root/'unsafe.json';outside.write_text('{}');outside.chmod(0o600)
        (p/'config.json').symlink_to(outside)
        self.assertEqual(self.request()[0],200)
        self.assertFalse(json.loads(self.request()[1])['profiles'][0]['manageable'])
        token=self.root/'data/tunnel-manager/access-token';token.chmod(0o644)
        with self.assertRaises(ValueError):m.Manager(self.root)

    def test_interrupted_operations_are_unconfirmed_not_replayed(self):
        ledger=self.root/'data/tunnel-manager/jobs.json'
        m.tunnel.save(ledger,[{'id':'old','state':'running','message':'old'}])
        manager=m.Manager(self.root,self.control)
        self.assertEqual(manager.jobs[0]['state'],'unconfirmed');self.control.assert_not_called()

    def test_terminal_authentication_error_is_preserved(self):
        cfg=m.tunnel.configuration('test-alias',19181)
        p=m.tunnel.folder(self.root,19181);m.tunnel.save(p/'config.json',cfg)
        m.tunnel.save(p/'status.json',{'state':'authentication-error'})
        with patch.object(m.tunnel,'supervisor_active',return_value=False),patch.object(m.tunnel,'launchctl',return_value=Mock(returncode=1)),patch.object(m.tunnel,'health',return_value='api-unreachable'):
            result=self.manager.status(19181)
        self.assertEqual(result['state'],'authentication-error');self.assertEqual(result['current_http'],'api-unreachable')


if __name__=='__main__':unittest.main()
