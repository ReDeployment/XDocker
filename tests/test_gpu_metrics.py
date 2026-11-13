"""GPU sensors fail independently; optional public reports never invent zeros."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gpu_metrics import gpu_sample, public_gpu, METRICS
from host_metrics import atomic_json, private_directory
from resource_monitor import report_read
from test_resource_monitor import sample_report


def gpu_fixture():
    return {'state':'ready','limit_reached':False,'devices':[{
        'id':'c'*64,'name':'AMD Radeon Vega / Cezanne','vendor':'AMD','driver':'amdgpu',
        'bus_id':'0000:05:00.0','pci_id':'1002:1638','integrated':True,
        'utilization_percent':0,'vram_total_bytes':536870912,'vram_used_bytes':16777216,
        'shared_total_bytes':32*1073741824,'shared_used_bytes':12*1048576,
        'temperature_celsius':39,'state':'ready','unavailable_metrics':[]}]}


class GPUTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.sys=self.root/'sys'
        (self.sys/'class/drm').mkdir(parents=True)

    def device(self, index=0, driver='amdgpu'):
        device=self.sys/f'devices/0000:05:{index:02x}.0';device.mkdir(parents=True)
        (self.sys/f'class/drm/card{index}').mkdir()
        (self.sys/f'class/drm/card{index}/device').symlink_to(device)
        self.put(device,'uevent',f'DRIVER={driver}\nPCI_ID=1002:1638\nPCI_SLOT_NAME=0000:05:{index:02x}.0')
        for name,value in {'gpu_busy_percent':0,'mem_info_vram_total':536870912,'mem_info_vram_used':16777216,
                           'mem_info_gtt_total':32*1073741824,'mem_info_gtt_used':12*1048576,
                           'hwmon/hwmon0/temp1_input':39000}.items(): self.put(device,name,value)
        return device

    def put(self,device,name,value):
        path=device/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(str(value)+'\n')

    def test_amd_sysfs_units_idle_zero_and_physical_deduplication(self):
        d=self.device()
        (self.sys/'class/drm/card99').mkdir();(self.sys/'class/drm/card99/device').symlink_to(d)
        (self.sys/'class/drm/card0-HDMI-A-1').mkdir();(self.sys/'class/drm/renderD128').mkdir()
        report=gpu_sample(self.sys);self.assertEqual(report['state'],'ready');self.assertEqual(len(report['devices']),1)
        row=report['devices'][0];self.assertEqual(row['utilization_percent'],0)
        self.assertEqual(row['temperature_celsius'],39);self.assertEqual(row['vram_total_bytes'],512*1048576)
        self.assertEqual(row['shared_total_bytes'],32*1073741824);self.assertTrue(row['integrated'])
        self.assertEqual(public_gpu(report),report);self.assertNotIn(str(self.sys),str(public_gpu(report)))
        old=row['id'];(self.sys/'class/drm/card0').rename(self.sys/'class/drm/card12')
        self.assertEqual(gpu_sample(self.sys)['devices'][0]['id'],old)
        self.assertNotIn('power',str(row));self.assertFalse((d/'gpu_metrics').exists())

    def test_invalid_missing_or_denied_sensor_is_null_preserving_other_sensors(self):
        d=self.device();self.put(d,'gpu_busy_percent',101);(d/'mem_info_gtt_used').unlink()
        self.put(d,'mem_info_vram_used',2**40);self.put(d,'hwmon/hwmon0/temp1_input','nan')
        row=gpu_sample(self.sys)['devices'][0]
        self.assertEqual(row['state'],'partial');self.assertIsNone(row['utilization_percent'])
        self.assertIsNone(row['shared_used_bytes']);self.assertIsNone(row['vram_used_bytes'])
        self.assertIsNone(row['temperature_celsius']);self.assertEqual(row['vram_total_bytes'],536870912)
        original=os.open
        def denied(path,*args,**kwargs):
            if Path(path).name=='mem_info_vram_total': raise PermissionError()
            return original(path,*args,**kwargs)
        with patch('gpu_metrics.os.open',side_effect=denied): row=gpu_sample(self.sys)['devices'][0]
        self.assertIsNone(row['vram_total_bytes']);self.assertIsNone(row['vram_used_bytes'])

    def test_missing_drm_unsupported_driver_capacity_and_untrusted_nodes(self):
        self.assertEqual(gpu_sample(self.sys)['state'],'not-detected')
        self.assertEqual(gpu_sample(self.root/'missing')['state'],'unavailable')
        d=self.device(driver='i915');row=gpu_sample(self.sys)['devices'][0]
        self.assertEqual(row['state'],'unsupported');self.assertTrue(all(row[k] is None for k in METRICS))
        for i in range(1,10): self.device(i)
        report=gpu_sample(self.sys);self.assertTrue(report['limit_reached']);self.assertEqual(len(report['devices']),8)
        self.put(d,'uevent','DRIVER=amdgpu\nPCI_ID=1002:1638\nPCI_SLOT_NAME=0000:05:00.0')
        (d/'gpu_busy_percent').unlink();outside=self.root/'private';outside.write_text('42')
        (d/'gpu_busy_percent').symlink_to(outside)
        self.assertIsNone(gpu_sample(self.sys)['devices'][0]['utilization_percent'])
        (d/'gpu_busy_percent').unlink();os.mkfifo(d/'gpu_busy_percent')
        self.assertIsNone(gpu_sample(self.sys)['devices'][0]['utilization_percent'])

    def test_public_report_allowlist_legacy_and_malformed_gpu_keep_cpu(self):
        root=private_directory(self.root/'reports');report=sample_report()
        atomic_json(root/'report.json',report)
        self.assertEqual(report_read(root)['gpu']['state'],'not-collected')
        report['gpu']=gpu_fixture();report['gpu']['devices'][0]['private_path']='/private/secret'
        report['gpu']['raw']='not public';atomic_json(root/'report.json',report)
        result=report_read(root);self.assertEqual(result['gpu']['state'],'ready');self.assertNotIn('private',str(result['gpu']))
        for mutation in ('range','bool','used','duplicate','name','overflow','shape'):
            raw=gpu_fixture();row=raw['devices'][0]
            if mutation=='range': row['utilization_percent']=-1
            if mutation=='bool': row['temperature_celsius']=True
            if mutation=='used': row['shared_used_bytes']=row['shared_total_bytes']+1
            if mutation=='duplicate': raw['devices']*=2
            if mutation=='name': row['name']='secret\npath'
            if mutation=='overflow': row['temperature_celsius']=10**1000
            if mutation=='shape': raw['devices']='wrong'
            report['gpu']=raw;atomic_json(root/'report.json',report);result=report_read(root)
            self.assertEqual(result['gpu']['state'],'unavailable',mutation)
            self.assertEqual(result['host']['cpu_percent'],12,mutation)
        raw=gpu_fixture();raw['devices'][0]['driver']='i915'
        self.assertIsNone(public_gpu(raw)['devices'][0]['utilization_percent'])


if __name__=='__main__': unittest.main()
