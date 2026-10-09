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

Changing, testing, or resetting the checkout never touches an installed copy. Nothing from an installed copy, such as its database, keys, posters, or logs, ends up in Git. Install from a tagged release (for example `v0.2.0rc3`), never from an unreleased branch.

## Native install (Linux with systemd)

Requirements: Python 3.12 or newer with the `venv` module, systemd, and root access.

```bash
git clone https://github.com/Copas/Curatarr.git curatarr-release
cd curatarr-release
git checkout v0.2.0rc3         # the release you want
sudo ./scripts/install.sh
```

The installer:

- creates a `curatarr` system user;
- installs the release into `/opt/curatarr/releases/<timestamp>` and points `/opt/curatarr/current` at it;
- writes `/etc/curatarr/curatarr.env` with a new random `CURATARR_SECRET_KEY` (only if that file does not exist yet);
- creates `/var/lib/curatarr` for the SQLite database and poster snapshots;
- backs up the default SQLite database with SQLite's backup API, then applies database migrations;
- installs and starts `curatarr-web` and `curatarr-worker`.

It builds from a temporary copy, so the checkout is only read. An upgrade builds the replacement before stopping the services and switches `current` only after migrations succeed. If an upgrade fails after stopping the services, the installer restores its default SQLite backup and the previous release, then restarts the services that were running. For PostgreSQL or a custom database path, make a separate database backup before upgrading; the installer cannot restore an external database automatically. After installing, you can delete the checkout or keep it for the next upgrade.

To change locations or the service account, set `PREFIX`, `CONFIG_DIR`, `DATA_DIR`, or `SERVICE_USER` when running the script. Edit `/etc/curatarr/curatarr.env` to change the listening address (`CURATARR_BIND`, default `0.0.0.0:8787`), pre-set the Jellyfin server (`CURATARR_JELLYFIN_URL`), or switch to PostgreSQL (`DATABASE_URL`). Then run `sudo systemctl restart curatarr-web curatarr-worker`.

### Upgrade

```bash
cd curatarr-release
git fetch --tags
git checkout v0.2.0rc3
sudo ./scripts/install.sh
```

The installer builds the new release, stops the services, backs up the default SQLite database to `/var/lib/curatarr/backups/`, migrates, selects the new release, and starts the services again. Configuration is kept. Read `CHANGELOG.md` before upgrading.

If a checkout is already on the same host as the installation, use it directly. Confirm that it is clean and at the intended tag before running the installer:

