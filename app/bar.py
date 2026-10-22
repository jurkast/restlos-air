"""Restlos Air as a panel docked to a screen edge (top, bottom, left or right).

X11 dock window: stays above other windows on all workspaces. Optionally reserves its
space via _NET_WM_STRUT_PARTIAL so maximised windows end at the bar. Values come from
AirWindow; the bar itself never reads sensors.
"""
from __future__ import annotations

import ctypes
import ctypes.util

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, Gtk, Pango

EDGES = ('bottom', 'top', 'left', 'right')
EDGE_TITLES = {'bottom': 'Unten', 'top': 'Oben', 'left': 'Links', 'right': 'Rechts'}
THICKNESS = 34
SIDE_WIDTH = 124
METRICS = (('cpu', 'CPU'), ('gpu', 'GPU'), ('hotspot', 'HOT'), ('ssd', 'SSD'))


def set_strut(xid: int, values: list[int]) -> bool:
    """Write _NET_WM_STRUT(_PARTIAL) with libX11; False when not on X11 or libX11 is missing."""
    name = ctypes.util.find_library('X11')
    if not name or len(values) != 12:
        return False
    try:
        x11 = ctypes.CDLL(name)
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x11.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        display = x11.XOpenDisplay(None)
        if not display:
            return False
        try:
            cardinal = x11.XInternAtom(display, b'CARDINAL', 0)
            # Format 32 properties are passed as C longs.
            partial = (ctypes.c_long * 12)(*values)
            simple = (ctypes.c_long * 4)(*values[:4])
            x11.XChangeProperty(display, xid, x11.XInternAtom(display, b'_NET_WM_STRUT_PARTIAL', 0),
                                cardinal, 32, 0, ctypes.cast(partial, ctypes.c_void_p), 12)
            x11.XChangeProperty(display, xid, x11.XInternAtom(display, b'_NET_WM_STRUT', 0),
                                cardinal, 32, 0, ctypes.cast(simple, ctypes.c_void_p), 4)
            x11.XFlush(display)
        finally:
            x11.XCloseDisplay(display)
        return True
    except (OSError, AttributeError, ctypes.ArgumentError):
        return False


def strut_values(edge: str, x: int, y: int, width: int, height: int, screen_width: int, screen_height: int) -> list[int]:
    """Strut relative to the whole X screen, limited to the bar's own span."""
    values = [0] * 12
    if edge == 'top':
        values[2], values[8], values[9] = y + height, x, x + width - 1
    elif edge == 'bottom':
        values[3], values[10], values[11] = screen_height - y, x, x + width - 1
    elif edge == 'left':
        values[0], values[4], values[5] = x + width, y, y + height - 1
    else:
        values[1], values[6], values[7] = screen_width - x, y, y + height - 1
    return values


def _label(text: str, style: str, xalign: float = 0) -> Gtk.Label:
    item = Gtk.Label(label=text, xalign=xalign)
    item.get_style_context().add_class(style)
    return item


