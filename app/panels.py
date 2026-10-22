"""Additional Air Studio sections: profiles, processes, history, mainboard, bar items, look."""
from __future__ import annotations

import time

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Pango

import profiles

BAR_ITEMS = (
    ('cpu', 'CPU-Temperatur und Last'), ('gpu', 'GPU-Temperatur und Last'), ('hotspot', 'GPU-Hotspot'),
    ('ssd', 'SSD-Temperatur'), ('ram', 'Arbeitsspeicher'), ('vram', 'Grafikspeicher'),
    ('top', 'Wer frisst? (größter Verbraucher)'), ('profile', 'Lüfterprofil'), ('pump', 'Pumpe'),
    ('ping', 'Ping und WLAN'), ('transfer', 'Datenrate Netzwerk'), ('disk', 'Festplatten lesen/schreiben'),
    ('power', 'GPU-Leistung in Watt'), ('clock', 'CPU-Takt'), ('audio', 'Lautstärke und Mikrofon'),
)
DEFAULT_BAR_ITEMS = ['cpu', 'gpu', 'hotspot', 'ssd', 'ram', 'vram', 'top', 'profile', 'pump', 'ping', 'transfer', 'audio']


def bar_items(settings: dict) -> list[str]:
    known = {key for key, _ in BAR_ITEMS}
    chosen = settings.get('bar_items')
    if not isinstance(chosen, list):
        return list(DEFAULT_BAR_ITEMS)
    return [key for key in dict.fromkeys(chosen) if key in known]


def _label(text: str = '', style: str = 'muted', xalign: float = 0) -> Gtk.Label:
    item = Gtk.Label(label=text, xalign=xalign)
    item.get_style_context().add_class(style)
    return item


def _panel(content: Gtk.Box, title: str) -> Gtk.Box:
    panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    panel.get_style_context().add_class('panel')
    content.pack_start(panel, False, False, 0)
    panel.pack_start(_label(title, 'section'), False, False, 0)
    return panel


def memory_text(value: int) -> str:
    from air_studio import de
    return de(value / 2**30) + ' GiB' if value >= 2**30 else de(value / 2**20, 0) + ' MiB'


class ProfilePanel:
    def __init__(self, window, content: Gtk.Box) -> None:
        self.window = window
        panel = _panel(content, 'Lüfterprofile')
        panel.pack_start(_label('Eine Kennlinie für alle regelbaren Lüfter. Alle Profile erreichen 100 % spätestens bei 85 °C.'),
                         False, False, 0)
        row = Gtk.Box(spacing=8)
        panel.pack_start(row, False, False, 0)
        self.buttons: dict[str, Gtk.ToggleButton] = {}
        for key in profiles.ORDER:
            button = Gtk.ToggleButton(label=profiles.title(key))
            button.connect('toggled', self.clicked, key)
            row.pack_start(button, True, True, 0)
            self.buttons[key] = button
        self.auto = Gtk.CheckButton(label='Spielmodus automatisch: bei Steam-, Lutris- oder Heroic-Spielen auf „Spiel“, danach zurück')
        self.auto.set_active(window.game_mode.enabled)
        self.auto.connect('toggled', self.auto_toggled)
        panel.pack_start(self.auto, False, False, 0)
        self.state = _label('')
        self.state.set_line_wrap(True)
        panel.pack_start(self.state, False, False, 0)
        self.painting = False
        self.refresh()

    def clicked(self, button: Gtk.ToggleButton, key: str) -> None:
        if self.painting:
            return
        self.window.set_profile(key)
        self.refresh()

    def auto_toggled(self, button: Gtk.CheckButton) -> None:
        self.window.game_mode.enabled = button.get_active()
        self.window.settings['game_mode'] = button.get_active()
        self.window.save()
        self.refresh()

    def refresh(self, message: str | None = None) -> None:
        self.painting = True
        for key, button in self.buttons.items():
            button.set_active(key == self.window.profile)
            button.set_sensitive(not self.window.backend_busy)
        self.painting = False
        game = self.window.game
        parts = [f'Aktiv: {profiles.title(self.window.profile)}']
        if game:
            parts.append(f'Spiel erkannt: {game}')
        if message:
            parts.append(message)
        self.state.set_text(' · '.join(parts))


