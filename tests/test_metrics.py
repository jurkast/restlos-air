"""Hardware-free fixtures for Linux counters and DRM identity selection."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from metrics import MetricsSampler


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / 'proc'
        self.drm = self.root / 'drm'
        self.proc.mkdir()
        self.drm.mkdir()

    def sampler(self):
        return MetricsSampler(proc_root=self.proc, drm_root=self.drm, sys_root=self.proc / "no-sys")

    def stat(self, values):
        (self.proc / 'stat').write_text('cpu ' + ' '.join(map(str, values)) + '\ncpu0 999 999 999 999\n')

    def mem(self, **values):
        (self.proc / 'meminfo').write_text(''.join(f'{key}: {value} kB\n' for key, value in values.items()))

    def gpu(self, name='card0', driver='amdgpu', boot=0, physical=None, **metrics):
        device = physical or self.root / ('pci-' + name)
        device.mkdir(exist_ok=True)
        driver_dir = self.root / 'drivers' / driver
        driver_dir.mkdir(parents=True, exist_ok=True)
        if not (device / 'driver').exists():
            (device / 'driver').symlink_to(driver_dir, target_is_directory=True)
        (device / 'boot_vga').write_text(str(boot))
        for key, value in metrics.items():
            (device / key).write_text(str(value))
        node = self.drm / name
        node.mkdir()
        (node / 'device').symlink_to(device, target_is_directory=True)
        return device

    def test_absent_data_is_none_and_keys_are_stable(self):
        result = self.sampler().sample()
        self.assertEqual(set(result), {'cpu_percent', 'gpu_percent', 'cpu_clock_mhz', 'disk_read_bytes_sec', 'disk_write_bytes_sec', 'ram_used_bytes',
                         'ram_total_bytes', 'ram_percent', 'vram_used_bytes',
                         'vram_total_bytes', 'vram_percent'})
        self.assertTrue(all(value is None for value in result.values()))

    def test_cpu_delta_guest_not_double_counted_and_iowait_idle(self):
        self.stat([100, 20, 30, 400, 10, 4, 6, 5, 50, 10])
        sampler = self.sampler()
        self.assertIsNone(sampler.sample()['cpu_percent'])
        # 40 busy + 60 idle/wait = 100. Guests are already in user/nice.
        self.stat([120, 25, 35, 450, 20, 6, 9, 10, 65, 15])
        self.assertAlmostEqual(sampler.sample()['cpu_percent'], 40.0)
        self.assertIsNone(sampler.sample()['cpu_percent'])

    def test_cpu_reset_and_failed_sample_need_fresh_baseline(self):
        self.stat([100, 0, 0, 100, 20, 0, 0, 0])
        sampler = self.sampler()
        sampler.sample()
        self.stat([5, 0, 0, 5, 1, 0, 0, 0])
        self.assertIsNone(sampler.sample()['cpu_percent'])
        self.stat([10, 0, 0, 10, 1, 0, 0, 0])
        self.assertEqual(sampler.sample()['cpu_percent'], 50.0)
        (self.proc / 'stat').write_text('cpu broken\n')
        self.assertIsNone(sampler.sample()['cpu_percent'])
        self.stat([20, 0, 0, 20, 1, 0, 0, 0])
        self.assertIsNone(sampler.sample()['cpu_percent'])

    def test_cpu_decreasing_iowait_and_valid_zero_busy(self):
        self.stat([10, 0, 0, 100, 20, 0, 0, 0])
        sampler = self.sampler()
        sampler.sample()
        self.stat([15, 0, 0, 110, 19, 0, 0, 0])
        self.assertIsNone(sampler.sample()['cpu_percent'])
        self.stat([15, 0, 0, 120, 20, 0, 0, 0])
        self.assertEqual(sampler.sample()['cpu_percent'], 0.0)

    def test_ram_uses_available_and_reports_bytes(self):
        self.mem(MemTotal=1000, MemAvailable=400, MemFree=10, Cached=900)
        result = self.sampler().sample()
        self.assertEqual(result['ram_total_bytes'], 1000 * 1024)
        self.assertEqual(result['ram_used_bytes'], 600 * 1024)
        self.assertEqual(result['ram_percent'], 60.0)

    def test_ram_fallback_reclaimable_cache_excludes_shared_memory(self):
        self.mem(MemTotal=1000, MemFree=100, Buffers=50, Cached=200, SReclaimable=60, Shmem=10)
        self.assertEqual(self.sampler().sample()['ram_used_bytes'], 600 * 1024)
        self.mem(MemTotal=1000, MemFree=100)
        self.assertEqual(self.sampler().sample()['ram_percent'], 90.0)

    def test_ram_invalid_or_missing_available_is_not_fake_zero(self):
        self.mem(MemTotal=1000, MemAvailable='broken', MemFree=100)
        result = self.sampler().sample()
        self.assertEqual(result['ram_total_bytes'], 1000 * 1024)
        self.assertIsNone(result['ram_used_bytes'])
        self.mem(MemTotal=1000, MemAvailable=1001)
        self.assertIsNone(self.sampler().sample()['ram_percent'])
        self.mem(MemTotal=0, MemAvailable=0)
        self.assertIsNone(self.sampler().sample()['ram_total_bytes'])
        self.mem(MemTotal=1000)
        self.assertIsNone(self.sampler().sample()['ram_used_bytes'])

    def test_select_amdgpu_instead_of_card_zero_and_read_vram(self):
        self.gpu('card0', driver='i915', gpu_busy_percent=99)
        selected = self.gpu('card1', boot=1, gpu_busy_percent=7,
                            mem_info_vram_used=1024, mem_info_vram_total=4096)
        sampler = self.sampler()
        self.assertEqual(sampler.gpu_device, selected)
        result = sampler.sample()
        self.assertEqual(result['gpu_percent'], 7)
        self.assertEqual(result['vram_used_bytes'], 1024)
        self.assertEqual(result['vram_total_bytes'], 4096)
        self.assertEqual(result['vram_percent'], 25.0)

    def test_render_only_node_and_duplicate_aliases(self):
        device = self.gpu('renderD128', gpu_busy_percent=0,
                          mem_info_vram_used=0, mem_info_vram_total=2048)
        self.gpu('card0', physical=device)
        sampler = self.sampler()
        self.assertEqual(sampler.gpu_device, device)
        self.assertEqual(sampler.sample()['gpu_percent'], 0)
        self.assertEqual(sampler.sample()['vram_percent'], 0.0)
        (self.drm / 'card0' / 'device').unlink()
        (self.drm / 'card0').rmdir()
        self.assertEqual(self.sampler().gpu_device, device)

    def test_multiple_devices_deterministic_boot_gpu_then_numeric_card(self):
        self.gpu('card10', gpu_busy_percent=10)
        card2 = self.gpu('card2', gpu_busy_percent=2)
        self.assertEqual(self.sampler().gpu_device, card2)
        (self.root / 'pci-card10' / 'boot_vga').write_text('1')
        self.assertEqual(self.sampler().gpu_device, self.root / 'pci-card10')

    def test_discovery_happens_only_at_construction_and_missing_attrs_stay_none(self):
        sampler = self.sampler()
        self.gpu('card0', gpu_busy_percent=8)
        self.assertIsNone(sampler.sample()['gpu_percent'])
        sampler = self.sampler()
        self.assertEqual(sampler.sample()['gpu_percent'], 8)
        (sampler.gpu_device / 'gpu_busy_percent').unlink()
        self.assertIsNone(sampler.sample()['gpu_percent'])
        self.assertIsNone(sampler.sample()['vram_used_bytes'])

    def test_gpu_invalid_ranges_preserve_other_valid_metrics(self):
        device = self.gpu(gpu_busy_percent=101, mem_info_vram_used=1100, mem_info_vram_total=1000)
        sampler = self.sampler()
        result = sampler.sample()
        self.assertIsNone(result['gpu_percent'])
        self.assertIsNone(result['vram_used_bytes'])
        self.assertEqual(result['vram_total_bytes'], 1000)
        self.assertIsNone(result['vram_percent'])
        (device / 'gpu_busy_percent').write_text('-1')
        (device / 'mem_info_vram_used').write_text('broken')
        (device / 'mem_info_vram_total').write_text('0')
        result = sampler.sample()
        self.assertIsNone(result['gpu_percent'])
        self.assertIsNone(result['vram_used_bytes'])
        self.assertIsNone(result['vram_total_bytes'])


    def test_disk_rates_cpu_clock_and_counter_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc, sys_root = root / 'proc', root / 'sys'
            proc.mkdir()
            for core, khz in ((0, 3600000), (1, 4650000)):
                freq = sys_root / f'devices/system/cpu/cpu{core}/cpufreq'
                freq.mkdir(parents=True)
                (freq / 'scaling_cur_freq').write_text(str(khz))
            def stats(read, write):
                (proc / 'diskstats').write_text(
                    f' 259 0 nvme0n1 1 0 {read} 0 1 0 {write} 0 0 0 0\n'
                    f' 259 1 nvme0n1p1 1 0 {read} 0 1 0 {write} 0 0 0 0\n'
                    '   7 0 loop0 1 0 999999 0 1 0 999999 0 0 0 0\n')
            now = [100.0]
            sampler = MetricsSampler(proc_root=proc, drm_root=root / 'drm', sys_root=sys_root, clock=lambda: now[0])
            stats(0, 0)
            self.assertEqual(sampler.sample()['disk_read_bytes_sec'], None)
            stats(2048, 4096); now[0] = 102.0
            result = sampler.sample()
            self.assertEqual((result['disk_read_bytes_sec'], result['disk_write_bytes_sec']), (2048 * 512 / 2, 4096 * 512 / 2))
            self.assertEqual(result['cpu_clock_mhz'], 4650.0)
            stats(0, 0); now[0] = 104.0
            self.assertIsNone(sampler.sample()['disk_read_bytes_sec'])


if __name__ == '__main__':
    unittest.main()
