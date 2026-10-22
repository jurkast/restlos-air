"""Small GTK audio controls sharing Restlos Air's process and settings."""
from concurrent.futures import ThreadPoolExecutor
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib, Pango
from audio import AudioMonitor


def text(value='', style='muted'):
    widget=Gtk.Label(label=value, xalign=0)
    widget.get_style_context().add_class(style)
    return widget


def row(vertical=False, spacing=8):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)


def pack(parent, child, expand=False):
    parent.pack_start(child, expand, expand, 0)
    return child


class AudioWindow(Gtk.ApplicationWindow):
    def __init__(self, app, owner):
        super().__init__(application=app, title='Restlos Audio')
        self.owner=owner
        self.state={}
        self.closed=False
        self.painting=False
        self.busy=False
        self.action_error=None
        self.volume_pending=None
        self.volume_timer=None
        self.device_lists=None
        self.executor=ThreadPoolExecutor(max_workers=1, thread_name_prefix='restlos-audio-action')
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_keep_above(owner.settings.get('on_top',True) is not False)
        self.set_app_paintable(True)
        visual=self.get_screen().get_rgba_visual()
        if visual:self.set_visual(visual)
        self.set_border_width(10)
        self.set_default_size(300,190)
        self.connect('delete-event',self.on_delete)
        self.connect('destroy',self.cleanup)
        self.connect('key-press-event',self.key)
        surface=row(True,10);surface.set_name('mini-surface');self.add(surface)
        for side in ('top','bottom','start','end'):getattr(surface,'set_margin_'+side)(0)
        body=row(True,10);pack(surface,body)
        for side in ('top','bottom','start','end'):getattr(body,'set_margin_'+side)(14)
        header=row();pack(body,header)
        drag=Gtk.EventBox();drag.add(text('RESTLOS AUDIO','eyebrow'))
        drag.connect('button-press-event',self.drag)
        drag.set_tooltip_text('Ziehen zum Verschieben')
        pack(header,drag,True)
        close=Gtk.Button(label='×');close.get_style_context().add_class('mini-close')
        close.set_tooltip_text('Audio-Widget ausblenden');close.connect('clicked',self.on_delete);pack(header,close)
        self.output_name=pack(body,text('Audio wird verbunden …'))
        self.output_name.set_ellipsize(Pango.EllipsizeMode.END)
        self.output_name.set_max_width_chars(34)
        volume_row=row();pack(body,volume_row)
        self.output_mute=Gtk.Button(label='Ton an')
        self.output_mute.set_tooltip_text('Ausgabe stummschalten oder wieder einschalten')
        self.output_mute.connect('clicked',lambda *_:self.action(self.monitor.toggle_output_mute));pack(volume_row,self.output_mute)
        self.volume=Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,0,100,1)
        self.volume.set_draw_value(False)
        self.volume.get_accessible().set_name('Ausgabelautstärke in Prozent')
        self.volume.connect('value-changed',self.volume_changed);pack(volume_row,self.volume,True)
        self.volume_value=pack(volume_row,text('— %'))
        self.volume_value.set_width_chars(5)
        controls=row();pack(body,controls)
        self.mic_mute=Gtk.Button(label='Mikrofon —')
        self.mic_mute.set_tooltip_text('Mikrofon stummschalten oder wieder einschalten')
        self.mic_mute.connect('clicked',lambda *_:self.action(self.monitor.toggle_mic_mute));pack(controls,self.mic_mute,True)
        self.devices_button=Gtk.ToggleButton(label='Geräte')
        self.devices_button.set_tooltip_text('Audioausgabe, Mikrofon und Bildschirm auswählen')
        self.devices_button.connect('toggled',self.toggle_devices);pack(controls,self.devices_button)
        self.device_panel=row(True,8);pack(body,self.device_panel)
        self.device_panel.set_no_show_all(True)
        pack(self.device_panel,text('Ausgabe'))
        self.sinks=Gtk.ComboBoxText();self.sinks.connect('changed',self.sink_changed);pack(self.device_panel,self.sinks)
        pack(self.device_panel,text('Mikrofon'))
        self.sources=Gtk.ComboBoxText();self.sources.connect('changed',self.source_changed);pack(self.device_panel,self.sources)
        pack(self.device_panel,text('Bildschirm'))
        self.monitors=Gtk.ComboBoxText();self.monitors.connect('changed',self.monitor_changed);pack(self.device_panel,self.monitors)
        for combo in (self.sinks,self.sources,self.monitors):
            for renderer in combo.get_cells():
                renderer.set_property('ellipsize',Pango.EllipsizeMode.END)
                renderer.set_property('max-width-chars',26)
        hint=pack(self.device_panel,text('Gerätewechsel übernimmt auch laufende Audioverbindungen.'))
        hint.set_line_wrap(True)
        hint.set_max_width_chars(34)
        self.feedback=pack(body,text(''))
        self.feedback.set_line_wrap(True)
        self.feedback.set_max_width_chars(34)
        self.feedback.set_no_show_all(True)
        display=Gdk.Display.get_default()
        self.display_handlers=[display.connect('monitor-added',lambda *_:self.refresh_monitors()),
                               display.connect('monitor-removed',lambda *_:self.refresh_monitors())]
        self.refresh_monitors()
        self.monitor=AudioMonitor(lambda data:GLib.idle_add(self.update_state,data))
        self.update_state({})
        self.ready=True
        self.monitor.start()

    def show_widget(self):
        if self.closed:return
        self.owner.settings['audio_visible']=True
        self.owner.save()
        self.show_all()
        if self.devices_button.get_active():self.show_device_panel()
        if self.feedback.get_text():self.feedback.show()
        self.place()
        GLib.idle_add(self.place)
        self.present()

    def drag(self, widget, event):
        if event.button==1:self.begin_move_drag(1,int(event.x_root),int(event.y_root),event.time)

    def key(self, widget, event):
        if event.keyval==Gdk.KEY_Escape:
            if self.devices_button.get_active():self.devices_button.set_active(False)
            else:self.on_delete()
            return True
        return False

    def remember_geometry(self):
        if self.get_mapped():
            x,y=self.get_position();w,_=self.get_size()
            self.owner.settings['audio_geometry']=[x,y,w]
            self.owner.save()

    def on_delete(self,*_):
        self.remember_geometry()
        self.owner.settings['audio_visible']=False
        self.owner.save()
        self.hide()
        return True

    def toggle_devices(self,*_):
        self.remember_geometry()
        if self.devices_button.get_active():self.show_device_panel()
        else:self.device_panel.hide()
        self.place();GLib.idle_add(self.place)

    def show_device_panel(self):
        self.device_panel.set_no_show_all(False)
        self.device_panel.show_all()
        self.device_panel.set_no_show_all(True)

    def refresh_monitors(self):
        display=Gdk.Display.get_default();self.painting=True
        self.monitors.remove_all()
        selected=max(0,self.owner.monitor.get_active())
        desired=self.owner.settings.get('audio_monitor',self.owner.settings.get('monitor'))
        for i in range(display.get_n_monitors()):
            monitor=display.get_monitor(i);g=monitor.get_geometry()
            model=monitor.get_model() or f'Monitor {i+1}'
            self.monitors.append(str(i),f'{model} · {g.width} × {g.height}')
            if model==desired:selected=i
        self.monitors.set_active(min(selected,display.get_n_monitors()-1));self.painting=False
        if self.get_visible():GLib.idle_add(self.place)

    def monitor_changed(self,*_):
        if self.painting:return
        i=self.monitors.get_active();display=Gdk.Display.get_default()
        if not 0<=i<display.get_n_monitors():return
        self.owner.settings['audio_monitor']=display.get_monitor(i).get_model()
        self.owner.settings.pop('audio_geometry',None)
        self.owner.save();self.place()

    def place(self):
        if self.closed:return False
        display=Gdk.Display.get_default();i=self.monitors.get_active()
        if not 0<=i<display.get_n_monitors():return False
        area=display.get_monitor(i).get_workarea()
        w=min(300,area.width-24)
        h=min(470 if self.devices_button.get_active() else 210,area.height-24)
        ax,ay=self.owner.get_position();_,ah=self.owner.get_size()
        x=area.x+area.width-w-24;y=area.y+300
        if area.x<=ax<area.x+area.width:
            x=ax;y=ay+ah+12
            if y+h>area.y+area.height:y=ay-h-12
        saved=self.owner.settings.get('audio_geometry')
        if isinstance(saved,list) and len(saved)==3 and all(type(v) is int for v in saved):
            sx,sy,sw=saved
            if area.x<=sx<area.x+area.width and area.y<=sy<area.y+area.height:
                x,y=sx,sy  # Breite bleibt fest wie beim Air-Widget
        x=max(area.x,min(x,area.x+area.width-w))
        y=max(area.y,min(y,area.y+area.height-h))
        self.resize(w,h);self.move(x,y)
        return False

    def update_state(self,data):
        if self.closed:return False
        self.state=data
        self.painting=True
        default=data.get('default_sink');mic=data.get('default_source')
        sinks=data.get('sinks',[]);sources=data.get('sources',[])
        chosen=next((item for item in sinks if item['id']==default),None)
        self.output_name.set_text(chosen['description'] if chosen else 'Keine Audioausgabe verfügbar')
        self.output_name.set_tooltip_text(chosen['description'] if chosen else 'PulseAudio oder PipeWire-Pulse mit pactl wird benötigt')
        value=data.get('volume_percent')
        self.volume_value.set_text('— %' if value is None else f'{value:.0f} %')
        if self.volume_pending is None and not self.busy:self.volume.set_value(0 if value is None else min(100,value))
        output_muted=data.get('output_muted')
        self.output_mute.set_label('Ton aus' if output_muted else 'Ton an')
        self.mic_mute.set_label('Mikrofon stumm' if data.get('mic_muted') else 'Mikrofon an' if mic is not None else 'Kein Mikrofon')
        self.output_mute.get_style_context().remove_class('warning')
        self.mic_mute.get_style_context().remove_class('warning')
        if output_muted:self.output_mute.get_style_context().add_class('warning')
        if data.get('mic_muted'):self.mic_mute.get_style_context().add_class('warning')
        signature=([tuple(item[k] for k in ('id','description')) for item in sinks],
                   [tuple(item[k] for k in ('id','description')) for item in sources])
        if signature!=self.device_lists:
            for combo,items in ((self.sinks,sinks),(self.sources,sources)):
                combo.remove_all()
                for item in items:combo.append(str(item['id']),item['description'])
            self.device_lists=signature
        self.sinks.set_active_id(str(default) if default is not None else None)
        self.sources.set_active_id(str(mic) if mic is not None else None)
        self.painting=False
        self.set_sensitivity()
        error=data.get('error') or self.action_error
        if error:self.feedback.set_text(error);self.feedback.show()
        elif not self.busy:self.feedback.set_text('');self.feedback.hide()
        if getattr(self,'ready',False) and hasattr(self.owner,'push_bar'):self.owner.push_bar()
        return False

    def set_sensitivity(self):
        available=self.state.get('available',False)
        output=available and self.state.get('default_sink') is not None
        mic=available and self.state.get('default_source') is not None
        self.volume.set_sensitive(output)
        self.output_mute.set_sensitive(output and not self.busy)
        self.mic_mute.set_sensitive(mic and not self.busy)
        self.sinks.set_sensitive(available and bool(self.state.get('sinks')) and not self.busy)
        self.sources.set_sensitive(available and bool(self.state.get('sources')) and not self.busy)

    def sink_changed(self,*_):
        value=self.sinks.get_active_id()
        if not self.painting and value is not None:self.action(lambda:self.monitor.set_default_sink(int(value)))

    def source_changed(self,*_):
        value=self.sources.get_active_id()
        if not self.painting and value is not None:self.action(lambda:self.monitor.set_default_source(int(value)))

    def volume_changed(self,*_):
        if self.painting or self.closed:return
        self.volume_pending=round(self.volume.get_value())
        if self.volume_timer is not None:GLib.source_remove(self.volume_timer)
        self.volume_timer=GLib.timeout_add(180,self.flush_volume)

    def flush_volume(self):
        self.volume_timer=None
        if self.closed:return False
        if self.busy:
            self.volume_timer=GLib.timeout_add(180,self.flush_volume);return False
        value=self.volume_pending;self.volume_pending=None
        if value is not None:self.action(lambda:self.monitor.set_volume(value))
        return False

    def action(self,work):
        if self.closed or self.busy or not self.state.get('available'):return
        self.action_error=None
        self.busy=True;self.set_sensitivity()
        future=self.executor.submit(work)
        def completed(result):
            try:data=result.result();error=None
            except Exception as exc:data=None;error=str(exc)
            GLib.idle_add(self.action_done,data,error)
        future.add_done_callback(completed)

    def action_done(self,data,error):
        if self.closed:return False
        self.busy=False
        self.action_error=error
        self.update_state(data if isinstance(data,dict) else self.monitor.snapshot())
        if error:self.feedback.set_text(error);self.feedback.show()
        self.set_sensitivity()
        return False

    def cleanup(self,*_):
        self.closed=True
        if self.volume_timer is not None:GLib.source_remove(self.volume_timer);self.volume_timer=None
        for handler in self.display_handlers:Gdk.Display.get_default().disconnect(handler)
        self.monitor.stop()
        self.executor.shutdown(wait=False,cancel_futures=True)
