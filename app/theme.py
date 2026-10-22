"""Light variant of the Restlos look (same colours as the Uninstaller's light mode)."""
from __future__ import annotations

import re

from gi.repository import Gio

LIGHT = {
    'rgba(21,35,46,0.93)': 'rgba(241,244,245,0.95)', 'rgba(21,35,46,0.96)': 'rgba(241,244,245,0.97)',
    'rgba(36,70,82,0.45)': 'rgba(22,124,119,0.10)',
    '#20323e': '#ffffff', '#3a505c': '#dce5e7', '#e3edef': '#203441', '#a6bbc5': '#667d89',
    '#6ce0c3': '#167c77', '#244652': '#dff3ed', '#9bf1dc': '#126b63', '#172a35': '#f8fafb',
    '#2b404b': '#dce5e7', '#3a2f1f': '#fff6e3', '#f2ba79': '#9a6400', '#4a2420': '#fde4e0',
    '#ff9284': '#b52d25', '#c0d2d9': '#40566a', '#6f8590': '#9aabb3', '#1b2b36': '#f4f7f8',
    '#58717a': '#a9bcc3',
}
_PATTERN = re.compile('|'.join(re.escape(color) for color in sorted(LIGHT, key=len, reverse=True)))


def light_css(css: bytes) -> bytes:
    # One pass, so a replaced colour is never replaced a second time.
    return _PATTERN.sub(lambda match: LIGHT[match.group(0)], css.decode('utf-8')).encode('utf-8')


def system_prefers_dark() -> bool:
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup('org.gnome.desktop.interface', True) is None:
        return True
    settings = Gio.Settings.new('org.gnome.desktop.interface')
    schema = settings.get_property('settings-schema')
    if schema.has_key('color-scheme') and settings.get_string('color-scheme') == 'prefer-dark':
        return True
    return 'dark' in settings.get_string('gtk-theme').lower()


def is_light(choice: str | None) -> bool:
    if choice == 'light':
        return True
    if choice == 'auto':
        try:
            return not system_prefers_dark()
        except Exception:
            return False
    return False
