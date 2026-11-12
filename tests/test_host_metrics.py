"""Linux procfs races, scope, counter identities and bounded collector history."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/admin'))
from host_metrics import proc_sample, docker_sample, collect, write_history, key, private_directory, read_json
from docker_api import DockerError


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.proc=self.root/'proc';self.proc.mkdir()
        boot=self.proc/'sys/kernel/random';boot.mkdir(parents=True);(boot/'boot_id').write_text('boot-one')
        (self.proc/'stat').write_text('cpu 10 0 20 100 0 0 0 0\n')
        (self.proc/'meminfo').write_text('MemTotal: 10000 kB\nMemAvailable: 8000 kB\nSwapTotal: 100 kB\nSwapFree: 90 kB\n')

    def process(self,pid=42,used=10,started=100,cgroup='0::/user.slice/session-1.scope',exe='/usr/bin/node',cwd='/work/project'):
        directory=self.proc/str(pid);directory.mkdir(exist_ok=True)
        tail=['0']*22;tail[0]='S';tail[1]='1';tail[11]=str(used);tail[19]=str(started);tail[21]='10'
        (directory/'stat').write_text(f'{pid} (worker ) name) '+ ' '.join(tail))
        (directory/'cgroup').write_text(cgroup)
        for name,value in [('exe',exe),('cwd',cwd)]:
            p=directory/name
            if p.is_symlink():p.unlink()
            p.symlink_to(value)
        return directory

    def test_counter_deltas_pid_reuse_boot_and_no_arguments(self):
        directory=self.process();(directory/'cmdline').write_text('secret-never-read');(directory/'environ').write_text('secret-never-read')
        report,previous=proc_sample(self.proc,clock=100)
        self.assertIsNone(report['targets'][0]['cpu_percent'])
        self.process(used=110);(self.proc/'stat').write_text('cpu 20 0 30 110 0 0 0 0\n')
        report,next_state=proc_sample(self.proc,previous,clock=102)
        self.assertAlmostEqual(report['targets'][0]['cpu_percent'],100/os.sysconf('SC_CLK_TCK')/2*100,places=2)
        self.assertAlmostEqual(report['host']['cpu_percent'],66.67)
        self.assertNotIn('secret-never-read',json.dumps(report));self.assertEqual(report['processes'][0]['name'],'worker ) name')
        self.process(used=10000,started=200)
        report,_=proc_sample(self.proc,next_state,clock=104);self.assertIsNone(report['targets'][0]['cpu_percent'])
        (self.proc/'sys/kernel/random/boot_id').write_text('boot-two')
        report,_=proc_sample(self.proc,next_state,clock=105);self.assertIsNone(report['host']['cpu_percent'])

    def test_systemd_does_not_require_executable_access_and_container_excluded(self):
        self.process(cgroup='0::/system.slice/powerx.service');(self.proc/'42/exe').unlink();(self.proc/'42/cwd').unlink()
        self.process(43,cgroup='0::/system.slice/docker-'+ 'a'*64+'.scope')
        report,_=proc_sample(self.proc,clock=100)
        self.assertEqual(len(report['targets']),1);self.assertEqual(report['targets'][0]['kind'],'systemd')
        self.assertEqual(report['coverage']['container_processes_excluded'],1)
        self.assertEqual(report['targets'][0]['pids'],1)

    def test_capacity_and_inaccessible_processes_are_explicit(self):
        self.process();self.process(43)
        with patch('host_metrics.MAX_PROCESSES',1):report,_=proc_sample(self.proc)
        self.assertTrue(report['coverage']['process_limit_reached'])
        (self.proc/'42/exe').unlink();report,_=proc_sample(self.proc)
        self.assertEqual(report['coverage']['processes_unreadable'],1)

    def test_readonly_docker_stable_recreation_and_partial_failure(self):
        class Engine:
            def __init__(self):self.identifier='a'*64;self.calls=[];self.failed=False
            def request(self,method,path,**kwargs):
                self.calls.append((method,path));assert method=='GET'
                if path.startswith('/containers/json'):return [{'Id':self.identifier,'Labels':{'com.docker.compose.project':'project','com.docker.compose.service':'web'},'State':'running','Image':'example:v1'}]
                if self.failed:raise DockerError('unavailable')
                return {'cpu_stats':{'cpu_usage':{'total_usage':100},'system_cpu_usage':200,'online_cpus':2},'memory_stats':{'usage':1000,'stats':{'inactive_file':300}},'pids_stats':{'current':3}}
        engine=Engine();rows,counters,_=docker_sample(engine)
        self.assertEqual(rows[0]['memory_bytes'],700);self.assertIsNone(rows[0]['cpu_percent'])
        stable=rows[0]['id'];engine.identifier='b'*64;rows,_,_=docker_sample(engine,counters)
        self.assertEqual(rows[0]['id'],stable);self.assertIsNone(rows[0]['cpu_percent'])
        engine.failed=True;rows,_,coverage=docker_sample(engine)
        self.assertFalse(rows[0]['complete']);self.assertIsNone(rows[0]['memory_bytes']);self.assertEqual(coverage['docker_incomplete'],1)

    def test_collection_private_report_history_retention_and_busy(self):
        self.process();output=self.root/'metrics'
        class EmptyDocker:
            def request(self,method,path,**kwargs):assert method=='GET';return []
        result=collect(output,self.proc,EmptyDocker());self.assertTrue(result['sampled'])
        self.assertEqual((output/'report.json').stat().st_mode&0o777,0o600)
        self.assertEqual(output.stat().st_mode&0o777,0o700)
        report=read_json(output/'report.json')
        with sqlite3.connect(output/'history.sqlite3') as db:
            db.execute('INSERT INTO samples VALUES(?,?,?,?,?)',(int(time.time())-90000,'old',1,1,1))
        write_history(output,report)
        with sqlite3.connect(output/'history.sqlite3') as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM samples WHERE target='old'").fetchone()[0],0)
        (output/'report.json').chmod(0o644)
        with self.assertRaises(ValueError):collect(output,self.proc,EmptyDocker())

    def test_render_units_do_not_install_and_quote_paths(self):
        spec=importlib.util.spec_from_file_location('monitor_units',ROOT/'scripts/host-monitor-unit.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        service=module.render('/home/user/a space%dir','michael','michael')
        self.assertIn('a space%%dir',service);self.assertIn('NoNewPrivileges=yes',service)
        self.assertIn('User=michael',service);self.assertNotIn('privileged',service)
        for path in ('relative','/tmp/../root','/tmp\nExecStart=bad'):
            with self.assertRaises(ValueError):module.render(path,'user','user')


if __name__=='__main__':unittest.main()
