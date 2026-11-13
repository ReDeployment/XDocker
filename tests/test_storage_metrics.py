"""Block topology, df semantics, sampling resets and optional sensor failures."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from host_metrics import atomic_json, private_directory
from resource_monitor import report_read
from storage_metrics import storage_sample,public_storage,_sensors,filesystem_sample
from test_resource_monitor import sample_report


def storage_fixture():
    names=('nvme0n1','nvme0n1p3','dm-0','dm-1','sda','sda2','sdb','sdb1','sdc','sdc1')
    ids={n:hashlib.sha256(n.encode()).hexdigest() for n in names}
    parent={'nvme0n1p3':'nvme0n1','dm-0':'nvme0n1p3','dm-1':'nvme0n1p3','sda2':'sda','sdb1':'sdb','sdc1':'sdc'}
    rows=[]
    for n in names:
        physical=n in ('nvme0n1','sda','sdb','sdc')
        rows.append({'id':ids[n],'name':n,'model':'Acer SSD N3500CN 2TB' if n=='nvme0n1' else 'Ultra Touch HDD' if n=='sda' else n,
            'kind':'disk' if physical else 'lvm' if n.startswith('dm-') else 'partition','size_bytes':(5 if n=='sda' else 2)*10**12,
            'transport':'nvme' if n.startswith('nvme') else 'virtual' if n.startswith('dm-') else 'usb','rotational':n.startswith('sd'),
            'parents':[ids[parent[n]]] if n in parent else [],'temperature_celsius':None,'health':'unavailable','health_at':None,
            'io':{'state':'ready','interval_seconds':15,'read_bytes_per_second':1048576,'write_bytes_per_second':2097152,'read_iops':10,'write_iops':20,'busy_percent':4}})
    filesystems=[]
    for name,mount,gib in (('dm-0','/',300),('dm-1','/data',1024),('sda2','/mnt/archive',4600),('sdb1','/mnt/backup1',1800),('sdc1','/mnt/backup2',1800)):
        total=gib*2**30;used=int(total*.25);reserved=0;available=total-used
        filesystems.append({'id':ids[name],'device':ids[name],'fstype':'exfat' if name=='sda2' else 'ext4',
            'mounts':[{'path':mount,'read_only':False}],'total_bytes':total,'used_bytes':used,'available_bytes':available,
            'reserved_bytes':reserved,'used_percent':25,'state':'ready'})
    return {'state':'ready','devices':rows,'filesystems':filesystems,'limit_reached':False,'unreadable_devices':0}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        self.sys=self.root/'sys';(self.sys/'class/block').mkdir(parents=True)
        self.proc=self.root/'proc';(self.proc/'sys/kernel/random').mkdir(parents=True);(self.proc/'self').mkdir()
        (self.proc/'sys/kernel/random/boot_id').write_text('boot-one');(self.proc/'self/mountinfo').write_text('')

    def device(self,name='nvme0n1',number='259:0',parent=None,kind='disk',size=4000000000):
        if parent:node=parent/name
        else:node=self.sys/('devices/virtual/block/'+name if kind=='lvm' else 'devices/pci/nvme/nvme0/'+name)
        node.mkdir(parents=True);(self.sys/'class/block'/name).symlink_to(node)
        for key,value in {'dev':number,'size':size,'diskseq':10,'stat':'10 0 1000 10 20 0 2000 20 0 30 40','queue/rotational':0,'device/model':'fixture SSD'}.items():self.put(node,key,value)
        if kind=='partition':self.put(node,'partition',1)
        if kind=='lvm':self.put(node,'dm/uuid','LVM-fixture');self.put(node,'dm/name','data-volume')
        return node

    def put(self,node,key,value):
        p=node/key;p.parent.mkdir(exist_ok=True,parents=True);p.write_text(str(value)+'\n')

    def stats(self,path,number):
        return SimpleNamespace(f_blocks=1000,f_frsize=4096,f_bfree=600,f_bavail=550)

    def sample(self,previous=None,clock=10,reader=None):
        return storage_sample(self.sys,self.proc,previous,clock,stat_reader=reader or self.stats)

    def test_topology_lvm_mount_escaping_bind_dedup_and_reserved_space(self):
        disk=self.device();part=self.device('nvme0n1p3','259:3',disk,'partition');lv=self.device('dm-1','252:1',kind='lvm')
        (lv/'slaves').mkdir();(lv/'slaves/nvme0n1p3').symlink_to(part)
        self.device('nbd0','43:0',size=0);self.device('loop0','7:0')
        (self.proc/'self/mountinfo').write_text('1 0 252:1 / /data rw - ext4 /dev/dm-1 rw\n2 0 252:1 /folder /a\\040space ro - ext4 /dev/dm-1 ro\n3 0 0:1 / /proc rw - proc proc rw\n')
        result,_=self.sample();safe=public_storage(result)
        self.assertEqual(safe['state'],'ready');self.assertEqual(len(safe['devices']),3)
        by_name={d['name']:d for d in safe['devices']}
        self.assertEqual(by_name['dm-1']['parents'],[by_name['nvme0n1p3']['id']])
        self.assertEqual(by_name['nvme0n1p3']['parents'],[by_name['nvme0n1']['id']])
        self.assertEqual(by_name['nvme0n1']['size_bytes'],4000000000*512)
        fs=safe['filesystems'][0];self.assertEqual(len(safe['filesystems']),1);self.assertEqual(fs['mounts'][1]['path'],'/a space')
        self.assertTrue(fs['mounts'][1]['read_only']);self.assertEqual(fs['reserved_bytes'],50*4096)
        self.assertEqual(fs['used_percent'],42.11);self.assertNotIn('/devices/',str(safe))

    def test_io_sector_size_interval_first_sample_reset_boot_and_hotplug(self):
        node=self.device();first,previous=self.sample();self.assertIsNone(first['devices'][0]['io']['read_bytes_per_second'])
        self.put(node,'stat','20 0 1100 10 40 0 2200 20 0 530 40')
        second,new=self.sample(previous,20);io=second['devices'][0]['io']
        self.assertEqual(io['read_bytes_per_second'],5120);self.assertEqual(io['write_bytes_per_second'],10240)
        self.assertEqual(io['read_iops'],1);self.assertEqual(io['busy_percent'],5)
        self.put(node,'diskseq',11);result,_=self.sample(new,30);self.assertEqual(result['devices'][0]['io']['state'],'sampling')
        self.put(node,'diskseq',10);self.put(node,'stat','0 0 0 0 0 0 0 0 0 0 0')
        result,_=self.sample(new,30);self.assertEqual(result['devices'][0]['io']['state'],'reset')
        (self.proc/'sys/kernel/random/boot_id').write_text('boot-two');result,_=self.sample(new,30)
        self.assertEqual(result['devices'][0]['io']['state'],'sampling')
        self.assertIsNone(result['devices'][0]['io']['read_iops'])

    def test_procfs_short_reads_include_later_mounts_and_keep_input_bounded(self):
        self.device();report,_=self.sample();device=report['devices'][0]
        path=self.proc/'self/mountinfo'
        path.write_text('2 0 0:1 / /pseudo rw - tmpfs tmpfs rw\n'*200+'3 0 259:0 / /mnt/backup2 rw - ext4 /dev/nvme0n1 rw\n')
        original=os.read
        with patch('storage_metrics.os.read',side_effect=lambda fd,n:original(fd,min(n,17))):
            filesystems,limit=filesystem_sample(self.proc,{'259:0':device},self.stats)
        self.assertFalse(limit);self.assertEqual(filesystems[0]['mounts'][0]['path'],'/mnt/backup2')
        path.write_text('x'*(1024*1024+1))
        with self.assertRaises(ValueError):filesystem_sample(self.proc,{'259:0':device},self.stats)

    def test_discovery_limit_unreadable_mount_and_no_device_commands_without_permission(self):
        self.device();(self.proc/'self/mountinfo').write_text('1 0 259:0 / /data rw - ext4 /dev/nvme0n1 rw\n')
        def denied(*args):raise PermissionError()
        with patch('storage_metrics.os.access',return_value=False),patch('storage_metrics.subprocess.run') as command:
            result,_=self.sample(reader=denied);command.assert_not_called()
        self.assertEqual(result['filesystems'][0]['state'],'unavailable');self.assertIsNone(result['filesystems'][0]['available_bytes'])
        self.assertEqual(result['devices'][0]['health'],'unavailable');self.assertEqual(result['state'],'partial')
        self.device('second','259:1')
        with patch('storage_metrics.MAX_DEVICES',1):result,_=self.sample()
        self.assertTrue(result['limit_reached']);self.assertEqual(len(result['devices']),1)
        self.assertEqual(storage_sample(self.root/'missing',self.proc)[0]['state'],'unavailable')

    def test_smart_failure_is_unknown_and_hwmon_missing_is_not_zero(self):
        node=self.device()
        result,_=self.sample();self.assertIsNone(result['devices'][0]['temperature_celsius'])
        self.put(node,'hwmon/hwmon0/temp1_input',38000)
        result,_=self.sample();self.assertEqual(result['devices'][0]['temperature_celsius'],38)

    def test_optional_smart_fixed_read_arguments_health_failure_cache_and_malformed_output(self):
        node=self.device()
        with patch('storage_metrics.shutil.which',return_value='/usr/sbin/smartctl'),patch('storage_metrics.stat.S_ISBLK',return_value=True),patch('storage_metrics.os.access',return_value=True),patch('storage_metrics.subprocess.run') as run:
            for status,passed,expected in ((0,True,'passed'),(8,False,'failed'),(2,True,'unavailable')):
                run.return_value=SimpleNamespace(stdout=json.dumps({'smartctl':{'exit_status':status},'smart_status':{'passed':passed},'temperature':{'current':38}}).encode(),returncode=status)
                value=_sensors('null',node,self.sys,None,100,[4]);self.assertEqual(value['health'],expected)
                self.assertEqual(run.call_args[0][0],['/usr/sbin/smartctl','-j','-d','nvme','-n','standby,3,5','-H','-A','/dev/null'])
            run.reset_mock();_sensors('null',self.sys/'devices/usb2/block/sda',self.sys,None,100,[4]);run.assert_not_called()
            run.reset_mock();cached=_sensors('null',node,self.sys,value,110,[4]);run.assert_not_called();self.assertEqual(cached['health'],'unavailable')
            for output in (b'[]',b'{"smartctl":[]}',b'not JSON'):
                run.return_value=SimpleNamespace(stdout=output,returncode=0)
                self.assertEqual(_sensors('null',node,self.sys,None,100,[4])['health'],'unavailable')

    def test_public_projection_legacy_failures_do_not_suppress_other_resources(self):
        root=private_directory(self.root/'reports');report=sample_report()
        atomic_json(root/'report.json',report);self.assertEqual(report_read(root)['storage']['state'],'not-collected')
        raw=storage_fixture();raw['devices'][0]['serial']='private serial';raw['raw_smart']='secret'
        report['storage']=raw;atomic_json(root/'report.json',report);result=report_read(root)
        self.assertEqual(len(result['storage']['devices']),10);self.assertNotIn('secret',str(result['storage']));self.assertNotIn('serial',str(result['storage']))
        for mutation in ('duplicate','dangling','capacity','mount','nan','huge','bool'):
            raw=storage_fixture()
            if mutation=='duplicate':raw['devices'].append(deepcopy(raw['devices'][0]))
            if mutation=='dangling':raw['devices'][1]['parents']=['f'*64]
            if mutation=='capacity':raw['filesystems'][0]['used_bytes']+=100
            if mutation=='mount':raw['filesystems'][0]['mounts'][0]['path']='relative'
            if mutation=='nan':raw['devices'][0]['io']['read_iops']=-1
            if mutation=='huge':raw['devices'][0]['size_bytes']=10**1000
            if mutation=='bool':raw['devices'][0]['size_bytes']=True
            report['storage']=raw;atomic_json(root/'report.json',report);result=report_read(root)
            self.assertEqual(result['storage']['state'],'unavailable',mutation);self.assertEqual(result['host']['cpu_percent'],12)


if __name__=='__main__':unittest.main()
