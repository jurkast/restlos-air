"""Bounded, event-driven PulseAudio / PipeWire-Pulse audio controls.

No recording is started and no shell is used. Call actions in a background
executor; they return the confirmed snapshot. Monitor callbacks run only in the
monitor worker, so GTK users must dispatch them with GLib.idle_add.
"""
from copy import deepcopy
import json
import math
import os
import re
import selectors
import shutil
import subprocess
import threading
import time

COMMAND_TIMEOUT = 2.0
MAX_OUTPUT = 1024 * 1024
MAX_DEVICES = 256
MAX_MOVES = 64
DEBOUNCE = .12
MAX_DEBOUNCE = .6
MOVE_BUDGET = 5.0
_EVENT = re.compile(r"\bon (sink|source|server|card)(?:\s|$)")


class AudioError(Exception):
    def __init__(self, message, snapshot=None):
        super().__init__(message)
        self.snapshot = deepcopy(snapshot) if snapshot is not None else None


def _id(value):
    if isinstance(value, bool):
        raise AudioError('Ungültige Audio-Gerätekennung.')
    if isinstance(value, str):
        if not re.fullmatch(r'[0-9]{1,10}', value):
            raise AudioError('Ungültige Audio-Gerätekennung.')
        value = int(value)
    if not isinstance(value, int) or not 0 <= value < 2**32 - 1:
        raise AudioError('Ungültige Audio-Gerätekennung.')
    return value


def _volume(value):
    if not isinstance(value, dict) or not value:
        return None
    channels = []
    for channel in value.values():
        if not isinstance(channel, dict):
            return None
        raw = channel.get('value')
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        try:
            if not math.isfinite(raw) or raw < 0:
                return None
            channels.append(raw / 65536 * 100)
        except (OverflowError, ValueError):
            return None
    return round(sum(channels) / len(channels), 1)


def _monitor_source(item):
    props = item.get('properties')
    props = props if isinstance(props, dict) else {}
    name = item.get('name')
    if (props.get('device.class') == 'monitor' or props.get('media.class') == 'Audio/Sink'
            or isinstance(name, str) and name.endswith('.monitor')):
        return True
    # pactl versions use monitor_of_sink; PipeWire-Pulse's JSON may instead
    # expose monitor_source as the associated sink name. Empty / uint32-max
    # values denote ordinary input sources.
    for field in ('monitor_of_sink', 'monitor_source'):
        value = item.get(field)
        if value not in (None, '', -1, 4294967295, '4294967295', 'n/a'):
            return True
    return False


def _items(data, sources=False):
    if not isinstance(data, list) or len(data) > MAX_DEVICES:
        raise AudioError('Die Audio-Geräteliste hat ein unerwartetes Format.')
    result, seen = [], set()
    for item in data:
        if not isinstance(item, dict):
            raise AudioError('Ein Audio-Gerät meldet ungültige Daten.')
        if sources and _monitor_source(item):
            continue
        index = _id(item.get('index'))
        name = item.get('name')
        if index in seen or not isinstance(name, str) or not name or len(name) > 1024:
            raise AudioError('Die Audio-Gerätekennung ist nicht eindeutig.')
        seen.add(index)
        description = item.get('description')
        result.append({'id': index, 'name': name,
                       'description': description[:2048] if isinstance(description, str) else name,
                       'volume_percent': _volume(item.get('volume')),
                       'muted': item.get('mute') if isinstance(item.get('mute'), bool) else None})
    return sorted(result, key=lambda item: item['id'])


def _empty(error=''):
    return {'available': False, 'error': error, 'sinks': [], 'sources': [],
            'default_sink': None, 'default_source': None, 'volume_percent': None,
            'output_muted': None, 'mic_muted': None, 'updated_at': time.monotonic()}


