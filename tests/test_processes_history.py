"""Process ranking, game detection, termination guard, history and dust hint."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import history  # noqa: E402
import processes  # noqa: E402


def fake_process(root, pid, name, ticks, rss_pages, cmdline='', start=100):
    folder = root / str(pid)
    folder.mkdir(exist_ok=True)
    fields = ['S'] + ['0'] * 10 + [str(ticks), '0'] + ['0'] * 6 + [str(start), '0', str(rss_pages)]
    (folder / 'stat').write_text(f'{pid} ({name}) ' + ' '.join(fields))
    (folder / 'cmdline').write_bytes(cmdline.replace(' ', '\0').encode())


class ProcessTests(unittest.TestCase):
    def test_groups_by_name_ranks_by_cpu_and_detects_games(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = [10.0]
            scanner = processes.ProcessScanner(root, cpu_count=2, clock=lambda: now[0])
            game = '/home/u/.steam/steam/steamapps/common/Hades II/Hades2.exe'
            for pid, name, cmd in ((1, 'chrome', ''), (2, 'chrome', ''), (3, 'Hades2.exe', game), (4, 'bash', '')):
                fake_process(root, pid, name, 0, 1000, cmd)
            self.assertEqual(scanner.sample(), [])
            self.assertEqual(scanner.game, 'Hades II')
            ticks = processes.CLOCK_TICKS
            for pid, name, used, cmd in ((1, 'chrome', ticks // 2, ''), (2, 'chrome', ticks // 2, ''),
                                         (3, 'Hades2.exe', ticks // 4, game), (4, 'bash', 0, '')):
                fake_process(root, pid, name, used, 1000, cmd)
            now[0] = 11.0
            top = scanner.sample()
            self.assertEqual(top[0].name, 'chrome')
            self.assertEqual(top[0].processes, 2)
            self.assertAlmostEqual(top[0].cpu_percent, 50.0, delta=1)
            self.assertEqual(top[0].memory_bytes, 2000 * processes.PAGE_SIZE)
            self.assertEqual(top[1].name, 'Hades2.exe')

    def test_runtime_paths_are_not_games(self):
        self.assertIsNone(processes.game_name('/x/steamapps/common/Proton 9.0/files/bin/wine64-preload'))
        self.assertIsNone(processes.game_name('/x/steamapps/common/SteamLinuxRuntime_sniper/run'))
        self.assertEqual(processes.game_name('/home/u/Games/Heroic/Alan Wake 2/AlanWake2.exe'), 'Alan Wake 2')
        self.assertEqual(processes.game_name('lutris-wrapper: Diablo /home/u/Games/diablo-ii/x.exe'), 'diablo-ii')
        self.assertIsNone(processes.game_name('/usr/bin/firefox'))

    def test_protected_programs_are_never_terminated(self):
        scanner = processes.ProcessScanner(Path('/nonexistent'))
        self.assertEqual(scanner.terminate('gnome-shell'), 0)
        self.assertEqual(scanner.terminate(''), 0)


class HistoryTests(unittest.TestCase):
    def test_buckets_peaks_persistence_and_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1_800_000_000.0]
            record = history.History(Path(directory), clock=lambda: now[0])
            for second, cpu in enumerate((60, 82, 70)):
                now[0] += 1
                record.add({'cpu': cpu, 'gpu': 50, 'ssd': None}, 30, 'chrome', 'Hades II' if cpu == 82 else None)
            now[0] += history.BUCKET_SECONDS
            record.add({'cpu': 50, 'gpu': 40}, 5, 'bash', None)
            peak = record.peaks(0)['cpu']
            self.assertEqual((peak['value'], peak['top'], peak['game']), (82, 'chrome', 'Hades II'))
            lines = (Path(directory) / 'history.jsonl').read_text().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])['load'], 30)
            reopened = history.History(Path(directory), clock=lambda: now[0])
            self.assertEqual(reopened.peaks(0)['cpu']['value'], 82)
            later = history.History(Path(directory), clock=lambda: now[0] + history.KEEP_SECONDS + 10_000)
            self.assertEqual(later.rows(), [])

    def test_dust_hint_needs_two_weeks_and_a_clear_rise(self):
        with tempfile.TemporaryDirectory() as directory:
            record = history.History(Path(directory))
            record.daily = {f'2026-09-{day:02d}': 40.0 for day in range(1, 14)}
            self.assertIsNone(record.dust_hint())
            record.daily.update({f'2026-09-{day:02d}': 41.0 for day in range(14, 21)})
            self.assertIsNone(record.dust_hint())
            record.daily.update({f'2026-10-{day:02d}': 48.0 for day in range(1, 8)})
            self.assertIn('Staub', record.dust_hint())

    def test_unwritable_directory_does_not_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / 'file'
            blocked.write_text('x')
            now = [1_800_000_000.0]
            record = history.History(blocked / 'sub', clock=lambda: now[0])
            record.add({'cpu': 50}, 10, None, None)
            now[0] += history.BUCKET_SECONDS
            record.add({'cpu': 50}, 10, None, None)


if __name__ == '__main__':
    unittest.main(verbosity=2)
