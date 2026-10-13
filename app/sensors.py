"""Read-only hwmon adapter. Never retains volatile hwmonN identifiers."""
from datetime import datetime, timezone
import math
from pathlib import Path


def number(path):
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def discover(root=Path('/sys/class/hwmon')):
    found = {}
    for path in sorted(root.glob('hwmon*')):
        try:
            found[path.joinpath('name').read_text().strip()] = path
        except OSError:
            continue
    return found


def snapshot(root=Path('/sys/class/hwmon'), include_curves=True, include_fans=True):
    devices = discover(root)
    def temperature(device, field):
        p = devices.get(device)
        value = number(p / field) if p else None
        return value / 1000 if value is not None else None
    result = {'cpu': temperature('k10temp', 'temp1_input'),
              'gpu': temperature('amdgpu', 'temp1_input'),
              'hotspot': temperature('amdgpu', 'temp2_input'),
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
