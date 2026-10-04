# Installing Curatarr

## Source code and installed copies are separate

A git checkout of this repository is **source code only**. Use it to develop, test, build, and publish Curatarr. Do not use it as your running installation.

A running installation is an **installed copy** with its own code, configuration, and data, all outside the checkout:

| | Source checkout (this repository) | Installed copy (native install) | Installed copy (Docker) |
|---|---|---|---|
| Purpose | Development, tests, releases, GitHub | Your real Curatarr | Your real Curatarr |
| Code | Working tree, any branch | `/opt/curatarr/current` (a tagged release) | Image built from a tagged release |
| Configuration | Shell variables, `.env` (git-ignored) | `/etc/curatarr/curatarr.env` | `.env` beside `compose.yaml` in your deployment directory |
| Data | `instance/` (git-ignored, disposable) | `/var/lib/curatarr` | Docker volumes `config` and `database` |
| Runs as | Your shell | `curatarr-web` and `curatarr-worker` systemd services | `web` and `worker` containers |

Changing, testing, or resetting the checkout never touches an installed copy. Nothing from an installed copy, such as its database, keys, posters, or logs, ends up in Git. Install from a tagged release (for example `v0.1.0`), never from an unreleased branch.

## Native install (Linux with systemd)

Requirements: Python 3.12 or newer with the `venv` module, systemd, and root access.

```bash
git clone <repository-url> curatarr-release
cd curatarr-release
git checkout v0.1.0            # the release you want
sudo ./scripts/install.sh
```

The installer:

- creates a `curatarr` system user;
- installs the release into `/opt/curatarr/releases/<timestamp>` and points `/opt/curatarr/current` at it;
- writes `/etc/curatarr/curatarr.env` with a new random `CURATARR_SECRET_KEY` (only if that file does not exist yet);
- creates `/var/lib/curatarr` for the SQLite database and poster snapshots;
- applies database migrations;
- installs and starts `curatarr-web` and `curatarr-worker`.

It builds from a temporary copy, so the checkout is only read. After installing, you can delete the checkout or keep it for the next upgrade.

To change locations or the service account, set `PREFIX`, `CONFIG_DIR`, `DATA_DIR`, or `SERVICE_USER` when running the script. Edit `/etc/curatarr/curatarr.env` to change the listening address (`CURATARR_BIND`, default `0.0.0.0:8787`), pre-set the Jellyfin server (`CURATARR_JELLYFIN_URL`), or switch to PostgreSQL (`DATABASE_URL`). Then run `sudo systemctl restart curatarr-web curatarr-worker`.

### Upgrade

```bash
cd curatarr-release
git fetch --tags
git checkout v0.2.0
sudo ./scripts/install.sh
```

The installer stops the services, copies the SQLite database to `/var/lib/curatarr/backups/`, installs the new release next to the old one, migrates, and starts the services again. Configuration is kept. Read `CHANGELOG.md` before upgrading.

### Roll back

The previous release stays in `/opt/curatarr/releases/`. Database migrations may not be reversible, so roll back the code and database together:

```bash
sudo systemctl stop curatarr-worker curatarr-web
sudo ln -sfn /opt/curatarr/releases/<previous-timestamp> /opt/curatarr/current
sudo -u curatarr cp /var/lib/curatarr/backups/<backup>.db /var/lib/curatarr/curatarr.db
sudo systemctl start curatarr-web curatarr-worker
```

### Back up

Back up `/etc/curatarr/curatarr.env` and `/var/lib/curatarr` together. The secret key in the configuration file is needed to read the integration keys stored in the database.

### Uninstall

```bash
sudo ./scripts/uninstall.sh           # removes services and code, keeps configuration and data
sudo ./scripts/uninstall.sh --purge   # also deletes configuration, data, and the curatarr user
```

## Docker install

Use a deployment directory that is separate from any development checkout, for example a release checkout used only for this purpose:

```bash
git clone <repository-url> curatarr-deploy
cd curatarr-deploy
git checkout v0.1.0
cp .env.example .env    # set CURATARR_SECRET_KEY and CURATARR_DB_PASSWORD
docker compose up -d --build
```

The image contains the installed package, not the source tree. Data lives in the `config` and `database` volumes, and migrations run when the web container starts. The example compose file binds the UI to `127.0.0.1:8787`; change the `ports` entry to reach it from your network. To upgrade, check out the new tag and run `docker compose up -d --build` again after backing up the volumes.

## First run

1. Open `http://<host>:8787`. Sign in with a Jellyfin **administrator** account. On first run, also enter your Jellyfin server URL (unless `CURATARR_JELLYFIN_URL` is set). Do this before exposing Curatarr beyond a trusted network.
2. Follow the `/setup` checklist: add Jellyfin, Sonarr, and Radarr API keys in Settings, copy the webhook token, then run Discover from Overview.
3. In Jellyfin's webhook plugin, send JSON to `http://<host>:8787/api/v1/webhook/jellyfin` with the token in the `X-Curatarr-Token` header.
4. Review the Acquisition and Retention rules. Every library starts in dry run; Curatarr only records what it would delete until you set Dry run to false for a library.

For HTTPS, put Curatarr behind a TLS-terminating reverse proxy and set `CURATARR_SESSION_COOKIE_SECURE=true`.
