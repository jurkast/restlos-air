#!/usr/bin/python3
"""Restlos Air — native desktop widget and CoolerControl interface."""
import json
import os
import signal
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk, Gio, GLib
from sensors import snapshot, cached_pump
from metrics import MetricsSampler
from coolercontrol import BackendError, Client, secret_store, validate_curve

BASE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config'))) / 'restlos-air'
SETTINGS = CONFIG / 'settings.json'
CSS = b'''
window { background-color: transparent; color: #ecf5f2; }
#surface { background-color: rgba(13,23,29,0.94); border-radius: 22px; }
label { color: #ecf5f2; font-family: Sans; }
.brand { font-size: 11px; letter-spacing: 3px; color: #a6babc; }
.title { font-size: 30px; font-weight: 500; }
.muted { color: #a6babc; font-size: 12px; }
.value { font-size: 38px; font-weight: 400; }
.section { font-size: 16px; font-weight: 500; }
.rpm { font-size: 22px; font-weight: 400; }
.compact-value { font-size: 32px; font-weight: 400; }
.compact-caption { color: #a6babc; font-size: 11px; }
#mini-surface { background-color: rgba(13,23,29,0.94); border-radius: 16px; }
button.mini-content { background: transparent; padding: 12px 16px; border-radius: 10px; }
button.mini-content:hover { background: #20343b; }
button.mini-close { background: transparent; padding: 2px 8px; min-height: 20px; }
.accent { color: #84dfc5; }
.warning { color: #f2ba79; }
.panel { background-color: #192a31; border-radius: 12px; padding: 16px; }
button { background: #23353c; color: #e6f4f0; border: none; border-radius: 8px; box-shadow: none; text-shadow: none; padding: 8px 12px; }
button:hover { background: #304a53; }
button:checked { background: #36594f; }
button:focus { outline: 2px solid #84dfc5; outline-offset: 1px; }
button:disabled { color: #afbdc1; background: #27343a; }
button.fan { background: transparent; padding: 11px 12px; }
button.fan:checked { background: #263d43; }
entry, combobox button, spinbutton { color: #ecf5f2; background: #23353c; }
scale trough { background: #304047; border: none; }
scale highlight { background: #84dfc5; border: none; }
scale slider { background: #84dfc5; border: none; box-shadow: none; }
entry { border: 1px solid #547079; border-radius: 7px; padding: 7px; }
separator { background: #2d4147; min-height: 1px; }
'''


def label(text='', style=None, xalign=0):
    item = Gtk.Label(label=text, xalign=xalign)
    if style:
        item.get_style_context().add_class(style)
    return item


def box(vertical=True, spacing=8):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)


def add(parent, child, expand=False):
    parent.pack_start(child, expand, expand, 0)
    return child


