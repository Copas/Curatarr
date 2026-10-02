#!/bin/sh
set -eu
if [ "${CURATARR_AUTO_MIGRATE:-false}" = "true" ]; then
    flask --app curatarr db upgrade
fi
exec "$@"

