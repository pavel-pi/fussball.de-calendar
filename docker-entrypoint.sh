#!/bin/sh
set -eu

if [ "$(id -u)" = '0' ]; then
    mkdir -p /data
    chown -R calendar:calendar /data
    exec gosu calendar "$@"
fi

exec "$@"
