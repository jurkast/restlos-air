"""Which programs use the most CPU and memory, and is a game running?

Reads /proc only (one ``stat`` per process; ``cmdline`` once per new process for game
detection). No root rights, no external tools. Processes are grouped by name, so the
many Chrome or Steam helper processes appear as one entry.
"""
from __future__ import annotations

import os
import re
import signal
import time
from dataclasses import dataclass
from pathlib import Path

CLOCK_TICKS = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100
PAGE_SIZE = os.sysconf('SC_PAGE_SIZE') if hasattr(os, 'sysconf') else 4096
# Never offered for "Beenden": the desktop, audio, display server or Restlos itself.
PROTECTED = frozenset({
    'systemd', 'gnome-shell', 'Xorg', 'Xwayland', 'gnome-session-b', 'gdm-x-session', 'pipewire',
    'pipewire-pulse', 'wireplumber', 'pulseaudio', 'dbus-daemon', 'dbus-broker', 'coolercontrold',
    'coolercontrol', 'ibus-daemon', 'gsd-xsettings', 'nautilus-desktop', 'zorin-appearance',
})
_GAME_RUNTIME = re.compile(r'/(Proton[^/]*|SteamLinuxRuntime[^/]*|Steamworks Shared|steamapps/common/Steam Controller Configs)/')
_STEAM_GAME = re.compile(r'/steamapps/common/([^/]+)/')
_HEROIC_GAME = re.compile(r'/Games/Heroic/([^/]+)/')
_LUTRIS_GAME = re.compile(r'/Games/([^/]+)/')


@dataclass(frozen=True, slots=True)
class ProgramUsage:
    name: str
    cpu_percent: float
    memory_bytes: int
    processes: int
    killable: bool


def game_name(cmdline: str) -> str | None:
    """Name of a game from its command line, or None for ordinary programs."""
    if not cmdline or _GAME_RUNTIME.search(cmdline):
        return None
    for pattern in (_STEAM_GAME, _HEROIC_GAME):
        match = pattern.search(cmdline)
        if match:
            return match.group(1)
    if 'lutris-wrapper' in cmdline:
        match = _LUTRIS_GAME.search(cmdline)
        return match.group(1) if match else 'Lutris-Spiel'
    return None


class ProcessScanner:
    def __init__(self, proc_root: Path = Path('/proc'), cpu_count: int | None = None,
                 clock=time.monotonic, uid: int | None = None) -> None:
        self.proc_root = Path(proc_root)
        self.cpu_count = max(1, cpu_count or os.cpu_count() or 1)
        self.clock = clock
        self.uid = os.getuid() if uid is None else uid
        self._previous: dict[tuple[int, int], int] = {}
        self._previous_at: float | None = None
        self._games: dict[tuple[int, int], str | None] = {}
        self.programs: list[ProgramUsage] = []
        self.game: str | None = None

    @staticmethod
    def _parse_stat(text: str) -> tuple[str, int, int, int] | None:
        """(name, cpu ticks, start time, rss pages) from /proc/PID/stat."""
        head, separator, tail = text.rpartition(')')
        if not separator:
            return None
        name = head.partition('(')[2]
        fields = tail.split()
        try:
            # tail starts at field 3 (state); utime=14, stime=15, starttime=22, rss=24.
            return name, int(fields[11]) + int(fields[12]), int(fields[19]), int(fields[21])
        except (IndexError, ValueError):
            return None

    def _cmdline(self, pid: str) -> str:
        try:
            return (self.proc_root / pid / 'cmdline').read_bytes()[:4096].replace(b'\0', b' ').decode('utf-8', 'replace')
        except OSError:
            return ''

    def sample(self, top: int = 5) -> list[ProgramUsage]:
        now = self.clock()
        elapsed = None if self._previous_at is None else now - self._previous_at
        current: dict[tuple[int, int], int] = {}
        groups: dict[str, list] = {}
        games: dict[tuple[int, int], str | None] = {}
        try:
            entries = [entry for entry in os.listdir(self.proc_root) if entry.isdigit()]
        except OSError:
            entries = []
        for pid in entries:
            try:
                parsed = self._parse_stat((self.proc_root / pid / 'stat').read_text(encoding='utf-8', errors='replace'))
            except OSError:
                continue
            if parsed is None:
                continue
            name, ticks, started, rss = parsed
            key = (int(pid), started)
            current[key] = ticks
            delta = ticks - self._previous.get(key, ticks)
            if key in self._games:
                games[key] = self._games[key]
            else:
                games[key] = game_name(self._cmdline(pid))
            group = groups.setdefault(name, [0, 0, 0, True])
            group[0] += max(0, delta)
            group[1] += max(0, rss) * PAGE_SIZE
            group[2] += 1
            if group[3]:
                try:
                    group[3] = (self.proc_root / pid).stat().st_uid == self.uid
                except OSError:
                    group[3] = False
        self._previous, self._previous_at, self._games = current, now, games
        found = [game for game in games.values() if game]
        self.game = max(set(found), key=found.count) if found else None
        if not elapsed or elapsed <= 0:
            self.programs = []
            return self.programs
        scale = 100.0 / (elapsed * CLOCK_TICKS * self.cpu_count)
        usage = [ProgramUsage(name, ticks * scale, memory, count, owned and name not in PROTECTED)
                 for name, (ticks, memory, count, owned) in groups.items()]
        usage.sort(key=lambda item: (item.cpu_percent, item.memory_bytes), reverse=True)
        self.programs = usage[:max(1, top)]
        return self.programs

    def terminate(self, name: str) -> int:
        """Politely end all own processes of one program (SIGTERM). Returns how many."""
        if not name or name in PROTECTED:
            return 0
        ended = 0
        try:
            entries = [entry for entry in os.listdir(self.proc_root) if entry.isdigit()]
        except OSError:
            return 0
        for pid in entries:
            if int(pid) == os.getpid():
                continue
            try:
                parsed = self._parse_stat((self.proc_root / pid / 'stat').read_text(encoding='utf-8', errors='replace'))
                if parsed is None or parsed[0] != name or (self.proc_root / pid).stat().st_uid != self.uid:
                    continue
                os.kill(int(pid), signal.SIGTERM)
                ended += 1
            except (OSError, ProcessLookupError, PermissionError):
                continue
        return ended
