"""Fan profiles stay within the guard rules; game mode switches and restores."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from coolercontrol import BackendError, validate_curve  # noqa: E402
import profiles  # noqa: E402


class FakeController:
    def __init__(self, connected=True):
        self.applied = []
        self.connected = connected

    def refresh(self):
        return {'connected': self.connected, 'reason': 'offline', 'channels': {
            'nct6798:1': {'controllable': True}, 'nct6798:2': {'controllable': True},
            'nct6798:6': {'controllable': False}}}

    def apply_curve(self, key, points, source='cpu'):
        self.applied.append((key, points, source))
        return {'connected': True, 'channels': {}}


class ProfileTests(unittest.TestCase):
    def test_every_profile_passes_the_curve_guard(self):
        for key, (name, points) in profiles.PROFILES.items():
            self.assertEqual(validate_curve(points)[-1][1], 100, name)

    def test_apply_to_all_controllable_channels_only(self):
        controller = FakeController()
        profiles.apply_profile(controller, 'game')
        self.assertEqual([item[0] for item in controller.applied], ['nct6798:1', 'nct6798:2'])
        self.assertTrue(all(item[2] == 'max' for item in controller.applied))
        with self.assertRaises(BackendError):
            profiles.apply_profile(FakeController(connected=False), 'quiet')

    def test_cycle_and_titles(self):
        self.assertEqual([profiles.next_profile(p) for p in ('quiet', 'normal', 'game', None)],
                         ['normal', 'game', 'quiet', 'quiet'])
        self.assertEqual(profiles.title(None), 'Eigene')

    def test_game_mode_switches_and_restores_after_a_minute(self):
        mode = profiles.GameMode()
        self.assertEqual(mode.decide('Hades II', 'quiet', 0), 'game')
        self.assertIsNone(mode.decide('Hades II', 'game', 5))
        self.assertIsNone(mode.decide(None, 'game', 10))
        self.assertIsNone(mode.decide(None, 'game', 60))
        self.assertEqual(mode.decide(None, 'game', 71), 'quiet')
        self.assertIsNone(mode.decide(None, 'quiet', 200))

    def test_short_gaps_and_manual_choice_are_respected(self):
        mode = profiles.GameMode()
        mode.decide('Hades II', 'normal', 0)
        mode.decide(None, 'game', 10)
        self.assertIsNone(mode.decide('Hades II', 'game', 30))   # loading screen, game back
        mode.decide(None, 'quiet', 40)                          # user picked "Leise" meanwhile
        self.assertIsNone(mode.decide(None, 'quiet', 200))
        disabled = profiles.GameMode(enabled=False)
        self.assertIsNone(disabled.decide('Hades II', 'quiet', 0))


if __name__ == '__main__':
    unittest.main(verbosity=2)
