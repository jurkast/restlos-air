#!/usr/bin/python3
"""Build the self-update archive dist/RestlosAir-VERSION.tar.gz (+ .sha256).

The archive holds only the program files from app/; the Debian package keeps the
autostart entry, driver configuration and launcher. Reproducible with SOURCE_DATE_EPOCH.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import os
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from updater import ARCHIVE_PREFIX, REQUIRED_FILES, version_of  # noqa: E402

FILES = sorted([*(ROOT / 'app').glob('*.py'), ROOT / 'app/update-public.pem'])


def build(output: Path) -> Path:
    version = version_of(ROOT / 'app')
    names = {path.name for path in FILES}
    missing = [name for name in REQUIRED_FILES if name not in names]
    if missing:
        raise SystemExit('Fehlende Programmdateien: ' + ', '.join(missing))
    epoch = int(os.environ.get('SOURCE_DATE_EPOCH', '0'))
    prefix = f'{ARCHIVE_PREFIX}-{version}'
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.PAX_FORMAT) as archive:
        directory = tarfile.TarInfo(prefix)
        directory.type, directory.mode, directory.mtime = tarfile.DIRTYPE, 0o755, epoch
        archive.addfile(directory)
        for path in FILES:
            data = path.read_bytes()
            info = tarfile.TarInfo(f'{prefix}/{path.name}')
            info.size, info.mtime = len(data), epoch
            info.mode = 0o755 if path.name == 'run_air.py' else 0o644
            archive.addfile(info, io.BytesIO(data))
    output.mkdir(parents=True, exist_ok=True)
    target = output / f'{prefix}.tar.gz'
    with target.open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=epoch) as packed:
        packed.write(buffer.getvalue())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / f'{prefix}.sha256').write_text(f'{digest}  {target.name}\n', encoding='utf-8')
    return target


if __name__ == '__main__':
    print(build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'dist'))
