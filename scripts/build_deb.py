#!/usr/bin/python3
"""Build a local Debian package; never install or access cooling hardware.

The final absolute package path is written to stdout. Build progress goes to
stderr, so Installieren.sh can safely use that path without parsing log output.
"""
from pathlib import Path
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / 'app'
RESOURCES = ROOT / 'packaging'
DIST = ROOT / 'dist'


def package_version():
    versions = [line.partition(':')[2].strip()
                for line in (RESOURCES / 'control').read_text(encoding='utf-8').splitlines()
                if line.startswith('Version:')]
    if len(versions) != 1 or not re.fullmatch(r'[0-9][A-Za-z0-9.+:~\-]*', versions[0]):
        raise ValueError('Die Paketversion fehlt oder ist ungültig.')
    return versions[0]


def copy(stage, source, destination, mode=0o644):
    target = stage / destination
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(mode)


def prepare(stage, epoch):
    for source in sorted(APP.glob('*.py')):
        copy(stage, source, 'usr/lib/restlos-air/' + source.name)
    if not (stage / 'usr/lib/restlos-air/air_studio.py').is_file():
        raise FileNotFoundError('app/air_studio.py fehlt.')
    for name in ('control', 'conffiles'):
        copy(stage, RESOURCES / name, 'DEBIAN/' + name)
    copy(stage, RESOURCES / 'postinst', 'DEBIAN/postinst', 0o755)
    copy(stage, RESOURCES / 'restlos-air', 'usr/bin/restlos-air', 0o755)
    copy(stage, RESOURCES / 'restlos-air.desktop', 'usr/share/applications/restlos-air.desktop')
    copy(stage, RESOURCES / 'autostart.desktop', 'etc/xdg/autostart/restlos-air.desktop')
    copy(stage, RESOURCES / 'restlos-air.conf', 'etc/modules-load.d/restlos-air.conf')
    copy(stage, RESOURCES / 'restlos-air.svg', 'usr/share/icons/hicolor/scalable/apps/restlos-air.svg')
    copy(stage, ROOT / 'README.md', 'usr/share/doc/restlos-air/README.md')
    copy(stage, ROOT / 'LICENSE', 'usr/share/doc/restlos-air/copyright')

    size = sum(path.stat().st_size for path in stage.rglob('*') if path.is_file())
    with (stage / 'DEBIAN/control').open('a', encoding='utf-8') as handle:
        handle.write('Installed-Size: ' + str((size + 1023) // 1024) + '\n')
    checksums = []
    for path in sorted(stage.rglob('*')):
        relative = path.relative_to(stage)
        if path.is_file() and relative.parts[0] != 'DEBIAN':
            checksums.append(hashlib.md5(path.read_bytes()).hexdigest() + '  ' + str(relative))
    (stage / 'DEBIAN/md5sums').write_text('\n'.join(checksums) + '\n', encoding='utf-8')

    for path in [stage, *stage.rglob('*')]:
        if path.is_dir():
            path.chmod(0o755)
        os.utime(path, (epoch, epoch))


def main():
    if shutil.which('dpkg-deb') is None:
        raise RuntimeError('dpkg-deb fehlt; bitte das Debian-Paket dpkg installieren.')
    version = package_version()
    epoch = int(os.environ.get('SOURCE_DATE_EPOCH', '1791763200'))
    if epoch < 0:
        raise ValueError('SOURCE_DATE_EPOCH darf nicht negativ sein.')
    output = DIST / f'restlos-air_{version}_all.deb'
    DIST.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env['SOURCE_DATE_EPOCH'] = str(epoch)
    package_temp = checksum_temp = None
    try:
        with tempfile.TemporaryDirectory(prefix='restlos-air-build-') as private:
            stage = Path(private) / 'package'
            stage.mkdir()
            prepare(stage, epoch)
            package_fd, package_name = tempfile.mkstemp(prefix='.restlos-air-', suffix='.deb', dir=DIST)
            os.close(package_fd)
            package_temp = Path(package_name)
            subprocess.run(['dpkg-deb', '--root-owner-group', '-Zxz', '--build', str(stage), str(package_temp)],
                           env=env, stdout=sys.stderr, check=True)
            digest = hashlib.sha256(package_temp.read_bytes()).hexdigest()
            checksum_fd, checksum_name = tempfile.mkstemp(prefix='.restlos-air-', suffix='.sha256', dir=DIST)
            checksum_temp = Path(checksum_name)
            with os.fdopen(checksum_fd, 'w', encoding='utf-8') as handle:
                handle.write(digest + '  ' + output.name + '\n')
            package_temp.chmod(0o644)
            checksum_temp.chmod(0o644)
            os.replace(package_temp, output)
            os.replace(checksum_temp, output.with_suffix('.deb.sha256'))
    finally:
        for path in (package_temp, checksum_temp):
            if path is not None:
                path.unlink(missing_ok=True)
    print(output, flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print('Paketbau fehlgeschlagen: ' + str(error), file=sys.stderr, flush=True)
        raise SystemExit(1)