class ProcessPanel:
    ROWS = 5

    def __init__(self, window, content: Gtk.Box) -> None:
        self.window = window
        panel = _panel(content, 'Wer frisst gerade?')
        panel.pack_start(_label('Programme nach CPU-Last, gleiche Programme zusammengefasst. Alle 3 Sekunden.'), False, False, 0)
        self.grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        panel.pack_start(self.grid, False, False, 0)
        self.rows = []
        for row in range(self.ROWS):
            name = _label('—', 'row-value')
            name.set_hexpand(True)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            cpu = _label('', 'info', 1)
            cpu.set_width_chars(6)
            memory = _label('', 'info', 1)
            memory.set_width_chars(9)
            end = Gtk.Button(label='Beenden')
            end.set_no_show_all(True)
            end.connect('clicked', self.end_clicked, row)
            for column, widget in enumerate((name, cpu, memory, end)):
                self.grid.attach(widget, column, row, 1, 1)
            self.rows.append((name, cpu, memory, end))
        self.feedback = _label('')
        panel.pack_start(self.feedback, False, False, 0)
        self.programs = []

    def refresh(self, programs) -> None:
        self.programs = list(programs)[:self.ROWS]
        for index, (name, cpu, memory, end) in enumerate(self.rows):
            if index < len(self.programs):
                item = self.programs[index]
                name.set_text(item.name + (f'  ×{item.processes}' if item.processes > 1 else ''))
                cpu.set_text(f'{item.cpu_percent:.0f} %')
                memory.set_text(memory_text(item.memory_bytes))
                end.set_visible(item.killable)
            else:
                name.set_text('—' if index == 0 else '')
                cpu.set_text('')
                memory.set_text('')
                end.set_visible(False)

    def end_clicked(self, _button: Gtk.Button, index: int) -> None:
        if index >= len(self.programs):
            return
        item = self.programs[index]
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.OK_CANCEL, text=f'„{item.name}“ beenden?')
        dialog.format_secondary_text('Das Programm wird gebeten, sich zu schließen. Nicht gespeicherte Arbeit kann verloren gehen.')
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.OK:
            return
        count = self.window.scanner.terminate(item.name)
        self.feedback.set_text(f'{item.name}: {count} Prozess{"e" if count != 1 else ""} beendet.' if count
                               else f'{item.name} konnte nicht beendet werden.')


class HistoryChart(Gtk.DrawingArea):
    """CPU (mint) and GPU (blue) maxima per 5 minutes over the last 24 hours."""

    def __init__(self) -> None:
        super().__init__()
        self.series = []
        self.light = False
        self.set_size_request(-1, 110)
        self.connect('draw', self.draw)
        self.get_accessible().set_name('Temperaturverlauf der letzten 24 Stunden')

    def draw(self, _widget, cr) -> bool:
        width, height = self.get_allocated_width(), self.get_allocated_height()
        left, right, top, bottom = 34, width - 8, 6, height - 18
        if right <= left:
            return False
        now = time.time()
        grid = (.86, .9, .91) if self.light else (.18, .27, .3)
        text = (.4, .49, .54) if self.light else (.66, .75, .76)
        cr.set_font_size(10)
        for value in (25, 50, 75, 100):
            y = bottom - value / 110 * (bottom - top)
            cr.set_source_rgb(*grid)
            cr.set_line_width(1)
            cr.move_to(left, y)
            cr.line_to(right, y)
            cr.stroke()
            cr.set_source_rgb(*text)
            cr.move_to(2, y + 4)
            cr.show_text(f'{value}°')
        for hours, caption in ((24, '−24 h'), (12, '−12 h'), (0, 'jetzt')):
            x = left + (1 - hours / 24) * (right - left)
            cr.move_to(max(left, min(right - 28, x - 12)), height - 3)
            cr.show_text(caption)
        for index, color in ((1, (.16, .62, .52) if self.light else (.52, .87, .77)), (2, (.25, .45, .8) if self.light else (.54, .70, .96))):
            cr.set_source_rgb(*color)
            cr.set_line_width(1.6)
            started = False
            previous = None
            for row in self.series:
                value = row[index]
                gap = previous is not None and row[0] - previous > 900
                previous = row[0]
                if value is None or gap:
                    if started:
                        cr.stroke()
                    started = False
                    if value is None:
                        continue
                x = left + (1 - (now - row[0]) / 86400) * (right - left)
                y = bottom - min(value, 110) / 110 * (bottom - top)
                if started:
                    cr.line_to(x, y)
                else:
                    cr.move_to(x, y)
                    started = True
            if started:
                cr.stroke()
        return False


