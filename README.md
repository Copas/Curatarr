# Curatarr

Curatarr is an independent Jellyfin, Sonarr, and Radarr lifecycle manager. It observes playback, stages TV acquisition one season ahead, evaluates retention rules, and presents cleanup decisions for review. Destructive actions begin in dry-run mode and are sent only through Sonarr or Radarr after fresh validation.

This is an early implementation. See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) for completed behavior and current gaps before connecting it to a real library.

> **This repository is source code, not an installation.** Run Curatarr from an installed copy made from a tagged release, never from a git checkout. An installed copy keeps its own code, configuration, and data outside the checkout (`/opt/curatarr`, `/etc/curatarr`, `/var/lib/curatarr`, or Docker volumes). Development here cannot change it, and its database, keys, posters, and logs cannot end up in Git. See [docs/INSTALL.md](docs/INSTALL.md).

## Installing

- **Native (Linux, systemd):** from a release checkout, run `sudo ./scripts/install.sh`. Re-run it from a newer release to upgrade; it backs up the database first.
- **Docker:** in a separate deployment directory, copy `.env.example` to `.env`, set the secrets, and run `docker compose up -d --build`.

[docs/INSTALL.md](docs/INSTALL.md) covers both in full, plus configuration, upgrades, rollback, backup, and uninstalling.

## First run

Open `http://<host>:8787` and sign in with a Jellyfin administrator account (see Signing in below). Until setup is complete, Overview links to the `/setup` checklist: connect Jellyfin, Sonarr, and Radarr in Settings, create the webhook token, run Discover from Overview, then optionally review the Acquisition and Retention rules. Saved keys are masked in the UI. The first Settings visit generates a webhook token and shows it once. Configure Jellyfin's webhook plugin to send JSON to `/api/v1/webhook/jellyfin` with that token in the `X-Curatarr-Token` request header. The webhook stays closed until the token exists.

Integration keys and the webhook token are encrypted in the database with a key derived from `CURATARR_SECRET_KEY`. Losing or changing that key makes them unreadable, so back up the key together with the database. The application needs no media volume; deletion always goes through Sonarr or Radarr.

## Signing in

Sign in with a Jellyfin account that has the Administrator permission, which is the permission Jellyfin uses for server configuration. Other Jellyfin accounts are refused. Curatarr checks the password with Jellyfin, ends the Jellyfin session it created, and keeps only the user's ID and name in a signed session cookie that lasts up to seven days. Passwords and Jellyfin user tokens are never stored. Every five minutes Curatarr re-checks the account using its own Jellyfin API key, so a removed, disabled, or demoted administrator loses access. If Jellyfin is unreachable, an existing session continues for up to one hour after its last successful check. After five failed sign-ins from one address within 15 minutes, further attempts are refused until the window passes.

On first run, the sign-in page also asks for the Jellyfin server URL and binds Curatarr to it. Until that first administrator signs in, anyone who can reach the page could bind a different server, so keep Curatarr on a trusted network until setup is done. Alternatively, set `CURATARR_JELLYFIN_URL` so the server is fixed from the start. After sign-in, add a Jellyfin API key in Settings; it is needed for library discovery and the periodic account re-check.

`/health`, `/api/v1/status`, and the token-protected webhook stay public for dashboards. Every other page and API endpoint requires sign-in. Set `CURATARR_ALLOW_UNAUTHENTICATED=true` only when another layer, such as an authenticating reverse proxy, already restricts access. In demo mode, sign in as `demo-admin` with password `demo`; `demo-viewer` shows the refusal for non-administrators.

## Retention behavior

Cleanup is driven by free space. Titles nobody has played are not removed just for being unwatched; a library can opt into that with "Clean up unwatched titles even when space is fine". When free space falls below a library's low threshold, Curatarr picks titles in purge-strategy order, longest unwatched first by default, and only enough to get back to the threshold. They show as Leaving Soon for the notice period (14 days by default) and are removed when it ends unless someone watches them. Below the critical threshold, removal happens without a notice. Dry run must be turned off per library before anything is actually removed. The default TV minimum is the first three episodes of Season 1. A completed episode may request missing episodes in that season and the next known season. Requests for a whole season also monitor that season in Sonarr, so episodes announced later are downloaded too. Shows tagged `curatarr-pilot` in Sonarr (for example by an import list that adds shows to try out) keep only what Sonarr monitors, usually the pilot, until someone watches an episode; then the usual rules apply. Any playback resets inactivity and rescues pending cleanup. The default grace period is 30 days and inactivity period is 90 days. Movie cleanup is retention only; deleted movies are never automatically reacquired.

Policy resolves title override → library → global → built-in default. The Acquisition and Retention pages edit global defaults or any one library, and show each effective value with its source. Every setting, including library-size limits, free-space enforcement, and dry run, can be set globally or per library. Library-size limits apply to each library separately; free space is measured per disk and shared by every library on it. A global change is rejected if it would leave any library with an invalid effective policy. Each library can override acquisition, retention, quota, disk-pressure, review mode, and dry-run settings. Review mode can also be set per cleanup rule: inactivity, quota, and low/critical disk pressure each have their own mode that inherits the library review mode when left blank, so one rule can run automatically while the others still require review. A title-level review mode overrides all rule modes. Title overrides include Never Purge, thresholds, minimum footprint, and review behavior. The review queue offers Delete, Keep, Snooze, and Never Purge. Quota rules require high-water bytes strictly above low-water bytes. Disk-pressure rules require an explicit path as seen by Sonarr or Radarr; when that path cannot be matched to a reported volume, Curatarr does not select media on disk pressure alone. All decisions and outcomes are written to History.

