"""Hardware-free checks for cached pump freshness and compact sensor reads."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import sensors


class CachedSensorsTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        self.sample = {'devices': [{'uid': 'board', 'status_history': [{
            'timestamp': self.now.isoformat(),
            'channels': [{'name': 'fan1', 'rpm': 600}, {'name': 'fan6', 'rpm': 2300}],
        }]}]}

    def pump(self, data=None, uid='board', now=None):
        return sensors.cached_pump(self.sample if data is None else data, uid,
                                   self.now if now is None else now)

    def test_fresh_rpm_and_stopped_pump(self):
        self.assertEqual(self.pump(), 2300)
        self.sample['devices'][0]['status_history'][-1]['channels'][1]['rpm'] = 0
        self.assertEqual(self.pump(), 0)

    def test_timestamp_limits_offsets_and_latest_row(self):
        latest = self.sample['devices'][0]['status_history'][-1]
        for seconds in (0, 10, -5):
            latest['timestamp'] = (self.now - timedelta(seconds=seconds)).isoformat()
            self.assertEqual(self.pump(), 2300)
        for seconds in (10.001, -5.001):
            latest['timestamp'] = (self.now - timedelta(seconds=seconds)).isoformat()
            self.assertIsNone(self.pump())
        latest['timestamp'] = '2026-10-13T14:00:00+02:00'
        self.assertEqual(self.pump(), 2300)
        stale = deepcopy(latest)
        stale['timestamp'] = (self.now - timedelta(seconds=11)).isoformat()
        self.sample['devices'][0]['status_history'].append(stale)
        self.assertIsNone(self.pump())

    def test_wrong_uid_duplicate_devices_and_channels(self):
        self.assertIsNone(self.pump(uid='another-board'))
        self.assertIsNone(self.pump(uid=''))
        duplicate = deepcopy(self.sample)
        duplicate['devices'].append(deepcopy(duplicate['devices'][0]))
        self.assertIsNone(self.pump(duplicate))
        channels = self.sample['devices'][0]['status_history'][-1]['channels']
        channels.append({'name': 'fan6', 'rpm': 2300})
        self.assertIsNone(self.pump())

    def test_invalid_rpm_and_bad_structures(self):
        for value in (float('nan'), float('inf'), -1, True, '2300', None, 10**10000):
            self.sample['devices'][0]['status_history'][-1]['channels'][1]['rpm'] = value
            self.assertIsNone(self.pump())
        for data in (None, [], {}, {'devices': None}, {'devices': {}},
                     {'devices': [{'uid': 'board', 'status_history': []}]},
                     {'devices': [{'uid': 'board', 'status_history': [None]}]}):
            self.assertIsNone(sensors.cached_pump(data, 'board', self.now))
        self.sample['devices'][0]['status_history'][-1]['channels'] = None
        self.assertIsNone(self.pump())

    def test_invalid_or_naive_timestamps_and_current_time(self):
        latest = self.sample['devices'][0]['status_history'][-1]
        for stamp in (None, 'bad', '2026-10-13T12:00:00', 123):
            latest['timestamp'] = stamp
            self.assertIsNone(self.pump())
        latest['timestamp'] = self.now.isoformat()
        self.assertIsNone(self.pump(now=self.now.replace(tzinfo=None)))
        self.assertIsNone(self.pump(now='bad'))

    def test_compact_reads_temperatures_but_no_fan_or_pwm_attributes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (name, attributes) in enumerate([
                ('k10temp', {'temp1_input': '41000'}),
                ('amdgpu', {'temp1_input': '39000', 'temp2_input': '44000',
                            'fan1_input': '0', 'pwm1': '0'}),
                ('nct6798', {'fan6_input': '2300', 'pwm6': '255',
                             'pwm6_temp_sel': '1', 'pwm6_enable': '5'}),
            ]):
                device = root / f'hwmon{index}'
                device.mkdir()
                (device / 'name').write_text(name)
                for key, value in attributes.items():
                    (device / key).write_text(value)
            read_paths = []
            original_read = Path.read_text
            def tracked_read(path, *args, **kwargs):
                read_paths.append(path.name)
                if path.name.startswith(('fan', 'pwm')):
                    raise AssertionError('Compact sensor read accessed fan/PWM hardware')
                return original_read(path, *args, **kwargs)
            with patch.object(Path, 'read_text', tracked_read):
                result = sensors.snapshot(root, include_fans=False)
            self.assertEqual(result, {'cpu': 41., 'gpu': 39., 'hotspot': 44., 'ssd': None,
                                      'fans': {}, 'mainboard': True})
            self.assertEqual(set(read_paths), {'name', 'temp1_input', 'temp2_input'})


    def make(self, root, devices):
        for index, (name, attributes) in enumerate(devices):
            device = root / f'hwmon{index}'
            device.mkdir()
            (device / 'name').write_text(name)
            for key, value in attributes.items():
                (device / key).write_text(value)

    def test_prefers_discrete_gpu_and_reports_hottest_ssd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make(root, [('amdgpu', {'temp1_input': '52000'}),
                             ('amdgpu', {'temp1_input': '61000', 'temp2_input': '75000', 'fan1_input': '900'}),
                             ('nvme', {'temp1_input': '41000'}), ('nvme', {'temp1_input': '57000'}),
                             ('zenpower', {'temp1_input': '48000'})])
            result = sensors.snapshot(root, include_fans=False)
            self.assertEqual((result['gpu'], result['hotspot'], result['ssd'], result['cpu']), (61., 75., 57., 48.))

    def test_discovery_is_cached_and_refreshed_when_a_device_disappears(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make(root, [('k10temp', {'temp1_input': '40000'})])
            first = sensors.discover(root, now=100)
            (root / 'hwmon9').mkdir(); (root / 'hwmon9' / 'name').write_text('nvme')
            self.assertIs(sensors.discover(root, now=110), first)
            self.assertIn('nvme', sensors.discover(root, now=131))
            for item in (root / 'hwmon0').iterdir():
                item.unlink()
            (root / 'hwmon0').rmdir()
            self.assertNotIn('k10temp', sensors.discover(root, now=132))


if __name__ == '__main__':
    unittest.main(verbosity=2)