class BarWindow(Gtk.Window):
    def __init__(self, owner, edge: str = 'bottom', reserve: bool = True):
        super().__init__(title='Restlos Air · Leiste')
        self.owner = owner
        self.edge = edge if edge in EDGES else 'bottom'
        self.reserve = reserve
        # Breite der Seitenleiste wächst höchstens, schrumpft nie: sonst rückt die rechte
        # Leiste bei jeder Wertänderung ein paar Pixel hin und her.
        self.locked_width = 0
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_keep_above(True)
        self.stick()
        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.connect('map-event', lambda *_: self.apply_strut())
        self.build()

    @property
    def vertical(self) -> bool:
        return self.edge in ('left', 'right')

    def build(self) -> None:
        for child in self.get_children():
            child.destroy()
        events = Gtk.EventBox()
        events.set_name('bar-surface')
        events.get_style_context().add_class('bar-' + self.edge)
        events.set_tooltip_text('Klicken öffnet Air Studio')
        events.connect('button-press-event', self.clicked)
        self.add(events)
        surface = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if self.vertical else Gtk.Orientation.HORIZONTAL,
                          spacing=10 if self.vertical else 16)
        events.add(surface)
        for setter in ('set_margin_top', 'set_margin_bottom', 'set_margin_start', 'set_margin_end'):
            getattr(surface, setter)(10 if self.vertical else 0)
        surface.set_margin_start(12 if not self.vertical else 10)
        surface.set_margin_end(8)
        head = _label('RESTLOS AIR', 'eyebrow')
        head.set_valign(Gtk.Align.CENTER)
        surface.pack_start(head, False, False, 0)
        self.temps, self.loads = {}, {}
        for key, title in METRICS:
            cell = Gtk.Box(spacing=6)
            cell.pack_start(_label(title, 'row-name'), False, False, 0)
            value = _label('—', 'bar-value')
            cell.pack_start(value, False, False, 0)
            self.temps[key] = value
            if key in ('cpu', 'gpu'):
                load = _label('', 'info')
                cell.pack_start(load, False, False, 0)
                self.loads[key] = load
            cell.set_valign(Gtk.Align.CENTER)
            surface.pack_start(cell, False, False, 0)
        self.memory = {}
        for key, title in (('ram', 'RAM'), ('vram', 'VRAM')):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            line = Gtk.Box(spacing=6)
            line.pack_start(_label(title, 'row-name'), False, False, 0)
            value = _label('—', 'row-value')
            line.pack_start(value, False, False, 0)
            cell.pack_start(line, False, False, 0)
            bar = self.owner_meter()
            bar.set_size_request(-1 if self.vertical else 70, -1)
            cell.pack_start(bar, False, False, 0)
            cell.set_valign(Gtk.Align.CENTER)
            surface.pack_start(cell, False, False, 0)
            self.memory[key] = (value, bar)
        self.pump = _label('Pumpe —', 'info')
        self.ping = _label('Ping —', 'info')
        self.transfer = _label('↓ —  ↑ —', 'info')
        for item in (self.pump, self.ping, self.transfer):
            item.set_valign(Gtk.Align.CENTER)
            if self.vertical:
                # Schmale Seitenleiste: umbrechen statt abschneiden.
                item.set_line_wrap(True)
                item.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                item.set_max_width_chars(14)
            else:
                item.set_ellipsize(Pango.EllipsizeMode.END)
            surface.pack_start(item, False, False, 0)
        if not self.vertical:
            # Mindestbreite, damit Werte nicht zu „…“ zusammenfallen.
            for item, chars in ((self.pump, 18), (self.ping, 14), (self.transfer, 17)):
                item.set_width_chars(chars)
        tail = Gtk.Box(spacing=6)
        tail.set_valign(Gtk.Align.CENTER if not self.vertical else Gtk.Align.END)
        self.pill = _label('LIVE', 'pill')
        self.pill.get_style_context().add_class('state-ok')
        tail.pack_start(self.pill, False, False, 0)
        self.update_button = Gtk.Button(label='UPDATE')
        self.update_button.get_style_context().add_class('update-pill')
        self.update_button.set_no_show_all(True)
        self.update_button.connect('clicked', lambda *_: self.owner.get_application().restart_for_update())
        tail.pack_start(self.update_button, False, False, 0)
        gear = Gtk.Button(label='⚙')
        gear.get_style_context().add_class('mini-close')
        gear.set_tooltip_text('Einstellungen öffnen')
        gear.get_accessible().set_name('Einstellungen öffnen')
        gear.connect('clicked', lambda *_: self.owner.show_view(False))
        tail.pack_start(gear, False, False, 0)
        surface.pack_end(tail, False, False, 0)
        events.show_all()

    def owner_meter(self) -> Gtk.LevelBar:
        from air_studio import meter
        return meter()

    def clicked(self, _widget, event) -> bool:
        if event.button == 1:
            self.owner.show_view(False)
        return True

    def configure(self, edge: str, reserve: bool) -> None:
        changed = edge != self.edge
        self.edge = edge if edge in EDGES else 'bottom'
        if changed:
            self.locked_width = 0
        self.reserve = reserve
        if changed:
            self.build()
        self.place()
        self.apply_strut()

    def area(self):
        display = Gdk.Display.get_default()
        index = self.owner.monitor.get_active()
        if not 0 <= index < display.get_n_monitors():
            index = 0
        # Full monitor geometry, not the work area: on X11 GDK derives secondary work areas from
        # _NET_WORKAREA (often only part of the monitor), and the bar's own strut shrinks the work
        # area, which would push the bar further inwards on every re-placement.
        return display.get_monitor(index).get_geometry()

    def on_primary(self) -> bool:
        display = Gdk.Display.get_default()
        index = self.owner.monitor.get_active()
        return 0 <= index < display.get_n_monitors() and display.get_monitor(index).is_primary()

    def geometry(self) -> tuple[int, int, int, int]:
        area = self.area()
        if self.vertical:
            # Real fonts can be wider than the design width; measure, never overflow the screen edge.
            child = self.get_child()
            natural = child.get_preferred_width()[1] if child is not None else 0
            width, height = min(max(SIDE_WIDTH, natural, self.locked_width), area.width // 3), area.height
            x = area.x if self.edge == 'left' else area.x + area.width - width
            return x, area.y, width, height
        height = THICKNESS
        y = area.y if self.edge == 'top' else area.y + area.height - height
        return area.x, y, area.width, height

    def place(self) -> None:
        x, y, width, height = self.geometry()
        if self.vertical:
            self.locked_width = width
        self.set_size_request(width, height)
        self.resize(width, height)
        self.move(x, y)

    def screen_bounds(self) -> tuple[int, int, int, int]:
        display = Gdk.Display.get_default()
        frames = [display.get_monitor(i).get_geometry() for i in range(display.get_n_monitors())]
        return (min(f.x for f in frames), min(f.y for f in frames),
                max(f.x + f.width for f in frames), max(f.y + f.height for f in frames))

    def reserve_possible(self) -> bool:
        """Struts are measured from the outer X-screen edge. At an edge where another monitor
        continues, a strut would reserve that whole monitor too – so only outer edges qualify."""
        left, top, right, bottom = self.screen_bounds()
        x, y, width, height = self.geometry()
        return {'left': x <= left, 'right': x + width >= right,
                'top': y <= top, 'bottom': y + height >= bottom}[self.edge]

    def apply_strut(self) -> bool:
        window = self.get_window()
        if window is None or not hasattr(window, 'get_xid'):
            return False
        _, _, screen_width, screen_height = self.screen_bounds()
        x, y, width, height = self.geometry()
        values = strut_values(self.edge, x, y, width, height, screen_width, screen_height)
        active = self.reserve and self.reserve_possible()
        return set_strut(window.get_xid(), values if active else [0] * 12)

    def show_bar(self) -> None:
        self.place()
        self.show()
        self.present()
        self.apply_strut()

    def show_values(self, source) -> None:
        """Copy the current readings from AirWindow (no extra sensor reads)."""
        from air_studio import de, level, rate, set_level
        latest = getattr(source, 'latest', {}) or {}
        for key, label in self.temps.items():
            value = latest.get(key)
            label.set_text('—' if value is None else f'{value:.0f}°')
            set_level(label, level(key, value, source.limits))
        metrics = source.metrics_state or {}
        for key, label in self.loads.items():
            value = metrics.get(key + '_percent')
            label.set_text('' if value is None else f'{value:.0f} %')
        for key, (label, bar) in self.memory.items():
            used, total = metrics.get(key + '_used_bytes'), metrics.get(key + '_total_bytes')
            label.set_text('—' if used is None or total is None else f'{de(used / 2**30)}/{de(total / 2**30, 0)} GiB')
            percent = metrics.get(key + '_percent')
            bar.set_value(0 if percent is None else max(0, min(100, percent)) / 100)
        separator = '\n' if self.vertical else ' · '
        self.pump.set_text(source.mini_pump.get_text().replace(' · ', separator))
        self.ping.set_text(source.mini_ping.get_text() + ('' if not source.mini_signal.get_text() else separator + source.mini_signal.get_text()))
        network = source.network_state or {}
        self.transfer.set_text(f"↓ {rate(network.get('rx_bytes_sec'))}{separator if self.vertical else '  '}↑ {rate(network.get('tx_bytes_sec'))}")
        self.pill.set_text(source.mini_live.get_text())
        context = self.pill.get_style_context()
        for name in ('state-ok', 'state-warn', 'state-hot', 'state-missing'):
            if source.mini_live.get_style_context().has_class(name):
                context.add_class(name)
            else:
                context.remove_class(name)
        if self.vertical:
            child = self.get_child()
            if child is not None and child.get_preferred_width()[1] > self.get_allocated_width():
                self.place()
                self.apply_strut()
        self.update_button.set_visible(source.mini_update.get_visible())
        self.update_button.set_tooltip_text(source.mini_update.get_tooltip_text() or '')
