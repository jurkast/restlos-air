#!/bin/sh
set -eu
if [ "$(id -u)" -eq 0 ]; then
    printf '%s\n' 'Bitte ohne sudo starten. Das Paket wird als normaler Benutzer gebaut.' >&2
    exit 1
fi
if ! command -v pkexec >/dev/null 2>&1; then
    printf '%s\n' 'pkexec fehlt. Bitte zuerst eine lokale Polkit-Installation einrichten.' >&2
    exit 1
fi
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
package_file=$(/usr/bin/python3 "$script_dir/scripts/build_deb.py")
if [ ! -r "$package_file" ]; then
    printf '%s\n' 'Das erstellte Installationspaket fehlt.' >&2
    exit 1
fi
exec pkexec /usr/bin/apt-get install --reinstall --yes "$package_file"
