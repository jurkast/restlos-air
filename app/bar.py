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
# Kurze Überschrift + fetter Wert statt Sätzen wie „Pumpe · 2.323 U/min“; feste Breite gegen Springen.
CAPTIONS = {'top': ('TOP', 18), 'pump': ('PUMPE', 11), 'ping': ('PING', 12), 'transfer': ('NETZ', 19),
            'disk': ('DISK', 19), 'power': ('GPU', 5), 'clock': ('TAKT', 8)}
# Dichte-Stufen, wenn nicht alles in die Monitorbreite passt: Abstand, Balkenbreite, Feldbreiten.
DENSITY = (
    {'spacing': 12, 'meter': 70, 'chars': {}, 'short': False},
    {'spacing': 8, 'meter': 46, 'chars': {'top': 14, 'transfer': 12, 'disk': 12, 'ping': 11}, 'short': True},
    {'spacing': 6, 'meter': 0, 'chars': {'top': 11, 'transfer': 11, 'disk': 11, 'ping': 6, 'pump': 5}, 'short': True},
)


def short_rate(value) -> str:
    """Bytes per second without the unit text: 1,0M, 4K, 512."""
    from air_studio import de
    if value is None:
        return '—'
    if value >= 1024**2:
        return de(value / 1024**2) + 'M'
    if value >= 1024:
        return f'{value / 1024:.0f}K'
    return f'{value:.0f}'
