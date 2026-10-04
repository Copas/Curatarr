#!/bin/sh
# Remove the Curatarr services and code installed by install.sh.
# Configuration and data are kept unless --purge is given.
set -eu

PREFIX=${PREFIX:-/opt/curatarr}
CONFIG_DIR=${CONFIG_DIR:-/etc/curatarr}
DATA_DIR=${DATA_DIR:-/var/lib/curatarr}
SYSTEMD_DIR=${SYSTEMD_DIR:-/etc/systemd/system}
SERVICE_USER=${SERVICE_USER:-curatarr}

purge=0
[ "${1:-}" = "--purge" ] && purge=1

if systemctl list-unit-files curatarr-web.service >/dev/null 2>&1; then
    systemctl disable --now curatarr-worker curatarr-web 2>/dev/null || true
fi
rm -f "$SYSTEMD_DIR/curatarr-web.service" "$SYSTEMD_DIR/curatarr-worker.service"
systemctl daemon-reload 2>/dev/null || true
rm -rf "$PREFIX"

if [ "$purge" = 1 ]; then
    rm -rf "$CONFIG_DIR" "$DATA_DIR"
    if [ "$SERVICE_USER" = curatarr ] && id curatarr >/dev/null 2>&1; then
        userdel curatarr
    fi
    echo "Removed Curatarr, its configuration, and its data."
else
    echo "Removed Curatarr. Kept $CONFIG_DIR and $DATA_DIR; use --purge to delete them."
fi
