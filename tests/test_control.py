"""Hardware-free fault tests for cooling policy and transactional restore."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from control import Controller, BackendError, CONTROLLABLE


def devices():
    return [
        {'name': 'nct6798', 'type': 'Hwmon', 'uid': 'board', 'info': {
            'channels': {f'fan{i}': {'speed_options': {'fixed_enabled': True, 'min_duty': 0, 'max_duty': 100}} for i in (1, 2, 3, 6)},
            'temps': {}, 'driver_info': {'locations': ['/sys/fake']}}},
        {'name': 'CPU', 'type': 'CPU', 'uid': 'cpu', 'info': {'temps': {'temp1': {'label': 'CPU Temp Tctl'}}}},
        {'name': 'GPU', 'type': 'GPU', 'uid': 'gpu', 'info': {'temps': {'temp1': {'label': 'GPU Temp Edge'}}}},
    ]


class Fake:
    def __init__(self, initial=None):
        self.initial = initial
        self.dev = devices()
        self.settings = {'board': [copy.deepcopy(initial)] if initial else [], 'cpu': [], 'gpu': []}
        self.profiles = {'0': {'uid': '0', 'name': 'Unmanaged', 'p_type': 'Default'}}
        if initial and initial.get('profile_uid'):
            self.profiles[initial['profile_uid']] = {'uid': initial['profile_uid'], 'p_type': 'Graph'}
        self.calls = []
        self.fail = None
        self.fail_after_commit = False
        self.age = 0

    def devices(self):
        return copy.deepcopy(self.dev)

    def path(self, uid, channel):
        return f'/devices/{uid}/settings/{channel}'

    def request(self, path, method='GET', data=None):
        self.calls.append((path, method, copy.deepcopy(data)))
        failing = self.fail and self.fail(path, method, data)
        if failing and not self.fail_after_commit:
            raise BackendError('Simulierter API-Fehler')
        if path == '/profiles':
            if method == 'GET':
                return {'profiles': copy.deepcopy(list(self.profiles.values()))}
            self.profiles[data['uid']] = copy.deepcopy(data)
        elif path.startswith('/profiles/') and method == 'DELETE':
            self.profiles.pop(path.rsplit('/', 1)[-1], None)
        elif path == '/status':
            stamp = (datetime.now(timezone.utc) - timedelta(seconds=self.age)).isoformat()
            return {'devices': [{'uid': uid, 'status_history': [{'timestamp': stamp, 'temps': [{'name': 'temp1', 'temp': 42}]}]} for uid in ('cpu', 'gpu')]}
        elif path.startswith('/devices/'):
            parts = path.strip('/').split('/')
            uid = parts[1]
            if method == 'GET':
                return {'settings': copy.deepcopy(self.settings[uid])}
            channel = parts[3]
            self.settings[uid] = [s for s in self.settings[uid] if s['channel_name'] != channel]
            if parts[4] != 'reset':
                self.settings[uid].append({'channel_name': channel, **data})
        else:
            raise AssertionError((path, method, data))
        if failing:
            raise BackendError('Simulierter verlorener Antworttext')


class ControlTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fake = Fake()
        self.c = Controller(self.fake, self.temp.name)
        self.c._match_board = lambda ds: next((d for d in ds if d['uid'] == 'board'), None)

    def writes(self):
        return [c for c in self.fake.calls if c[1] != 'GET']

    def test_manual_guard_and_original_restore(self):
        result = self.c.apply_manual('nct6798:1', 55)
        self.assertEqual(result['channels']['nct6798:1']['mode'], 'manual')
        graphs = [p for p in self.fake.profiles.values() if p['p_type'] == 'Graph']
        self.assertEqual(len(graphs), 2)
        self.assertTrue(all(p['speed_profile'] == [[20., 55], [75., 55], [85., 100]] for p in graphs))
        mixes = [p for p in self.fake.profiles.values() if p['p_type'] == 'Mix']
        self.assertEqual(mixes[0]['mix_function_type'], 'Max')
        saved = json.loads(self.c.state_file.read_text())
        self.assertIsNone(saved['channels']['nct6798:1']['baseline'])
        self.assertEqual(self.c.state_file.stat().st_mode & 0o777, 0o600)
        self.c.restore('nct6798:1')
        self.assertEqual(self.fake.settings['board'], [])
        self.assertEqual(set(self.fake.profiles), {'0'})

    def test_pump_gpu_unused_locked(self):
        for key in ('nct6798:6', 'amdgpu:1', 'nct6798:4', '../../cpu'):
            with self.assertRaises(BackendError):
                self.c.apply_manual(key, 50)
        self.assertFalse(self.writes())

    def test_invalid_values_never_write(self):
        for duty in (0, 39, 101, float('nan'), float('inf'), True, 40.5, '60'):
            with self.assertRaises(BackendError):
                self.c.apply_manual('nct6798:1', duty)
        for curve in ([[20, 40], [60, 70]], [[20, 40], [60, 35], [85, 100]], [[20, 40], [60, 70], [86, 100]], [['a', 50], [60, 70], [85, 100]], None):
            with self.assertRaises(BackendError):
                self.c.apply_curve('nct6798:1', curve)
        self.assertFalse(self.writes())

    def test_stale_and_missing_cpu_never_write(self):
        self.fake.age = 30
        with self.assertRaises(BackendError):
            self.c.apply_manual('nct6798:1', 50)
        self.fake.age = 0
        self.fake.dev = [d for d in self.fake.dev if d['uid'] != 'cpu']
        with self.assertRaises(BackendError):
            self.c.apply_manual('nct6798:1', 50)
        self.assertFalse(self.writes())

    def test_other_source_has_guard_curve(self):
        points = [[25, 45], [60, 70], [80, 100]]
        self.c.apply_curve('nct6798:2', points, 'gpu')
        graphs = {p['temp_source']['device_uid']: p for p in self.fake.profiles.values() if p['p_type'] == 'Graph'}
        self.assertEqual(graphs['gpu']['speed_profile'], points)
        self.assertEqual(graphs['cpu']['speed_profile'], [[20., 40], [75., 40], [85., 100]])

    def test_api_failure_rollback_retains_current_successful_profile(self):
        self.c.apply_manual('nct6798:1', 60)
        previous = copy.deepcopy(self.fake.settings['board'])
        self.fake.fail = lambda path, method, data: path == '/profiles' and method == 'POST' and data['p_type'] == 'Mix'
        with self.assertRaises(BackendError):
            self.c.apply_manual('nct6798:1', 80)
        self.assertEqual(self.fake.settings['board'], previous)
        self.assertNotIn('pending_target', self.c.state['channels']['nct6798:1'])
        self.c.restore('nct6798:1')
        self.assertEqual(self.fake.settings['board'], [])

    def test_lost_apply_response_rolls_back(self):
        self.fake.fail_after_commit = True
        count = [0]
        def fail(path, method, data):
            if path.endswith('/profile') and method == 'PUT':
                count[0] += 1
                return count[0] == 1
            return False
        self.fake.fail = fail
        with self.assertRaises(BackendError):
            self.c.apply_manual('nct6798:1', 75)
        self.assertEqual(self.fake.settings['board'], [])
        self.assertEqual(set(self.fake.profiles), {'0'})

    def test_baseline_kept_after_repeated_applies_and_restart(self):
        initial = {'channel_name': 'fan1', 'speed_fixed': 65}
        self.fake = Fake(initial)
        self.c.client = self.fake
        self.c.apply_manual('nct6798:1', 50)
        self.c.apply_manual('nct6798:1', 75)
        self.assertEqual(len(self.fake.profiles), 4)
        resumed = Controller(self.fake, self.temp.name)
        resumed._match_board = self.c._match_board
        resumed.restore('nct6798:1')
        self.assertEqual(self.fake.settings['board'], [initial])
        self.assertEqual(set(self.fake.profiles), {'0'})

    def test_journal_failure_never_writes_or_blocks_next_action(self):
        with patch.object(self.c, '_save', side_effect=BackendError('Disk voll')):
            with self.assertRaises(BackendError):
                self.c.apply_manual('nct6798:1', 55)
        self.assertFalse(self.writes())
        self.assertEqual(self.c.state['channels'], {})
        self.c.apply_manual('nct6798:1', 55)

    def test_cleanup_respects_external_profile_reference(self):
        self.c.apply_manual('nct6798:1', 50)
        old = self.fake.settings['board'][0]['profile_uid']
        self.fake.profiles['external'] = {'uid': 'external', 'p_type': 'Mix', 'member_profile_uids': [old]}
        self.c.apply_manual('nct6798:1', 60)
        self.assertIn(old, self.fake.profiles)
        self.assertIn('external', self.fake.profiles)

    def test_post_apply_disk_failure_rolls_back(self):
        original_save = self.c._save
        count = [0]
        def save():
            count[0] += 1
            if count[0] == 2:
                raise BackendError('Disk voll')
            original_save()
        with patch.object(self.c, '_save', side_effect=save):
            with self.assertRaises(BackendError):
                self.c.apply_manual('nct6798:1', 55)
        self.assertEqual(self.fake.settings['board'], [])
        self.assertNotIn('pending_target', self.c.state['channels']['nct6798:1'])
        self.assertEqual(set(self.fake.profiles), {'0'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