# Zusammengehörige Werte bilden Gruppen; zwischen Gruppen steht eine dünne Trennlinie.
GROUPS = {'cpu': 'temp', 'gpu': 'temp', 'hotspot': 'temp', 'ssd': 'temp', 'ram': 'memory', 'vram': 'memory',
          'top': 'system', 'profile': 'cooling', 'pump': 'cooling', 'power': 'system', 'clock': 'system',
          'disk': 'system', 'ping': 'net', 'transfer': 'net', 'audio': 'audio'}


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
        self.density = 0
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

    def rebuild(self) -> None:
        """Rebuild after the item selection changed; keeps the edge."""
        self.locked_width = 0
        self.density = 0
        self.build()
        if self.get_visible():
            self.place()
            self.apply_strut()

    def _separator(self) -> Gtk.Separator:
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL if self.vertical else Gtk.Orientation.VERTICAL)
        separator.get_style_context().add_class('bar-separator')
        if not self.vertical:
            separator.set_margin_top(8)
            separator.set_margin_bottom(8)
        return separator

    def _add_items(self, surface: Gtk.Box, keys: list[str], density: dict) -> None:
        """One row: items with a thin line wherever the group changes."""
        titles = dict(METRICS)
        group = None
        for key in keys:
            if group is not None and GROUPS.get(key) != group:
                surface.pack_start(self._separator(), False, False, 0)
            group = GROUPS.get(key)
            if key in titles:
                cell = Gtk.Box(spacing=6)
                cell.pack_start(_label(titles[key], 'row-name'), False, False, 0)
                value = _label('—', 'bar-value')
                value.set_width_chars(3)  # Breite hängt nicht vom Wert ab: Leiste springt nicht
                cell.pack_start(value, False, False, 0)
                self.temps[key] = value
                if key in ('cpu', 'gpu'):
                    load = _label('', 'info')
                    load.set_width_chars(4)
                    cell.pack_start(load, False, False, 0)
                    self.loads[key] = load
            elif key in ('ram', 'vram'):
                cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                line = Gtk.Box(spacing=6)
                line.pack_start(_label(key.upper(), 'row-name'), False, False, 0)
                value = _label('—', 'row-value')
                value.set_width_chars(10)
                line.pack_start(value, False, False, 0)
                cell.pack_start(line, False, False, 0)
                bar = self.owner_meter()
                bar.set_size_request(-1 if self.vertical else max(density['meter'], 1), -1)
                if density['meter'] or self.vertical:
                    cell.pack_start(bar, False, False, 0)
                self.memory[key] = (value, bar)
            elif key == 'profile':
                cell = self.profile_button = Gtk.Button(label='Profil')
                cell.get_style_context().add_class('profile-pill')
                cell.set_tooltip_text('Lüfterprofil wechseln: Leise → Normal → Spiel')
                cell.connect('clicked', lambda *_: self.owner.cycle_profile())
            elif key == 'audio':
                cell = Gtk.Box(spacing=4)
                self.audio_button = Gtk.Button(label='🔊 —')
                self.audio_button.get_style_context().add_class('mini-close')
                self.audio_button.set_tooltip_text('Klicken: Ton an/aus · Mausrad: Lautstärke')
                self.audio_button.add_events(Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK)
                self.audio_button.connect('clicked', lambda *_: self.owner.audio_action('mute'))
                self.audio_button.connect('scroll-event', self.audio_scroll)
                self.mic_button = Gtk.Button(label='🎤')
                self.mic_button.get_style_context().add_class('mini-close')
                self.mic_button.set_tooltip_text('Mikrofon stumm schalten / einschalten')
                self.mic_button.connect('clicked', lambda *_: self.owner.audio_action('mic'))
                cell.pack_start(self.audio_button, False, False, 0)
                cell.pack_start(self.mic_button, False, False, 0)
            else:
                caption, chars = CAPTIONS[key]
                chars = density['chars'].get(key, chars)
                cell = Gtk.Box(spacing=6)
                cell.pack_start(_label(caption, 'row-name'), False, False, 0)
                value = _label('—', 'row-value')
                if self.vertical:
                    value.set_line_wrap(True)
                    value.set_width_chars(11)
                    value.set_max_width_chars(11)
                else:
                    value.set_width_chars(chars)
                    value.set_ellipsize(Pango.EllipsizeMode.END)
                cell.pack_start(value, False, False, 0)
                self.texts[key] = value
            cell.set_valign(Gtk.Align.CENTER)
            surface.pack_start(cell, False, False, 0)

    def build(self) -> None:
        from panels import bar_layout
        for child in self.get_children():
            child.destroy()
        events = Gtk.EventBox()
        events.set_name('bar-surface')
        events.get_style_context().add_class('bar-' + self.edge)
        events.set_tooltip_text('Klicken öffnet Air Studio')
        events.connect('button-press-event', self.clicked)
        self.add(events)
        density = DENSITY[0 if self.vertical else self.density]
        orientation = Gtk.Orientation.VERTICAL if self.vertical else Gtk.Orientation.HORIZONTAL
        spacing = 8 if self.vertical else density['spacing']
        self.items, self.extra = bar_layout(self.owner.settings)
        self.expanded = bool(self.extra) and self.owner.settings.get('bar_expanded', False) is True
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8 if self.vertical else 0)
        for setter in ('set_margin_top', 'set_margin_bottom'):
            getattr(outer, setter)(10 if self.vertical else 0)
        outer.set_margin_start(10 if self.vertical else 12)
        outer.set_margin_end(8)
        events.add(outer)
        main = Gtk.Box(orientation=orientation, spacing=spacing)
        if not self.vertical:
            main.set_size_request(-1, THICKNESS)
        outer.pack_start(main, False, False, 0)
        head = _label('R', 'eyebrow')
        head.set_tooltip_text('Restlos Air')
        head.set_valign(Gtk.Align.CENTER)
        main.pack_start(head, False, False, 0)
        main.pack_start(self._separator(), False, False, 0)
        self.temps, self.loads, self.memory, self.texts = {}, {}, {}, {}
        self.profile_button = self.audio_button = self.mic_button = None
        self._add_items(main, self.items, density)
        self.extra_row = None
        if self.expanded:
            # Zweite Reihe: Zusatzwerte, unter der ersten eingerückt, damit die Spalten ruhig wirken.
            outer.pack_start(self._separator_line(), False, False, 0)
            self.extra_row = Gtk.Box(orientation=orientation, spacing=spacing)
            if not self.vertical:
                self.extra_row.set_size_request(-1, THICKNESS - 6)
                self.extra_row.set_margin_start(head.get_preferred_width()[1] + spacing * 2 + 1)
            outer.pack_start(self.extra_row, False, False, 0)
            self._add_items(self.extra_row, self.extra, density)
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
        self.expand_button = Gtk.Button(label=('▴' if not self.vertical else '▴') if self.expanded else '▾')
        self.expand_button.get_style_context().add_class('mini-close')
        self.expand_button.set_tooltip_text('Zweite Reihe einklappen' if self.expanded else
                                            f'Zweite Reihe ausklappen ({len(self.extra)} weitere Werte)')
        self.expand_button.connect('clicked', lambda *_: self.toggle_extra())
        self.expand_button.set_sensitive(bool(self.extra))
        tail.pack_start(self.expand_button, False, False, 0)
        gear = Gtk.Button(label='⚙')
        gear.get_style_context().add_class('mini-close')
        gear.set_tooltip_text('Einstellungen öffnen')
        gear.get_accessible().set_name('Einstellungen öffnen')
        gear.connect('clicked', lambda *_: self.owner.show_view(False))
        tail.pack_start(gear, False, False, 0)
        if self.vertical:
            outer.pack_end(tail, False, False, 0)
        else:
            main.pack_end(tail, False, False, 0)
        events.show_all()

    def _separator_line(self) -> Gtk.Separator:
        line = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        line.get_style_context().add_class('bar-separator')
        return line

    def toggle_extra(self) -> None:
        self.owner.settings['bar_expanded'] = not self.expanded
        self.owner.save()
        self.rebuild()
        self.owner.push_bar()

    def audio_scroll(self, _widget, event) -> bool:
        direction = event.direction
        if direction == Gdk.ScrollDirection.SMOOTH:
            delta = -event.delta_y
        else:
            delta = 1 if direction == Gdk.ScrollDirection.UP else -1 if direction == Gdk.ScrollDirection.DOWN else 0
        if delta:
            self.owner.audio_action('volume', 5 if delta > 0 else -5)
        return True

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
        child = self.get_child()
        natural = child.get_preferred_height()[1] if child is not None else 0
        height = max(THICKNESS, natural)
        y = area.y if self.edge == 'top' else area.y + area.height - height
        return area.x, y, area.width, height

    def fit(self) -> None:
        """Choose the lowest density whose content fits the monitor width."""
        if self.vertical:
            return
        available = self.area().width
        while self.density < len(DENSITY) - 1 and self.get_child().get_preferred_width()[1] > available:
            self.density += 1
            self.build()

    def place(self) -> None:
        self.fit()
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
        import profiles
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
        gap = '\n' if self.vertical else '  '
        short = not self.vertical and DENSITY[self.density]['short']
        speed = short_rate if short else rate
        if short:
            gap = ' '
        network = source.network_state or {}
        extras = source.extras or {}
        top = source.top[0] if source.top else None
        ping = network.get('internet_ms')
        signal = network.get('signal_percent')
        offline = network.get('internet_status') in ('no_route', 'disconnected')
        values = {
            'top': '—' if top is None else f'{top.name} {top.cpu_percent:.0f} %',
            'pump': source.mini_pump.get_text().split(' · ', 1)[-1].replace(' U/min', '' if DENSITY[self.density]['meter'] == 0 else ' U/min'),
            'ping': ('offline' if offline else '—' if ping is None else f'{ping:.0f} ms')
                    + ('' if signal is None or network.get('connection') != 'wlan' or DENSITY[self.density]['meter'] == 0 else f' · {signal:.0f} %'),
            'transfer': f"↓ {speed(network.get('rx_bytes_sec'))}{gap}↑ {speed(network.get('tx_bytes_sec'))}",
            'disk': f"R {speed(metrics.get('disk_read_bytes_sec'))}{gap}W {speed(metrics.get('disk_write_bytes_sec'))}",
            'power': '—' if extras.get('gpu_power_w') is None else f"{extras['gpu_power_w']:.0f} W",
            'clock': '—' if not metrics.get('cpu_clock_mhz') else f"{de(metrics['cpu_clock_mhz'] / 1000)} GHz",
        }
        tips = {'top': source.top_text() + ' · Details im Studio', 'ping': 'Ping zu 1.1.1.1' + (' · WLAN-Signal' if signal is not None else ''),
                'pump': 'Drehzahl der Wasserpumpe', 'transfer': 'Empfang / Versand der aktiven Verbindung',
                'disk': 'Laufwerke lesen / schreiben', 'power': 'Leistungsaufnahme der Grafikkarte', 'clock': 'Höchster CPU-Kerntakt'}
        for key, label in self.texts.items():
            label.set_text(values[key])
            label.get_parent().set_tooltip_text(tips[key])
        if self.profile_button is not None:
            self.profile_button.set_label(profiles.title(source.profile).upper())
            self.profile_button.set_sensitive(not source.backend_busy)
        if self.audio_button is not None:
            audio = source.audio_state()
            volume = audio.get('volume_percent')
            muted = audio.get('output_muted')
            self.audio_button.set_label(('🔇 ' if muted else '🔊 ') + ('—' if volume is None else f'{volume:.0f} %'))
            self.mic_button.set_label('🎤✕' if audio.get('mic_muted') else '🎤')
            available = bool(audio.get('available'))
            self.audio_button.set_sensitive(available)
            self.mic_button.set_sensitive(available and audio.get('default_source') is not None)
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
