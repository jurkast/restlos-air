"""Global shortcut via GNOME/Zorin custom keybindings (no key grabbing by Restlos itself).

GNOME runs ``restlos-air --toggle``; the running instance receives it over D-Bus and
shows or hides the widget or bar.
"""
from __future__ import annotations

import shutil

from gi.repository import Gio

LIST_SCHEMA = 'org.gnome.settings-daemon.plugins.media-keys'
ENTRY_SCHEMA = 'org.gnome.settings-daemon.plugins.media-keys.custom-keybinding'
PATH = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/restlos-air/'
BINDING = '<Super><Shift>a'
LABEL = 'Super+Umschalt+A'


def _schema(name: str):
    source = Gio.SettingsSchemaSource.get_default()
    return source.lookup(name, True) if source is not None else None


def available() -> bool:
    return _schema(LIST_SCHEMA) is not None and _schema(ENTRY_SCHEMA) is not None


def _command() -> str:
    return (shutil.which('restlos-air') or '/usr/bin/restlos-air') + ' --toggle'


def installed() -> bool:
    if not available():
        return False
    return PATH in Gio.Settings.new(LIST_SCHEMA).get_strv('custom-keybindings')


def install() -> None:
    if not available():
        raise RuntimeError('GNOME-Tastenkürzel sind auf diesem System nicht verfügbar.')
    entry = Gio.Settings.new_with_path(ENTRY_SCHEMA, PATH)
    entry.set_string('name', 'Restlos Air ein-/ausblenden')
    entry.set_string('command', _command())
    entry.set_string('binding', BINDING)
    settings = Gio.Settings.new(LIST_SCHEMA)
    paths = settings.get_strv('custom-keybindings')
    if PATH not in paths:
        settings.set_strv('custom-keybindings', [*paths, PATH])
    Gio.Settings.sync()


def remove() -> None:
    if not available():
        return
    settings = Gio.Settings.new(LIST_SCHEMA)
    settings.set_strv('custom-keybindings', [path for path in settings.get_strv('custom-keybindings') if path != PATH])
    entry = Gio.Settings.new_with_path(ENTRY_SCHEMA, PATH)
    for key in ('name', 'command', 'binding'):
        entry.reset(key)
    Gio.Settings.sync()
