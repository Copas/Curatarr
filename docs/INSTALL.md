# Installing Curatarr

Pick the section for your server, follow it top to bottom, then do [First run](#first-run).

| Your server | Section | Notes |
|---|---|---|
| Linux with systemd | [Linux](#linux) | Recommended. Easiest GPU setup for Smart Chapters. SQLite database. |
| Linux with Docker | [Docker on Linux](#docker-on-linux) | Docker Compose with PostgreSQL. |
| Windows 10 or 11 | [Windows](#windows) | Docker Desktop with WSL 2. |
| macOS | [Windows](#windows) | Docker Desktop; run the same commands in Terminal. Not regularly tested. |

Every install runs two parts: **web** (the interface, port 8787) and **worker** (background jobs). Both use the same database and configuration.

**Contents**

- [Before you start](#before-you-start)
- [Linux](#linux): [install](#install) · [chapter generation](#optional-chapter-generation) · [settings](#change-settings) · [upgrade](#upgrade) · [roll back](#roll-back) · [back up](#back-up) · [uninstall](#uninstall)
- [Docker on Linux](#docker-on-linux): [chapter generation in Docker](#chapter-generation-in-docker)
- [Windows](#windows)
- [First run](#first-run)
- [HTTPS and remote access](#https-and-remote-access)
- [Why install from a release](#why-install-from-a-release)

---

## Before you start

You need:

- **Jellyfin**, and **Sonarr** and/or **Radarr** (at least one: Sonarr for TV, Radarr for films), reachable from the Curatarr machine, and an administrator login for each to create API keys.
- A Jellyfin account with the **Administrator** permission to sign in to Curatarr.
- Optional: Jellyfin's **Webhook** plugin (Dashboard → Plugins → Catalog) for instant playback updates, the **Intro Skipper** plugin if you want recap and preview skip buttons, and **Seerr** (or Jellyseerr/Overseerr) so requested titles are kept until whoever asked has watched them.

Always install a **tagged release** (the newest is at the top of [CHANGELOG.md](../CHANGELOG.md), and in the commands below), not the `main` branch.

---

## Linux

Requirements: a systemd-based distribution, **Python 3.12 or newer** with the `venv` module (`sudo apt install python3 python3-venv` on Debian/Ubuntu), Git, and root access.

### Install

```bash
git clone https://github.com/Copas/Curatarr.git curatarr-release
cd curatarr-release
git checkout v0.2.0rc65
sudo ./scripts/install.sh
```

Add `EXTRAS=chapters-gpu` (NVIDIA GPU) or `EXTRAS=chapters` (CPU only) before `./scripts/install.sh` if you want [chapter generation](#optional-chapter-generation).

When it finishes, `curatarr-web` and `curatarr-worker` are running and the interface is at `http://<your-server>:8787`. Check with:

```bash
sudo systemctl is-active curatarr-web curatarr-worker
curl -fsS http://127.0.0.1:8787/health
```

What the installer does:

- creates a `curatarr` system user;
- installs the release into `/opt/curatarr/releases/<timestamp>` and points `/opt/curatarr/current` at it;
- writes `/etc/curatarr/curatarr.env` with a new random `CURATARR_SECRET_KEY` (only if that file doesn't exist yet);
- creates `/var/lib/curatarr` for the SQLite database, poster snapshots and transcription models;
- backs up the database, applies migrations, and installs and starts the two services.

It builds from a temporary copy and only reads the checkout. To use other locations or another service account, set `PREFIX`, `CONFIG_DIR`, `DATA_DIR` or `SERVICE_USER` when running it.

### Optional: chapter generation

Chapters, subtitles, recap and preview detection, and dub checks need a few more tools on the same machine:

1. **FFmpeg and MKVToolNix.** If Jellyfin is installed on this machine, its bundled FFmpeg (`/usr/lib/jellyfin-ffmpeg`) is found automatically.
   ```bash
   sudo apt install ffmpeg mkvtoolnix        # Debian/Ubuntu
   sudo dnf install ffmpeg mkvtoolnix        # Fedora (ffmpeg from RPM Fusion)
   ```
2. **Speech recognition** (faster-whisper), installed by the installer when asked:
   ```bash
   sudo EXTRAS=chapters-gpu ./scripts/install.sh   # NVIDIA GPU: adds the CUDA libraries
   sudo EXTRAS=chapters ./scripts/install.sh       # CPU only (much slower)
   ```
   Not sure which? The installer says when it finds an NVIDIA card, and **Settings → Enhance → Paths & tools** shows the card and whether transcription uses it (Enhance also points out a card going unused). Upgrades keep the choice, remembered in `/opt/curatarr/extras`; give `EXTRAS` only to change it. The model (about 1.5 GB for `medium`) downloads to `/var/lib/curatarr/models` on first use.
3. **Ollama** with a model, for Smart Chapters' chapter names and recap endings:
   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ollama pull qwen3:14b                     # about 10 GB of GPU memory; qwen3:8b for smaller cards
   ```
4. **Write access to your media.** Chapters and track changes are written into the files, so the `curatarr` user must be able to modify them. For a CIFS/SMB mount with a forced owner, add the user to the mount's group and restart the worker:
   ```bash
   sudo usermod -aG <group> curatarr
   sudo systemctl restart curatarr-worker
   ```

Then switch it on per library in **Settings → Enhance**. If Sonarr and Radarr see the files under a different path than this machine does, add a path mapping there (for example `/tv => /mnt/media/tv`).

### Optional: generating artwork

**Library → Posters → Generate** needs a worker: a program on this machine or another one with a graphics card, running ComfyUI with a local image model, or Codex. It's part of the installed package (`/opt/curatarr/current/bin/curatarr-artwork-worker` here) and runs as an ordinary user, apart from Curatarr. Setting it up, the models it uses and what leaves the machine: [ARTWORK.md](ARTWORK.md).

### Change settings

Edit `/etc/curatarr/curatarr.env`, then restart:

```bash
sudo nano /etc/curatarr/curatarr.env
sudo systemctl restart curatarr-web curatarr-worker
```

| Variable | Default | Purpose |
|---|---|---|
| `CURATARR_BIND` | `0.0.0.0:8787` | Address and port the interface listens on |
| `CURATARR_JELLYFIN_URL` | unset | Fix the Jellyfin server instead of entering it at first sign-in |
| `DATABASE_URL` | SQLite in `/var/lib/curatarr` | Use PostgreSQL instead (`postgresql+psycopg://…`) |
| `CURATARR_SESSION_COOKIE_SECURE` | `false` | Set `true` behind HTTPS |
| `CURATARR_LOG_LEVEL` | `INFO` | `DEBUG` to log every outgoing request |

[`.env.example`](../.env.example) lists every variable.

### Upgrade

```bash
cd curatarr-release
git fetch --tags
git checkout v0.2.0rc65                 # the new release
git status --short                     # must print nothing
sudo ./scripts/install.sh              # remembers your EXTRAS; pass EXTRAS=… to change them
sudo systemctl is-active curatarr-web curatarr-worker
```

The installer builds the new release first, then stops the services, backs up the SQLite database to `/var/lib/curatarr/backups/`, migrates, switches `current` to the new release and starts the services. Configuration is kept. If anything fails after the services stop, it restores the backup and the previous release and restarts what was running. With PostgreSQL or a custom database path, make your own database backup first; the installer can't restore an external database. Read [CHANGELOG.md](../CHANGELOG.md) before upgrading.

### Roll back

The previous release stays in `/opt/curatarr/releases/`. Migrations may not be reversible, so roll back code and database together:

```bash
sudo systemctl stop curatarr-worker curatarr-web
sudo ln -sfn /opt/curatarr/releases/<previous-timestamp> /opt/curatarr/current
sudo -u curatarr cp /var/lib/curatarr/backups/<backup>.db /var/lib/curatarr/curatarr.db
sudo systemctl start curatarr-web curatarr-worker
```

### Back up

Back up `/etc/curatarr/curatarr.env` and `/var/lib/curatarr` together. The secret key in the configuration file is needed to read the integration keys stored in the database; without it they have to be entered again.

### Uninstall

```bash
sudo ./scripts/uninstall.sh           # removes services and code, keeps configuration and data
sudo ./scripts/uninstall.sh --purge   # also deletes configuration, data and the curatarr user
```

---

## Docker on Linux

Requirements: Docker Engine with the Compose plugin, and Git.

```bash
git clone https://github.com/Copas/Curatarr.git curatarr-deploy
cd curatarr-deploy
git checkout v0.2.0rc65
cp .env.example .env
```

Edit `.env`:

- replace `CURATARR_SECRET_KEY=change-me` with a long random value (`openssl rand -hex 32`);
- add `CURATARR_DB_PASSWORD=` followed by a different random value (letters and digits only).

Keep `.env` safe: changing the secret key makes saved integration keys unreadable. Then start it:

```bash
docker compose up -d --build
docker compose ps                        # web should become "healthy"
curl -fsS http://127.0.0.1:8787/health
```

The stack is PostgreSQL (`db`), `web` and `worker`. Data lives in the `database` and `config` volumes, and migrations run when `web` starts. The supplied `compose.yaml` publishes the interface on `127.0.0.1:8787` only; to reach it from other computers change the `ports` entry to `"8787:8787"` (and see [HTTPS and remote access](#https-and-remote-access)).

If Jellyfin, Sonarr or Radarr run in Docker on the same host, use their container or host addresses in Curatarr, not `localhost` (which is Curatarr's own container).

**Upgrade:** back up the volumes, then `git fetch --tags`, `git checkout <new-tag>` and `docker compose up -d --build`.
**Logs:** `docker compose logs --tail=100 web worker`.
**Never** run `docker compose down --volumes` unless you mean to delete the database.

### Chapter generation in Docker

The default image has no media tools. Create `compose.override.yaml` beside `compose.yaml`, which Compose reads automatically:

```yaml
services:
  worker:
    build:
      context: .
      args:
        EXTRAS: chapters-gpu        # or "chapters" for CPU only
    volumes:
      - /path/to/media:/media       # writable: chapters are written into the files
    deploy:                         # GPU only; needs the NVIDIA Container Toolkit
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

Then run `docker compose up -d --build`. Only the worker needs the media. In Settings → Enhance, add a path mapping from how Sonarr and Radarr see the files to `/media` (for example `/tv => /media/tv`), and point the Ollama URL at an Ollama the container can reach (for Ollama on the host, `http://host.docker.internal:11434` with `extra_hosts: ["host.docker.internal:host-gateway"]` on the worker, or the host's LAN address). Transcription models download to `/config/models`.

---

## Windows

Curatarr runs on Windows inside **Docker Desktop**. There's no native Windows service.

### 1. Install the prerequisites

1. Install **[Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/)** and choose the **WSL 2** backend when asked. Restart if prompted, then start Docker Desktop and wait until it says *Engine running*.
2. Install **[Git for Windows](https://git-scm.com/download/win)** with the default options.

### 2. Get Curatarr

Open **PowerShell** in a folder where you keep apps (for example `C:\Apps`) and run:

```powershell
git clone https://github.com/Copas/Curatarr.git curatarr-deploy
Set-Location curatarr-deploy
git checkout v0.2.0rc65
Copy-Item .env.example .env
notepad .env
```

### 3. Set the two secrets

In Notepad:

- replace `change-me` in `CURATARR_SECRET_KEY=change-me` with a long random value;
- add a line `CURATARR_DB_PASSWORD=` followed by a different random value (letters and digits only).

To generate a random value in PowerShell: `-join ((48..57)+(97..122) | Get-Random -Count 48 | % {[char]$_})`. Save and close Notepad. Keep this file: changing the secret key later makes saved integration keys unreadable.

### 4. Start it

```powershell
docker compose up -d --build
docker compose ps
(Invoke-RestMethod http://127.0.0.1:8787/api/v1/status).version
```

The first build takes a few minutes. When `web` shows *healthy*, open **http://localhost:8787** and continue with [First run](#first-run).

Out of the box Curatarr only answers on this PC. To use it from phones and other computers (household members, or Jellyfin's webhook from another machine), change `"127.0.0.1:8787:8787"` to `"8787:8787"` in `compose.yaml`, run `docker compose up -d`, allow port 8787 through Windows Firewall, and open `http://<this-pc>:8787`.

### Connecting to Jellyfin, Sonarr and Radarr on Windows

- **On the same PC:** use `host.docker.internal` instead of `localhost`, for example `http://host.docker.internal:8096` for Jellyfin, `:8989` for Sonarr and `:7878` for Radarr. Windows Firewall must allow those apps to accept connections from Docker Desktop.
- **On another machine:** use its normal address, such as `http://media-server:8096`.
- **Jellyfin's webhook:** from Jellyfin on the same PC, send to `http://127.0.0.1:8787/api/v1/webhook/jellyfin`. From another machine, publish the port to your network first by changing `"127.0.0.1:8787:8787"` to `"8787:8787"` in `compose.yaml` and running `docker compose up -d`.
- **Free-space rules:** enter the disk path exactly as Sonarr or Radarr shows it, such as `D:\Media\Movies` or `\\nas\Media\Movies`. Curatarr matches drive letters and UNC paths regardless of slash style or case. Use Retention → Preview to confirm the match.

### Chapter generation on Windows

Create `compose.override.yaml` in the `curatarr-deploy` folder:

```yaml
services:
  worker:
    build:
      context: .
      args:
        EXTRAS: chapters-gpu        # or "chapters" without an NVIDIA GPU
    volumes:
      - "D:/Media:/media"           # your media folder; must be writable
    deploy:                         # NVIDIA GPU only; works through WSL 2
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

Run `docker compose up -d --build` again. For media on a NAS, map it in Docker Desktop's *Settings → Resources → File sharing*, or mount the share into the container. Install **[Ollama for Windows](https://ollama.com/download/windows)**, run `ollama pull qwen3:14b` in PowerShell, and set the Ollama URL in Chapter settings to `http://host.docker.internal:11434`. Add a path mapping in Chapter settings from how Sonarr and Radarr see the files (for example `D:\Media\TV`) to the container path (`/media/TV`).

### Upgrade, logs and backups on Windows

Before upgrading, keep a copy of `.env` and back up the `database` and `config` volumes (Docker Desktop → *Volumes* → each volume → *Export*). Then:

```powershell
Set-Location curatarr-deploy
git fetch --tags
git checkout <new-release-tag>
docker compose up -d --build
```

Logs: `docker compose logs --tail=100 web worker`. Don't use `docker compose down --volumes`: it deletes the database and saved posters.

---

## First run

1. **Sign in.** Open `http://<your-server>:8787` and sign in with a Jellyfin **administrator** account. On the very first sign-in, also enter your Jellyfin server's address (unless `CURATARR_JELLYFIN_URL` is set). Do this before exposing Curatarr beyond a trusted network: until then, anyone who can reach the page could bind it to a different server.
2. **Connect your apps.** Follow the **Setup** checklist (Settings → Setup) and add the API keys in Settings → Integrations:
   - **Jellyfin:** Dashboard → API Keys → **+** (default port 8096)
   - **Sonarr** (for TV): Settings → General → Security → API Key (default port 8989)
   - **Radarr** (for films): Settings → General → Security → API Key (default port 7878)

   Connect one or both. Titles only Jellyfin knows about are shown but never managed: Curatarr deletes and downloads only through Sonarr and Radarr.
3. **Set up the webhook** (recommended). Curatarr's Integrations page shows these steps with your exact address and a template to copy:
   - Jellyfin Dashboard → Plugins → Webhook → **Add Generic Destination**, URL `http://<your-server>:8787/api/v1/webhook/jellyfin`
   - Notification types **Playback Start**, **Playback Stop** and **User Data Saved**; item types **Movies**, **Episodes** and **Series**
   - Leave *Send All Properties* off and paste the template from Curatarr
   - Add the request header `X-Curatarr-Token` with the token shown once on the Integrations page (generate a new one there if you missed it)

   Without the webhook, the hourly reconciliation still picks up playback, just later.

   **Optional: the Curatarr plugin for Jellyfin.** With it, Delete in any Jellyfin app marks a film or show Leaving Soon instead of deleting its files behind Radarr's and Sonarr's backs. In Settings → Integrations, under *Curatarr plugin for Jellyfin*, click **Install in Jellyfin**. Curatarr installs it, restarts Jellyfin and sets it up when Jellyfin is back, so there's nothing to type or copy. See [jellyfin-plugin](../jellyfin-plugin/README.md).
4. **Discover.** Click **Discover** on Overview. It runs in the background; a large library takes about a minute. Settings → Operations shows when it's done.
5. **Review the rules.** Look over Settings → Acquisition and Settings → Retention. Every library starts in **dry run**: Curatarr goes through the whole process and records what it *would* remove in History, but deletes nothing. Turn dry run off globally or per library when you're happy.
6. **Look at what it finds.** The checks (problem files, wrong dubs, the wrong show, files Roku can't seek in, …) start straight away, and what they find waits on **Enhance → Problems** with a button to fix each one. Once you trust a kind of fix, switch it on in **Settings → Automatic** and Curatarr does it by itself from then on. Find Favorites, which adds the rest of a favourite film's series to Radarr, is there too.
7. **Optional:** switch on chapters per library in Settings → Enhance, allow household members in Settings → Integrations → Household access, connect Seerr on the same page, and look over Settings → Household choices (requests, import lists, seasonal shelves).

---

## HTTPS and remote access

Curatarr serves plain HTTP. To reach it from outside your home network, put it behind a reverse proxy that terminates TLS (Caddy, nginx, Traefik, or your NAS's built-in one) and set `CURATARR_SESSION_COOKIE_SECURE=true`. Sign-in is checked against Jellyfin and repeated failures are rate-limited, but a VPN is still the safer way in.

---

## Why install from a release

A git checkout is **source code**. Your running Curatarr is an **installed copy** with its own code, configuration and data, kept outside the checkout:

| | Source checkout | Native install | Docker |
|---|---|---|---|
| Purpose | Development, tests, releases | Your real Curatarr | Your real Curatarr |
| Code | Working tree, any branch | `/opt/curatarr/current` (a tagged release) | Image built from a tagged release |
| Configuration | Shell variables, `.env` (git-ignored) | `/etc/curatarr/curatarr.env` | `.env` beside `compose.yaml` |
| Data | `instance/` (git-ignored, disposable) | `/var/lib/curatarr` | Volumes `config` and `database` |
| Runs as | Your shell | `curatarr-web` and `curatarr-worker` services | `web` and `worker` containers |

So changing, testing or resetting a checkout never touches an installation, and nothing from an installation (its database, keys, posters or logs) can end up in Git. See [decision 008](decisions/008-source-and-installed-copies.md).
