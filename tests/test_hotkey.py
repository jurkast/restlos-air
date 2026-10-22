"""Shortcut setup writes a GNOME custom keybinding (memory backend, nothing persists)."""
import os
from pathlib import Path
import sys
import unittest

os.environ['GSETTINGS_BACKEND'] = 'memory'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))

try:
    import gi
    gi.require_version('Gio', '2.0')
    import hotkey
    AVAILABLE = hotkey.available()
except (ImportError, ValueError):
    AVAILABLE = False


@unittest.skipUnless(AVAILABLE, 'GNOME-Schemas für Tastenkürzel fehlen')
class HotkeyTests(unittest.TestCase):
    def test_install_and_remove_keep_other_shortcuts(self):
        from gi.repository import Gio
        listing = Gio.Settings.new(hotkey.LIST_SCHEMA)
        other = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/'
        listing.set_strv('custom-keybindings', [other])
        hotkey.install()
        hotkey.install()
        self.assertEqual(listing.get_strv('custom-keybindings'), [other, hotkey.PATH])
        entry = Gio.Settings.new_with_path(hotkey.ENTRY_SCHEMA, hotkey.PATH)
        self.assertEqual(entry.get_string('binding'), '<Super><Shift>a')
        self.assertTrue(entry.get_string('command').endswith('restlos-air --toggle'))
        hotkey.remove()
        self.assertEqual(listing.get_strv('custom-keybindings'), [other])
        self.assertFalse(hotkey.installed())


if __name__ == '__main__':
    unittest.main(verbosity=2)
