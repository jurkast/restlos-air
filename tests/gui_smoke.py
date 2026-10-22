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
    return {'cpu': 41., 'gpu': 39., 'hotspot': 44., 'ssd': 38., 'mainboard': True, 'fans': fans}


class GuiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='restlos-gui-test-')
        cls.environment = patch.dict(os.environ, {'XDG_CONFIG_HOME': cls.directory.name,
                                                  'XDG_STATE_HOME': cls.directory.name + '/state'})
        cls.environment.start()
        import air_studio
        import control
        import coolercontrol
        import network
        from gi.repository import Gtk, Gio, GLib
        cls.air, cls.Gtk, cls.Gio, cls.GLib = air_studio, Gtk, Gio, GLib
        cls.css = Gtk.CssProvider(); cls.css.load_from_data(air_studio.CSS)
        Gtk.StyleContext.add_provider_for_screen(air_studio.Gdk.Screen.get_default(), cls.css,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
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
        self.assertEqual(self.window.mini_values['cpu'].get_text(), '12 %')
        self.assertEqual(self.window.mini_values['gpu'].get_text(), '37 %')
        self.assertEqual(self.window.mini_values['ram'].get_text(), '8,0/32 GiB')
        self.assertAlmostEqual(self.window.mini_bars['ram'].get_value(), .25)
        self.window.update_network({'internet_ms': 20, 'gateway_ms': 2, 'internet_status': 'ok',
            'signal_percent': 65, 'connection': 'wlan', 'rx_bytes_sec': 2*1024**2, 'tx_bytes_sec': 1024})
        self.assertEqual(self.window.mini_ping.get_text(), 'Ping · 20 ms')
        self.assertEqual(self.window.mini_signal.get_text(), 'WLAN-Signal 65 %')
        self.assertEqual(self.window.mini_transfer.get_text(), '↓ 2,0 MiB/s   ↑ 1 KiB/s')
        self.window.metrics_state={};self.window.update_metrics()
        self.assertEqual(self.window.mini_values['cpu'].get_text(), '— %')
        self.assertEqual(self.window.mini_bars['cpu'].get_value(), 0)
        self.window.update_network({'internet_status':'disconnected'})
        self.assertEqual(self.window.mini_ping.get_text(), 'Ping · offline')
        self.assertEqual(self.window.mini_transfer.get_text(), '↓ —   ↑ —')

    def test_compact_widget_is_narrow_and_shows_four_temperatures(self):
        self.window.update()
        self.assertEqual(set(self.window.mini_temps), {'cpu', 'gpu', 'hotspot', 'ssd'})
        self.assertEqual(self.window.mini_temps['ssd'].get_text(), '38°')
        _, natural = self.window.stack.get_child_by_name('compact').get_preferred_width()
        self.assertLessEqual(natural, self.air.COMPACT_WIDTH)  # system fonts vary by a few pixels
        self.assertEqual(self.window.mini_live.get_text(), 'LIVE')

    def test_overheating_alerts_while_hidden_with_hysteresis(self):
        sent = []
        self.window.get_application = lambda: type('App', (), {
            'send_notification': lambda _, key, note: sent.append(key),
            'withdraw_notification': lambda _, key: sent.append('withdraw')})()
        hot = fake_sensor_data(False); hot['cpu'] = 91.
        self.window.hide()
        self.snapshot_patch.stop()
        try:
            for cpu in (91., 83., 82., 79.):
                data = dict(hot, cpu=cpu)
                with patch.object(self.air, 'snapshot', return_value=data):
                    self.window.update()
                if cpu == 91.:
                    self.assertEqual(sent, ['temperature'])
                    self.assertEqual(self.window.mini_live.get_text(), 'HEISS')
                if cpu in (83., 82.):
                    self.assertEqual(sent, ['temperature'], 'no flapping inside the hysteresis band')
            self.assertEqual(sent, ['temperature', 'withdraw'])
            self.assertEqual(self.window.mini_live.get_text(), 'WARM')
        finally:
            self.snapshot_patch.start()

    def test_on_top_choice_is_saved(self):
        self.window.pin.set_active(False)
        self.assertFalse(self.window.settings['on_top'])
        self.window.pin.set_active(True)
        self.assertTrue(self.window.settings['on_top'])

    def test_gear_and_values_open_settings(self):
        header = self.window.stack.get_child_by_name('compact').get_children()[0]
        gear = [c for c in header.get_children() if isinstance(c, self.Gtk.Button) and c.get_label() == '⚙'][0]
        gear.clicked()
        self.assertFalse(self.window.compact)
        self.window.set_compact(True)
        self.window.open_studio.clicked()
        self.assertFalse(self.window.compact)

    def test_thresholds_are_editable_saved_and_validated(self):
        warn, hot = self.window.limit_spins['cpu']
        hot.set_value(80); warn.set_value(70)
        self.assertEqual(self.window.limits['cpu'], (70, 80))
        self.assertEqual(self.window.settings['limits'], {'cpu': [70, 80]})
        hot_data = fake_sensor_data(False); hot_data['cpu'] = 81.
        self.snapshot_patch.stop()
        try:
            with patch.object(self.air, 'snapshot', return_value=hot_data):
                self.window.update()
        finally:
            self.snapshot_patch.start()
        self.assertEqual(self.window.mini_live.get_text(), 'HEISS')
        warn.set_value(90)
        self.assertEqual(self.window.limits['cpu'], (70, 80))
        self.assertIn('unter dem Alarm', self.window.limits_feedback.get_text())
        self.window.reset_limits()
        self.assertEqual(self.window.limits['cpu'], (75, 85))
        self.assertNotIn('limits', self.window.settings)
        self.assertEqual(self.air.load_limits({'cpu': [90, 80], 'gpu': [60, 70], 'x': [1, 2]})['cpu'], (75, 85))
        self.assertEqual(self.air.load_limits({'gpu': [60, 70]})['gpu'], (60, 70))


    def test_update_status_shows_restart_buttons_only_when_ready(self):
        from types import SimpleNamespace
        status = SimpleNamespace(automatic=True, checked=None, restart_ready=False, kind='current',
                                 version='', pending='', detail='')
        self.window.show_update_status(status)
        self.assertIn('ist aktuell', self.window.update_info.get_text())
        self.assertFalse(self.window.mini_update.get_visible())
        self.assertFalse(self.window.update_restart.get_visible())
        ready = SimpleNamespace(**{**vars(status), 'restart_ready': True, 'pending': '9.9.9', 'kind': 'ready'})
        self.window.show_update_status(ready)
        self.assertTrue(self.window.mini_update.get_visible())
        self.assertTrue(self.window.update_restart.get_visible())
        self.assertIn('9.9.9', self.window.update_info.get_text())
        self.window.show_update_status(None, supported=False)
        self.assertIn('git pull', self.window.update_info.get_text())


    def test_bar_view_replaces_widget_and_opens_studio(self):
        self.window.view_combo.set_active_id('bar')
        self.drain()
        self.assertEqual(self.window.settings['view_mode'], 'bar')
        self.assertTrue(self.window.bar_visible())
        self.assertFalse(self.window.get_visible())
        self.window.metrics_last = 0; self.window.update()
        self.assertEqual(self.window.bar.temps['cpu'].get_text(), '41°')
        self.assertEqual(self.window.bar.loads['gpu'].get_text(), '37 %')
        self.assertEqual(self.window.bar.memory['ram'][0].get_text(), '8,0/32 GiB')
        x, y, width, height = self.window.bar.geometry()
        self.assertIn(height, range(34, 80))  # one row, or two rows on narrow monitors
        self.window.edge_combo.set_active_id('right')
        self.assertTrue(self.window.bar.vertical)
        self.assertGreaterEqual(self.window.bar.geometry()[2], 124)  # measured; wider system fonts are allowed
        self.window.show_view(False)
        self.assertTrue(self.window.get_visible())
        self.assertFalse(self.window.compact)
        self.window.set_compact(True)
        self.assertFalse(self.window.get_visible())
        self.assertTrue(self.window.bar_visible())
        self.window.view_combo.set_active_id('widget')
        self.assertFalse(self.window.bar_visible())

    def test_strut_values_follow_the_edge(self):
        import bar
        self.assertEqual(bar.strut_values('bottom', 1920, 1046, 1920, 34, 3840, 1080)[3], 34)
        self.assertEqual(bar.strut_values('bottom', 1920, 1046, 1920, 34, 3840, 1080)[10:], [1920, 3839])
        self.assertEqual(bar.strut_values('top', 0, 0, 1920, 34, 3840, 1080)[2], 34)
        self.assertEqual(bar.strut_values('right', 3716, 0, 124, 1080, 3840, 1080)[1], 124)
        self.assertEqual(bar.strut_values('left', 1920, 0, 124, 1080, 3840, 1080)[0], 2044)


    def test_no_strut_at_an_edge_shared_with_another_monitor(self):
        self.window.view_combo.set_active_id('bar'); self.window.edge_combo.set_active_id('right')
        bar = self.window.bar
        # Second monitor on the left of the primary: its right edge touches the primary monitor.
        with patch.object(bar, 'screen_bounds', return_value=(0, 0, 3840, 1080)), \
             patch.object(bar, 'geometry', return_value=(1796, 0, 124, 1080)):
            self.assertFalse(bar.reserve_possible())
            self.window.refresh_bar_hint()
            self.assertTrue(self.window.bar_hint.get_visible())
        with patch.object(bar, 'screen_bounds', return_value=(0, 0, 3840, 1080)), \
             patch.object(bar, 'geometry', return_value=(3716, 0, 124, 1080)):
            self.assertTrue(bar.reserve_possible())
        self.window.view_combo.set_active_id('widget')


    def test_horizontal_bar_spans_the_whole_monitor_not_the_workarea(self):
        self.window.view_combo.set_active_id('bar'); self.window.edge_combo.set_active_id('bottom')
        monitor = self.air.Gdk.Display.get_default().get_monitor(self.window.monitor.get_active()).get_geometry()
        x, y, width, height = self.window.bar.geometry()
        self.assertEqual((x, width, y + height), (monitor.x, monitor.width, monitor.y + monitor.height))
        for item in (self.window.bar.texts['pump'], self.window.bar.texts['ping'], self.window.bar.texts['transfer']):
            self.assertGreaterEqual(item.get_width_chars(), 5)
        self.window.view_combo.set_active_id('widget')


    def test_chosen_edge_stays_fixed_and_survives_restart(self):
        self.window.view_combo.set_active_id('bar'); self.window.edge_combo.set_active_id('right')
        bar = self.window.bar
        self.window.metrics_last = 0; self.window.update(); self.drain()
        first = bar.geometry()  # after the first values; the side bar may widen once
        for _ in range(5):
            self.window.place(); self.window.metrics_last = 0; self.window.update(); self.drain()
        self.window.reserve_check.set_active(False); self.window.reserve_check.set_active(True)
        self.assertEqual(bar.geometry(), first)
        self.assertEqual(bar.edge, 'right')
        # A wider value may widen the side bar once; shorter values never move it back.
        bar.locked_width = first[2] + 20
        wider = bar.geometry(); bar.place()
        self.assertEqual(wider[0] + wider[2], first[0] + first[2])
        self.assertEqual(bar.geometry(), wider)
        # Settings reload (restart / update) restores the same edge.
        self.window.save()
        reopened = self.air.AirWindow(self.app)
        try:
            self.assertEqual(reopened.edge_combo.get_active_id(), 'right')
            self.assertEqual(reopened.view_combo.get_active_id(), 'bar')
            reopened.show_view(True)
            self.assertEqual(reopened.bar.edge, 'right')
        finally:
            reopened.view_combo.set_active_id('widget'); reopened.destroy()
        self.window.view_combo.set_active_id('widget')


    def test_profile_cycle_applies_to_all_fans_and_manual_change_clears_it(self):
        self.window.mini_profile.clicked()
        self.wait_for(lambda: not self.window.backend_busy)
        self.assertEqual(self.window.profile, 'quiet')
        self.assertEqual(self.window.settings['fan_profile'], 'quiet')
        applied = [action[1] for action in self.window.controller.actions if action[0] == 'curve']
        self.assertEqual(applied, ['nct6798:1', 'nct6798:2', 'nct6798:3'])
        self.assertEqual(self.window.mini_profile.get_label(), 'LEISE')
        self.window.profile_panel.buttons['game'].set_active(True)
        self.wait_for(lambda: not self.window.backend_busy)
        self.assertEqual(self.window.profile, 'game')
        self.window.open_studio.clicked()
        self.wait_for(lambda: not self.window.backend_busy)
        self.window.apply_control()
        self.wait_for(lambda: not self.window.backend_busy)
        self.assertIsNone(self.window.profile, self.window.control_feedback.get_text())
        self.assertEqual(self.window.mini_profile.get_label(), 'EIGENE')

    def test_game_detection_switches_profile_automatically(self):
        from processes import ProgramUsage
        self.window.game_mode.enabled = True
        self.window.profile = 'normal'
        game = [ProgramUsage('Hades2.exe', 40.0, 2**31, 1, True)]
        with patch.object(self.window.scanner, 'sample', return_value=game):
            self.window.scanner.game = 'Hades II'
            self.window.process_last = 0
            self.window.background_tasks(time.monotonic(), False)
        self.wait_for(lambda: not self.window.backend_busy)
        self.assertEqual(self.window.profile, 'game')
        self.assertEqual(self.window.game_mode.previous, 'normal')
        self.assertIn('Hades2.exe 40 %', self.window.top_text())

    def test_process_panel_lists_programs_and_hides_protected_end_buttons(self):
        from processes import ProgramUsage
        self.window.set_compact(False)
        self.window.process_panel.refresh([ProgramUsage('chrome', 34.2, 3 * 2**30, 12, True),
                                           ProgramUsage('gnome-shell', 5.0, 2**29, 1, False)])
        name, cpu, memory, end = self.window.process_panel.rows[0]
        self.assertEqual((name.get_text(), cpu.get_text(), memory.get_text()), ('chrome  ×12', '34 %', '3,0 GiB'))
        self.assertTrue(end.get_visible())
        self.assertFalse(self.window.process_panel.rows[1][3].get_visible())

    def test_bar_items_can_be_chosen_and_reordered(self):
        self.window.view_combo.set_active_id('bar'); self.window.edge_combo.set_active_id('bottom')
        panel = self.window.bar_items_panel
        panel.store(['ssd', 'cpu', 'disk', 'power', 'clock'])
        bar = self.window.bar
        self.assertEqual(bar.items, ['ssd', 'cpu', 'disk', 'power', 'clock'])
        self.assertEqual(set(bar.temps), {'ssd', 'cpu'})
        self.assertNotIn('ram', bar.memory)
        panel.move(None, 'cpu', -1)
        self.assertEqual(self.window.settings['bar_items'][:2], ['cpu', 'ssd'])
        self.window.metrics_state = {'disk_read_bytes_sec': 2 * 1024**2, 'disk_write_bytes_sec': 0, 'cpu_clock_mhz': 4650}
        self.window.extras = {'gpu_power_w': 212.4}
        bar.show_values(self.window)
        self.assertEqual(bar.texts['power'].get_text(), '212 W')
        self.assertEqual(bar.texts['clock'].get_text(), '4,7 GHz')
        self.assertIn('R 2,0 MiB/s', bar.texts['disk'].get_text())
        panel.reset()
        self.assertIn('audio', bar.items)
        self.window.view_combo.set_active_id('widget')

    def test_history_panel_and_light_theme_css(self):
        import theme
        self.window.history_store.add({'cpu': 77, 'gpu': 60}, 50, 'chrome', 'Hades II')
        self.window.history_panel.refresh(self.window.history_store)
        self.assertIn('CPU max. 77 °C', self.window.history_panel.peaks.get_text())
        self.assertIn('Hades II', self.window.history_panel.peaks.get_text())
        light = theme.light_css(self.air.CSS)
        self.assertNotIn(b'#20323e', light)
        self.assertIn(b'#ffffff', light)
        self.air.Gtk.CssProvider().load_from_data(light)

    def test_toggle_hides_and_shows_the_current_view(self):
        self.window.hide_views()
        self.assertFalse(self.window.get_visible())
        self.window.show_view(True)
        self.assertTrue(self.window.get_visible())


    def test_a_failing_extra_never_stops_the_values(self):
        with patch.object(self.window, 'background_tasks', side_effect=RuntimeError('kaputt')):
            self.assertTrue(self.window.update())
        self.assertEqual(self.window.mini_temps['cpu'].get_text(), '41°')
        with patch.object(self.window, 'refresh_values', side_effect=RuntimeError('ganz kaputt')):
            self.assertTrue(self.window.update())
        log = Path(os.environ['XDG_STATE_HOME']) / 'restlos-air' / 'fehler.log'
        self.assertIn('kaputt', log.read_text())


    def test_audio_in_bar_starts_audio_once_without_recursion(self):
        import types
        import audio_widget
        class QuietAudio:
            def __init__(self, callback): self.callback = callback
            def start(self): return self
            def stop(self, *args): pass
            def snapshot(self): return {}
        app = self.app
        app.audio_window, app.audio_pending, app.audio_creating, app.window = None, False, False, self.window
        app.ensure_audio = types.MethodType(self.air.AirApp.ensure_audio, app)
        created = []
        original = audio_widget.AudioWindow.__init__
        def counting(window, *args, **kwargs):
            created.append(window)
            original(window, *args, **kwargs)
        self.window.settings['bar_items'] = ['cpu', 'audio']
        try:
            with patch.object(audio_widget, 'AudioMonitor', QuietAudio), \
                 patch.object(audio_widget.AudioWindow, '__init__', counting):
                self.window.view_combo.set_active_id('bar')
                self.window.bar.rebuild()
                for _ in range(3):
                    self.window.push_bar(); self.drain()
            self.assertEqual(len(created), 1)
            self.assertIs(app.audio_window, created[0])
            self.assertFalse(app.audio_pending)
        finally:
            if app.audio_window: app.audio_window.destroy()
            for name in ('audio_window', 'audio_pending', 'audio_creating', 'window', 'ensure_audio'):
                delattr(app, name)
            self.window.settings.pop('bar_items', None)
            self.window.view_combo.set_active_id('widget')

    def test_bar_groups_values_with_short_captions(self):
        from processes import ProgramUsage
        self.window.view_combo.set_active_id('bar'); self.window.edge_combo.set_active_id('bottom')
        bar = self.window.bar
        def walk(widget):
            yield widget
            if isinstance(widget, self.Gtk.Container):
                for child in widget.get_children():
                    yield from walk(child)
        separators = [w for w in walk(bar.get_child()) if isinstance(w, self.Gtk.Separator)]
        self.assertGreaterEqual(len(separators), 5)  # groups: temp | memory | system | cooling | net | audio
        self.window.top = [ProgramUsage('claude-desktop', 1.0, 2**30, 3, True)]
        self.window.update_network({'internet_ms': 21, 'internet_status': 'ok', 'connection': 'wlan', 'signal_percent': 65})
        bar.show_values(self.window)
        self.assertEqual(bar.texts['top'].get_text(), 'claude-desktop 1 %')
        self.assertEqual(bar.texts['ping'].get_text(), '21 ms · 65 %')
        self.assertNotIn('Pumpe', bar.texts['pump'].get_text())
        self.window.view_combo.set_active_id('widget')


    def test_all_items_by_default_and_bar_gets_denser_instead_of_overflowing(self):
        import panels
        self.assertEqual(set(panels.bar_items({})), {key for key, _ in panels.BAR_ITEMS})
        self.assertEqual(panels.bar_items({'bar_items': panels.OLD_DEFAULT_BAR_ITEMS}), panels.DEFAULT_BAR_ITEMS)
        self.assertEqual(panels.bar_items({'bar_items': ['cpu']}), ['cpu'])
        self.window.view_combo.set_active_id('bar'); self.window.edge_combo.set_active_id('bottom')
        bar = self.window.bar
        monitor = bar.area().width
        try:
            with patch.object(bar, 'area', return_value=type('Area', (), {'x': 0, 'y': 0, 'width': 1100, 'height': 800})()):
                bar.rebuild(); bar.place()
                self.assertGreater(bar.density, 0)
                self.assertLessEqual(bar.get_child().get_preferred_width()[1], 1100)
            bar.rebuild()
            self.assertLessEqual(bar.get_child().get_preferred_width()[1], monitor)
        finally:
            self.window.view_combo.set_active_id('widget')


if __name__ == '__main__':
    unittest.main(verbosity=2)
