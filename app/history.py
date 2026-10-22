"""Long-term temperature record and dust hint, stored locally and sparingly.

Every second's reading only updates an in-memory 5-minute bucket (maximum temperatures,
average CPU load, what ran at the hottest moment). One JSON line per bucket is appended,
seven days are kept (about 2000 lines). For the dust hint, each finished day keeps a
single number: the median CPU temperature in idle buckets (CPU load below 15 %).
"""
from __future__ import annotations

import json
import os
import statistics
import time
from datetime import datetime
from pathlib import Path

BUCKET_SECONDS = 300
KEEP_SECONDS = 7 * 24 * 3600
KEEP_DAYS = 180
IDLE_LOAD = 15.0
DUST_RISE = 6.0
KEYS = ('cpu', 'gpu', 'hotspot', 'ssd')


def _state_dir() -> Path:
    base = os.environ.get('XDG_STATE_HOME')
    return (Path(base) if base else Path.home() / '.local/state') / 'restlos-air'


class History:
    def __init__(self, directory: Path | None = None, clock=time.time) -> None:
        self.directory = Path(directory) if directory else _state_dir()
        self.path = self.directory / 'history.jsonl'
        self.daily_path = self.directory / 'idle-days.json'
        self.clock = clock
        self.bucket: dict | None = None
        self.buckets = self._load()
        self.daily = self._load_daily()

    # ----- storage ---------------------------------------------------------------------
    def _load(self) -> list[dict]:
        rows = []
        try:
            with self.path.open(encoding='utf-8') as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and isinstance(row.get('t'), (int, float)):
                        rows.append(row)
        except OSError:
            pass
        cutoff = self.clock() - KEEP_SECONDS
        return [row for row in rows if row['t'] >= cutoff]

    def _load_daily(self) -> dict[str, float]:
        try:
            data = json.loads(self.daily_path.read_text(encoding='utf-8'))
            return {k: float(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, (int, float))}
        except (OSError, ValueError, AttributeError):
            return {}

    def _write_all(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in self.buckets), encoding='utf-8')
        temporary.replace(self.path)

    def _append(self, row: dict) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            cutoff = self.clock() - KEEP_SECONDS
            if self.buckets and self.buckets[0]['t'] < cutoff - BUCKET_SECONDS:
                self.buckets = [item for item in self.buckets if item['t'] >= cutoff]
                self._write_all()
            else:
                with self.path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        except OSError:
            pass  # A full disk must never disturb the widget.

    # ----- recording -------------------------------------------------------------------
    def add(self, latest: dict, cpu_load: float | None, top: str | None, game: str | None) -> None:
        now = self.clock()
        start = int(now // BUCKET_SECONDS * BUCKET_SECONDS)
        if self.bucket and self.bucket['t'] != start:
            self._close()
        if self.bucket is None:
            self.bucket = {'t': start, 'n': 0, 'load': 0.0, 'loads': 0}
        bucket = self.bucket
        bucket['n'] += 1
        if cpu_load is not None:
            bucket['load'] += cpu_load
            bucket['loads'] += 1
        for key in KEYS:
            value = latest.get(key)
            if value is None:
                continue
            if value > bucket.get(key, float('-inf')):
                bucket[key] = round(float(value), 1)
                if key == 'cpu':
                    bucket['at'] = round(now)
                    bucket['top'] = top
            if key == 'cpu':
                bucket.setdefault('cpus', []).append(float(value))
        if game:
            bucket['game'] = game

    def _close(self) -> None:
        bucket, self.bucket = self.bucket, None
        if not bucket or not bucket['n']:
            return
        cpus = bucket.pop('cpus', [])
        loads = bucket.pop('loads')
        bucket['load'] = round(bucket['load'] / loads, 1) if loads else None
        bucket['cpu_median'] = round(statistics.median(cpus), 1) if cpus else None
        bucket.pop('n')
        day = datetime.fromtimestamp(bucket['t']).strftime('%Y-%m-%d')
        if self.buckets and datetime.fromtimestamp(self.buckets[-1]['t']).strftime('%Y-%m-%d') != day:
            self._close_day(datetime.fromtimestamp(self.buckets[-1]['t']).strftime('%Y-%m-%d'))
        self.buckets.append(bucket)
        self._append(bucket)

    def _close_day(self, day: str) -> None:
        idle = [row['cpu_median'] for row in self.buckets
                if datetime.fromtimestamp(row['t']).strftime('%Y-%m-%d') == day
                and row.get('cpu_median') is not None and row.get('load') is not None and row['load'] < IDLE_LOAD]
        if len(idle) < 6:  # at least 30 idle minutes, otherwise the day says nothing
            return
        self.daily[day] = round(statistics.median(idle), 1)
        for old in sorted(self.daily)[:-KEEP_DAYS]:
            del self.daily[old]
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.daily_path.write_text(json.dumps(self.daily, indent=1), encoding='utf-8')
        except OSError:
            pass

    def flush(self) -> None:
        self._close()

    # ----- questions -------------------------------------------------------------------
    def rows(self) -> list[dict]:
        rows = list(self.buckets)
        if self.bucket and self.bucket['n']:
            current = {k: v for k, v in self.bucket.items() if k not in ('cpus', 'n', 'loads')}
            rows.append(current)
        return rows

    def peaks(self, since: float) -> dict[str, dict]:
        """Highest value per sensor since a time, with time and what was running."""
        result: dict[str, dict] = {}
        for row in self.rows():
            if row['t'] < since:
                continue
            for key in KEYS:
                value = row.get(key)
                if value is not None and value > result.get(key, {}).get('value', float('-inf')):
                    result[key] = {'value': value, 't': row.get('at', row['t']) if key == 'cpu' else row['t'],
                                   'game': row.get('game'), 'top': row.get('top')}
        return result

    def today_peaks(self) -> dict[str, dict]:
        midnight = datetime.fromtimestamp(self.clock()).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.peaks(midnight.timestamp())

    def series(self, hours: int = 24) -> list[tuple[float, float | None, float | None]]:
        since = self.clock() - hours * 3600
        return [(row['t'], row.get('cpu'), row.get('gpu')) for row in self.rows() if row['t'] >= since]

    def dust_hint(self) -> str | None:
        """Idle temperature rising over weeks hints at dust (or a warmer room)."""
        days = [self.daily[day] for day in sorted(self.daily)]
        if len(days) < 14:
            return None
        baseline = statistics.median(days[:7])
        recent = statistics.median(days[-7:])
        if recent - baseline >= DUST_RISE:
            return (f'Im Leerlauf ist die CPU jetzt etwa {recent - baseline:.0f} °C wärmer als in den ersten '
                    f'Wochen ({baseline:.0f} → {recent:.0f} °C). Kühler und Filter auf Staub prüfen – '
                    'oder der Raum ist wärmer geworden.')
        return None