```bash
cd /path/to/your/curatarr-release
git status --short                   # must print nothing
git describe --tags --exact-match   # must print v0.2.0rc3
sudo ./scripts/install.sh
sudo systemctl is-active curatarr-web curatarr-worker
curl -fsS http://127.0.0.1:8787/api/v1/status | python3 -m json.tool
```

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
git clone https://github.com/Copas/Curatarr.git curatarr-deploy
cd curatarr-deploy
git checkout v0.2.0rc3
cp .env.example .env    # set CURATARR_SECRET_KEY and CURATARR_DB_PASSWORD
docker compose up -d --build
```

The image contains the installed package, not the source tree. Data lives in the `config` and `database` volumes, and migrations run when the web container starts. The example compose file binds the UI to `127.0.0.1:8787`; change the `ports` entry to reach it from your network. To upgrade, check out the new tag and run `docker compose up -d --build` again after backing up the volumes.

### Windows with Docker Desktop

Install Git and [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/), using its Linux containers backend (WSL 2 is the usual choice). Run these commands in PowerShell from a directory where you keep deployments:

```powershell
git clone https://github.com/Copas/Curatarr.git curatarr-deploy
Set-Location curatarr-deploy
git checkout v0.2.0rc3
Copy-Item .env.example .env
notepad .env
docker compose up -d --build
docker compose ps
(Invoke-RestMethod http://127.0.0.1:8787/api/v1/status).version
```

Before starting Compose, replace `CURATARR_SECRET_KEY=change-me` in `.env` with a long, random secret and add `CURATARR_DB_PASSWORD=` with a separate random alphanumeric password. Keep `.env`: changing the secret key makes saved integration keys unreadable. Compose uses PostgreSQL, so its database URL overrides the SQLite example in `.env`. The web page is at `http://localhost:8787` once the `web` container is healthy.

If Jellyfin, Sonarr, or Radarr run directly on the same Windows PC, use `host.docker.internal` in the URLs entered into Curatarr (for example `http://host.docker.internal:8096` for Jellyfin). `localhost` inside Curatarr points to its container; the host services must also accept connections from Docker Desktop. A Jellyfin webhook running on that Windows PC can send to `http://127.0.0.1:8787/api/v1/webhook/jellyfin`; another PC needs a reachable host address and a corresponding port binding in `compose.yaml`. The supplied binding accepts connections only from the Docker host.

For free-space rules, enter the disk path **as Sonarr or Radarr reports it**, such as `C:\Media\Movies` or `\\nas\Media\Movies`. Curatarr matches drive and UNC paths regardless of slash style or letter case. If the Arr app does not report that disk and the container cannot see the path, Curatarr skips free-space cleanup for it rather than measuring a different disk. Use Retention → Preview to check the match before enabling cleanup.

Before an upgrade, save `.env` and back up both the `database` and `config` named volumes in Docker Desktop's Volumes view. Then, in PowerShell:

```powershell
Set-Location curatarr-deploy
git fetch --tags
git checkout <new-release-tag>
docker compose up -d --build
docker compose ps
(Invoke-RestMethod http://127.0.0.1:8787/api/v1/status).version
```

Keep the named volumes when replacing containers: `docker compose down --volumes` deletes the database and saved posters. Use `docker compose logs --tail=100 web worker` if startup fails.

## First run

1. Open `http://<host>:8787`. Sign in with a Jellyfin **administrator** account. On first run, also enter your Jellyfin server URL (unless `CURATARR_JELLYFIN_URL` is set). Do this before exposing Curatarr beyond a trusted network.
2. Follow the `/setup` checklist: add Jellyfin, Sonarr, and Radarr API keys in Settings, copy the webhook token, then run Discover from Overview. The Settings page links to each key's location:
   - **Jellyfin:** Dashboard → API Keys → add a key (default port 8096).
   - **Sonarr:** Settings → General → Security → API Key (default port 8989).
   - **Radarr:** Settings → General → Security → API Key (default port 7878).
3. Set up Jellyfin's Webhook plugin. Curatarr's Settings page shows these steps with your exact address and a template to copy:
   - Dashboard → Plugins → Webhook → **Add Generic Destination**, with URL `http://<host>:8787/api/v1/webhook/jellyfin`.
   - Notification types **Playback Start**, **Playback Stop**, and **User Data Saved**; item types **Movies**, **Episodes**, and **Series**.
   - Leave *Send All Properties* off and paste the template from Settings.
   - Add the header `X-Curatarr-Token` with the webhook token. Generate a new token in Settings if you did not copy the first one.

   Finished episodes arrive as Playback Stop with *played to completion*, and marking an item watched or favorite arrives as User Data Saved. Without the webhook, the hourly reconciliation still picks up playback, just later.
4. Click Discover on Overview. Discovery runs in the background worker, because a large library takes longer than a web request is allowed (about 45 seconds for roughly 400 titles and 50,000 Jellyfin items). Overview → Operations shows when it finishes.
5. Review the Acquisition and Retention rules. Every library starts in dry run: Curatarr goes through the whole process (Leaving Soon notices, countdowns, checks) and records what it would remove in History, but never asks Sonarr or Radarr to delete anything. Turn Dry run off on the Retention page, as the global default or for one library, when you are ready.

For HTTPS, put Curatarr behind a TLS-terminating reverse proxy and set `CURATARR_SESSION_COOKIE_SECURE=true`.