Dry run is on by default. In dry run, Curatarr goes through the whole process and records what it would remove in History, but never asks Sonarr or Radarr to delete anything. The banner at the top of every page says which libraries are in dry run. Turn it off globally or per library. Before each deletion, Curatarr rechecks mapping, activity, grace, policy, queue state, and integration health. If it cannot verify these, it blocks deletion.

## API and health

Read-only endpoints (all except `/health` and `/api/v1/status` require sign-in): `/health`, `/api/v1/status`, `/api/v1/metrics`, `/api/v1/libraries`, `/api/v1/review/summary`, and `/api/v1/history`. The webhook endpoint is `/api/v1/webhook/jellyfin`.

## Dashboard integration

Curatarr runs on its own and does not depend on any dashboard. To add it to a home dashboard, link to these pages and endpoints:

- `/` for the overview and `/review` for the review queue (both require sign-in);
- `/health` for a health check and `/api/v1/status` for review and Leaving Soon counts, dry-run state, and integration health (both public, no secrets);
- `/static/logo.svg` for the app icon. The file is also in the repository at `curatarr/static/logo.svg` to copy into a dashboard's own assets.

## Logs and metrics

Operational log lines from the `curatarr.ops` logger are single JSON objects. They are limited to `event_id`, `action_id`, `media_identity_id`, `candidate_id`, `integration`, `operation`, `duration_ms`, `result`, and `status_code`, and never include API keys or payloads. Each external call, processed event, executed action, and reconciliation is logged. Successful GET requests log at DEBUG, failures at WARNING, and everything else at INFO. Set the level with `CURATARR_LOG_LEVEL`.

`/api/v1/metrics` and the Operations table on Overview report: events processed, duplicate events ignored, acquisition actions, purge candidates created, rescues, bytes proposed and actually reclaimed, external API failures per integration, last reconciliation duration, pending actions, and oldest pending action age. Most values are computed from stored history. Duplicate-event and API-failure counts are held in memory and written to the `metric_counters` table when each request or worker cycle ends.

## Developing Curatarr

Everything in this section uses the source checkout. The database and posters it creates under `instance/` belong to the checkout only. They are a disposable development database, separate from any installation.

### Local development

Python 3.12 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
export CURATARR_SECRET_KEY='replace-with-a-random-secret'
export CURATARR_DEMO_MODE=true          # synthetic data; never contacts real services
.venv/bin/flask --app curatarr db upgrade
.venv/bin/flask --app curatarr demo-seed
.venv/bin/python -m curatarr
```

In a second shell, run the worker with `.venv/bin/flask --app curatarr worker`. The development server listens on port 8787. In demo mode, sign in as `demo-admin` with password `demo`.

### Demo mode

Demo mode uses synthetic adapters and never contacts private services. After creating the database, set `CURATARR_DEMO_MODE=true` and run `flask --app curatarr demo-seed`. The overview includes controls for playback, favorites, time/size changes, and simulated outages. Run `flask --app curatarr worker --once` or use Reconcile in the UI to evaluate the sample libraries.

### Tests

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Tests use synthetic media names and mocked integration boundaries. No real credentials or private media data belong in this repository.

PostgreSQL contention tests are opt-in: point `CURATARR_TEST_POSTGRES_URL` at an isolated, migrated test database and run `pytest tests/test_postgres_concurrency.py`. They skip during the normal SQLite test run.

`tests/test_repository_hygiene.py` keeps the repository publishable (spec section 99). It fails if a tracked file is an `.env`, database, log, key, or `instance/` file, is larger than 1 MB, or contains a private IPv4 address or 32-hex-character key. Run the full test suite before every push.

### Configuration and records

Configuration comes from the environment; `.env.example` lists every variable. The database URL is read from `DATABASE_URL`. If it is unset, Curatarr uses SQLite at `instance/curatarr.db`, and poster snapshots go to `instance/posters/` (or `CURATARR_DATA_DIR`). Both are git-ignored local state. Before running migrations, downgrades, or demo seeding for verification, point `DATABASE_URL` and `CURATARR_DATA_DIR` at a scratch location so your working database is not modified. The test suite already uses temporary databases and data directories.

`IMPLEMENTATION_STATUS.md` tracks the current phase, open work, known issues, and blockers. `docs/decisions/` records design decisions. Update both alongside behavior changes.

### Release checklist

Run against a scratch database (spec section 92):

```bash
export DATABASE_URL=sqlite:////tmp/curatarr-release.db CURATARR_DATA_DIR=/tmp/curatarr-release
.venv/bin/pytest && .venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/flask --app curatarr db upgrade
.venv/bin/flask --app curatarr db check            # models match migrations
.venv/bin/flask --app curatarr db downgrade base
.venv/bin/flask --app curatarr db upgrade
```

Also build the release the way users install it, into scratch locations, and confirm the installed copy starts outside the checkout:

```bash
PREFIX=/tmp/c/opt CONFIG_DIR=/tmp/c/etc DATA_DIR=/tmp/c/data \
  SERVICE_USER=$(id -un) NO_SYSTEMD=1 ./scripts/install.sh
```

Then start the app on a fresh database and in demo mode (`demo-seed`, `worker --once`), confirm `/health` and the main pages load, update `CHANGELOG.md`, and create an annotated `vX.Y.Z` tag.
