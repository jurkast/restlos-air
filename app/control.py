"""Safe CoolerControl policy client; CoolerControl remains the sole hardware writer.

Call from a serial background executor. No polling threads or direct sysfs writes.
Every change journals the preceding setting before a daemon mutation. Temperature
guards run in the daemon and therefore keep running after the widget exits.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import urllib.parse
import uuid

from coolercontrol import BackendError, Client, profile, secret_store, validate_curve

MIN_DUTY = 40
GUARD_POINTS = [[20., 40], [75., 40], [85., 100]]
CONTROLLABLE = {'nct6798:1': 'fan1', 'nct6798:2': 'fan2', 'nct6798:3': 'fan3'}
LOCAL_KEYS = (*CONTROLLABLE, 'nct6798:6', 'amdgpu:1')


def _config_dir():
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'restlos-air'


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 160:
        raise BackendError('CoolerControl lieferte eine ungültige Gerätekennung.')
    return value


def _setting(settings, channel):
    entries = settings.get('settings') if isinstance(settings, dict) else None
    if not isinstance(entries, list):
        raise BackendError('Die gespeicherten Lüftereinstellungen sind nicht lesbar.')
    result = [s for s in entries if isinstance(s, dict) and s.get('channel_name') == channel]
    if len(result) > 1:
        raise BackendError('Mehrdeutige Lüftereinstellungen; Änderung abgebrochen.')
    return deepcopy(result[0]) if result else None


def _same_setting(actual, expected):
    # Older APIs include unused null fields; compare only the active speed kind.
    if actual is None or actual.get('profile_uid') == '0' or actual.get('reset_to_default'):
        return expected is None or expected.get('profile_uid') == '0' or expected.get('reset_to_default')
    if expected is None:
        return False
    for field in ('profile_uid', 'speed_fixed'):
        if actual.get(field) is not None:
            return actual.get(field) == expected.get(field)
    return False


class Controller:
    def __init__(self, client=None, state_dir=None, hwmon_root=Path('/sys/class/hwmon')):
        self.client = client
        self.state_dir = Path(state_dir) if state_dir is not None else _config_dir()
        self.state_file = self.state_dir / 'control-state.json'
        self.hwmon_root = Path(hwmon_root)
        self.lock = threading.RLock()
        self.state = self._load()
        self.last_status = {'connected': False, 'reason': 'Steuerung wird geprüft.',
                            'channels': {}, 'sources': []}
        self.devices = []
        self.sources = {}
        self.board = None

    def _load(self):
        try:
            if self.state_file.is_symlink():
                raise BackendError('Die Sicherungsdatei darf keine Verknüpfung sein.')
            with self.state_file.open('r', encoding='utf-8') as stream:
                data = json.loads(stream.read(1024 * 1024))
            if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('channels'), dict):
                raise ValueError()
            for key, value in data['channels'].items():
                if key not in CONTROLLABLE or not isinstance(value, dict):
                    raise ValueError()
                _identifier(value['device_uid'])
                if value.get('channel') != CONTROLLABLE[key]:
                    raise ValueError()
                for field in ('baseline', 'last', 'pending_previous'):
                    setting = value.get(field)
                    if setting is not None and (not isinstance(setting, dict) or setting.get('channel_name') != value['channel']):
                        raise ValueError()
                if not isinstance(value.get('owned_profiles', []), list):
                    raise ValueError()
            return data
        except FileNotFoundError:
            return {'version': 1, 'channels': {}}
        except (OSError, ValueError, KeyError, TypeError):
            raise BackendError('Die Sicherung der Lüftersteuerung ist beschädigt. Änderungen sind gesperrt.') from None

    def _save(self):
        try:
            if self.state_dir.is_symlink():
                raise OSError('Symlink')
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(prefix='.control-', dir=self.state_dir)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(self.state, stream, ensure_ascii=False, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, self.state_file)
                dfd = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except (OSError, ValueError):
            raise BackendError('Die vorherige Lüftereinstellung konnte nicht sicher gespeichert werden. Änderung abgebrochen.') from None

    def _client(self):
        if self.client is None:
            try:
                token = secret_store()
            except Exception:
                raise BackendError('Linux-Schlüsselbund entsperren und Steuerung verbinden.') from None
            if not token:
                raise BackendError('Steuerung mit CoolerControl verbinden.')
            self.client = Client(token)
        return self.client

    def reconnect(self):
        with self.lock:
            self.client = None
        return self.refresh()

    def _local_board(self):
        candidates = []
        for path in self.hwmon_root.glob('hwmon*'):
            try:
                if (path / 'name').read_text().strip() == 'nct6798':
                    candidates.append(path)
            except OSError:
                pass
        return candidates[0] if len(candidates) == 1 else None

    def _match_board(self, devices):
        local = self._local_board()
        if local is None:
            return None
        # Match physical sysfs identity, never a remembered volatile hwmon number.
        matches = []
        for device in devices:
            info = device.get('info') or {}
            if device.get('type') != 'Hwmon' or device.get('name') != 'nct6798':
                continue
            locations = (info.get('driver_info') or {}).get('locations') or []
            for location in locations:
                if isinstance(location, str) and location.startswith('/sys/'):
                    physical = Path(location).resolve()
                    if physical in (local.resolve(), (local / 'device').resolve()):
                        matches.append(device)
                        break
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _source(devices, kind, temp_name):
        matches = [d for d in devices if d.get('type') == kind and temp_name in (d.get('info') or {}).get('temps', {})]
        if len(matches) != 1:
            return None
        return {'device_uid': _identifier(matches[0]['uid']), 'temp_name': temp_name}

    def refresh(self):
        with self.lock:
            channels = {}
            for key in LOCAL_KEYS:
                channels[key] = {'controllable': False, 'reason': 'Steuerung nicht verfügbar.',
                                 'mode': 'default', 'duty': None, 'curve': None,
                                 'source': None, 'can_restore': False}
            channels['nct6798:6']['reason'] = 'Wasserpumpe · eigene Drehzahlanzeige, geschützte Regelung.'
            channels['amdgpu:1']['reason'] = 'Die Grafikkarte bietet derzeit keine freigegebene Regelung.'
            try:
                client = self._client()
                self.devices = client.devices()
                self.board = self._match_board(self.devices)
                self.sources = {}
                for name, kind in (('cpu', 'CPU'), ('gpu', 'GPU')):
                    source = self._source(self.devices, kind, 'temp1')
                    if source:
                        self.sources[name] = source
                if self.board:
                    uid = _identifier(self.board['uid'])
                    saved = client.request('/devices/' + urllib.parse.quote(uid, safe='') + '/settings')
                    infos = (self.board.get('info') or {}).get('channels', {})
                    for key, channel in CONTROLLABLE.items():
                        options = infos.get(channel, {}).get('speed_options') or {}
                        actual = _setting(saved, channel)
                        backup = self.state['channels'].get(key, {})
                        owned = backup.get('device_uid') == uid and _same_setting(actual, backup.get('last')) and bool(backup.get('last'))
                        controllable = options.get('fixed_enabled') is True and 'cpu' in self.sources
                        channels[key].update(controllable=controllable,
                            reason='' if controllable else 'CPU-Sensor oder regelbarer PWM-Anschluss fehlt.',
                            mode=backup.get('mode', 'curve') if owned else ('default' if actual is None or actual.get('profile_uid') == '0' else 'external'),
                            duty=backup.get('duty') if owned else None,
                            curve=deepcopy(backup.get('curve')) if owned else None,
                            source=backup.get('source') if owned else None,
                            can_restore=backup.get('device_uid') == uid,
                            actual_setting=actual,
                            min_duty=max(MIN_DUTY, options.get('min_duty', MIN_DUTY)),
                            max_duty=options.get('max_duty', 100))
                else:
                    for key in CONTROLLABLE:
                        channels[key]['reason'] = 'Mainboard-Sensortreiber laden und CoolerControl neu starten.'
                self.last_status = {'connected': True, 'reason': '', 'channels': channels,
                                    'sources': list(self.sources), 'mainboard': bool(self.board)}
            except (BackendError, KeyError, TypeError, ValueError) as exc:
                self.last_status = {'connected': False, 'reason': str(exc) if isinstance(exc, BackendError) else 'CoolerControl-Gerätedaten sind ungültig.',
                                    'channels': channels, 'sources': [], 'mainboard': False}
            return deepcopy(self.last_status)

    def status(self):
        with self.lock:
            return deepcopy(self.last_status)

    def _ready(self, key):
        status = self.refresh()
        if key not in CONTROLLABLE:
            raise BackendError('Dieser Anschluss ist für Änderungen gesperrt.')
        if not status['connected']:
            raise BackendError(status['reason'])
        item = status['channels'][key]
        if not item['controllable']:
            raise BackendError(item['reason'])
        # Fresh temperatures are required before engaging software control. The daemon
        # handles future missing readings at emergency temperature / maximum duty.
        data = self._client().request('/status')
        if not isinstance(data, dict) or not isinstance(data.get('devices'), list):
            raise BackendError('Aktuelle Temperaturen sind nicht verfügbar.')
        for source in self.sources.values():
            rows = [d for d in data['devices'] if d.get('uid') == source['device_uid']]
            if len(rows) != 1 or not rows[0].get('status_history'):
                raise BackendError('Ein Temperatursensor meldet keine aktuellen Werte.')
            row = rows[0]['status_history'][-1]
            try:
                stamp = datetime.fromisoformat(row['timestamp'])
                age = (datetime.now(timezone.utc) - stamp).total_seconds()
                values = [t['temp'] for t in row.get('temps', []) if t.get('name') == source['temp_name']]
                if len(values) != 1 or not math.isfinite(values[0]) or not -5 <= values[0] <= 150 or not -5 <= age <= 10:
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                raise BackendError('Ein Temperatursensor meldet veraltete oder ungültige Werte.') from None
        uid = _identifier(self.board['uid'])
        return uid, CONTROLLABLE[key], item

    def _graphs(self, points, selected, label):
        graphs = []
        for name, source in self.sources.items():
            curve = points if selected in (name, 'max') else GUARD_POINTS
            graphs.append(profile(curve, source, 'Restlos Air · ' + label + ' · ' + name.upper()))
        if len(graphs) == 1:
            return graphs, graphs[0]['uid']
        mixed = {'uid': str(uuid.uuid4()), 'name': 'Restlos Air · ' + label,
                 'function_uid': '0', 'p_type': 'Mix',
                 'member_profile_uids': [p['uid'] for p in graphs], 'mix_function_type': 'Max'}
        return graphs + [mixed], mixed['uid']

    def apply_manual(self, key, duty):
        if isinstance(duty, bool) or not isinstance(duty, (int, float)) or not math.isfinite(duty) or int(duty) != duty or not MIN_DUTY <= duty <= 100:
            raise BackendError('Die manuelle Leistung muss eine ganze Zahl zwischen 40 und 100 % sein.')
        points = [[20., int(duty)], [75., int(duty)], [85., 100]]
        return self._apply(key, points, 'max', 'manual', int(duty))

    def apply_curve(self, key, points, source='cpu'):
        try:
            points = validate_curve(points)
        except (TypeError, ValueError, IndexError):
            raise BackendError('Ungültige Kennlinie. Bitte Temperatur-/Leistungspaare eingeben.') from None
        if source not in ('cpu', 'gpu', 'max'):
            raise BackendError('Unbekannte Temperaturquelle.')
        return self._apply(key, points, source, 'curve', None)

    def _apply(self, key, points, source, mode, duty):
        with self.lock:
            uid, channel, info = self._ready(key)
            if source == 'gpu' and 'gpu' not in self.sources:
                raise BackendError('Der GPU-Temperatursensor ist nicht verfügbar.')
            if any(d < info['min_duty'] or d > info['max_duty'] for _, d in points) or info['max_duty'] < 100:
                raise BackendError('Diese Leistung liegt außerhalb des freigegebenen Anschlussbereichs.')
            client = self._client()
            previous = info['actual_setting']
            before_journal = deepcopy(self.state)
            entry = self.state['channels'].get(key)
            if entry and entry['device_uid'] != uid:
                raise BackendError('Die Mainboard-Gerätekennung hat sich geändert. Vorherige Sicherung zuerst prüfen.')
            if entry and entry.get('pending_target'):
                raise BackendError('Eine frühere Änderung ist unbestätigt. Zuerst „Zurück zur Automatik“ verwenden.')
            if not entry:
                entry = {'device_uid': uid, 'channel': channel, 'baseline': previous,
                         'owned_profiles': [], 'last': None}
                self.state['channels'][key] = entry
            profiles, target_uid = self._graphs(points, source, channel + (' · Manuell' if mode == 'manual' else ' · Kennlinie'))
            target = {'channel_name': channel, 'profile_uid': target_uid}
            # Journal all intended profile IDs before requests, including requests whose
            # response could be lost after the daemon committed the transaction.
            entry.update(pending_previous=previous, pending_target=target,
                         pending_profiles=[p['uid'] for p in profiles])
            try:
                self._save()
            except BackendError:
                self.state = before_journal
                raise
            try:
                for p in profiles:
                    client.request('/profiles', 'POST', p)
                client.request(client.path(uid, channel) + '/profile', 'PUT', {'profile_uid': target_uid})
                actual = _setting(client.request('/devices/' + urllib.parse.quote(uid, safe='') + '/settings'), channel)
                if not _same_setting(actual, target):
                    raise BackendError('CoolerControl hat die angewendete Kennlinie nicht bestätigt.')
            except BackendError as exc:
                try:
                    actual = _setting(client.request('/devices/' + urllib.parse.quote(uid, safe='') + '/settings'), channel)
                    if _same_setting(actual, target):
                        self._restore_setting(uid, channel, previous)
                    elif not _same_setting(actual, previous):
                        raise BackendError('Andere Lüftereinstellung erkannt.')
                    self._clear_pending(entry)
                    entry['owned_profiles'].extend(p['uid'] for p in profiles)
                    self._save()
                    self._cleanup(entry)
                except BackendError:
                    raise BackendError(str(exc) + ' Der Zustand ist unbestätigt; „Zurück zur Automatik“ stellt die Sicherung wieder her.') from None
                raise exc
            entry['owned_profiles'].extend(p['uid'] for p in profiles)
            pending_state = deepcopy(self.state)
            entry.update(last=target, mode=mode, duty=duty, curve=deepcopy(points), source=source)
            self._clear_pending(entry)
            try:
                self._save()
            except BackendError:
                # The applied profile is safe, but do not leave a change whose local
                # commit failed. Keep the durable pending journal for recovery even
                # if this rollback's response or second disk write also fails.
                self.state = pending_state
                entry = self.state['channels'][key]
                try:
                    self._restore_setting(uid, channel, previous)
                    self._clear_pending(entry)
                    self._save()
                except BackendError:
                    raise BackendError('Die Lüfteränderung konnte nicht dauerhaft bestätigt werden. „Zurück zur Automatik“ stellt die gespeicherte Einstellung wieder her.') from None
                self._cleanup(entry)
                raise BackendError('Die Sicherung konnte nicht abgeschlossen werden. Die vorherige Lüftereinstellung wurde wiederhergestellt.') from None
            self._cleanup(entry)
            return self.refresh()

    @staticmethod
    def _clear_pending(entry):
        for field in ('pending_previous', 'pending_target', 'pending_profiles'):
            entry.pop(field, None)

    def _restore_setting(self, uid, channel, setting):
        client = self._client()
        path = client.path(uid, channel)
        if setting is None or setting.get('reset_to_default') or setting.get('profile_uid') == '0':
            client.request(path + '/reset', 'PUT')
        elif setting.get('profile_uid') is not None:
            client.request(path + '/profile', 'PUT', {'profile_uid': setting['profile_uid']})
        elif setting.get('speed_fixed') is not None:
            client.request(path + '/manual', 'PUT', {'speed_fixed': setting['speed_fixed']})
        else:
            raise BackendError('Die ursprüngliche Einstellung ist nicht wiederherstellbar.')
        actual = _setting(client.request('/devices/' + urllib.parse.quote(uid, safe='') + '/settings'), channel)
        if not _same_setting(actual, setting):
            raise BackendError('Die wiederhergestellte Einstellung wurde nicht bestätigt.')

    def restore(self, key):
        with self.lock:
            if key not in CONTROLLABLE:
                raise BackendError('Dieser Anschluss ist für Änderungen gesperrt.')
            status = self.refresh()
            if not status['connected'] or not self.board:
                raise BackendError(status['reason'] or 'Mainboard nicht verfügbar.')
            entry = self.state['channels'].get(key)
            if not entry:
                return status
            if entry['device_uid'] != self.board['uid']:
                raise BackendError('Die Mainboard-Gerätekennung stimmt nicht mit der Sicherung überein.')
            # Explicit Restore is allowed even with missing temperatures, since it hands
            # control back to the saved daemon setting / firmware startup mode.
            self._restore_setting(entry['device_uid'], entry['channel'], entry['baseline'])
            entry['owned_profiles'].extend(entry.get('pending_profiles', []))
            self._clear_pending(entry)
            entry.update(last=None, mode='default', duty=None, curve=None, source=None)
            self._save()
            self._cleanup(entry)
            return self.refresh()

    def _cleanup(self, entry):
        """Remove only unused profiles we created. Never touch an external reference."""
        owned = set(entry.get('owned_profiles', []))
        if not owned:
            return
        try:
            client = self._client()
            data = client.request('/profiles')
            profiles = data['profiles']
            if not isinstance(profiles, list):
                return
            live = {p['uid']: p for p in profiles}
            used = set()
            for device in self.devices:
                settings = client.request('/devices/' + urllib.parse.quote(device['uid'], safe='') + '/settings')
                for s in settings.get('settings', []):
                    if s.get('profile_uid'):
                        used.add(s['profile_uid'])
            # Protect candidates referenced by any non-owned profile, and recursively
            # protect members of every active/protected profile.
            for p in profiles:
                if p['uid'] not in owned:
                    used.update(p.get('member_profile_uids', []))
            frontier = list(used)
            while frontier:
                for member in live.get(frontier.pop(), {}).get('member_profile_uids', []):
                    if member not in used:
                        used.add(member)
                        frontier.append(member)
            unused = owned - used
            # Parents first: deleting members of a still-existing Mix is rejected.
            ordered = sorted(unused, key=lambda p: bool(live.get(p, {}).get('member_profile_uids')), reverse=True)
            for pid in ordered:
                if pid in live:
                    if not live[pid].get('name', '').startswith('Restlos Air · '):
                        continue
                    client.request('/profiles/' + urllib.parse.quote(pid, safe=''), 'DELETE')
                owned.discard(pid)
            entry['owned_profiles'] = sorted(owned)
            self._save()
        except (BackendError, KeyError, TypeError):
            # Cleanup is advisory; a successfully verified cooling setting remains valid.
            pass
