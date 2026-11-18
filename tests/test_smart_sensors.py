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

from smart_sensors import parse_smart,probe,sensor,read_cache,cached_sensor,public_sensor,validate_config,collect_smart,transport_blocked
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
            with patch('smart_sensors.device_identity',side_effect=[(Path('/dev/sda'),'a'*64,'12'),(Path('/dev/sda'),'a'*64,'13')]),patch('smart_sensors.usb_transport',return_value=(False,None)),patch('smart_sensors.transport_blocked',return_value=False),patch('smart_sensors.probe',return_value=sensor('sleeping',100)):
                result=collect_smart(config,proc=proc);self.assertEqual(result['devices'],{});self.assertEqual(result['errors'],['device-unavailable'])

    def test_idle_policy_uses_kernel_counters_and_resumes_only_after_io(self):
        config={'schema':1,'devices':[{'path':'/dev/disk/by-id/usb-fixture','type':'sat','idle_pause_seconds':60}]}
        identity='a'*64;device=Path('/dev/sda')
        with tempfile.TemporaryDirectory() as tmp:
            proc=Path(tmp);(proc/'sys/kernel/random').mkdir(parents=True);(proc/'sys/kernel/random/boot_id').write_text('boot-one')
            with patch('smart_sensors.device_identity',return_value=(device,identity,'12')),patch('smart_sensors.usb_transport',return_value=(False,None)),patch('smart_sensors.transport_blocked',return_value=False),patch('smart_sensors.io_activity',return_value=([1,8,1,8,0,0,0],False)),patch('smart_sensors.time.time',return_value=100),patch('smart_sensors.probe',return_value=parse_smart(payload(32),32,100)) as run:
                first=collect_smart(config,proc=proc,clock=1000);self.assertEqual(run.call_count,1)
                idle=collect_smart(config,proc=proc,previous=first,clock=1061);self.assertEqual(run.call_count,1)
                row=cached_sensor(idle['devices'],identity,'12',101);self.assertIsNone(row['temperature_celsius']);self.assertIsNone(row['health_at'])
                self.assertEqual(row['sensor']['state'],'idle-paused');self.assertEqual(row['sensor']['last']['temperature_celsius'],42)
                self.assertEqual(row['sensor']['last']['health'],'warning')
                again=collect_smart(config,proc=proc,previous=idle,clock=3000);self.assertEqual(run.call_count,1);self.assertEqual(again['devices'][identity]['sensor']['last']['at'],100)
                with patch('smart_sensors.io_activity',return_value=([2,16,1,8,0,0,0],False)):
                    active=collect_smart(config,proc=proc,previous=again,clock=3010);self.assertEqual(run.call_count,2);self.assertEqual(active['devices'][identity]['sensor']['state'],'ready')
                with patch('smart_sensors.io_activity',return_value=([1,8,1,8,0,0,0],True)):
                    collect_smart(config,proc=proc,previous=first,clock=4000);self.assertEqual(run.call_count,3)
                (proc/'sys/kernel/random/boot_id').write_text('boot-two');collect_smart(config,proc=proc,previous=idle,clock=4001);self.assertEqual(run.call_count,4)
        for pause in (True,0,59,1801):
            bad=deepcopy(config);bad['devices'][0]['idle_pause_seconds']=pause
            with self.assertRaises(ValueError):validate_config(bad)

    def test_selected_standby_unit_uses_stable_ids_and_defaults_others_off(self):
        path=Path(__file__).resolve().parents[1]/'scripts/standby-unit.py';spec=importlib.util.spec_from_file_location('standby_unit',path);unit=importlib.util.module_from_spec(spec);spec.loader.exec_module(unit)
        config={'schema':1,'devices':[{'path':'/dev/disk/by-id/usb-one','type':'sat','idle_pause_seconds':60},{'path':'/dev/disk/by-id/usb-two','type':'sat','idle_pause_seconds':60},{'path':'/dev/disk/by-id/usb-archive','type':'sat'}]}
        text=unit.render(config)
        self.assertIn('/usr/local/lib/xdocker-standby/hd-idle-1.21 -i 0 -c ata -s 0',text);self.assertEqual(text.count('-i 1800'),2);self.assertNotIn('usb-archive',text)
        self.assertIn('BindsTo=dev-disk-by\\x2did-usb\\x2done.device',text);self.assertEqual(text.count('DeviceAllow='),2)
        self.assertEqual(text.count('-c ata'),3);self.assertIn('ProtectSystem=strict',text)
        with self.assertRaises(ValueError):unit.render(config,seconds=30)
        config['devices'][2]['idle_pause_seconds']=60
        with self.assertRaises(ValueError):unit.render(config)

    def test_smart_counter_increments_do_not_keep_the_monitor_awake(self):
        config={'schema':1,'devices':[{'path':'/dev/disk/by-id/usb-fixture','type':'sat','idle_pause_seconds':60}]}
        identity='a'*64
        with tempfile.TemporaryDirectory() as tmp:
            proc=Path(tmp);(proc/'sys/kernel/random').mkdir(parents=True);(proc/'sys/kernel/random/boot_id').write_text('boot-one')
            with patch('smart_sensors.device_identity',return_value=(Path('/dev/sda'),identity,'12')),patch('smart_sensors.usb_transport',return_value=(False,None)),patch('smart_sensors.transport_blocked',return_value=False),patch('smart_sensors.probe',return_value=parse_smart(payload(),0,100)) as probe_call:
                with patch('smart_sensors.io_activity',side_effect=[([1,8,1,8,0,0,0],False),([2,16,1,8,0,0,0],False)]):
                    active=collect_smart(config,proc=proc,clock=1000)
                with patch('smart_sensors.io_activity',return_value=([2,16,1,8,0,0,0],False)):
                    idle=collect_smart(config,proc=proc,previous=active,clock=1061)
                self.assertEqual(probe_call.call_count,1);self.assertEqual(idle['devices'][identity]['sensor']['state'],'idle-paused')

    def test_ultra_touch_uas_filter_does_not_probe_or_claim_sleeping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sys=root/'sys';proc=root/'proc';usb=sys/'devices/usb2/2-2';interface=usb/'2-2:1.0';node=interface/'host1/block/sdc'
            node.mkdir(parents=True);(sys/'class/block').mkdir(parents=True);(sys/'class/block/sdc').symlink_to(node)
            for driver in ('uas','usb-storage'):(sys/'bus/usb/drivers'/driver).mkdir(parents=True)
            (interface/'driver').symlink_to(sys/'bus/usb/drivers/uas');(node/'stat').write_text('1 0 8 0 0 0 0 0 0 0 0');(usb/'idVendor').write_text('0bc2');(usb/'idProduct').write_text('2065')
            quirks=sys/'module/usb_storage/parameters/quirks';quirks.parent.mkdir(parents=True);quirks.write_text('')
            device=Path('/dev/sdc');self.assertTrue(transport_blocked(device,sys))
            for text,blocked in [('174c:1356:u',True),('0bc2:2065:t',True),('0bc2:2065:u',False)]:
                quirks.write_text(text);self.assertEqual(transport_blocked(device,sys),blocked)
            quirks.write_text('');config={'schema':1,'devices':[{'path':'/dev/disk/by-id/usb-fixture','type':'sat'}]}
            (proc/'sys/kernel/random').mkdir(parents=True);(proc/'sys/kernel/random/boot_id').write_text('boot-one')
            identity='a'*64;old={'boot':'boot-one','devices':{identity:{'generation':'12',**parse_smart(payload(32),32,99)}}}
            with patch('smart_sensors.device_identity',return_value=(device,identity,'12')),patch('smart_sensors.time.time',return_value=100),patch('smart_sensors.probe') as run:
                value=collect_smart(config,sys_root=sys,proc=proc,previous=old)
                run.assert_not_called();row=cached_sensor(value['devices'],identity,'12',101)
                self.assertEqual(row['sensor']['state'],'transport-blocked');self.assertIsNone(row['temperature_celsius']);self.assertIsNone(row['health_at']);self.assertEqual(row['health'],'unavailable')
                self.assertEqual(row['sensor']['last']['temperature_celsius'],42)
                (interface/'driver').unlink();(interface/'driver').symlink_to(sys/'bus/usb/drivers/usb-storage')
                run.return_value=parse_smart(payload(),0,100);active=collect_smart(config,sys_root=sys,proc=proc,clock=1000);row=active['devices'][identity]
                self.assertEqual(run.call_count,1);self.assertEqual(row['temperature_celsius'],42)
                paused=collect_smart(config,sys_root=sys,proc=proc,previous=active,clock=1061)['devices'][identity]
                self.assertEqual(run.call_count,1);self.assertEqual(paused['sensor']['state'],'idle-paused');self.assertEqual(paused['sensor']['last']['temperature_celsius'],42)
            (interface/'driver').unlink();(interface/'driver').symlink_to(sys/'bus/usb/drivers/uas')
            (usb/'idVendor').write_text('174c');self.assertFalse(transport_blocked(device,sys))
            (usb/'idVendor').write_text('0bc2');(usb/'idProduct').write_text('9999');self.assertFalse(transport_blocked(device,sys))

    def test_public_sensor_whitelist_and_hardened_root_unit(self):
        raw=storage_fixture();raw['devices'][0].update(health='warning',temperature_celsius=42,sensor={'state':'ready','source':'smart','alerts':['past-temperature-threshold'],'raw_serial':'PRIVATE'})
        safe=public_storage(raw);self.assertEqual(safe['devices'][0]['sensor']['alerts'],['past-temperature-threshold']);self.assertNotIn('PRIVATE',str(safe))
        self.assertIsNone(public_sensor({'state':'ready','source':'smart','alerts':['untrusted']}))
        path=Path(__file__).resolve().parents[1]/'scripts/smart-monitor-unit.py';spec=importlib.util.spec_from_file_location('smart_unit',path);unit=importlib.util.module_from_spec(spec);spec.loader.exec_module(unit)
        config={'schema':1,'devices':[{'path':'/dev/disk/by-id/usb-fixture','type':'sat'}]};text=unit.render(config,'a'*40,'michael')
        for value in ('User=root','Group=michael','python3 -I /usr/local/lib/xdocker-smart/','ProtectSystem=strict','NoNewPrivileges=yes','DevicePolicy=closed','DeviceAllow="/dev/disk/by-id/usb-fixture" r','ReadWritePaths=/var/lib/xdocker-smart','TimeoutStartSec=25'):self.assertIn(value,text)
        self.assertNotIn('/data/workspace',text);self.assertNotIn('sudo',text);self.assertIn('OnUnitActiveSec=60',unit.TIMER)


if __name__=='__main__':unittest.main()