class Chart(Gtk.DrawingArea):
    def __init__(self, history=False):
        super().__init__()
        self.history = history
        self.data = []
        self.set_size_request(-1, 136)
        self.connect('draw', self.draw_chart)
        self.get_accessible().set_name('CPU- und GPU-Temperaturverlauf' if history else 'Ausgelesene Hardware-Kennlinie')

    def draw_chart(self, widget, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        left, right, top, bottom = 37, w - 18, 10, h - 26
        if right <= left:
            return False
        xmin, xmax = (-120, 0) if self.history else (20, 130)
        ymax = 110 if self.history else 100
        def pos(x, y):
            return left + (x-xmin)/(xmax-xmin)*(right-left), bottom-y/ymax*(bottom-top)
        cr.set_font_size(10)
        for y in [0, 50, 100]:
            px, py = pos(xmin, y)
            cr.set_source_rgb(.18,.27,.3); cr.set_line_width(1)
            cr.move_to(left, py); cr.line_to(right, py); cr.stroke()
            cr.set_source_rgb(.66,.75,.76); cr.move_to(1, py+4)
            cr.show_text(f'{y}' + ('°' if self.history else '%'))
        for x in ([-120,-60,0] if self.history else [20,60,100,130]):
            px, py = pos(x,0)
            cr.move_to(max(left,min(right-25,px-10)),h-5)
            cr.show_text((f'{x}s' if x else 'jetzt') if self.history else f'{x}°C')
        series = self.data if self.history else [self.data]
        colors = [(.52,.87,.77),(.54,.70,.96)]
        cr.save(); cr.rectangle(left,top,right-left,bottom-top); cr.clip()
        for i, points in enumerate(series):
            cr.set_source_rgb(*colors[i % 2]); cr.set_line_width(2)
            started = False
            for x,y in points:
                if y is None:
                    if started: cr.stroke()
                    started = False
                    continue
                px,py=pos(x,y)
                if not started: cr.move_to(px,py); started=True
                else: cr.line_to(px,py)
            cr.stroke()
        cr.restore()
        return False


class AirWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title='Restlos Air Studio')
        try:
            source=SETTINGS if SETTINGS.exists() else BASE/'settings.json'
            self.settings = json.loads(source.read_text())
            if not isinstance(self.settings,dict): self.settings={}
        except (OSError,ValueError): self.settings={}
        self.names = self.settings.get('names',{})
        if not isinstance(self.names,dict): self.names={}
        self.selected = 'nct6798:2'
        self.rows = {}
        self.history = deque(maxlen=121)
        self.compact = True
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='restlos-control')
        self.controller = None
        self.backend_state = {'connected':False,'reason':'Steuerung wird verbunden …','channels':{}}
        self.backend_busy = False
        self.editor_channel = None
        self.backend_last = 0
        self.fan_cache = {}
        self.curve_last = 0
        self.pump_rpm = None
        self.pump_last = 0
        self.pump_read_at = 0
        self.pump_busy = False
        self.network_state = {}
        self.metrics_sampler = MetricsSampler()
        self.metrics_state = {}
        self.metrics_last = 0
        self.alarm_state = None
        self.auth_window = None
        self.closed = False
        self.geometries = self.settings.get('geometries', {})
        if not isinstance(self.geometries, dict): self.geometries = {}
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_app_paintable(True)
        screen=self.get_screen()
        visual=screen.get_rgba_visual()
        if visual: self.set_visual(visual)
        self.set_default_size(620,1040)
        self.set_border_width(10)
        self.connect('key-press-event',self.key)
        self.connect('delete-event',self.on_delete)
        self.stack = Gtk.Stack()
        self.stack.set_homogeneous(False)
        self.add(self.stack)
        scroller=Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.AUTOMATIC)
        self.stack.add_named(scroller, 'studio')
        outer=box(spacing=16);outer.set_name('surface')
        for side in ['top','bottom','start','end']: getattr(outer,'set_margin_'+side)(0)
        scroller.add(outer)
        content=box(spacing=16)
        for side in ['top','bottom','start','end']: getattr(content,'set_margin_'+side)(25)
        add(outer,content)
        top=box(False);add(content,top)
        drag=Gtk.EventBox(); heading=box(spacing=6)
        add(heading,label('RESTLOS  /  AIR','brand'))
        add(heading,label('Ruhe. Mit Kontrolle.','title'))
        drag.add(heading);drag.connect('button-press-event',self.drag)
        drag.set_tooltip_text('Hier ziehen, um Air Studio zu verschieben')
        add(top,drag,True)
        close=Gtk.Button(label='×');close.set_tooltip_text('Air Studio schließen');close.connect('clicked',lambda *_:self.close());add(top,close)
        self.status=add(content,label('Sensoren werden gelesen …','muted'))
        controls=box(False);add(content,controls)
        self.monitor=Gtk.ComboBoxText();self.monitor.connect('changed',self.monitor_changed);add(controls,self.monitor,True)
        self.pin=Gtk.ToggleButton(label='Im Vordergrund');self.pin.connect('toggled',lambda b:self.set_keep_above(self.compact or b.get_active()));add(controls,self.pin)
        self.compact_button=Gtk.Button(label='Widget')
        self.compact_button.set_tooltip_text('Kleine Temperaturanzeige öffnen · Strg+M')
        self.compact_button.connect('clicked',lambda *_:self.set_compact(True));add(controls,self.compact_button)
        display=Gdk.Display.get_default()
        display.connect('monitor-added',lambda *_:self.refresh_monitors())
        display.connect('monitor-removed',lambda *_:self.refresh_monitors())
        temps=box(False,28);add(content,temps)
        self.temp_labels={}
        for key,title in [('cpu','CPU'),('gpu','GPU'),('hotspot','GPU-HOTSPOT')]:
            col=box(spacing=2);add(temps,col,True);add(col,label(title,'brand'));self.temp_labels[key]=add(col,label('—','value'))
        history_box=box(spacing=6);history_box.get_style_context().add_class('panel');add(content,history_box)
        add(history_box,label('Temperaturen · letzte 2 Minuten','muted'))
        self.history_chart=add(history_box,Chart(True))
        add(history_box,label('CPU: Mint     GPU: Blau','muted'))
        load_box=box(spacing=6);load_box.get_style_context().add_class('panel');add(content,load_box)
        add(load_box,label('Auslastung & Speicher','section'))
        self.load_detail=add(load_box,label('Messung startet …','muted'));self.load_detail.set_line_wrap(True)
        audio_button=Gtk.Button(label='Audio-Widget öffnen')
        audio_button.connect('clicked',lambda *_:self.get_application().show_audio());add(load_box,audio_button)
        fan_head=box(False);add(content,fan_head)
        add(fan_head,label('Lüfter & Wasserpumpe','section'),True)
        self.show_zero=Gtk.CheckButton(label='0 U/min anzeigen');self.show_zero.connect('toggled',lambda *_:self.update());add(fan_head,self.show_zero)
        self.fan_box=add(content,box(spacing=2))
        self.detail=box(spacing=9);self.detail.get_style_context().add_class('panel');add(content,self.detail)
        name_row=box(False);add(self.detail,name_row)
        self.name_entry=Gtk.Entry();self.name_entry.set_placeholder_text('Anschluss benennen');self.name_entry.connect('activate',self.rename);add(name_row,self.name_entry,True)
        rename=Gtk.Button(label='Benennen');rename.connect('clicked',self.rename);add(name_row,rename)
        self.channel_label=add(self.detail,label('','muted'))
        self.mode_label=add(self.detail,label('','accent'))
        self.curve=add(self.detail,Chart())
        self.curve_note=add(self.detail,label('','muted'));self.curve_note.set_line_wrap(True)
        self.build_control_panel()
        network_box=box(spacing=8);network_box.get_style_context().add_class('panel');add(content,network_box)
        add(network_box,label('WLAN & Verbindung','section'))
        self.network_detail=add(network_box,label('Verbindung wird geprüft …','muted'));self.network_detail.set_line_wrap(True)
        add(network_box,label('Messung alle 5 Sekunden · Internetziel 1.1.1.1','muted'))
        footer=box(False);add(content,footer)
        note=label('Restlos Air · Die Regelung läuft auch bei geschlossenem Widget weiter.','muted');note.set_line_wrap(True);add(footer,note,True)
        self.build_compact()
        # Gtk.Stack can select a named child only after that child is visible.
        self.stack.show_all()
        self.stack.set_visible_child_name('compact' if self.compact else 'studio')
        self.set_keep_above(self.compact)
        self.update()
        self.refresh_monitors()
        self.timer=GLib.timeout_add_seconds(1,self.update)
        from network import NetworkMonitor
        self.network = NetworkMonitor(lambda data:GLib.idle_add(self.update_network,data))
        self.network.start()
        self.connect('destroy',self.cleanup)
        self.save()

    def default_name(self,key):
        if key=='nct6798:6':return 'DeepCool · Wasserpumpe'
        if key=='amdgpu:1':return 'Radeon RX 9070 XT'
        return 'Mainboard · Kanal '+key.split(':')[-1]

    def name(self,key):
        return str(self.names.get(key,self.default_name(key)))

    def save(self):
        self.settings['names']=self.names
        self.settings['view']='compact' if self.compact else 'studio'
        self.settings['geometries']=self.geometries
        try:
            SETTINGS.parent.mkdir(parents=True,exist_ok=True)
            tmp=SETTINGS.with_suffix('.tmp');tmp.write_text(json.dumps(self.settings,ensure_ascii=False,indent=2));tmp.replace(SETTINGS)
        except OSError:
            self.status.set_text('Namen konnten nicht gespeichert werden.')

    def rename(self,*_):
        value=self.name_entry.get_text().strip()[:64]
        if value:
            self.names[self.selected]=value;self.save();self.update()

    def drag(self,widget,event):
        if event.button==1:self.begin_move_drag(1,int(event.x_root),int(event.y_root),event.time)

    def key(self,widget,event):
        if event.keyval in (Gdk.KEY_m,Gdk.KEY_M) and event.state & Gdk.ModifierType.CONTROL_MASK:
            self.set_compact(not self.compact);return True
        if event.keyval==Gdk.KEY_Escape:
            if not self.compact:self.set_compact(True)
            else:self.hide()
            return True
        return False

    def build_compact(self):
        mini=box(spacing=0);mini.set_name('mini-surface')
        self.stack.add_named(mini,'compact')
        header=box(False,8)
        header.set_margin_top(10);header.set_margin_start(16);header.set_margin_end(8)
        drag=Gtk.EventBox();drag.add(label('RESTLOS / AIR','brand'))
        drag.connect('button-press-event',self.drag)
        drag.set_tooltip_text('Ziehen zum Verschieben · Strg+M wechselt die Ansicht')
        add(header,drag,True)
        self.mini_live=add(header,label('LIVE','compact-caption'))
        close=Gtk.Button(label='×');close.get_style_context().add_class('mini-close')
        close.set_tooltip_text('Air Studio schließen');close.connect('clicked',lambda *_:self.close())
        add(header,close);add(mini,header)
        self.open_studio=Gtk.Button()
        self.open_studio.get_style_context().add_class('mini-content')
        self.open_studio.get_accessible().set_name('Air Studio öffnen')
        self.open_studio.set_tooltip_text('Klicken öffnet die Einstellungen')
        self.open_studio.connect('clicked',lambda *_:self.set_compact(False))
        body=box(spacing=12);self.open_studio.add(body)
        temps=box(False,22);add(body,temps)
        self.mini_temps={}
        for key,title in [('cpu','CPU'),('gpu','GPU'),('hotspot','GPU-HOTSPOT')]:
            col=box(spacing=2);add(temps,col,True)
            add(col,label(title,'compact-caption'))
            self.mini_temps[key]=add(col,label('—','compact-value'))
        add(body,Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self.mini_pump=add(body,label('Pumpe · — U/min','muted'))
        self.mini_ping=add(body,label('Ping · — ms','accent'))
        self.mini_load=add(body,label('CPU — %   ·   GPU — %','muted'))
        self.mini_memory=add(body,label('RAM —   ·   VRAM —','muted'))
        self.mini_transfer=add(body,label('↓ —   ·   ↑ —','muted'))
        add(mini,self.open_studio)

    def remember_geometry(self):
        if not self.get_mapped(): return
        x,y=self.get_position();w,h=self.get_size()
        self.geometries['compact' if self.compact else 'studio']=[x,y,w,h]

    def on_delete(self,*_):
        self.remember_geometry();self.save()
        if not self.compact:self.set_compact(True)
        else:self.hide()
        return True

    def cleanup(self,*_):
        self.closed = True
        GLib.source_remove(self.timer)
        self.network.stop()
        self.executor.shutdown(wait=False,cancel_futures=True)

    def show_view(self,compact=True):
        self.set_compact(compact)
        self.show_all();self.update();self.present()

    def set_compact(self,compact):
        if self.compact == compact: return
        self.remember_geometry()
        self.compact=compact
        self.stack.set_visible_child_name('compact' if compact else 'studio')
        self.set_keep_above(compact or self.pin.get_active())
        self.place()
        # Let GTK recompute its minimum size after changing stack children.
        GLib.idle_add(self.place)
        self.save()
        if compact:self.open_studio.grab_focus()
        else:
            self.compact_button.grab_focus()
            self.curve_last=0
            self.update()

    def refresh_monitors(self):
        self.monitor.handler_block_by_func(self.monitor_changed)
        self.monitor.remove_all();d=Gdk.Display.get_default();selected=0
        desired=self.settings.get('monitor')
        for i in range(d.get_n_monitors()):
            m=d.get_monitor(i);g=m.get_geometry();model=m.get_model() or f'Monitor {i+1}'
            self.monitor.append(str(i),f'{model} · {g.width} × {g.height}')
            if model==desired or (desired is None and not m.is_primary()):selected=i
        self.monitor.set_active(selected);self.monitor.handler_unblock_by_func(self.monitor_changed)
        GLib.idle_add(self.place)

    def monitor_changed(self,*_):
        self.geometries.clear()
        self.place()
        i=self.monitor.get_active();d=Gdk.Display.get_default()
        if 0<=i<d.get_n_monitors():self.settings['monitor']=d.get_monitor(i).get_model();self.save()

    def place(self):
        d=Gdk.Display.get_default();i=self.monitor.get_active()
        if not 0<=i<d.get_n_monitors():return False
        g=d.get_monitor(i).get_workarea()
        w=min(390 if self.compact else 720,g.width-36)
        h=min(326 if self.compact else 1560,g.height-60)
        x=g.x+g.width-w-24 if self.compact else g.x+(g.width-w)//2
        y=g.y+24 if self.compact else g.y+max(20,(g.height-h)//3)
        saved=self.geometries.get('compact' if self.compact else 'studio')
        if isinstance(saved,list) and len(saved)==4 and all(isinstance(n,int) for n in saved) and (self.compact or saved[3]>=600):
            sx,sy,sw,sh=saved
            # Restore only to a connected screen, and keep the whole panel reachable.
            target=next((d.get_monitor(j).get_workarea() for j in range(d.get_n_monitors())
                         if d.get_monitor(j).get_workarea().x <= sx < d.get_monitor(j).get_workarea().x+d.get_monitor(j).get_workarea().width
                         and d.get_monitor(j).get_workarea().y <= sy < d.get_monitor(j).get_workarea().y+d.get_monitor(j).get_workarea().height),None)
            if target is not None:
                w=min(max(360 if self.compact else 620,sw),target.width-24)
                h=min(max(326 if self.compact else 600,sh),target.height-24)
                x=max(target.x,min(sx,target.x+target.width-w))
                y=max(target.y,min(sy,target.y+target.height-h))
        self.resize(w,h);self.move(x,y);return False

    def select(self,b,key):
        if b.get_active():
            self.selected=key
            self.name_entry.set_text(self.name(key))
            self.update_detail()

    def update_detail(self):
        self.update_control_state()
        fan=self.latest['fans'].get(self.selected)
        if not fan:
            self.channel_label.set_text('Anschluss derzeit nicht verfügbar')
            self.mode_label.set_text('Keine aktuellen Daten')
            self.curve.data=[];self.curve.queue_draw();self.curve_note.set_text('');return
        self.channel_label.set_text(f"{fan['chip']} · Kanal {fan['channel']} · Zuordnung editierbar")
        modes={0:'Volle Leistung',1:'Manuell',2:'Thermal Cruise',3:'Speed Cruise',4:'Smart Fan III',5:'Smart Fan IV'}
        mode=modes.get(fan['mode'],'Modus nicht auslesbar')
        output=f" · PWM {round(fan['pwm']/255*100)} %" if fan['pwm'] is not None else ''
        self.mode_label.set_text(mode+output)
        active=fan['mode'] in (4,5)
        self.curve.data=fan['curve'] if active else [];self.curve.queue_draw()
        if fan['curve'] and active:
            text='Ausgelesene Hardware-Kennlinie · Quelle: '+str(fan['source'] or 'unbekannt')
        else:text='Keine aktive Kennlinie über diese Schnittstelle auslesbar.'
        if self.selected=='nct6798:6':text+=' Pumpenzuordnung aus dem Gespräch übernommen.'
        managed=self.backend_state.get('channels',{}).get(self.selected,{})
        if managed.get('mode') in ('manual','curve'):
            self.mode_label.set_text('Restlos Air · '+('Manuell mit Temperaturschutz' if managed['mode']=='manual' else 'Eigene Kennlinie'))
            if managed.get('curve'):self.curve.data=managed['curve'];self.curve.queue_draw()
            text='Aktive Restlos-Kennlinie · Temperaturschutz ab 85 °C'
        self.curve_note.set_text(text)

    def update(self):
        now=time.monotonic()
        if not self.get_visible() and hasattr(self,'latest'):return True
        curves=not self.compact and now-self.curve_last>=5
        self.latest=snapshot(include_curves=curves,include_fans=curves)
        if curves:
            self.fan_cache=self.latest['fans'];self.curve_last=now
        else:
            self.latest['fans']=self.fan_cache
        if now-self.backend_last>=10 and not self.backend_busy:
            self.backend_last=now;self.refresh_backend()
        # UI changes must not manufacture additional time samples.
        if not self.history or now-self.history[-1][0]>=.8:self.history.append((now,self.latest['cpu'],self.latest['gpu']))
        for key in self.temp_labels:
            value=self.latest[key];text='—' if value is None else f'{value:.0f}°'
            if self.compact:self.mini_temps[key].set_text(text)
            else:self.temp_labels[key].set_text(text)
        if self.compact:
            if now-self.pump_last>=2:self.refresh_pump()
            self.show_pump(self.pump_rpm if now-self.pump_read_at<=10 else None)
        self.mini_live.set_text('LIVE' if self.latest['cpu'] is not None and self.latest['gpu'] is not None else 'DATEN FEHLEN')
        if now-self.metrics_last>=2:
            self.metrics_last=now
            self.metrics_state=self.metrics_sampler.sample()
            self.update_metrics()
        self.check_alarms()
        if self.compact:return True
        self.history_chart.data=[[(t-now,cpu) for t,cpu,gpu in self.history],[(t-now,gpu) for t,cpu,gpu in self.history]];self.history_chart.queue_draw()
        fans=self.latest['fans'];visible=[k for k,v in fans.items() if self.show_zero.get_active() or v['rpm'] not in (0,) or k in (self.selected,'nct6798:6','amdgpu:1')]
        if list(self.rows)!=visible:
            for child in self.fan_box.get_children():child.destroy()
            self.rows={};group=None
            for key in visible:
                b=Gtk.RadioButton.new_from_widget(group);group=b;b.set_mode(False);b.get_style_context().add_class('fan')
                row=box(False,10);b.add(row);col=box(spacing=2);add(row,col,True)
                title=add(col,label(self.name(key)));sub=add(col,label('','muted'));rpm=add(row,label('','rpm'))
                b.set_active(key==self.selected);b.connect('toggled',self.select,key);add(self.fan_box,b)
                self.rows[key]=(b,title,sub,rpm)
            self.fan_box.show_all()
        for key,(b,title,sub,rpm) in self.rows.items():
            value=fans[key];title.set_text(self.name(key));rpm.set_text('—' if value['rpm'] is None else f"{value['rpm']:,}".replace(',','.'))
            sub.set_text('U/min · '+('kein Messwert' if value['rpm'] is None else 'Stillstand / kein Drehzahlsignal' if value['rpm']==0 else 'Live-Messwert'))
        if not self.name_entry.has_focus() and self.name_entry.get_text()!=self.name(self.selected):self.name_entry.set_text(self.name(self.selected))
        self.status.set_text('● Live · '+time.strftime('%H:%M:%S') if self.latest['mainboard'] else 'Mainboard-Sensor fehlt · nct6775 ist möglicherweise nicht geladen')
        self.update_detail()
        return True

    def show_pump(self,rpm):
        self.mini_pump.set_text('Pumpe · '+('—' if rpm is None else f'{rpm:,.0f}'.replace(',','.'))+' U/min')

    def update_metrics(self):
        data=self.metrics_state
        percent=lambda key:'—' if data.get(key) is None else f'{data[key]:.0f}'
        load=f"CPU {percent('cpu_percent')} %   ·   GPU {percent('gpu_percent')} %"
        def memory(prefix):
            used,total=data.get(prefix+'_used_bytes'),data.get(prefix+'_total_bytes')
            return '—' if used is None or total is None else f'{used/2**30:.1f}/{total/2**30:.1f} GiB'
        ram, vram=memory('ram'),memory('vram')
        self.mini_load.set_text(load)
        self.mini_memory.set_text(f'RAM {ram}   ·   VRAM {vram}')
        self.load_detail.set_text(load+f'\nArbeitsspeicher: {ram} · {percent("ram_percent")} %\nGrafikspeicher: {vram} · {percent("vram_percent")} %')
        self.mini_memory.set_tooltip_text('Verwendeter / gesamter Speicher in GiB · Messung alle 2 Sekunden')

    def refresh_pump(self):
        if self.pump_busy or self.backend_busy or not self.controller or not self.backend_state.get('connected'):return
        self.pump_last=time.monotonic();self.pump_busy=True
        def work():
            board=self.controller.board
            if not board:return None
            return cached_pump(self.controller.client.request('/status'),board.get('uid'))
        future=self.executor.submit(work)
        def completed(f):
            try:value=f.result()
            except Exception:value=None
            GLib.idle_add(self.pump_done,value)
        future.add_done_callback(completed)

    def pump_done(self,value):
        if self.closed:return False
        self.pump_busy=False;self.pump_rpm=value
        self.pump_read_at=time.monotonic() if value is not None else 0
        if self.compact and self.get_visible():self.show_pump(value)
        return False

    def build_control_panel(self):
        self.control_reason=add(self.detail,label('Steuerung wird verbunden …','muted'));self.control_reason.set_line_wrap(True)
        self.control_area=box(spacing=10);add(self.detail,self.control_area)
        self.control_mode=Gtk.ComboBoxText()
        self.control_mode.append('manual','Manuell mit Temperaturschutz')
        self.control_mode.append('curve','Eigene Kennlinie')
        self.control_mode.set_active_id('manual')
        self.control_mode.connect('changed',lambda *_:self.editor_stack.set_visible_child_name(self.control_mode.get_active_id()))
        add(self.control_area,self.control_mode)
        self.editor_stack=Gtk.Stack();self.editor_stack.set_homogeneous(False);add(self.control_area,self.editor_stack)
        manual=box(spacing=4)
        add(manual,label('Sollleistung · 40 bis 100 %','muted'))
        self.duty=Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,40,100,1);self.duty.set_value(50);self.duty.set_digits(0)
        self.duty.get_accessible().set_name('Manuelle Lüfterleistung in Prozent');add(manual,self.duty)
        self.editor_stack.add_named(manual,'manual')
        curve=box(spacing=8)
        self.source_combo=Gtk.ComboBoxText()
        for key,title in [('max','CPU und GPU · höherer Kühlbedarf'),('cpu','CPU-Temperatur'),('gpu','GPU-Temperatur')]:self.source_combo.append(key,title)
        self.source_combo.set_active_id('max');add(curve,self.source_combo)
        self.points=[]
        for i,(temp,duty) in enumerate([(40,40),(65,65),(85,100)]):
            row=box(False,10);add(curve,row)
            t=Gtk.SpinButton.new_with_range(20,85,1);t.set_value(temp);t.set_numeric(True)
            d=Gtk.SpinButton.new_with_range(40,100,1);d.set_value(duty);d.set_numeric(True)
            t.get_accessible().set_name(f'Temperatur Kennlinienpunkt {i+1}')
            d.get_accessible().set_name(f'Leistung Kennlinienpunkt {i+1}')
            if i==2:d.set_sensitive(False)
            add(row,t,True);add(row,label('°C','muted'));add(row,d,True);add(row,label('%','muted'))
            self.points.append((t,d))
        self.editor_stack.add_named(curve,'curve');self.editor_stack.show_all();self.editor_stack.set_visible_child_name('manual')
        actions=box(False,8);add(self.control_area,actions)
        self.apply_button=Gtk.Button(label='Anwenden');self.apply_button.connect('clicked',self.apply_control);add(actions,self.apply_button,True)
        self.restore_button=Gtk.Button(label='Vorherige Regelung');self.restore_button.connect('clicked',self.restore_control);add(actions,self.restore_button,True)
        self.control_feedback=add(self.detail,label('','muted'));self.control_feedback.set_line_wrap(True)
        self.connect_button=Gtk.Button(label='Steuerung verbinden');self.connect_button.connect('clicked',self.connect_control);add(self.detail,self.connect_button)

    def update_control_state(self):
        state=self.backend_state.get('channels',{}).get(self.selected,{})
        connected=self.backend_state.get('connected',False)
        enabled=connected and state.get('controllable',False) and not self.backend_busy
        self.control_area.set_sensitive(not self.backend_busy)
        self.control_mode.set_sensitive(enabled)
        self.editor_stack.set_sensitive(enabled)
        self.apply_button.set_sensitive(enabled)
        self.restore_button.set_sensitive(connected and state.get('can_restore',False) and not self.backend_busy)
        if connected and self.backend_state.get('mainboard') and self.editor_channel!=self.selected:
            self.load_editor(state)
        self.control_reason.set_text(state.get('reason') or self.backend_state.get('reason') or 'Regelung über den Hintergrunddienst. 100 % spätestens bei 85 °C.')
        self.connect_button.set_sensitive(not self.backend_busy)
        self.connect_button.set_label('Verbindung neu laden' if self.backend_state.get('connected') else 'Steuerung verbinden')

    def load_editor(self,state):
        # Load on selection / confirmed action, never overwrite an edit on a timer.
        self.editor_channel=self.selected
        self.control_mode.set_active_id('curve' if state.get('mode')=='curve' else 'manual')
        duty=state.get('duty')
        if duty is None:duty=(state.get('actual_setting') or {}).get('speed_fixed',50)
        self.duty.set_value(50 if duty is None else duty)
        self.source_combo.set_active_id(state.get('source') or 'max')
        points=state.get('curve') if state.get('mode')=='curve' else None
        if not points or len(points)!=len(self.points):points=[(40,40),(65,65),(85,100)]
        for (temp,value),(t,d) in zip(points,self.points):t.set_value(temp);d.set_value(value)

    def refresh_backend(self):
        if self.backend_busy:return
        def work():
            if self.controller is None:
                from control import Controller
                token=secret_store()
                if not token:raise BackendError('CoolerControl-Anmeldung erforderlich.')
                self.controller=Controller(Client(token))
            return self.controller.refresh()
        self.backend_job(work)

    def backend_job(self,work,message=None):
        if self.backend_busy:return
        self.backend_busy=True
        if not self.compact:self.update_control_state()
        if message:self.control_feedback.set_text('Wird angewendet …')
        future=self.executor.submit(work)
        def completed(f):
            try:result=f.result();error=None
            except Exception as e:result=None;error=str(e)
            GLib.idle_add(self.backend_done,result,error,message)
        future.add_done_callback(completed)

    def backend_done(self,result,error,message):
        if self.closed:return False
        self.backend_busy=False
        if error:
            self.backend_state=(self.controller.status() if message and self.controller else
                                {'connected':False,'reason':error,'channels':{}})
            self.backend_last=0
            self.control_feedback.set_text(error)
        elif isinstance(result,dict):
            self.backend_state=result
            if message:
                self.control_feedback.set_text(message)
                self.editor_channel=None
        if not self.compact:
            if message:
                self.curve_last=0;self.backend_last=time.monotonic();self.update()
            else:self.update_detail()
        return False

    def apply_control(self,*_):
        key=self.selected
        if not self.controller or not self.backend_state.get('channels',{}).get(key,{}).get('controllable'):return
        try:
            if self.control_mode.get_active_id()=='manual':
                duty=int(self.duty.get_value())
                def work():return self.controller.apply_manual(key,duty)
            else:
                points=validate_curve([(t.get_value(),d.get_value()) for t,d in self.points]);source=self.source_combo.get_active_id()
                def work():return self.controller.apply_curve(key,points,source=source)
        except BackendError as e:self.control_feedback.set_text(str(e));return
        self.backend_job(work,self.name(key)+' · Regelung bestätigt.')

    def restore_control(self,*_):
        if not self.controller:return
        key=self.selected
        def work():return self.controller.restore(key)
        self.backend_job(work,self.name(key)+' · Vorherige Regelung wiederhergestellt.')

    def connect_control(self,*_):
        if self.backend_state.get('connected'):
            self.backend_last=0;self.refresh_backend();return
        from connect_backend import ConnectWindow
        if self.auth_window:
            self.auth_window.present();return
        def done():
            self.controller=None;self.backend_last=0;self.refresh_backend()
        self.auth_window=ConnectWindow(done)
        self.auth_window.set_transient_for(self)
        self.auth_window.connect('destroy',lambda *_:setattr(self,'auth_window',None))
        self.auth_window.show_all();self.auth_window.present()

    def update_network(self,data):
        if self.closed:return False
        self.network_state=data
        internet=data.get('internet_ms');router=data.get('gateway_ms')
        status=data.get('internet_status')
        text=f'Ping · {internet:.0f} ms' if internet is not None else 'Ping · keine Antwort'
        if status in ('no_route','disconnected'):text='Ping · offline'
        signal=data.get('signal_percent')
        connection=data.get('connection')
        if connection=='wlan':text+=' · WLAN '+('—' if signal is None else f'{signal:.0f}')+' %'
        elif connection=='ethernet':text+=' · LAN'
        elif connection=='vpn':text+=' · VPN'
        self.mini_ping.set_text(text)
        def rate(value):
            if value is None:return '—'
            if value>=1024**2:return f'{value/1024**2:.1f} MiB/s'
            if value>=1024:return f'{value/1024:.0f} KiB/s'
            return f'{value:.0f} B/s'
        transfer=f"↓ {rate(data.get('rx_bytes_sec'))}   ·   ↑ {rate(data.get('tx_bytes_sec'))}"
        self.mini_transfer.set_text(transfer)
        self.mini_transfer.set_tooltip_text('Empfang / Versand über die aktive Verbindung · Durchschnitt seit der letzten Messung')
        gateway=f'{router:.0f} ms' if router is not None else 'keine Antwort'
        loss=data.get('loss_percent')
        detail=f"{data.get('interface') or 'Keine Verbindung'} · Router {gateway} · {text}"
        if loss is not None:detail+=f' · ICMP-Verlust {loss:.0f} %'
        detail+='\n'+transfer
        self.network_detail.set_text(detail)
        self.mini_ping.set_tooltip_text(detail+' · Keine Antwort kann auch eine ICMP-Sperre bedeuten.')
        return False

    def check_alarms(self):
        hot=any(self.latest.get(k) is not None and self.latest[k]>=85 for k in ('cpu','gpu'))
        missing=self.latest.get('cpu') is None or self.latest.get('gpu') is None
        alarm='Temperatur hoch' if hot else 'Sensordaten fehlen' if missing else None
        if alarm:
            self.mini_live.set_text('TEMPERATUR' if hot else 'DATEN FEHLEN')
            self.mini_live.get_style_context().add_class('warning')
        else:self.mini_live.get_style_context().remove_class('warning')
        if alarm and alarm!=self.alarm_state:
            app=self.get_application()
            if app:
                notification=Gio.Notification.new('Restlos Air · '+alarm)
                notification.set_body('Klicke auf das Widget, um die Messwerte und Regelung zu prüfen.')
                app.send_notification('temperature',notification)
        self.alarm_state=alarm


class AirApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='de.restlos.AirStudio',flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.add_main_option('compact',0,GLib.OptionFlags.NONE,GLib.OptionArg.NONE,'Widget öffnen',None)
        self.add_main_option('settings',0,GLib.OptionFlags.NONE,GLib.OptionArg.NONE,'Einstellungen öffnen',None)
        self.window=None
        self.audio_window=None
        self.requested_compact=True
        self.indicator=None
        self.held=False
    def do_startup(self):
        Gtk.Application.do_startup(self)
        for signum in (signal.SIGTERM,signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT,signum,self.on_signal)

    def on_signal(self):
        self.quit_app()
        return False
    def do_command_line(self,command_line):
        self.requested_compact=not command_line.get_options_dict().contains('settings')
        self.activate();return 0
    def do_activate(self):
        if not self.window:
            css=Gtk.CssProvider();css.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(),css,Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.window=AirWindow(self)
            self.create_indicator()
            if not self.held:self.hold();self.held=True
        self.window.show_view(self.requested_compact)
        if self.window.settings.get('audio_visible',True):
            # X11 applies the restored Air size asynchronously; use its final
            # position before placing the companion directly underneath it.
            GLib.timeout_add(250,self.show_audio)

    def show_audio(self,*_):
        if not self.audio_window:
            from audio_widget import AudioWindow
            self.audio_window=AudioWindow(self,self.window)
        self.audio_window.show_widget()

    def create_indicator(self):
        try:
            gi.require_version('AyatanaAppIndicator3','0.1')
            from gi.repository import AyatanaAppIndicator3 as Indicator
            icon='restlos-air'
            if (BASE/'restlos-air.svg').exists():icon=str(BASE/'restlos-air.svg')
            self.indicator=Indicator.Indicator.new('restlos-air',icon,Indicator.IndicatorCategory.HARDWARE)
            self.indicator.set_title('Restlos Air')
            self.indicator.set_status(Indicator.IndicatorStatus.ACTIVE)
            menu=Gtk.Menu()
            for title,callback in [('Widget anzeigen',lambda *_:self.window.show_view(True)),
                                   ('Einstellungen öffnen',lambda *_:self.window.show_view(False)),
                                   ('Audio-Widget öffnen',self.show_audio),
                                   ('Widget ausblenden',lambda *_:self.window.hide()),
                                   ('Beenden',self.quit_app)]:
                item=Gtk.MenuItem(label=title);item.connect('activate',callback);menu.append(item)
            menu.show_all();self.indicator.set_menu(menu)
        except (ImportError,ValueError):
            # Portable fallback; the installed package supplies Ayatana for Zorin.
            self.indicator=Gtk.StatusIcon.new_from_icon_name('utilities-system-monitor')
            self.indicator.set_tooltip_text('Restlos Air')
            self.indicator.connect('activate',lambda *_:self.window.show_view(True))
            self.indicator.connect('popup-menu',self.fallback_menu)

    def fallback_menu(self,icon,button,timestamp):
        menu=Gtk.Menu()
        for title,callback in [('Einstellungen',lambda *_:self.window.show_view(False)),('Audio-Widget öffnen',self.show_audio),('Beenden',self.quit_app)]:
            item=Gtk.MenuItem(label=title);item.connect('activate',callback);menu.append(item)
        menu.show_all();menu.popup(None,None,None,None,button,timestamp)

    def quit_app(self,*_):
        if self.audio_window:
            self.audio_window.remember_geometry();self.audio_window.destroy()
        if self.window:
            self.window.remember_geometry();self.window.save();self.window.destroy()
        if self.held:self.release();self.held=False
        self.quit()


if __name__=='__main__':
    import sys
    raise SystemExit(AirApp().run(sys.argv))
