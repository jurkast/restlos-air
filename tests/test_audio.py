"""Audio transactions using an in-memory fake pactl server."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import audio


def device(index,name,muted=False,percent=50,monitor=False):
    return {'index':index,'name':name,'description':name.title(), 'mute':muted,
            'properties':{'device.class':'monitor' if monitor else 'sound'},
            'volume':{'left':{'value':round(percent*65536/100)},
                      'right':{'value':round(percent*65536/100)}}}


class Server:
    def __init__(self):
        self.sinks=[device(1,'speaker'),device(2,'headset')]
        self.sources=[device(3,'microphone'),device(4,'headset-mic'),device(9,'speaker.monitor',monitor=True)]
        self.defaults={'sink':1,'source':3}
        self.streams={'sink-inputs':[{'index':10,'sink':1,'corked':False}],
                      'source-outputs':[{'index':11,'source':3,'corked':False},
                                        {'index':12,'source':9,'corked':False}]}
        self.calls=[];self.fail=None

    def run(self,command,**kwargs):
        self.calls.append((command,kwargs))
        args=command[1:]
        if self.fail and self.fail(args):return subprocess.CompletedProcess(command,1,'','failure')
        if args[:1]==['--format=json']:
            query=args[1:]
            if query==['info']:
                response={f'default_{kind}_name':next(d['name'] for d in getattr(self,kind+'s') if d['index']==index)
                          for kind,index in self.defaults.items()}
            elif query==['list','sinks']:response=self.sinks
            elif query==['list','sources']:response=self.sources
            elif query[0]=='list':response=self.streams[query[1]]
            else:raise AssertionError(query)
            return subprocess.CompletedProcess(command,0,json.dumps(response),'')
        op=args[0]
        if op.startswith('set-default-'):self.defaults[op.removeprefix('set-default-')]=int(args[1])
        elif op.startswith('move-'):
            kind='sink' if op=='move-sink-input' else 'source'
            streams=self.streams['sink-inputs' if kind=='sink' else 'source-outputs']
            next(s for s in streams if s['index']==int(args[1]))[kind]=int(args[2])
        else:
            kind='sink' if op.startswith('set-sink') else 'source'
            item=next(d for d in getattr(self,kind+'s') if d['index']==int(args[1]))
            if op.endswith('-mute'):item['mute']=args[2]=='1'
            elif op.endswith('-volume'):
                item['volume']={'left':{'value':round(float(args[2][:-1])*65536/100)}}
            else:raise AssertionError(op)
        return subprocess.CompletedProcess(command,0,'','')

    def writes(self):return [args[1:] for args,_ in self.calls if args[1]!='--format=json']


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.server=Server()
        self.which=patch.object(audio.shutil,'which',return_value='/fake/pactl');self.which.start()
        self.run_patch=patch.object(audio.subprocess,'run',side_effect=self.server.run)
        self.run=self.run_patch.start()
        self.monitor=audio.AudioMonitor()
    def tearDown(self):self.run_patch.stop();self.which.stop()

    def test_read_snapshot_filters_monitors_and_is_independent(self):
        state=self.monitor._refresh()
        self.assertEqual(state['default_sink'],1);self.assertEqual(state['default_source'],3)
        self.assertEqual([d['id'] for d in state['sources']],[3,4])
        self.assertEqual(state['volume_percent'],50)
        state['sinks'].clear();self.assertEqual(len(self.monitor.snapshot()['sinks']),2)
        self.assertEqual(self.server.writes(),[])

    def test_commands_are_bounded_and_never_shell(self):
        self.monitor._refresh()
        for command,options in self.server.calls:
            self.assertEqual(command[0],'/fake/pactl');self.assertFalse(options.get('shell',False))
            self.assertEqual(options['timeout'],2);self.assertEqual(options['env']['LC_ALL'],'C')
            self.assertEqual(options['stdin'],subprocess.DEVNULL)

    def test_volume_and_mutes_return_confirmed_state(self):
        self.assertEqual(self.monitor.set_volume(72)['volume_percent'],72)
        self.assertTrue(self.monitor.toggle_output_mute()['output_muted'])
        self.assertFalse(self.monitor.set_muted(False)['output_muted'])
        self.assertTrue(self.monitor.set_mic_muted(True)['mic_muted'])
        self.assertFalse(self.monitor.toggle_mic_mute()['mic_muted'])
        self.assertIn(['set-sink-volume','1','72.00%'],self.server.writes())

    def test_toggle_reads_current_default_instead_of_old_cache(self):
        self.monitor._refresh();self.server.defaults['sink']=2
        self.monitor.toggle_output_mute()
        self.assertEqual(self.server.writes(),[['set-sink-mute','2','1']])
        self.assertFalse(self.server.sinks[0]['mute'])

    def test_invalid_actions_never_call_server(self):
        for value in (True,-1,101,float('nan'),float('inf'),10**1000,'50'):
            with self.assertRaises(audio.AudioError):self.monitor.set_volume(value)
        for value in ('--help','1;bad',True,-1,4294967295,None):
            with self.assertRaises(audio.AudioError):self.monitor.set_default_sink(value)
        for value in (1,'false',None):
            with self.assertRaises(audio.AudioError):self.monitor.set_mic_muted(value)
        self.assertEqual(self.server.calls,[])

    def test_missing_device_and_unknown_mute_never_write(self):
        with self.assertRaises(audio.AudioError):self.monitor.set_default_sink(99)
        self.server.sinks[0]['mute']='unknown'
        with self.assertRaises(audio.AudioError):self.monitor.toggle_output_mute()
        self.assertEqual(self.server.writes(),[])

    def test_output_switch_moves_active_streams_and_confirms_default(self):
        self.server.streams['sink-inputs'] += [{'index':20,'sink':2,'corked':False}, {'index':21,'sink':1,'corked':True}]
        result=self.monitor.set_default_sink(2)
        self.assertEqual(result['default_sink'],2)
        self.assertEqual(self.server.writes(),[['set-default-sink','2'],['move-sink-input','10','2']])

    def test_microphone_switch_leaves_monitor_recordings_untouched(self):
        result=self.monitor.set_default_source(4)
        self.assertEqual(result['default_source'],4)
        self.assertEqual(self.server.writes(),[['set-default-source','4'],['move-source-output','11','4']])
        self.assertEqual(self.server.streams['source-outputs'][1]['source'],9)

    def test_partial_move_failure_preserves_confirmed_default(self):
        self.server.fail=lambda args:args[0]=='move-sink-input'
        with self.assertRaises(audio.AudioError) as caught:self.monitor.set_default_sink(2)
        self.assertEqual(caught.exception.snapshot['default_sink'],2)
        self.assertIn('1 aktive',str(caught.exception))
        self.assertEqual(self.monitor.snapshot()['default_sink'],2)

    def test_command_failure_keeps_actual_state_in_error(self):
        self.server.fail=lambda args:args[0]=='set-sink-volume'
        with self.assertRaises(audio.AudioError) as caught:self.monitor.set_volume(70)
        self.assertEqual(caught.exception.snapshot['volume_percent'],50)
        self.assertTrue(caught.exception.snapshot['available'])

    def test_moves_are_limited(self):
        self.server.streams['sink-inputs']=[{'index':i,'sink':1,'corked':False} for i in range(65)]
        with self.assertRaises(audio.AudioError):self.monitor.set_default_sink(2)
        moves=[c for c in self.server.writes() if c[0]=='move-sink-input']
        self.assertEqual(len(moves),64)

    def test_unavailable_pactl_and_server_timeout(self):
        self.monitor._pactl=None
        with self.assertRaises(audio.AudioError):self.monitor._refresh()
        self.assertFalse(self.monitor.snapshot()['available'])
        self.monitor._pactl='/fake/pactl'
        self.run.side_effect=subprocess.TimeoutExpired('fake',2)
        with self.assertRaises(audio.AudioError):self.monitor._refresh()
        self.assertIn('rechtzeitig',self.monitor.snapshot()['error'])

    def test_malformed_json_and_duplicate_devices_fail_closed(self):
        self.run.side_effect=lambda *a,**k:subprocess.CompletedProcess(a[0],0,'not JSON','')
        with self.assertRaises(audio.AudioError):self.monitor._refresh()
        self.run.side_effect=self.server.run
        self.server.sinks.append(deepcopy(self.server.sinks[0]))
        with self.assertRaises(audio.AudioError):self.monitor._refresh()
        self.assertFalse(self.monitor.snapshot()['available'])

    def test_volume_parser_and_monitor_formats(self):
        for value in ({},{'x':{'value':True}},{'x':{'value':-1}},{'x':{'value':float('nan')}},{'x':{'value':10**1000}}):
            self.assertIsNone(audio._volume(value))
        self.assertTrue(audio._monitor_source({'monitor_of_sink':0}))
        self.assertTrue(audio._monitor_source({'monitor_source':'speaker'}))
        self.assertFalse(audio._monitor_source({'monitor_of_sink':4294967295}))

    def test_callback_suppresses_identical_state_and_stops_after_stop(self):
        callbacks=[];self.monitor.callback=callbacks.append
        self.monitor._refresh();self.monitor._publish();self.monitor._refresh();self.monitor._publish()
        self.assertEqual(len(callbacks),1)
        self.monitor._stop.set();self.server.sinks[0]['mute']=True
        self.monitor._refresh();self.monitor._publish();self.assertEqual(len(callbacks),1)

    def test_subscriber_ignores_client_noise_debounces_and_stops(self):
        class Process:
            def __init__(self):
                read,self.write=os.pipe();self.stdout=os.fdopen(read,'rb',buffering=0)
                self.terminated=False
            def emit(self,line):os.write(self.write,line.encode()+b'\n')
            def terminate(self):
                self.terminated=True
                if self.write is not None:os.close(self.write);self.write=None
            kill=terminate
            def wait(self,timeout=None):return 0
        process=Process();callbacks=[];self.monitor.callback=callbacks.append
        def wait(condition):
            until=time.monotonic()+2
            while not condition() and time.monotonic()<until:time.sleep(.005)
            self.assertTrue(condition())
        with patch.object(audio.subprocess,'Popen',return_value=process) as popen:
            try:
                self.monitor.start();self.monitor.start();wait(lambda:len(callbacks)==1)
                self.assertEqual(popen.call_count,1)
                self.assertEqual(len(self.server.calls),3)
                process.emit("Event 'new' on client #1");time.sleep(.03)
                self.assertEqual(len(self.server.calls),3)
                self.server.sinks[0]['mute']=True
                for _ in range(5):process.emit("Event 'change' on sink #1")
                wait(lambda:len(callbacks)==2)
                self.assertEqual(len(self.server.calls),6)
            finally:self.monitor.stop()
        self.assertFalse(self.monitor._thread.is_alive());self.assertTrue(process.terminated)
        self.assertIsNone(self.monitor._wake_read);self.assertIsNone(self.monitor._wake_write)


if __name__=='__main__':unittest.main()
