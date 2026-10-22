"""Read-only hwmon adapter. Never retains volatile hwmonN identifiers."""
from datetime import datetime, timezone
import math
from pathlib import Path
import time

CPU_SENSORS = (('k10temp', 'temp1_input'), ('zenpower', 'temp1_input'), ('coretemp', 'temp1_input'))


def number(path):
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


_CACHE = {}
_CACHE_SECONDS = 30


def _score(path, name):
    """Prefer the discrete GPU when an APU exposes a second amdgpu sensor."""
    if name == 'amdgpu':
        return (path.joinpath('fan1_input').exists(), path.joinpath('temp2_input').exists())
    return (True,)


def discover(root=Path('/sys/class/hwmon'), now=None):
    """Map sensor names to hwmon paths; re-read names only every 30 seconds.

    hwmonN numbers can change after a reboot, so nothing is persisted. Duplicate
    names keep the most capable device (dGPU instead of APU) and all NVMe drives
    are kept separately under ``nvme*``.
    """
    root = Path(root)
    now = time.monotonic() if now is None else now
    cached = _CACHE.get(root)
    if cached and now - cached[0] < _CACHE_SECONDS and all(p.exists() for p in cached[1].values()):
        return cached[1]
    found, drives = {}, []
    for path in sorted(root.glob('hwmon*')):
        try:
            name = path.joinpath('name').read_text().strip()
        except OSError:
            continue
        if name == 'nvme':
            drives.append(path)
        elif name not in found or _score(path, name) > _score(found[name], name):
            found[name] = path
    for index, path in enumerate(drives):
        found['nvme' if index == 0 else f'nvme{index}'] = path
    _CACHE[root] = (now, found)
    return found


def snapshot(root=Path('/sys/class/hwmon'), include_curves=True, include_fans=True):
    devices = discover(root)
    def temperature(device, field):
        p = devices.get(device)
        value = number(p / field) if p else None
        return value / 1000 if value is not None else None
    cpu = next((value for device, field in CPU_SENSORS
                if device in devices and (value := temperature(device, field)) is not None), None)
    drives = [value for name in devices if name.startswith('nvme')
              if (value := temperature(name, 'temp1_input')) is not None]
    result = {'cpu': cpu,
              'gpu': temperature('amdgpu', 'temp1_input'),
              'hotspot': temperature('amdgpu', 'temp2_input'),
              'ssd': max(drives) if drives else None,
              'fans': {}, 'mainboard': 'nct6798' in devices}
    if not include_fans:
        return result
    for chip in ['nct6798', 'amdgpu']:
        p = devices.get(chip)
        if p is None:
            continue
        for f in sorted(p.glob('fan*_input')):
            index = f.name[3:].split('_')[0]
            key = f'{chip}:{index}'
            points = []
            for t in (sorted(p.glob(f'pwm{index}_auto_point*_temp')) if include_curves else []):
                temp = number(t)
                pwm = number(t.with_name(t.name[:-4] + 'pwm'))
                if temp is not None and pwm is not None:
                    points.append((temp / 1000, pwm / 255 * 100))
            source_index = number(p / f'pwm{index}_temp_sel')
            source = None
            if source_index is not None:
                try:
                    source = (p / f'temp{source_index}_label').read_text().strip()
                except OSError:
                    source = f'Temperaturkanal {source_index}'
            result['fans'][key] = {
                'rpm': number(f), 'pwm': number(p / f'pwm{index}'),
                'mode': number(p / f'pwm{index}_enable'),
                'source': source, 'curve': sorted(points),
                'channel': index, 'chip': chip,
            }
    return result


def cached_pump(status_data, board_uid, now=None):
    """Read a fresh, uniquely identified pump RPM from CoolerControl's cache."""
    if not isinstance(status_data, dict) or not isinstance(board_uid, str) or not board_uid:
        return None
    devices = status_data.get('devices')
    if not isinstance(devices, list):
        return None
    matches = [device for device in devices
               if isinstance(device, dict) and device.get('uid') == board_uid]
    if len(matches) != 1:
        return None
    history = matches[0].get('status_history')
    if not isinstance(history, list) or not history or not isinstance(history[-1], dict):
        return None
    latest = history[-1]
    timestamp = latest.get('timestamp')
    if not isinstance(timestamp, str):
        return None
    try:
        sample_time = datetime.fromisoformat(timestamp)
        current_time = datetime.now(timezone.utc) if now is None else now
        if (sample_time.tzinfo is None or not isinstance(current_time, datetime)
                or current_time.tzinfo is None):
            return None
        age = (current_time.astimezone(timezone.utc)
               - sample_time.astimezone(timezone.utc)).total_seconds()
        if not -5 <= age <= 10:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    channels = latest.get('channels')
    if not isinstance(channels, list):
        return None
    pumps = [channel for channel in channels
             if isinstance(channel, dict) and channel.get('name') == 'fan6']
    if len(pumps) != 1:
        return None
    rpm = pumps[0].get('rpm')
    if isinstance(rpm, bool) or not isinstance(rpm, (int, float)):
        return None
    try:
        return rpm if math.isfinite(rpm) and rpm >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None
