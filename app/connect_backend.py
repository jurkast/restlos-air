#!/usr/bin/python3
"""Credential entry remains in the local native dialog, never in chat or logs."""
import threading
import gi
gi.require_version('Gtk','3.0')
from gi.repository import Gtk,GLib
from coolercontrol import Client


class ConnectWindow(Gtk.Window):
    def __init__(self, done=None):
        super().__init__(title='Restlos Air · Steuerung verbinden')
        self.done=done
        self.set_default_size(470,230);self.set_border_width(22)
        content=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=12);self.add(content)
        title=Gtk.Label(label='CoolerControl mit Restlos Air verbinden',xalign=0)
        content.pack_start(title,False,False,0)
        text=Gtk.Label(label='Gib das vorhandene CoolerControl-Passwort ein.\nEs bleibt auf diesem Rechner. Restlos Air speichert\neinen eigenen Zugang im Linux-Schlüsselbund.',xalign=0)
        content.pack_start(text,False,False,0)
        self.entry=Gtk.Entry();self.entry.set_visibility(False);self.entry.set_placeholder_text('CoolerControl-Passwort (nicht das Linux-Passwort)')
        self.entry.connect('activate',self.connect_backend);content.pack_start(self.entry,False,False,0)
        self.button=Gtk.Button(label='Verbinden');self.button.connect('clicked',self.connect_backend);content.pack_start(self.button,False,False,0)
        self.status=Gtk.Label(xalign=0);self.status.set_line_wrap(True);content.pack_start(self.status,False,False,0)
        link=Gtk.LinkButton(uri='http://127.0.0.1:11987',label='CoolerControl öffnen');content.pack_start(link,False,False,0)
    def connect_backend(self,*_):
        password=self.entry.get_text()
        if not password:return
        self.entry.set_text('');self.button.set_sensitive(False);self.status.set_text('Verbindung wird hergestellt …')
        def work():
            try:Client().login(password);GLib.idle_add(self.finish,None)
            except Exception as e:GLib.idle_add(self.finish,str(e))
        threading.Thread(target=work,daemon=True).start()
    def finish(self,error):
        self.button.set_sensitive(True)
        if error:self.status.set_text(error)
        else:
            self.status.set_text('Verbunden. Du kannst dieses Fenster schließen.')
            self.entry.set_sensitive(False);self.button.set_sensitive(False)
            if self.done:self.done()
        return False


if __name__=='__main__':
    w=ConnectWindow();w.connect('destroy',Gtk.main_quit);w.show_all();Gtk.main()
