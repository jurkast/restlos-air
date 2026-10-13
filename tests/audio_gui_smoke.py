"""Native audio widget checks using a fake audio server and temporary settings."""
from copy import deepcopy
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import audio_widget
from gi.repository import Gtk, Gio, GLib


class FakeAudio:
    def __init__(self,callback=None):
        self.callback=callback;self.actions=[];self.stopped=False
        self.state={'available':True,'error':'','sinks':[
            {'id':1,'description':'Speaker'},{'id':2,'description':'Headset'}],
            'sources':[{'id':3,'description':'Microphone'},{'id':4,'description':'Headset microphone'}],
            'default_sink':1,'default_source':3,'volume_percent':50,'output_muted':False,'mic_muted':False}
    def start(self):self.callback(self.snapshot());return self
    def stop(self):self.stopped=True
    def snapshot(self):return deepcopy(self.state)
    def set_volume(self,value):self.actions.append(('volume',value));self.state['volume_percent']=value;return self.snapshot()
    def toggle_output_mute(self):
        self.actions.append(('output',));self.state['output_muted']=not self.state['output_muted'];return self.snapshot()
    def toggle_mic_mute(self):
        self.actions.append(('mic',));self.state['mic_muted']=not self.state['mic_muted'];return self.snapshot()
    def set_default_sink(self,value):self.actions.append(('sink',value));self.state['default_sink']=value;return self.snapshot()
    def set_default_source(self,value):self.actions.append(('source',value));self.state['default_source']=value;return self.snapshot()


class Owner:
    def __init__(self):
        self.settings={};self.saves=0
        self.monitor=type('MonitorChoice',(),{'get_active':lambda self:0})()
    def save(self):self.saves+=1
    def get_position(self):return 20,20
    def get_size(self):return 390,326


class AudioGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import air_studio
        cls.css=Gtk.CssProvider();cls.css.load_from_data(air_studio.CSS)
        Gtk.StyleContext.add_provider_for_screen(audio_widget.Gdk.Screen.get_default(),cls.css,Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        cls.patch=patch.object(audio_widget,'AudioMonitor',FakeAudio);cls.patch.start()
        cls.app=Gtk.Application(application_id='de.restlos.Audio.GuiTests',flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)
    @classmethod
    def tearDownClass(cls):cls.patch.stop()
    def setUp(self):
        self.owner=Owner();self.window=audio_widget.AudioWindow(self.app,self.owner)
        self.window.show_widget();self.drain()
    def tearDown(self):
        if not self.window.closed:self.window.destroy()
        self.drain()
    def drain(self):
        context=GLib.MainContext.default()
        while context.pending():context.iteration(False)
    def wait(self,condition):
        until=time.monotonic()+2
        while not condition() and time.monotonic()<until:self.drain();time.sleep(.005)
        self.drain();self.assertTrue(condition())

    def test_hydration_never_changes_audio_and_external_change_is_visible(self):
        self.assertEqual(self.window.monitor.actions,[])
        self.assertEqual(self.window.output_name.get_text(),'Speaker')
        data=self.window.monitor.snapshot();data.update(default_sink=2,volume_percent=75,mic_muted=True)
        self.window.update_state(data)
        self.assertEqual(self.window.monitor.actions,[])
        self.assertEqual(self.window.volume_value.get_text(),'75 %')
        self.assertEqual(self.window.mic_mute.get_label(),'Mikrofon stumm')
        self.assertEqual(self.window.sinks.get_active_id(),'2')

    def test_slider_debounce_applies_only_last_value(self):
        self.window.volume.set_value(60);self.window.volume.set_value(65);self.window.volume.set_value(72)
        self.assertEqual(self.window.monitor.actions,[])
        self.wait(lambda: bool(self.window.monitor.actions) and not self.window.busy)
        self.assertEqual(self.window.monitor.actions,[('volume',72)])
        self.assertEqual(self.window.volume_value.get_text(),'72 %')

    def test_buttons_and_device_choices_apply_to_fake_audio(self):
        self.window.output_mute.clicked();self.wait(lambda:not self.window.busy)
        self.assertTrue(self.window.state['output_muted'])
        self.window.mic_mute.clicked();self.wait(lambda:not self.window.busy)
        self.assertTrue(self.window.state['mic_muted'])
        self.window.sinks.set_active_id('2');self.wait(lambda:not self.window.busy)
        self.window.sources.set_active_id('4');self.wait(lambda:not self.window.busy)
        self.assertEqual(self.window.monitor.actions,[('output',),('mic',),('sink',2),('source',4)])

    def test_missing_devices_and_errors_are_visible(self):
        self.window.update_state({'available':False,'error':'Audio unavailable','sinks':[],'sources':[]})
        self.assertFalse(self.window.volume.get_sensitive());self.assertFalse(self.window.mic_mute.get_sensitive())
        self.assertFalse(self.window.sinks.get_sensitive());self.assertFalse(self.window.sources.get_sensitive())
        self.assertEqual(self.window.feedback.get_text(),'Audio unavailable')

    def test_device_panel_is_visible_and_collapses(self):
        self.assertFalse(self.window.device_panel.get_mapped())
        self.window.devices_button.set_active(True);self.drain()
        self.assertTrue(self.window.device_panel.get_mapped());self.assertTrue(self.window.sinks.get_mapped())
        self.assertTrue(self.window.sources.get_mapped());self.assertTrue(self.window.monitors.get_mapped())
        self.window.devices_button.set_active(False);self.drain()
        self.assertFalse(self.window.device_panel.get_mapped())

    def test_action_error_remains_visible_after_status_event(self):
        self.window.action_done(self.window.monitor.snapshot(),'Streamwechsel fehlgeschlagen')
        self.window.update_state(self.window.monitor.snapshot())
        self.assertEqual(self.window.feedback.get_text(),'Streamwechsel fehlgeschlagen')
        self.window.output_mute.clicked();self.wait(lambda:not self.window.busy)
        self.assertEqual(self.window.feedback.get_text(),'')

    def test_hide_show_and_cleanup_cancel_pending_volume(self):
        self.window.on_delete();self.assertFalse(self.window.get_visible())
        self.assertFalse(self.owner.settings['audio_visible'])
        self.window.show_widget();self.assertTrue(self.window.get_visible())
        self.assertTrue(self.owner.settings['audio_visible'])
        self.window.volume.set_value(20);self.window.destroy()
        self.assertTrue(self.window.monitor.stopped);self.assertIsNone(self.window.volume_timer)
        self.assertFalse(self.window.update_state(self.window.monitor.snapshot()))
        self.assertFalse(self.window.action_done({},None));self.assertEqual(self.window.monitor.actions,[])


if __name__=='__main__':unittest.main(verbosity=2)
