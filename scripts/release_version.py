#!/usr/bin/python3
"""Read or set the version in app/version.py and packaging/control together.

  release_version.py current      → print the current version
  release_version.py next-patch   → print the next patch version (1.4.0 → 1.4.1)
  release_version.py set 1.5.0    → set the version everywhere
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r'\d+\.\d+\.\d+')


def current() -> str:
    match = re.search(r'^__version__ = "([^"]+)"$', (ROOT / 'app/version.py').read_text(encoding='utf-8'), re.M)
    if match is None or not VERSION.fullmatch(match.group(1)):
        raise SystemExit('Version in app/version.py nicht lesbar.')
    return match.group(1)


def next_patch(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split('.'))
    return f'{major}.{minor}.{patch + 1}'


def _replace(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding='utf-8')
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.M)
    if count != 1:
        raise SystemExit(f'Versionszeile nicht gefunden: {path.relative_to(ROOT)}')
    path.write_text(updated, encoding='utf-8')


def set_version(version: str) -> None:
    if not VERSION.fullmatch(version):
        raise SystemExit(f'Ungültige Version: {version}')
    _replace(ROOT / 'app/version.py', r'^__version__ = "[^"]+"$', f'__version__ = "{version}"')
    _replace(ROOT / 'packaging/control', r'^Version: .+$', f'Version: {version}')


def main(arguments: list[str]) -> int:
    if arguments == ['current']:
        print(current())
    elif arguments == ['next-patch']:
        print(next_patch(current()))
    elif len(arguments) == 2 and arguments[0] == 'set':
        set_version(arguments[1])
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
