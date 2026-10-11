import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import plistlib
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('admin_tunnel', ROOT/'scripts/admin_tunnel.py')
tunnel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tunnel)


class HealthHandler(BaseHTTPRequestHandler):
    code = 200
    body = b'{"status":"ok"}'
    def do_GET(self):
        self.send_response(self.code)
        self.end_headers()
        self.wfile.write(self.body)
    def log_message(self, *_):pass


class TunnelTests(unittest.TestCase):
    def test_system_ssh_is_noninteractive_and_does_not_share_connections(self):
        config=tunnel.configuration('ubuntu@host-alias')
        args=tunnel.ssh_command(config,'/usr/bin/ssh')
        for value in ('StrictHostKeyChecking=yes','BatchMode=yes','ExitOnForwardFailure=yes',
                      'ControlMaster=no','ControlPath=none','ServerAliveInterval=30','ServerAliveCountMax=3'):
            self.assertIn(value,args)
        self.assertIn('127.0.0.1:19080:127.0.0.1:9080',args)
        self.assertNotIn('StrictHostKeyChecking=no',args)
        for value in ('-oProxyCommand=evil','host; command','host\nother','https://host',''):
            with self.assertRaises(ValueError):tunnel.configuration(value)
        for value in (0,65536,True,'19080'):
            with self.assertRaises(ValueError):tunnel.configuration('host',value)

    def test_task_is_scoped_to_checkout_and_port_without_credentials(self):
        config=tunnel.configuration('alias')
        agent=tunnel.descriptor(ROOT,config)
        self.assertEqual(plistlib.loads(plistlib.dumps(agent)),agent)
        self.assertEqual(agent['KeepAlive'],{'SuccessfulExit':False})
        self.assertNotIn('EnvironmentVariables',agent)
        self.assertNotIn('alias',str(agent))
        self.assertNotEqual(agent['Label'],tunnel.descriptor('/tmp/other-checkout',config)['Label'])
        self.assertNotEqual(agent['Label'],tunnel.descriptor(ROOT,tunnel.configuration('alias',19081))['Label'])

    def test_health_requires_expected_api_response_and_rejects_redirect(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),HealthHandler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            for code,body,state in [(200,b'{"status":"ok"}','connected'),(503,b'fail','service-error'),
                                    (302,b'','unexpected-response'),(200,b'<html>proxy</html>','unexpected-response'),
                                    (200,b'[]','unexpected-response')]:
                with patch.object(HealthHandler,'code',code),patch.object(HealthHandler,'body',body):
                    self.assertEqual(tunnel.health(server.server_port),state)
        finally:server.shutdown();server.server_close();worker.join()
        self.assertEqual(tunnel.health(server.server_port),'api-unreachable')

    def test_occupied_port_leaves_owner_alive_and_never_starts_ssh(self):
        with tempfile.TemporaryDirectory() as name,socket.socket() as owner:
            owner.bind(('127.0.0.1',0));owner.listen()
            config=tunnel.configuration('alias',owner.getsockname()[1])
            with patch.object(tunnel.subprocess,'Popen') as spawn:
                tunnel.supervise(name,config,'/unused')
                spawn.assert_not_called()
            self.assertEqual(json.loads((tunnel.folder(name,config['local_port'])/'status.json').read_text())['state'],'port-conflict')
            self.assertFalse(tunnel.port_free(config['local_port']))

    def test_duplicate_supervisor_cannot_replace_active_configuration(self):
        with tempfile.TemporaryDirectory() as name:
            config=tunnel.configuration('original',19442)
            directory=tunnel.folder(name,19442);tunnel.save(directory/'config.json',config)
            with (directory/'supervisor.lock').open('a') as lock:
                tunnel.fcntl.flock(lock,tunnel.fcntl.LOCK_EX)
                self.assertTrue(tunnel.supervisor_active(name,19442))
                with self.assertRaises(ValueError):tunnel.supervise(name,tunnel.configuration('other',19442),'/unused')
            self.assertEqual(json.loads((directory/'config.json').read_text()),config)
            self.assertFalse(tunnel.supervisor_active(name,19442))

    def test_status_files_private_and_symlink_directory_rejected(self):
        with tempfile.TemporaryDirectory() as name,tempfile.TemporaryDirectory() as other:
            path=tunnel.folder(name,19080)/'config.json';tunnel.save(path,{'fixture':True})
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertEqual(path.parent.stat().st_mode&0o777,0o700)
            (Path(name)/'data/admin-tunnel/19081').symlink_to(other)
            with self.assertRaises(ValueError):tunnel.save(tunnel.folder(name,19081)/'config.json',{})
            self.assertEqual(list(Path(other).iterdir()),[])

    def test_launchd_start_and_stop_refuse_unowned_task_and_occupied_port(self):
        config=tunnel.configuration('alias')
        class Result:
            def __init__(self,code):self.returncode=code
        with tempfile.TemporaryDirectory() as name,tempfile.TemporaryDirectory() as home,patch.object(tunnel.sys,'platform','darwin'):
            with patch.object(tunnel,'launchctl',return_value=Result(0)) as launch:
                with self.assertRaises(ValueError):tunnel.manage('stop',name,config,Path(home))
                self.assertEqual(launch.call_count,1)
            with patch.object(tunnel,'launchctl',return_value=Result(1)),patch.object(tunnel,'port_free',return_value=False):
                with self.assertRaises(ValueError):tunnel.manage('start',name,config,Path(home))
                self.assertFalse((Path(home)/'Library').exists())
            plist=Path(home)/'Library/LaunchAgents'/(tunnel.descriptor(name,config)['Label']+'.plist')
            plist.parent.mkdir(parents=True);plist.write_bytes(plistlib.dumps({'ProgramArguments':['unrelated']}))
            with patch.object(tunnel,'launchctl',return_value=Result(0)) as launch:
                with self.assertRaises(ValueError):tunnel.manage('stop',name,config,Path(home))
                self.assertTrue(plist.exists());self.assertEqual(launch.call_count,1)

    def test_real_child_retries_network_failure_recovers_and_stops_own_child(self):
        # A fixture process implements a loopback HTTP listener; no real SSH/network interruption.
        with tempfile.TemporaryDirectory() as name,socket.socket() as allocator:
            allocator.bind(('127.0.0.1',0));port=allocator.getsockname()[1];allocator.close()
            script=Path(name)/'ssh-fixture'
            script.write_text('#!'+tunnel.sys.executable+'\n'+'''import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
flag=Path(__file__).with_suffix('.attempt')
if not flag.exists():
 flag.write_text('first')
 print('Connection reset by peer fixture-secret',file=sys.stderr)
 sys.exit(1)
port=int(sys.argv[sys.argv.index('-L')+1].split(':')[1])
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'{"status":"ok"}')
 def log_message(self,*args):pass
HTTPServer(('127.0.0.1',port),Handler).serve_forever()
''');script.chmod(0o700)
            stop=threading.Event();config=tunnel.configuration('fixture',port)
            states=[];original_save=tunnel.save
            def capture(path,data):
                original_save(path,data)
                if Path(path).name=='status.json':
                    states.append(data)
                    if data['state']=='connected':stop.set()
            with patch.object(tunnel,'save',side_effect=capture),patch.object(tunnel,'retry_delay',return_value=.03):
                worker=threading.Thread(target=tunnel.supervise,args=(name,config,script,stop,.03),daemon=True)
                worker.start();worker.join(timeout=8)
                if worker.is_alive():stop.set();worker.join(timeout=6)
            self.assertFalse(worker.is_alive())
            codes=[s['state'] for s in states]
            self.assertIn('reconnecting',codes);self.assertIn('connected',codes);self.assertEqual(codes[-1],'stopped')
            self.assertNotIn('fixture-secret',json.dumps(states))
            self.assertTrue(tunnel.port_free(port))

    def test_managed_task_start_stop_use_only_own_identity(self):
        config=tunnel.configuration('fixture-alias',19445)
        class Result:
            def __init__(self,code):self.returncode=code
        calls=[];loaded=False
        def fake_launch(*args,**kwargs):
            nonlocal loaded
            calls.append(args)
            if args[0]=='print':return Result(0 if loaded else 1)
            if args[0]=='bootstrap':loaded=True
            if args[0]=='bootout':loaded=False
            return Result(0)
        with tempfile.TemporaryDirectory() as name,tempfile.TemporaryDirectory() as home,patch.object(tunnel.sys,'platform','darwin'),patch.object(tunnel,'launchctl',side_effect=fake_launch),patch.object(tunnel,'port_free',return_value=True),patch('builtins.print'):
            tunnel.manage('start',name,config,Path(home))
            files=list((Path(home)/'Library/LaunchAgents').glob('*.plist'))
            self.assertEqual(len(files),1);self.assertEqual(files[0].stat().st_mode&0o777,0o600)
            stored=tunnel.folder(name,19445)/'config.json'
            self.assertEqual(json.loads(stored.read_text()),config)
            with self.assertRaises(ValueError):tunnel.manage('start',name,config,Path(home))
            tunnel.manage('stop',name,config,Path(home))
            self.assertFalse(files[0].exists());self.assertTrue(stored.exists())
            bootout=[c for c in calls if c[0]=='bootout']
            self.assertEqual(bootout,[('bootout','gui/'+str(tunnel.os.getuid())+'/'+tunnel.descriptor(name,config)['Label'])])

    def test_stop_never_kills_foreground_supervisor_by_pid(self):
        class Result:returncode=1
        with tempfile.TemporaryDirectory() as name,tempfile.TemporaryDirectory() as home,patch.object(tunnel.sys,'platform','darwin'),patch.object(tunnel,'launchctl',return_value=Result()),patch.object(tunnel,'supervisor_active',return_value=True):
            with self.assertRaisesRegex(ValueError,'Ctrl\\+C'):
                tunnel.manage('stop',name,tunnel.configuration('alias'),Path(home))

    def test_auth_failure_is_terminal_and_not_logged(self):
        self.assertEqual(tunnel.ssh_failure('Permission denied (publickey). secret'),'authentication-error')
        self.assertEqual(tunnel.ssh_failure('Host key verification failed.'),'host-key-error')
        self.assertEqual(tunnel.ssh_failure('bind: Address already in use'),'port-conflict')
        self.assertEqual([tunnel.retry_delay(i) for i in range(8)],[5,10,20,40,60,60,60,60])
        with tempfile.TemporaryDirectory() as name,socket.socket() as allocator:
            allocator.bind(('127.0.0.1',0));port=allocator.getsockname()[1];allocator.close()
            script=Path(name)/'ssh-fixture';script.write_text('#!/bin/sh\nprintf "Permission denied (publickey) fixture-secret" >&2\nexit 255\n');script.chmod(0o700)
            with patch.object(tunnel,'retry_delay') as retry:
                tunnel.supervise(name,tunnel.configuration('fixture',port),script,poll_interval=.02)
                retry.assert_not_called()
            text=(tunnel.folder(name,port)/'status.json').read_text()
            self.assertEqual(json.loads(text)['state'],'authentication-error');self.assertNotIn('fixture-secret',text)
