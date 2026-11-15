"""Generated Nginx routing, exact plan gates, ownership and real isolated proxy traffic."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import errno
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'services/admin'),str(ROOT/'tests')]
from gateway_runtime import GatewayRuntime, GatewayRecovery, atomic_text
from host_metrics import atomic_json, read_json
from native_services import ServiceHub, ServiceError, probe_endpoint
from test_native_services import policy, project


def free_port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        self.hub=ServiceHub(self.root/'state');value=policy();value['gateway'].update(http_port=free_port(),https_port=free_port());self.hub.import_policy(value)
        self.hub.save(project());self.calls=[];self.live=False;self.fail=False;self.marker_ok=True
        def runner(command,**kwargs):
            self.calls.append(command)
            if '-t' not in command:
                if self.fail:return {'code':1,'stdout':'private runtime detail','truncated':False}
                self.live=True;(self.gateway.root/'nginx.pid').write_text('9000')
            return {'code':0,'stdout':'','truncated':False}
        self.gateway=GatewayRuntime(self.hub,runner=runner,
            owner_reader=lambda *args:{'pid':9000,'started':42} if self.live else None,
            marker_reader=lambda *args:self.marker_ok)

    def enable(self):
        value=self.hub.policy();value['revision']+=1;value['gateway']['enabled']=True;self.hub.import_policy(value)

    def test_preview_routes_native_ports_websocket_no_container_dns(self):
        plan=self.gateway.preview();text=plan['configuration']
        self.assertIn('proxy_pass http://127.0.0.1:3000;',text)
        self.assertIn('proxy_set_header Upgrade $http_upgrade;',text)
        self.assertIn('proxy_set_header Connection $xdocker_connection;',text)
        for name in ('client_body','proxy','fastcgi','uwsgi','scgi'):
            self.assertIn(name+'_temp_path "'+str(self.gateway.root)+'/',text)
        self.assertNotIn('127.0.0.11',text);self.assertEqual(self.calls,[])
        with self.assertRaises(ServiceError):self.gateway.apply({'fingerprint':plan['fingerprint'],'confirmation':'xdocker-gateway'})

    def test_exact_plan_test_before_start_and_ownership_checks(self):
        self.enable();plan=self.gateway.preview()
        self.hub.save(project(revision=2,domain='changed.home.test'))
        with self.assertRaises(ServiceError):self.gateway.apply({'fingerprint':plan['fingerprint'],'confirmation':'xdocker-gateway'})
        self.assertEqual(self.calls,[])
        plan=self.gateway.preview();result=self.gateway.apply({'fingerprint':plan['fingerprint'],'confirmation':'xdocker-gateway'})
        self.assertEqual(result['state'],'applied');self.assertIn('-t',self.calls[0]);self.assertNotIn('-s',self.calls[1])
        self.assertEqual(self.hub.store.read_gateway()['fingerprint'],plan['fingerprint'])
        self.live=False
        with self.assertRaises(ServiceError):self.gateway.apply({'fingerprint':plan['fingerprint'],'confirmation':'xdocker-gateway'})

    def test_failed_reload_restores_previous_configuration(self):
        self.enable();first=self.gateway.preview();self.gateway.apply({'fingerprint':first['fingerprint'],'confirmation':'xdocker-gateway'})
        old=(self.gateway.root/'active.conf').read_text();self.hub.save(project(revision=2,domain='new.home.test'));next_plan=self.gateway.preview();self.fail=True
        with self.assertRaises(ServiceError):self.gateway.apply({'fingerprint':next_plan['fingerprint'],'confirmation':'xdocker-gateway'})
        self.assertEqual((self.gateway.root/'active.conf').read_text(),old)
        self.assertEqual(self.hub.store.read_gateway()['fingerprint'],first['fingerprint'])
        self.assertTrue(self.hub.store.read_gateway_error()['runtime_unconfirmed'])

    def test_foreign_pid_file_is_never_signalled(self):
        self.enable();(self.gateway.root/'nginx.pid').write_text('1234');plan=self.gateway.preview()
        with self.assertRaises(ServiceError):self.gateway.apply({'fingerprint':plan['fingerprint'],'confirmation':'xdocker-gateway'})
        self.assertFalse(self.calls)

    def test_empty_pid_from_nginx_test_can_start_but_nonempty_unknown_pid_cannot(self):
        self.enable();(self.gateway.root/'nginx.pid').touch(mode=0o600);plan=self.gateway.preview()
        self.assertEqual(self.gateway.apply({'fingerprint':plan['fingerprint'],'confirmation':'xdocker-gateway'})['state'],'applied')
        self.live=False;(self.gateway.root/'nginx.pid').write_text('invalid');before=len(self.calls)
        with self.assertRaisesRegex(ServiceError,'OWNER_UNCONFIRMED'):self.gateway.apply({'fingerprint':plan['fingerprint'],'confirmation':'xdocker-gateway'})
        self.assertEqual(len(self.calls),before)

    def test_recent_connections_do_not_prevent_restart_but_live_listener_does(self):
        listener=socket.socket();listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);listener.bind(('127.0.0.1',0));port=listener.getsockname()[1];listener.listen(1)
        client=socket.create_connection(('127.0.0.1',port));accepted,_=listener.accept()
        accepted.shutdown(socket.SHUT_WR);client.recv(1);client.close();accepted.close();listener.close()
        value=self.hub.policy();value['revision']+=1;value['gateway'].update(enabled=True,http_port=port);self.hub.import_policy(value)
        plan=self.gateway.preview();self.assertEqual(self.gateway.apply({'fingerprint':plan['fingerprint'],'confirmation':'xdocker-gateway'})['state'],'applied')
        self.live=False;(self.gateway.root/'nginx.pid').unlink();before=len(self.calls)
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);occupied.bind(('127.0.0.1',port));occupied.listen(1)
            with self.assertRaisesRegex(ServiceError,'PORT_IN_USE'):self.gateway.apply({'fingerprint':plan['fingerprint'],'confirmation':'xdocker-gateway'})
        self.assertEqual(len(self.calls),before)

    def test_bind_failures_distinguish_address_port_permission_and_other_errors(self):
        self.enable();plan=self.gateway.preview()
        for failure,code in ((OSError(errno.EADDRNOTAVAIL,'not ready'),'ADDRESS_UNAVAILABLE'),
                             (OSError(errno.EADDRINUSE,'in use'),'PORT_IN_USE'),
                             (PermissionError(errno.EACCES,'denied'),'BIND_PERMISSION_REQUIRED'),
                             (OSError(errno.ENETDOWN,'down'),'BIND_FAILED')):
            with patch('gateway_runtime.socket.socket') as sock:
                sock.return_value.__enter__.return_value.bind.side_effect=failure
                with self.assertRaisesRegex(ServiceError,'XD_GATEWAY_'+code):
                    self.gateway.apply({'fingerprint':plan['fingerprint'],'confirmation':'xdocker-gateway'})
        self.assertEqual(self.calls,[])

    def test_recovery_survives_long_address_delay_then_recovers_and_resets_backoff(self):
        now=time.time();messages=[];worker=GatewayRecovery(self.gateway,clock=lambda:now,log=messages.append)
        results=[ServiceError('XD_GATEWAY_ADDRESS_UNAVAILABLE')]*7+[{'state':'applied'}]
        with patch.object(self.gateway,'restore',side_effect=results) as restore:
            delays=[worker.tick() for _ in results]
        self.assertEqual(delays,[5,10,20,30,30,30,30,10]);self.assertEqual(restore.call_count,8)
        status=self.gateway.recovery_status();self.assertEqual(status['state'],'ready');self.assertEqual(status['failures'],0)
        self.assertEqual(len(messages),2);self.assertEqual((self.gateway.root/'recovery.json').stat().st_mode&0o777,0o600)

    def test_recovery_restarts_only_saved_gateway_after_exit_and_rejects_pending_edits(self):
        self.enable();plan=self.gateway.preview();self.gateway.apply({'fingerprint':plan['fingerprint'],'confirmation':'xdocker-gateway'})
        worker=GatewayRecovery(self.gateway,log=lambda message:None)
        before=len(self.calls);worker.tick();self.assertEqual(len(self.calls),before)
        self.live=False;(self.gateway.root/'nginx.pid').unlink();worker.tick()
        self.assertEqual(self.gateway.recovery_status()['state'],'ready');self.assertEqual(len(self.calls),before+2)
        self.assertEqual(self.hub.store.read_gateway()['fingerprint'],plan['fingerprint'])
        self.hub.save(project(revision=2,domain='pending.home.test'));before=len(self.calls)
        self.live=False;(self.gateway.root/'nginx.pid').unlink()
        worker.tick();worker.tick()
        status=self.gateway.recovery_status();self.assertEqual(status['state'],'blocked');self.assertEqual(status['error'],'XD_GATEWAY_PENDING_CONFIGURATION')
        self.assertEqual(len(self.calls),before);self.assertEqual(self.hub.store.read_gateway()['fingerprint'],plan['fingerprint'])

    def test_recovery_loop_stops_promptly_and_does_not_enable_unconfigured_gateway(self):
        worker=GatewayRecovery(self.gateway,log=lambda message:None)
        worker.tick();self.assertEqual(self.gateway.recovery_status()['state'],'not-configured')
        self.assertEqual(self.calls,[]);self.assertFalse(self.hub.policy()['gateway']['enabled'])
        class Stop:
            def __init__(self):self.delays=[]
            def is_set(self):return len(self.delays)>=3
            def wait(self,delay):self.delays.append(delay);return self.is_set()
        stop=Stop()
        with patch.object(self.gateway,'restore',side_effect=[ServiceError('XD_GATEWAY_ADDRESS_UNAVAILABLE')]*2+[{'state':'applied'}]) as restore:
            worker.run(stop)
        self.assertEqual(stop.delays,[5,10,10]);self.assertEqual(restore.call_count,3)

    def test_recovery_status_staleness_allowlist_and_private_exception_redaction(self):
        messages=[];worker=GatewayRecovery(self.gateway,clock=lambda:1,log=messages.append)
        with patch.object(self.gateway,'restore',side_effect=RuntimeError('private-super-secret')):worker.tick()
        self.assertNotIn('private-super-secret',' '.join(messages))
        self.assertEqual(self.gateway.recovery_status()['state'],'stale')
        snapshot=read_json(self.gateway.root/'recovery.json');snapshot.update(at=time.time(),private='never returned')
        atomic_json(self.gateway.root/'recovery.json',snapshot)
        self.assertNotIn('private',self.gateway.recovery_status())
        snapshot['error']='private-super-secret';atomic_json(self.gateway.root/'recovery.json',snapshot)
        self.assertEqual(self.gateway.recovery_status(),{'state':'unavailable'})

    def test_daemon_identity_is_polled_without_replaying_start(self):
        self.enable();reads=[]
        def owner(*args):
            if not self.live:return None
            reads.append(True)
            return {'pid':9000,'started':42} if len(reads)>1 else None
        self.gateway.owner_reader=owner;plan=self.gateway.preview()
        self.assertEqual(self.gateway.apply({'fingerprint':plan['fingerprint'],'confirmation':'xdocker-gateway'})['state'],'applied')
        self.assertEqual(len(reads),2);self.assertEqual(sum('-t' not in call for call in self.calls),1)

    def test_restore_only_applied_plan_and_reject_pending_edits(self):
        self.assertEqual(self.gateway.restore()['state'],'not-configured')
        self.enable();plan=self.gateway.preview();self.gateway.apply({'fingerprint':plan['fingerprint'],'confirmation':'xdocker-gateway'})
        before=len(self.calls);self.assertTrue(self.gateway.restore()['reused']);self.assertEqual(len(self.calls),before)
        self.live=False;(self.gateway.root/'nginx.pid').unlink()
        self.assertEqual(self.gateway.restore()['state'],'applied')
        self.hub.save(project(revision=2,domain='pending.home.test'))
        before=len(self.calls)
        with self.assertRaisesRegex(ServiceError,'PENDING_CONFIGURATION'):self.gateway.restore()
        self.assertEqual(len(self.calls),before)

    def test_restore_does_not_remove_pid_for_an_existing_process(self):
        self.enable();plan=self.gateway.preview();self.gateway.apply({'fingerprint':plan['fingerprint'],'confirmation':'xdocker-gateway'})
        receipt=self.hub.store.read_gateway();receipt['owner']['pid']=os.getpid();self.hub.store.write_gateway(receipt)
        self.live=False;(self.gateway.root/'nginx.pid').write_text(str(os.getpid()));before=len(self.calls)
        exists=Path.exists
        with patch.object(Path,'exists',lambda path:True if str(path)=='/proc/'+str(os.getpid()) else exists(path)):
            with self.assertRaisesRegex(ServiceError,'OWNER_UNCONFIRMED'):self.gateway.restore()
        self.assertTrue((self.gateway.root/'nginx.pid').exists());self.assertEqual(len(self.calls),before)


class RealProxyTests(unittest.TestCase):
    def test_real_nginx_config_http_and_websocket_upgrade_in_own_prefix(self):
        binary=shutil.which('nginx')
        if not binary:self.skipTest('Nginx binary unavailable; no host package installation')
        class Upstream(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*args):pass
            def do_GET(self):
                if self.headers.get('Upgrade')=='websocket':
                    self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','upgrade');self.end_headers();return
                self.send_response(200);body=b'independent-application';self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Upstream);server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='xdocker-native-proxy-') as raw:
                root=Path(raw).resolve();hub=ServiceHub(root/'state');value=policy()
                value['endpoints']['example']['port']=server.server_port
                value['gateway'].update(enabled=True,binary=binary,http_port=free_port(),https_port=free_port());hub.import_policy(value);hub.save(project())
                gateway=GatewayRuntime(hub)
                plan=gateway.preview();config=gateway.root/'active.conf';atomic_text(config,plan['configuration'])
                command=[binary,'-p',str(gateway.root)+'/', '-c',str(config)]
                checked=subprocess.run([*command,'-t'],capture_output=True,text=True,timeout=5)
                self.assertEqual(checked.returncode,0,checked.stderr)
                subprocess.run(command,capture_output=True,timeout=5,check=True)
                try:
                    self.assertTrue(gateway.marker(hub.policy(),plan['fingerprint']))
                    conn=http.client.HTTPConnection('127.0.0.1',value['gateway']['http_port'],timeout=3)
                    conn.request('GET','/healthz',headers={'Host':'example.home.test'});response=conn.getresponse()
                    self.assertEqual(response.status,200);self.assertEqual(response.read(),b'independent-application');conn.close()
                    conn=http.client.HTTPConnection('127.0.0.1',value['gateway']['http_port'],timeout=3)
                    conn.request('GET','/socket',headers={'Host':'example.home.test','Upgrade':'websocket','Connection':'Upgrade'});response=conn.getresponse()
                    self.assertEqual(response.status,101);self.assertEqual(response.getheader('Upgrade'),'websocket');conn.close()
                    self.assertEqual(probe_endpoint(value['endpoints']['example'])['state'],'healthy')
                finally:
                    subprocess.run([*command,'-s','quit'],capture_output=True,timeout=5,check=True)
                    for _ in range(50):
                        if not (gateway.root/'nginx.pid').exists():break
                        time.sleep(.05)
                    self.assertFalse((gateway.root/'nginx.pid').exists(),'Own Nginx did not stop')
        finally:server.shutdown();server.server_close();thread.join(timeout=2)


if __name__=='__main__':unittest.main()
