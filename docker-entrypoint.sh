#!/bin/sh
set -eu
if [ "${CURATARR_AUTO_MIGRATE:-false}" = "true" ]; then
    flask --app curatarr db upgrade
    flask --app curatarr encrypt-secrets
fi
exec "$@"
