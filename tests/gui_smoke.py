#!/usr/bin/python3
"""Native GTK smoke checks with fake sensors, cooling and network workers.

Run separately with xvfb-run. No hardware, keyring or network access is used.
"""
from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))


class QuietNetwork:
    def __init__(self, *args, **kwargs):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        return self

    def stop(self, *args, **kwargs):
        self.stopped = True


class FakeCache:
    def request(self, path, method='GET', data=None):
        if path != '/status' or method != 'GET':
            raise AssertionError('GUI test attempted an unexpected backend request')
        return {'devices': [{'uid': 'fake-board', 'status_history': [{
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'channels': [{'name': 'fan6', 'rpm': 2307}],
        }]}]}


class FakeController:
    def __init__(self, *args, **kwargs):
        self.client = FakeCache()
        self.board = {'uid': 'fake-board'}
        self.actions = []
        self.state = {'connected': True, 'mainboard': True, 'sources': ['cpu', 'gpu'],
                      'reason': '', 'channels': {}}
        for channel in ('1', '2', '3'):
            self.state['channels']['nct6798:' + channel] = {
                'controllable': True, 'reason': '', 'mode': 'default',
                'duty': None, 'curve': None, 'source': None,
                'can_restore': False, 'actual_setting': None,
            }
        for key in ('nct6798:6', 'amdgpu:1'):
            self.state['channels'][key] = {'controllable': False, 'can_restore': False,
                                          'reason': 'Read only', 'mode': 'default'}

    def refresh(self):
        return deepcopy(self.state)

    def status(self):
        return self.refresh()

    def apply_manual(self, key, duty):
        self.actions.append(('manual', key, duty))
        self.state['channels'][key].update(mode='manual', duty=duty, source='max',
                                          curve=[[20, duty], [75, duty], [85, 100]],
                                          can_restore=True)
        return self.refresh()

    def apply_curve(self, key, points, source='cpu'):
        self.actions.append(('curve', key, deepcopy(points), source))
        self.state['channels'][key].update(mode='curve', duty=None, curve=deepcopy(points),
                                          source=source, can_restore=True)
        return self.refresh()

    def restore(self, key):
        self.actions.append(('restore', key))
        self.state['channels'][key].update(mode='default', duty=None, curve=None, source=None)
        return self.refresh()


def fake_sensor_data(include_fans=True):
    fans = {}
    if include_fans:
        for channel, rpm in [('1', 600), ('2', 750), ('3', 590), ('6', 2307)]:
            fans['nct6798:' + channel] = {'rpm': rpm, 'pwm': 255 if channel == '6' else 100,
                                         'mode': 5, 'source': 'Fake CPU',
                                         'curve': [[40, 40], [65, 65], [85, 100]],
                                         'channel': channel, 'chip': 'nct6798'}
        fans['amdgpu:1'] = {'rpm': 0, 'pwm': 0, 'mode': None, 'source': None,
                            'curve': [], 'channel': '1', 'chip': 'amdgpu'}
    return {'cpu': 41., 'gpu': 39., 'hotspot': 44., 'mainboard': True, 'fans': fans}


class GuiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='restlos-gui-test-')
        cls.environment = patch.dict(os.environ, {'XDG_CONFIG_HOME': cls.directory.name})
        cls.environment.start()
        import air_studio
        import control
        import coolercontrol
        import network
        from gi.repository import Gtk, Gio, GLib
        cls.air, cls.Gtk, cls.Gio, cls.GLib = air_studio, Gtk, Gio, GLib
        cls.module_patches = [
            patch.object(air_studio, 'MetricsSampler', return_value=type('FakeMetrics', (), {'sample': lambda self: {
                'cpu_percent': 12, 'gpu_percent': 37, 'ram_used_bytes': 8*2**30,
                'ram_total_bytes': 32*2**30, 'ram_percent': 25,
                'vram_used_bytes': 4*2**30, 'vram_total_bytes': 16*2**30, 'vram_percent': 25,
            }})()),
            patch.object(network, 'NetworkMonitor', QuietNetwork),
            patch.object(control, 'Controller', FakeController),
            patch.object(air_studio, 'secret_store', return_value='fake-test-token'),
            patch.object(coolercontrol.Client, 'request', side_effect=AssertionError('Real HTTP disabled in GUI tests')),
            patch.object(coolercontrol, 'secret_store', side_effect=AssertionError('Real keyring disabled in GUI tests')),
        ]
        for mock in cls.module_patches:
            mock.start()
        cls.app = Gtk.Application(application_id='de.restlos.AirStudio.GuiTests',
                                  flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)

    @classmethod
    def tearDownClass(cls):
        for mock in reversed(cls.module_patches):
            mock.stop()
        cls.environment.stop()
        cls.directory.cleanup()

    def setUp(self):
        self.snapshots = []
        def snapshot(**kwargs):
            self.snapshots.append(dict(kwargs))
            return fake_sensor_data(kwargs.get('include_fans', True))
        self.snapshot_patch = patch.object(self.air, 'snapshot', side_effect=snapshot)
        self.snapshot_patch.start()
        self.window = self.air.AirWindow(self.app)
        self.window.show_all()
        self.wait_for(lambda: not self.window.backend_busy and self.window.controller is not None)

    def tearDown(self):
        if not self.window.closed:
            self.window.destroy()
        self.drain()
        self.snapshot_patch.stop()

    def drain(self):
        context = self.GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

    def wait_for(self, condition, timeout=2):
        until = time.monotonic() + timeout
        while not condition() and time.monotonic() < until:
            self.drain()
            time.sleep(.005)
        self.drain()
        self.assertTrue(condition(), 'GTK background operation timed out')

    def test_compact_sampling_click_and_cached_pump(self):
        self.window.update()
        self.wait_for(lambda: not self.window.pump_busy)
        self.assertFalse(self.snapshots[-1]['include_fans'])
        self.assertEqual(self.window.rows, {})
        self.assertEqual(self.window.mini_pump.get_text(), 'Pumpe · 2.307 U/min')
        self.window.open_studio.clicked()
        self.assertFalse(self.window.compact)
        self.assertTrue(self.snapshots[-1]['include_fans'])
        self.assertIn('nct6798:2', self.window.rows)
        self.window.update()
        self.assertFalse(self.snapshots[-1]['include_fans'])
        self.window.set_compact(True)
        self.window.pump_done(None)
        self.assertEqual(self.window.mini_pump.get_text(), 'Pumpe · — U/min')

    def test_native_controls_hydrate_and_keep_restore_available(self):
        manual = {'controllable': True, 'mode': 'manual', 'duty': 63,
                  'source': 'max', 'can_restore': True}
        curve = {'controllable': True, 'mode': 'curve', 'curve': [[38, 48], [64, 76], [85, 100]],
                 'source': 'cpu', 'can_restore': True}
        self.window.backend_state['channels'].update({'nct6798:2': manual, 'nct6798:1': curve})
        self.window.editor_channel = None
        self.window.update_control_state()
        self.assertEqual(self.window.duty.get_value(), 63)
        self.window.duty.set_value(71)
        self.window.update_control_state()
        self.assertEqual(self.window.duty.get_value(), 71)
        self.window.selected = 'nct6798:1'
        self.window.update_control_state()
        self.assertEqual(self.window.control_mode.get_active_id(), 'curve')
        self.assertEqual(self.window.source_combo.get_active_id(), 'cpu')
        self.assertEqual([[t.get_value(), d.get_value()] for t, d in self.window.points], curve['curve'])
        self.window.selected = 'nct6798:2'
        manual['controllable'] = False
        self.window.update_control_state()
        self.assertFalse(self.window.apply_button.is_sensitive())
        self.assertTrue(self.window.restore_button.is_sensitive())
        self.window.backend_busy = True
        self.window.update_control_state()
        self.assertFalse(self.window.restore_button.is_sensitive())
        self.window.backend_busy = False
        self.window.selected = 'nct6798:6'
        self.window.update_control_state()
        self.assertFalse(self.window.apply_button.is_sensitive())
        self.assertFalse(self.window.restore_button.is_sensitive())

    def test_apply_and_restore_only_use_fake_backend(self):
        self.window.open_studio.clicked()
        self.window.duty.set_value(70)
        self.window.apply_control()
        self.wait_for(lambda: not self.window.backend_busy)
        self.assertEqual(self.window.controller.actions[-1], ('manual', 'nct6798:2', 70))
        self.assertIn('bestätigt', self.window.control_feedback.get_text())
        self.window.restore_control()
        self.wait_for(lambda: not self.window.backend_busy)
        self.assertEqual(self.window.controller.actions[-1], ('restore', 'nct6798:2'))
        self.assertEqual(self.window.backend_state['channels']['nct6798:2']['mode'], 'default')

    def test_destroy_stops_workers_and_ignores_late_completion(self):
        self.window.destroy()
        self.assertTrue(self.window.closed)
        self.assertTrue(self.window.network.stopped)
        self.assertFalse(self.window.pump_done(2307))
        self.assertFalse(self.window.backend_done({}, None, None))

    def test_load_memory_network_and_missing_values(self):
        self.assertEqual(self.window.mini_load.get_text(), 'CPU 12 %   ·   GPU 37 %')
        self.assertIn('RAM 8.0/32.0 GiB', self.window.mini_memory.get_text())
        self.window.update_network({'internet_ms': 20, 'gateway_ms': 2, 'internet_status': 'ok',
            'signal_percent': 65, 'connection': 'wlan', 'rx_bytes_sec': 2*1024**2, 'tx_bytes_sec': 1024})
        self.assertIn('WLAN-Signal 65 %', self.window.mini_ping.get_text())
        self.assertEqual(self.window.mini_transfer.get_text(), '↓ 2.0 MiB/s   ·   ↑ 1 KiB/s')
        self.window.metrics_state={};self.window.update_metrics()
        self.assertIn('CPU — %', self.window.mini_load.get_text())
        self.window.update_network({'internet_status':'disconnected'})
        self.assertEqual(self.window.mini_ping.get_text(), 'Ping · offline')
        self.assertEqual(self.window.mini_transfer.get_text(), '↓ —   ·   ↑ —')


if __name__ == '__main__':
    unittest.main(verbosity=2)
