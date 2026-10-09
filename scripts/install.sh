#!/bin/sh
# Install or upgrade Curatarr as native systemd services.
#
# Run from a release checkout:   sudo ./scripts/install.sh
# The source tree is only read. The installed copy lives elsewhere:
#   code    $PREFIX       (default /opt/curatarr; releases/<stamp> plus a
#                          "current" symlink; the previous release is kept)
#   config  $CONFIG_DIR   (default /etc/curatarr; created once, never overwritten)
#   data    $DATA_DIR     (default /var/lib/curatarr; database and posters)
# Re-running the script upgrades in place, backing up the SQLite database first.
set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PREFIX=${PREFIX:-/opt/curatarr}
CONFIG_DIR=${CONFIG_DIR:-/etc/curatarr}
DATA_DIR=${DATA_DIR:-/var/lib/curatarr}
SYSTEMD_DIR=${SYSTEMD_DIR:-/etc/systemd/system}
SERVICE_USER=${SERVICE_USER:-curatarr}
PYTHON=${PYTHON:-python3}
NO_SYSTEMD=${NO_SYSTEMD:-0}
ENV_FILE="$CONFIG_DIR/curatarr.env"

say() { printf '%s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
as_service_user() {
    if [ "$(id -un)" = "$SERVICE_USER" ]; then
        "$@"
    else
        runuser -u "$SERVICE_USER" -- "$@"
    fi
}

[ -f "$SOURCE_DIR/pyproject.toml" ] && [ -d "$SOURCE_DIR/curatarr" ] \
    || fail "run this script from a Curatarr source checkout"
case "$PREFIX" in "$SOURCE_DIR"|"$SOURCE_DIR"/*)
    fail "PREFIX must be outside the source checkout" ;;
esac
case "$DATA_DIR" in "$SOURCE_DIR"|"$SOURCE_DIR"/*)
    fail "DATA_DIR must be outside the source checkout" ;;
esac
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 12))' \
    || fail "Python 3.12 or newer is required ($PYTHON)"

if [ "$(id -u)" -eq 0 ]; then
    if ! id "$SERVICE_USER" >/dev/null 2>&1; then
        say "Creating system user $SERVICE_USER"
        useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
elif [ "$(id -un)" != "$SERVICE_USER" ]; then
    fail "run as root, or set SERVICE_USER to your own account for a user install"
fi

say "Installing code to $PREFIX"
mkdir -p "$PREFIX/releases" "$CONFIG_DIR" "$DATA_DIR"
previous=$(readlink -f "$PREFIX/current" 2>/dev/null || true)
release=$(mktemp -d "$PREFIX/releases/$(date +%Y%m%d-%H%M%S)-XXXXXX")
staging=""
backup=""
next="$PREFIX/.current-$$"
services_stopped=0
migration_started=0
switched=0
completed=0
web_was_active=0
worker_was_active=0
cleanup() {
    status=$?
    trap - EXIT
    [ -z "$staging" ] || rm -rf "$staging"
    rm -f "$next"
    if [ "$status" -ne 0 ] && [ "$completed" -eq 0 ]; then
        if [ "$switched" -eq 1 ] && [ "$NO_SYSTEMD" != 1 ]; then
            systemctl stop curatarr-worker curatarr-web || true
        fi
        if [ "$switched" -eq 1 ]; then
            if [ -n "$previous" ]; then
                ln -s "$previous" "$next"
                mv -Tf "$next" "$PREFIX/current"
            else
                rm -f "$PREFIX/current"
            fi
        fi
        if [ "$migration_started" -eq 1 ] && [ -n "$backup" ]; then
            say "Restoring SQLite database from $backup"
            rm -f "$DATA_DIR/curatarr.db-wal" "$DATA_DIR/curatarr.db-shm"
            cp -p "$backup" "$DATA_DIR/curatarr.db"
        fi
        if [ "$services_stopped" -eq 1 ] && [ -n "$previous" ]; then
            if [ "$migration_started" -eq 0 ] || [ -n "$backup" ]; then
                [ "$web_was_active" -eq 0 ] || systemctl start curatarr-web
                [ "$worker_was_active" -eq 0 ] || systemctl start curatarr-worker
            else
                say "Database migration may have changed an external database; services remain stopped for manual recovery."
            fi
        fi
        rm -rf "$release"
        fail "installation failed; previous release remains selected"
    fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
# Virtualenvs cannot be moved, so each install gets its own release
# directory and "current" switches only after it installed successfully.
"$PYTHON" -m venv "$release"
"$release/bin/python" -m pip install --quiet --upgrade pip
# Build from a temporary copy so the source checkout is never written to.
staging=$(mktemp -d)
cp -R "$SOURCE_DIR/pyproject.toml" "$SOURCE_DIR/README.md" "$SOURCE_DIR/LICENSE" \
    "$SOURCE_DIR/curatarr" "$staging/"
find "$staging" -name __pycache__ -prune -exec rm -rf {} +
"$release/bin/python" -m pip install --quiet "$staging"

if [ ! -f "$ENV_FILE" ]; then
    say "Writing new configuration to $ENV_FILE"
    secret=$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(48))')
    umask 077
    cat > "$ENV_FILE" <<EOF
# Curatarr configuration. Kept across upgrades; back it up with the database.
# Losing CURATARR_SECRET_KEY makes saved integration keys unreadable.
CURATARR_SECRET_KEY=$secret
DATABASE_URL=sqlite:///$DATA_DIR/curatarr.db
CURATARR_DATA_DIR=$DATA_DIR
CURATARR_BIND=0.0.0.0:8787
# Optional: fix the Jellyfin server used for sign-in before first run.
CURATARR_JELLYFIN_URL=
CURATARR_ALLOW_UNAUTHENTICATED=false
CURATARR_SESSION_COOKIE_SECURE=false
CURATARR_LOG_LEVEL=INFO
EOF
    umask 022
else
    say "Keeping existing configuration $ENV_FILE"
fi

if [ "$(id -u)" -eq 0 ]; then
    chown -R "$SERVICE_USER:" "$DATA_DIR"
    chown root:"$SERVICE_USER" "$CONFIG_DIR" "$ENV_FILE"
    chmod 750 "$CONFIG_DIR"
    chmod 640 "$ENV_FILE"
fi

if [ "$NO_SYSTEMD" != 1 ]; then
    if systemctl is-active --quiet curatarr-web 2>/dev/null; then web_was_active=1; fi
    if systemctl is-active --quiet curatarr-worker 2>/dev/null; then worker_was_active=1; fi
    if [ "$web_was_active" -eq 1 ] || [ "$worker_was_active" -eq 1 ]; then
        say "Stopping running services for upgrade"
        services_stopped=1
        systemctl stop curatarr-worker curatarr-web
    fi
fi

if [ -f "$DATA_DIR/curatarr.db" ]; then
    mkdir -p "$DATA_DIR/backups"
    backup=$(mktemp "$DATA_DIR/backups/curatarr-$(date +%Y%m%d-%H%M%S)-XXXXXX.db")
    say "Backing up database to $backup"
    "$PYTHON" - "$DATA_DIR/curatarr.db" "$backup" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
source.backup(target)
assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
target.close()
source.close()
PY
    if [ "$(id -u)" -eq 0 ]; then
        chown -R "$SERVICE_USER:" "$DATA_DIR/backups"
    fi
fi

say "Applying database migrations"
migration_started=1
(
    set -a
    . "$ENV_FILE"
    set +a
    cd "$DATA_DIR"
    as_service_user "$release/bin/flask" --app curatarr db upgrade
    as_service_user "$release/bin/flask" --app curatarr encrypt-secrets
)
ln -s "$release" "$next"
mv -Tf "$next" "$PREFIX/current"
switched=1

if [ "$NO_SYSTEMD" = 1 ]; then
    say "Skipping systemd units (NO_SYSTEMD=1)"
else
say "Installing systemd units"
cat > "$SYSTEMD_DIR/curatarr-web.service" <<EOF
[Unit]
Description=Curatarr web interface
Wants=network-online.target
After=network-online.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=$ENV_FILE
WorkingDirectory=$DATA_DIR
ExecStart=$PREFIX/current/bin/gunicorn --bind \${CURATARR_BIND} --workers 1 curatarr:create_app()
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
cat > "$SYSTEMD_DIR/curatarr-worker.service" <<EOF
[Unit]
Description=Curatarr lifecycle worker
Wants=network-online.target
After=network-online.target curatarr-web.service

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=$ENV_FILE
WorkingDirectory=$DATA_DIR
ExecStart=$PREFIX/current/bin/flask --app curatarr worker
Restart=on-failure
RestartSec=15
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now curatarr-web curatarr-worker
say "Curatarr is running. Open http://<this-host>:8787 and sign in with a Jellyfin administrator."
fi

completed=1
# Keep the new release and the one before it for rollback, only after success.
for old in "$PREFIX"/releases/*; do
    if [ "$old" != "$release" ] && [ "$old" != "$previous" ]; then
        if ! rm -rf "$old"; then
            say "Could not remove old release $old; remove it manually if needed."
        fi
    fi
done
