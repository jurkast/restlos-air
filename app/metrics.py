"""Small read-only Linux sampler; no processes, timers or hardware changes.

CPU is busy time / total time between samples. The first eight /proc/stat
columns are counted once; guest columns are already part of user/nice.
Idle and I/O wait are excluded from busy time, steal is counted once.
DRM discovery happens once when the sampler is created.
"""
from pathlib import Path
import re


def _number(path):
    try:
        value = int(path.read_text(encoding='ascii').strip())
        return value if value >= 0 else None
    except (OSError, UnicodeError, ValueError):
        return None


class MetricsSampler:
    def __init__(self, proc_root=Path('/proc'), drm_root=Path('/sys/class/drm')):
        self.proc_root = Path(proc_root)
        self.drm_root = Path(drm_root)
        self._previous_cpu = None
        self.gpu_device = self._discover_gpu()

    @staticmethod
    def _amdgpu(device):
        try:
            return (device / 'driver').resolve(strict=True).name == 'amdgpu'
        except (OSError, RuntimeError):
            try:
                return 'DRIVER=amdgpu' in (device / 'uevent').read_text(encoding='ascii').splitlines()
            except (OSError, UnicodeError):
                return False

    def _discover_gpu(self):
        try:
            nodes = list(self.drm_root.iterdir())
        except OSError:
            return None
        candidates = {}
        for node in nodes:
            match = re.fullmatch(r'(card|renderD)([0-9]+)', node.name)
            if match is None:
                continue
            try:
                device = (node / 'device').resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not self._amdgpu(device):
                continue
            supported = sum((device / name).is_file() for name in
                            ('gpu_busy_percent', 'mem_info_vram_total'))
            order = (-supported, -int(_number(device / 'boot_vga') == 1),
                     0 if match[1] == 'card' else 1, int(match[2]), str(device))
            if device not in candidates or order < candidates[device]:
                candidates[device] = order
        return min(candidates, key=candidates.get) if candidates else None

    def _cpu(self):
        try:
            with (self.proc_root / 'stat').open(encoding='ascii') as stream:
                fields = stream.readline().split()
            if not fields or fields[0] != 'cpu' or len(fields) < 5:
                raise ValueError()
            counters = tuple(int(value) for value in fields[1:9])
            counters += (0,) * (8 - len(counters))
            if any(value < 0 for value in counters):
                raise ValueError()
        except (OSError, UnicodeError, ValueError):
            self._previous_cpu = None
            return None
        previous, self._previous_cpu = self._previous_cpu, counters
        if previous is None:
            return None
        delta = tuple(current - old for current, old in zip(counters, previous))
        # A reset or an unreliable decreasing iowait sample needs a new baseline.
        if any(value < 0 for value in delta):
            return None
        total = sum(delta)
        if total <= 0:
            return None
        idle = delta[3] + delta[4]
        return (total - idle) / total * 100.0

    def _ram(self):
        wanted = {'MemTotal', 'MemAvailable', 'MemFree', 'Buffers', 'Cached',
                  'SReclaimable', 'Shmem'}
        values = {}
        try:
            with (self.proc_root / 'meminfo').open(encoding='ascii') as stream:
                for line in stream:
                    key, separator, raw = line.partition(':')
                    if not separator or key not in wanted:
                        continue
                    fields = raw.split()
                    try:
                        number = int(fields[0])
                        valid = len(fields) == 2 and fields[1] == 'kB' and number >= 0
                        values[key] = number * 1024 if valid else None
                    except (IndexError, ValueError):
                        values[key] = None
        except (OSError, UnicodeError):
            return None, None, None
        total = values.get('MemTotal')
        if total is None or total <= 0:
            return None, None, None
        if 'MemAvailable' in values:
            available = values['MemAvailable']
        elif values.get('MemFree') is not None:
            extras = [values.get(name, 0) for name in ('Buffers', 'Cached', 'SReclaimable', 'Shmem')]
            available = (values['MemFree'] + extras[0] + extras[1] + extras[2] - extras[3]
                         if all(value is not None for value in extras) else None)
        else:
            available = None
        if available is None or not 0 <= available <= total:
            return None, total, None
        used = total - available
        return used, total, used / total * 100.0

    def _gpu(self):
        if self.gpu_device is None:
            return None, None, None, None
        busy = _number(self.gpu_device / 'gpu_busy_percent')
        if busy is not None and busy > 100:
            busy = None
        used = _number(self.gpu_device / 'mem_info_vram_used')
        total = _number(self.gpu_device / 'mem_info_vram_total')
        if total == 0:
            total = None
        if used is not None and total is not None and used > total:
            used = None
        percent = used / total * 100.0 if used is not None and total is not None else None
        return busy, used, total, percent

    def sample(self):
        """Return current metrics; unsupported or invalid readings are None.

        Byte values are integers and percentages are numbers in [0, 100].
        The first valid CPU sample establishes its baseline and returns None.
        """
        cpu = self._cpu()
        ram_used, ram_total, ram_percent = self._ram()
        gpu, vram_used, vram_total, vram_percent = self._gpu()
        return {'cpu_percent': cpu, 'gpu_percent': gpu,
                'ram_used_bytes': ram_used, 'ram_total_bytes': ram_total,
                'ram_percent': ram_percent, 'vram_used_bytes': vram_used,
                'vram_total_bytes': vram_total, 'vram_percent': vram_percent}
