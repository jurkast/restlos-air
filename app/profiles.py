"""Fan profiles (Leise / Normal / Spiel) and automatic game mode.

A profile is one fan curve applied to every controllable mainboard channel through the
existing, journaled Controller (CoolerControl stays the only hardware writer). All curves
reach 100 % by 85 °C, so the temperature guard of manual control is kept.
"""
from __future__ import annotations

from dataclasses import dataclass

PROFILES: dict[str, tuple[str, list[list[float]]]] = {
    'quiet': ('Leise', [[40, 40], [70, 55], [85, 100]]),
    'normal': ('Normal', [[40, 45], [65, 65], [85, 100]]),
    'game': ('Spiel', [[30, 60], [60, 80], [80, 100]]),
}
ORDER = ('quiet', 'normal', 'game')
RESTORE_AFTER_SECONDS = 60


def title(profile: str | None) -> str:
    return PROFILES[profile][0] if profile in PROFILES else 'Eigene'


def next_profile(profile: str | None) -> str:
    return ORDER[(ORDER.index(profile) + 1) % len(ORDER)] if profile in ORDER else 'quiet'


def apply_profile(controller, profile: str) -> dict:
    """Apply one profile to all controllable channels; returns the final backend status."""
    if profile not in PROFILES:
        raise ValueError('Unbekanntes Profil.')
    status = controller.refresh()
    channels = [key for key, item in sorted(status.get('channels', {}).items()) if item.get('controllable')]
    if not status.get('connected') or not channels:
        from coolercontrol import BackendError
        raise BackendError(status.get('reason') or 'Keine regelbaren Lüfter verbunden. Zuerst „Steuerung verbinden“.')
    for key in channels:
        status = controller.apply_curve(key, PROFILES[profile][1], source='max')
    return status


@dataclass
class GameMode:
    """Decides when to switch to the game profile and back. Pure logic, easy to test."""
    enabled: bool = True
    active_game: str | None = None
    previous: str | None = None
    gone_since: float | None = None

    def decide(self, game: str | None, current: str | None, now: float) -> str | None:
        """Return the profile to apply now, or None for no change."""
        if not self.enabled:
            self.active_game = self.gone_since = None
            return None
        if game:
            self.gone_since = None
            if self.active_game is None:
                self.active_game = game
                if current != 'game':
                    self.previous = current
                    return 'game'
            self.active_game = game
            return None
        if self.active_game is None:
            return None
        if self.gone_since is None:
            self.gone_since = now
            return None
        if now - self.gone_since < RESTORE_AFTER_SECONDS:
            return None
        self.active_game = self.gone_since = None
        restore, self.previous = self.previous, None
        # Only return to the earlier profile if the user did not choose another one meanwhile.
        if current != 'game':
            return None
        return restore if restore in PROFILES else 'normal'
