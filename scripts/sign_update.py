#!/usr/bin/python3
"""Create the signed update manifest (restlos-update.json/.sig) for a built release.

Usage: sign_update.py DIST_DIR PRIVATE_KEY
The private Ed25519 key must live outside the project (CI: GitHub secret
RESTLOS_SIGNING_KEY, only as a temporary file). Same key as Restlos Bewerbung.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from updater import APPLICATION, ARCHIVE_PREFIX, MANIFEST_NAME, PUBLIC_KEY_NAME, SIGNATURE_NAME, version_of  # noqa: E402

# First version whose package contains the run_air.py launcher; older ones need one manual install.
MINIMUM_BASE = '1.4.0'


def main(arguments: list[str]) -> int:
    if len(arguments) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    dist, key = Path(arguments[0]).resolve(), Path(arguments[1]).resolve()
    if key.is_relative_to(ROOT):
        raise SystemExit('Der private Signierschlüssel muss außerhalb des Projektordners liegen.')
    version = version_of(ROOT / 'app')
    archive = dist / f'{ARCHIVE_PREFIX}-{version}.tar.gz'
    if not archive.is_file():
        raise SystemExit(f'Release-Archiv fehlt: {archive}')
    manifest = {'application': APPLICATION, 'architecture': 'all', 'version': version,
                'minimum_base': MINIMUM_BASE, 'archive': archive.name,
                'size': archive.stat().st_size, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    manifest_path, signature_path = dist / MANIFEST_NAME, dist / SIGNATURE_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(key), '-rawin',
                    '-in', str(manifest_path), '-out', str(signature_path)], check=True)
    # Cross-check with the public key the program ships: a wrong secret fails here, not for users.
    check = subprocess.run(['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(ROOT / 'app' / PUBLIC_KEY_NAME),
                            '-rawin', '-in', str(manifest_path), '-sigfile', str(signature_path)],
                           capture_output=True, check=False)
    if check.returncode != 0:
        signature_path.unlink(missing_ok=True)
        raise SystemExit('Der Signierschlüssel passt nicht zu app/update-public.pem – '
                         'RESTLOS_SIGNING_KEY muss derselbe Schlüssel wie bei Restlos Bewerbung sein.')
    print(f'Signiertes Update-Manifest für {version}: {manifest_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
