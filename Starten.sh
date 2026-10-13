#!/bin/sh
if [ -x /usr/bin/restlos-air ]; then
    exec /usr/bin/restlos-air "$@"
fi
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
exec /usr/bin/python3 "$script_dir/app/air_studio.py" "$@"