class HistoryPanel:
    def __init__(self, window, content: Gtk.Box) -> None:
        self.window = window
        panel = _panel(content, 'Verlauf · Spitzenwerte')
        self.peaks = _label('Noch keine Messwerte für heute.')
        self.peaks.set_line_wrap(True)
        panel.pack_start(self.peaks, False, False, 0)
        self.chart = HistoryChart()
        panel.pack_start(self.chart, False, False, 0)
        panel.pack_start(_label('Letzte 24 Stunden · Höchstwert je 5 Minuten · CPU: Mint  GPU: Blau'), False, False, 0)
        self.dust = _label('', 'warning')
        self.dust.set_line_wrap(True)
        self.dust.set_no_show_all(True)
        panel.pack_start(self.dust, False, False, 0)

    def refresh(self, history) -> None:
        from air_studio import TEMP_TITLES
        names = dict(TEMP_TITLES)
        lines = []
        for key, peak in history.today_peaks().items():
            when = time.strftime('%H:%M', time.localtime(peak['t']))
            context = peak.get('game') or peak.get('top')
            lines.append(f'{names.get(key, key)} max. {peak["value"]:.0f} °C um {when}' + (f' · {context}' if context and key == 'cpu' else ''))
        self.peaks.set_text('Heute: ' + ' · '.join(lines) if lines else 'Noch keine Messwerte für heute.')
        self.chart.series = history.series(24)
        self.chart.queue_draw()
        hint = history.dust_hint()
        self.dust.set_text(hint or '')
        self.dust.set_visible(bool(hint))


class BoardPanel:
    def __init__(self, window, content: Gtk.Box) -> None:
        panel = _panel(content, 'Mainboard, Takt & Laufwerke')
        self.summary = _label('Messung startet …')
        self.summary.set_line_wrap(True)
        panel.pack_start(self.summary, False, False, 0)
        self.board = _label('')
        self.board.set_line_wrap(True)
        panel.pack_start(self.board, False, False, 0)

    def refresh(self, extras: dict, metrics: dict) -> None:
        from air_studio import de, rate
        parts = []
        if metrics.get('cpu_clock_mhz'):
            parts.append(f'CPU-Takt max. {de(metrics["cpu_clock_mhz"], 0)} MHz')
        if extras.get('gpu_clock_mhz'):
            parts.append(f'GPU-Takt {de(extras["gpu_clock_mhz"], 0)} MHz')
        if extras.get('gpu_power_w') is not None:
            parts.append(f'GPU-Leistung {extras["gpu_power_w"]:.0f} W')
        parts.append(f'Laufwerke ↓ lesen {rate(metrics.get("disk_read_bytes_sec"))} · ↑ schreiben {rate(metrics.get("disk_write_bytes_sec"))}')
        self.summary.set_text('\n'.join(parts))
        board = extras.get('board') or []
        self.board.set_text('Mainboard-Sensoren: ' + ' · '.join(f'{name} {value:.0f} °C' for name, value in board)
                            if board else 'Keine zusätzlichen Mainboard-Sensoren lesbar (nct6775 geladen?).')


