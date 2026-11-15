"""SMART fixed commands, real cache trust rules, identity changes and unit scope."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from smart_sensors import parse_smart,probe,sensor,read_cache,cached_sensor,public_sensor,validate_config,collect_smart
from storage_metrics import public_storage
from test_storage_metrics import storage_fixture


def payload(status=0,passed=True):
    return {'smartctl':{'exit_status':status},'smart_status':{'passed':passed},'temperature':{'current':42},'serial_number':'PRIVATE'}


class SmartTests(unittest.TestCase):
    def test_temperature_and_current_versus_historical_failures(self):
        r=payload(32);r['ata_smart_attributes']={'table':[{'id':190,'when_failed':'past'}]}
        result=parse_smart(r,32,100)
        self.assertEqual(result['temperature_celsius'],42);self.assertEqual(result['health'],'warning')
        self.assertEqual(result['sensor']['alerts'],['past-temperature-threshold']);self.assertNotIn('PRIVATE',str(result))
        for status,passed,health in ((0,True,'passed'),(8,False,'failed'),(16,True,'failed'),(96,True,'warning'),(2,True,'unavailable')):
            r=parse_smart(payload(status,passed),status,100);self.assertEqual(r['health'],health)
        r=payload(224);self.assertEqual(parse_smart(r,224,100)['sensor']['alerts'],['past-attribute-threshold','error-log','self-test-log'])
        for value in (True,float('nan'),201,-41):
            r=payload();r['temperature']['current']=value;self.assertIsNone(parse_smart(r,0,100)['temperature_celsius'])

    def test_sleeping_and_unsupported_require_power_evidence_and_never_return_a_temperature(self):
        for mode in ('SLEEP','STANDBY'):
            raw={'smartctl':{'exit_status':3,'messages':[{'string':f'Device is in {mode} mode, exit(3)','severity':'information'}]}}
            result=parse_smart(raw,3,100);self.assertEqual(result['sensor']['state'],'sleeping');self.assertIsNone(result['temperature_celsius']);self.assertEqual(result['health'],'unavailable')
        self.assertEqual(parse_smart(payload(3),3,100)['sensor']['state'],'read-error')
        raw={'power_mode':'UNKNOWN','smartctl':{'exit_status':5}}
        self.assertEqual(parse_smart(raw,5,100)['sensor']['state'],'unsupported')
        for raw in ([],{}, {'smartctl':[]}, {'smartctl':{'exit_status':True}}):self.assertEqual(parse_smart(raw,0,100)['health'],'unavailable')

    def test_fixed_read_only_commands_timeouts_and_no_fallback(self):
        with patch('smart_sensors.subprocess.run') as run:
            run.return_value=SimpleNamespace(stdout=json.dumps(payload()).encode(),returncode=0)
            probe('/dev/sda','sat',100)
            self.assertEqual(run.call_args.args[0],['/usr/sbin/smartctl','-j','-d','sat','-n','standby,3,5','-H','-A','/dev/sda'])
            self.assertEqual(run.call_args.kwargs['timeout'],2)
            probe('/dev/nvme0n1','nvme',100);self.assertNotIn('-n',run.call_args.args[0])
            for exc,state in ((subprocess.TimeoutExpired('smartctl',2),'timeout'),(PermissionError(),'permission-denied'),(FileNotFoundError(),'tool-missing')):
                run.reset_mock();run.side_effect=exc;self.assertEqual(probe('/dev/sda','sat',100)['sensor']['state'],state);self.assertEqual(run.call_count,1)
            with self.assertRaises(ValueError):probe('/dev/sda','sat -s on',100)

    def test_cache_trust_expiry_boot_identity_and_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'report.json';row={'generation':'12',**parse_smart(payload(),0,100)}
            raw={'schema':1,'boot':'boot-one','at':100,'devices':{'id':row}}
            p.write_text(json.dumps(raw));p.chmod(0o640)
            devices,state=read_cache('boot-one',101,p,os.getuid());self.assertEqual(state,'ready')
            self.assertEqual(cached_sensor(devices,'id','12',101)['temperature_celsius'],42)
            self.assertEqual(cached_sensor(devices,'id','13',101)['sensor']['state'],'stale');self.assertIsNone(cached_sensor(devices,'other','12',101))
            for boot,now in (('boot-two',101),('boot-one',281),('boot-one',90)):
                self.assertEqual(read_cache(boot,now,p,os.getuid()),({},'stale'))
            p.chmod(0o660);self.assertEqual(read_cache('boot-one',101,p,os.getuid())[1],'read-error')
            p.chmod(0o640);self.assertEqual(read_cache('boot-one',101,p,os.getuid()+1)[1],'read-error')
            link=Path(tmp)/'link';link.symlink_to(p);self.assertEqual(read_cache('boot-one',101,link,os.getuid())[1],'read-error')
            p.write_text('[]');self.assertEqual(read_cache('boot-one',101,p,os.getuid())[1],'read-error')
            row['sensor']['state']='sleeping';self.assertEqual(cached_sensor({'id':row},'id','12',101)['sensor']['state'],'read-error')

    def test_config_and_device_change_fail_closed_without_raw_device_data(self):
        config={'schema':1,'devices':[{'path':'/dev/disk/by-id/usb-fixture-0:0','type':'sat'}]}
        for mutation in ('path','type','duplicate','extra'):
            r=deepcopy(config)
            if mutation=='path':r['devices'][0]['path']='/dev/disk/by-id/../../sda'
            if mutation=='type':r['devices'][0]['type']='auto'
            if mutation=='duplicate':r['devices']*=2
            if mutation=='extra':r['devices'][0]['args']=['-s','on']
            with self.assertRaises(ValueError):validate_config(r)
        with tempfile.TemporaryDirectory() as tmp:
            proc=Path(tmp);(proc/'sys/kernel/random').mkdir(parents=True);(proc/'sys/kernel/random/boot_id').write_text('boot-one')
            with patch('smart_sensors.device_identity',side_effect=[(Path('/dev/sda'),'a'*64,'12'),(Path('/dev/sda'),'a'*64,'13')]),patch('smart_sensors.probe',return_value=sensor('sleeping',100)):
                result=collect_smart(config,proc=proc);self.assertEqual(result['devices'],{});self.assertEqual(result['errors'],['device-unavailable'])

    def test_public_sensor_whitelist_and_hardened_root_unit(self):
        raw=storage_fixture();raw['devices'][0].update(health='warning',temperature_celsius=42,sensor={'state':'ready','source':'smart','alerts':['past-temperature-threshold'],'raw_serial':'PRIVATE'})
        safe=public_storage(raw);self.assertEqual(safe['devices'][0]['sensor']['alerts'],['past-temperature-threshold']);self.assertNotIn('PRIVATE',str(safe))
        self.assertIsNone(public_sensor({'state':'ready','source':'smart','alerts':['untrusted']}))
        path=Path(__file__).resolve().parents[1]/'scripts/smart-monitor-unit.py';spec=importlib.util.spec_from_file_location('smart_unit',path);unit=importlib.util.module_from_spec(spec);spec.loader.exec_module(unit)
        config={'schema':1,'devices':[{'path':'/dev/disk/by-id/usb-fixture','type':'sat'}]};text=unit.render(config,'a'*40,'michael')
        for value in ('User=root','Group=michael','python3 -I /usr/local/lib/xdocker-smart/','ProtectSystem=strict','NoNewPrivileges=yes','DevicePolicy=closed','DeviceAllow="/dev/disk/by-id/usb-fixture" r','ReadWritePaths=/var/lib/xdocker-smart','TimeoutStartSec=25'):self.assertIn(value,text)
        self.assertNotIn('/data/workspace',text);self.assertNotIn('sudo',text);self.assertIn('OnUnitActiveSec=60',unit.TIMER)


if __name__=='__main__':unittest.main()