class AudioMonitor:
    def __init__(self, callback=None):
        self.callback = callback
        self._pactl = shutil.which('pactl')
        self._lock = threading.RLock()
        self._io_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._process = None
        self._wake_read = self._wake_write = None
        self._latest = _empty('Audio wird verbunden.' if self._pactl else 'pactl ist nicht installiert.')
        self._published = None

    def snapshot(self):
        with self._lock:
            return deepcopy(self._latest)

    def _store(self, state):
        with self._lock:
            self._latest = deepcopy(state)
        return deepcopy(state)

    def _command(self, *args):
        if not self._pactl:
            raise AudioError('pactl ist nicht installiert.')
        try:
            result = subprocess.run([self._pactl, *args], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=COMMAND_TIMEOUT,
                                    check=False, env={**os.environ, 'LC_ALL': 'C'})
        except subprocess.TimeoutExpired:
            raise AudioError('Der Audiodienst antwortet nicht rechtzeitig.') from None
        except OSError:
            raise AudioError('Der Audiodienst ist nicht erreichbar.') from None
        if result.returncode != 0:
            raise AudioError('Der Audiodienst hat die Anfrage abgelehnt.')
        if not isinstance(result.stdout, str) or len(result.stdout) > MAX_OUTPUT:
            raise AudioError('Der Audiodienst liefert zu viele oder ungültige Daten.')
        return result.stdout

    def _json(self, *args):
        try:
            return json.loads(self._command('--format=json', *args))
        except (ValueError, TypeError):
            raise AudioError('Der Audiodienst liefert kein gültiges JSON.') from None

    def _refresh(self):
        """Serial caller; publish is separate so callbacks stay in the worker."""
        try:
            info = self._json('info')
            if not isinstance(info, dict):
                raise AudioError('Die Audio-Serverdaten sind ungültig.')
            sinks = _items(self._json('list', 'sinks'))
            sources = _items(self._json('list', 'sources'), sources=True)
            sink = next((item for item in sinks if item['name'] == info.get('default_sink_name')), None)
            source = next((item for item in sources if item['name'] == info.get('default_source_name')), None)
            return self._store({'available': True, 'error': '', 'sinks': sinks, 'sources': sources,
                                'default_sink': sink['id'] if sink else None,
                                'default_source': source['id'] if source else None,
                                'volume_percent': sink['volume_percent'] if sink else None,
                                'output_muted': sink['muted'] if sink else None,
                                'mic_muted': source['muted'] if source else None,
                                'updated_at': time.monotonic()})
        except AudioError as error:
            self._store(_empty(str(error)))
            raise

    def _current(self, kind):
        state = self._refresh()
        index = state['default_' + kind]
        if index is None:
            raise AudioError('Kein Audioausgang verfügbar.' if kind == 'sink' else 'Kein Mikrofoneingang verfügbar.', state)
        return state, index

    def _action(self, command, kind, value):
        with self._io_lock:
            _, index = self._current(kind)
            try:
                self._command(command, str(index), value)
                return self._refresh()
            except AudioError as error:
                self._action_error(error)
            finally:
                self._wake()

    def _action_error(self, error):
        try:
            current = self._refresh()
        except AudioError:
            current = self.snapshot()
        current['error'] = str(error)
        self._store(current)
        raise AudioError(str(error), current) from None

    def set_volume(self, percent):
        if (isinstance(percent, bool) or not isinstance(percent, (int, float))
                or not 0 <= percent <= 100 or not math.isfinite(percent)):
            raise AudioError('Die Lautstärke muss zwischen 0 und 100 % liegen.')
        return self._action('set-sink-volume', 'sink', format(percent, '.2f') + '%')

    def set_muted(self, muted):
        if not isinstance(muted, bool):
            raise AudioError('Der Stummstatus muss wahr oder falsch sein.')
        return self._action('set-sink-mute', 'sink', '1' if muted else '0')

    def set_mic_muted(self, muted):
        if not isinstance(muted, bool):
            raise AudioError('Der Mikrofonstatus muss wahr oder falsch sein.')
        return self._action('set-source-mute', 'source', '1' if muted else '0')

    def _toggle(self, kind):
        with self._io_lock:
            state, index = self._current(kind)
            field = 'output_muted' if kind == 'sink' else 'mic_muted'
            if not isinstance(state[field], bool):
                raise AudioError('Der aktuelle Stummstatus ist nicht auslesbar.', state)
            try:
                self._command('set-' + kind + '-mute', str(index), '0' if state[field] else '1')
                return self._refresh()
            except AudioError as error:
                self._action_error(error)
            finally:
                self._wake()

    def toggle_output_mute(self):
        return self._toggle('sink')

    def toggle_mic_mute(self):
        return self._toggle('source')

    def _set_default(self, kind, index):
        index = _id(index)
        with self._io_lock:
            state = self._refresh()
            if index not in {item['id'] for item in state[kind + 's']}:
                raise AudioError('Das ausgewählte Audiogerät ist nicht verfügbar.', state)
            try:
                self._command('set-default-' + kind, str(index))
                streams = self._json('list', 'sink-inputs' if kind == 'sink' else 'source-outputs')
                if not isinstance(streams, list) or len(streams) > MAX_DEVICES:
                    raise AudioError('Default gewechselt; aktive Audiostreams konnten nicht gelesen werden.')
                movable = []
                physical_sources = {item['id'] for item in state['sources']}
                for stream in streams:
                    if not isinstance(stream, dict):
                        raise AudioError('Default gewechselt; ein Audiostream meldet ungültige Daten.')
                    if stream.get('corked') is True:
                        continue
                    origin = _id(stream.get(kind))
                    if origin == index or kind == 'source' and origin not in physical_sources:
                        continue
                    movable.append(_id(stream.get('index')))
                failed = max(0, len(movable) - MAX_MOVES)
                until = time.monotonic() + MOVE_BUDGET
                for stream in movable[:MAX_MOVES]:
                    if time.monotonic() >= until:
                        failed += 1
                        continue
                    try:
                        self._command('move-sink-input' if kind == 'sink' else 'move-source-output',
                                      str(stream), str(index))
                    except AudioError:
                        failed += 1
                current = self._refresh()
                if current['default_' + kind] != index:
                    raise AudioError('Der neue Audio-Default wurde nicht bestätigt.')
                if failed:
                    raise AudioError(f'Default gewechselt; {failed} aktive Audiostreams konnten nicht umgezogen werden.')
                return current
            except AudioError as error:
                self._action_error(error)
            finally:
                self._wake()

    def set_default_sink(self, index):
        return self._set_default('sink', index)

    def set_default_source(self, index):
        return self._set_default('source', index)

    def _wake(self):
        with self._lock:
            fd = self._wake_write
        if fd is not None:
            try:
                os.write(fd, b'x')
            except OSError:
                pass

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self
            self._stop.clear()
            self._published = None
            self._wake_read, self._wake_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
            self._thread = threading.Thread(target=self._run, name='restlos-air-audio', daemon=True)
            self._thread.start()
        return self

    def stop(self, join_timeout=3.0):
        self._stop.set()
        self._wake()
        with self._lock:
            process, worker = self._process, self._thread
        if process:
            try:
                process.terminate()
            except OSError:
                pass
        if worker and worker is not threading.current_thread():
            worker.join(timeout=join_timeout)

    def _publish(self):
        current = self.snapshot()
        compared = {key: value for key, value in current.items() if key != 'updated_at'}
        if compared == self._published:
            return
        self._published = deepcopy(compared)
        if self.callback and not self._stop.is_set():
            try:
                self.callback(current)
            except Exception:
                # A broken UI callback cannot orphan the subscriber process.
                pass

    @staticmethod
    def _terminate(process):
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=.5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except OSError:
            pass
        if process.stdout:
            process.stdout.close()

    def _listen(self, process):
        buffer = b''
        first = deadline = None
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, 'events')
            selector.register(self._wake_read, selectors.EVENT_READ, 'wake')
            while not self._stop.is_set():
                timeout = max(0, deadline - time.monotonic()) if deadline is not None else None
                for key, _ in selector.select(timeout):
                    try:
                        chunk = os.read(key.fd, 4096)
                    except BlockingIOError:
                        continue
                    if key.data == 'events':
                        if not chunk:
                            raise AudioError('Die Verbindung zum Audiodienst wurde getrennt.')
                        buffer = (buffer + chunk)[-16384:]
                        lines = buffer.split(b'\n')
                        buffer = lines.pop()
                        dirty = any(_EVENT.search(line.decode('utf-8', errors='replace')) for line in lines)
                    else:
                        dirty = bool(chunk)
                    if dirty:
                        now = time.monotonic()
                        first = now if first is None else first
                        deadline = min(now + DEBOUNCE, first + MAX_DEBOUNCE)
                if self._stop.is_set():
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    first = deadline = None
                    with self._io_lock:
                        self._refresh()
                    self._publish()

    def _run(self):
        backoff = 2.0
        try:
            if not self._pactl:
                self._publish()
                return
            while not self._stop.is_set():
                process = None
                try:
                    process = subprocess.Popen([self._pactl, 'subscribe'], stdin=subprocess.DEVNULL,
                                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                               env={**os.environ, 'LC_ALL': 'C'}, bufsize=0,
                                               start_new_session=True)
                    with self._lock:
                        self._process = process
                    if self._stop.is_set():
                        break
                    with self._io_lock:
                        self._refresh()
                    self._publish()
                    backoff = 2.0
                    self._listen(process)
                except (AudioError, OSError, ValueError) as error:
                    self._store(_empty(str(error) if isinstance(error, AudioError)
                                       else 'Der Audiodienst ist nicht erreichbar.'))
                    self._publish()
                finally:
                    self._terminate(process)
                    with self._lock:
                        self._process = None
                if not self._stop.is_set():
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 15)
        finally:
            with self._lock:
                descriptors = (self._wake_read, self._wake_write)
                self._wake_read = self._wake_write = None
            for fd in descriptors:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