class BarItemsPanel:
    def __init__(self, window, content: Gtk.Box) -> None:
        self.window = window
        panel = _panel(content, 'Leiste zusammenstellen')
        panel.pack_start(_label('Häkchen = anzeigen. Mit ↑ und ↓ die Reihenfolge ändern.'), False, False, 0)
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        panel.pack_start(self.list, False, False, 0)
        reset = Gtk.Button(label='Standard wiederherstellen')
        reset.connect('clicked', self.reset)
        panel.pack_start(reset, False, False, 0)
        self.render()

    def order(self) -> list[str]:
        chosen = bar_items(self.window.settings)
        return chosen + [key for key, _ in BAR_ITEMS if key not in chosen]

    def render(self) -> None:
        for child in self.list.get_children():
            child.destroy()
        enabled = set(bar_items(self.window.settings))
        titles = dict(BAR_ITEMS)
        for index, key in enumerate(self.order()):
            row = Gtk.Box(spacing=6)
            check = Gtk.CheckButton(label=titles[key])
            check.set_active(key in enabled)
            check.connect('toggled', self.toggled, key)
            row.pack_start(check, True, True, 0)
            for symbol, step in (('↑', -1), ('↓', 1)):
                button = Gtk.Button(label=symbol)
                button.get_style_context().add_class('mini-close')
                button.set_sensitive(key in enabled and 0 <= index + step < len(enabled))
                button.connect('clicked', self.move, key, step)
                row.pack_start(button, False, False, 0)
            self.list.pack_start(row, False, False, 0)
        self.list.show_all()

    def store(self, items: list[str]) -> None:
        self.window.settings['bar_items'] = items
        self.window.save()
        if self.window.bar is not None:
            self.window.bar.rebuild()
            self.window.push_bar()
        self.render()

    def toggled(self, check: Gtk.CheckButton, key: str) -> None:
        items = bar_items(self.window.settings)
        if check.get_active() and key not in items:
            items.append(key)
        elif not check.get_active() and key in items:
            items.remove(key)
        self.store(items)

    def move(self, _button, key: str, step: int) -> None:
        items = bar_items(self.window.settings)
        if key not in items:
            return
        index = items.index(key)
        target = index + step
        if 0 <= target < len(items):
            items[index], items[target] = items[target], items[index]
            self.store(items)

    def reset(self, *_args) -> None:
        self.store(list(DEFAULT_BAR_ITEMS))


class AppearancePanel:
    def __init__(self, window, content: Gtk.Box) -> None:
        self.window = window
        panel = _panel(content, 'Darstellung & Tastenkürzel')
        row = Gtk.Box(spacing=10)
        panel.pack_start(row, False, False, 0)
        row.pack_start(_label('Design', 'row-name'), False, False, 0)
        self.theme = Gtk.ComboBoxText()
        for key, title in (('dark', 'Dunkel'), ('light', 'Hell'), ('auto', 'Wie das System')):
            self.theme.append(key, title)
        self.theme.set_active_id(window.settings.get('theme', 'dark') if window.settings.get('theme') in ('dark', 'light', 'auto') else 'dark')
        self.theme.connect('changed', self.theme_changed)
        row.pack_start(self.theme, True, True, 0)
        import hotkey
        self.hotkey = hotkey
        keys = Gtk.Box(spacing=10)
        panel.pack_start(keys, False, False, 0)
        self.hotkey_button = Gtk.Button()
        self.hotkey_button.connect('clicked', self.hotkey_clicked)
        keys.pack_start(self.hotkey_button, True, True, 0)
        self.hotkey_state = _label('')
        self.hotkey_state.set_line_wrap(True)
        panel.pack_start(self.hotkey_state, False, False, 0)
        self.refresh_hotkey()

    def theme_changed(self, combo: Gtk.ComboBoxText) -> None:
        self.window.settings['theme'] = combo.get_active_id()
        self.window.save()
        application = self.window.get_application()
        if application is not None and hasattr(application, 'apply_theme'):
            application.apply_theme()

    def refresh_hotkey(self, message: str = '') -> None:
        installed = self.hotkey.installed()
        available = self.hotkey.available()
        self.hotkey_button.set_label('Tastenkürzel entfernen' if installed else f'Tastenkürzel {self.hotkey.LABEL} einrichten')
        self.hotkey_button.set_sensitive(available)
        text = message or (f'{self.hotkey.LABEL} blendet Widget oder Leiste ein und aus.' if installed else
                           'Noch kein Tastenkürzel eingerichtet.' if available else
                           'Tastenkürzel lassen sich hier nur unter GNOME/Zorin einrichten.')
        self.hotkey_state.set_text(text)

    def hotkey_clicked(self, _button) -> None:
        try:
            if self.hotkey.installed():
                self.hotkey.remove()
                self.refresh_hotkey('Tastenkürzel entfernt.')
            else:
                self.hotkey.install()
                self.refresh_hotkey(f'{self.hotkey.LABEL} ist eingerichtet und wirkt sofort.')
        except Exception as error:  # Desktop settings may be read-only or missing.
            self.refresh_hotkey(f'Tastenkürzel konnte nicht geändert werden: {error}')
