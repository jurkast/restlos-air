#!/usr/bin/python3
"""Start Restlos Air: the installed base version chooses a signed, staged update or itself."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _run_prepared_version(target: Path) -> int | None:
    """Start and supervise a staged version.

    If it exits with an error before confirming its start, the base version switches back
    immediately and starts itself, so a widget always appears. Returns None when the base
    version should take over, otherwise the staged version's exit code.
    """
    from updater import Updater

    child = subprocess.Popen([sys.executable, '-I', '-B', str(target / 'run_air.py'), *sys.argv[1:]])

    def forward(signum: int, _frame: object) -> None:
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, forward)
    code = child.wait()
    try:
        # Error code = crash; a signal (logout) is not a fault of the new version.
        if Updater(root=ROOT, base=ROOT).abandon_failed_launch(target.name, crashed=code > 0):
            return None
    except Exception:  # Fallback checks must never block the installed version.
        return None
    return code


def main() -> int:
    # A started update version must not choose again (endless loop).
    if os.environ.get('RESTLOS_AIR_LAUNCH_CHOSEN') != '1':
        os.environ['RESTLOS_AIR_BASE_DIR'] = str(ROOT)
        os.environ['RESTLOS_AIR_LAUNCH_CHOSEN'] = '1'
        try:
            from updater import Updater
            target = Updater(root=ROOT, base=ROOT).choose_launch()
        except Exception:  # The installed version starting always wins.
            target = ROOT
        if target != ROOT:
            result = _run_prepared_version(target)
            if result is not None:
                return result
    import air_studio
    return air_studio.main(sys.argv)


if __name__ == '__main__':
    raise SystemExit(main())
