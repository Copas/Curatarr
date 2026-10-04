# Curatarr

Curatarr is an independent Jellyfin, Sonarr, and Radarr lifecycle manager. It observes playback, stages TV acquisition one season ahead, evaluates retention rules, and presents cleanup decisions for review. Destructive actions begin in dry-run mode and are sent only through Sonarr or Radarr after fresh validation.

This is an early implementation. See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) for completed behavior and current gaps before connecting it to a real library.

## Demo mode

Demo mode uses synthetic adapters and never contacts private services. After creating the database, set `CURATARR_DEMO_MODE=true` and run `flask --app curatarr demo-seed`. The overview includes controls for playback, favorites, time/size changes, and simulated outages. Run `flask --app curatarr worker --once` or use Reconcile in the UI to evaluate the sample libraries.

## Local development

Python 3.12 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
export CURATARR_SECRET_KEY='replace-with-a-random-secret'
.venv/bin/flask --app curatarr db upgrade
.venv/bin/flask --app curatarr encrypt-secrets
.venv/bin/python -m curatarr
```

In a second shell, run the persistent worker:

```bash
.venv/bin/flask --app curatarr worker
```

The web app listens on port 8787 by default. Until setup is complete, Overview links to the `/setup` checklist: connect Jellyfin, Sonarr, and Radarr in Settings, create the webhook token, run Discover from Overview, then optionally review the Acquisition and Retention rules. Saved keys are masked in the UI. The first Settings visit generates a webhook token and shows it once. Configure Jellyfin's webhook plugin to send JSON to `/api/v1/webhook/jellyfin` with that token in the `X-Curatarr-Token` request header. The webhook stays closed until the token exists.

Use a unique, stable random `CURATARR_SECRET_KEY` before starting. Integration keys and the webhook token are encrypted in the database using a key derived from `CURATARR_SECRET_KEY`; losing or changing that key makes them unreadable. Back up the key separately from the database. Existing installations should back up the database, run `db upgrade`, then run `encrypt-secrets` once; the command is safe to repeat.

## Signing in

Sign in with a Jellyfin account that has the Administrator permission, which is the permission Jellyfin uses for server configuration. Other Jellyfin accounts are refused. Curatarr checks the password with Jellyfin, ends the Jellyfin session it created, and keeps only the user's ID and name in a signed session cookie that lasts up to seven days. Passwords and Jellyfin user tokens are never stored. Every five minutes Curatarr re-checks the account using its own Jellyfin API key, so a removed, disabled, or demoted administrator loses access. If Jellyfin is unreachable, an existing session continues for up to one hour after its last successful check. After five failed sign-ins from one address within 15 minutes, further attempts are refused until the window passes.

On first run, the sign-in page also asks for the Jellyfin server URL and binds Curatarr to it. Until that first administrator signs in, anyone who can reach the page could bind a different server, so keep Curatarr on a trusted network until setup is done. Alternatively, set `CURATARR_JELLYFIN_URL` so the server is fixed from the start. After sign-in, add a Jellyfin API key in Settings; it is needed for library discovery and the periodic account re-check.

`/health`, `/api/v1/status`, and the token-protected webhook stay public for dashboards. Every other page and API endpoint requires sign-in. Set `CURATARR_ALLOW_UNAUTHENTICATED=true` only when another layer, such as an authenticating reverse proxy, already restricts access. In demo mode, sign in as `demo-admin` with password `demo`; `demo-viewer` shows the refusal for non-administrators.

## Retention behavior

The default TV minimum is the first three episodes of Season 1. A completed episode may request missing episodes in that season and the next known season. Any playback resets inactivity and rescues pending cleanup. The default grace period is 30 days and inactivity period is 90 days. Movie cleanup is retention only; deleted movies are never automatically reacquired.

Policy resolves title override → library → global → built-in default. The Acquisition and Retention pages edit global defaults or any one library, and show each effective value with its source. Library-size limits, disk pressure, and dry run can only be set per library. A global change is rejected if it would leave any library with an invalid effective policy. Each library can override acquisition, retention, quota, disk-pressure, review mode, and dry-run settings. Review mode can also be set per cleanup rule: inactivity, quota, and low/critical disk pressure each have their own mode that inherits the library review mode when left blank, so one rule can run automatically while the others still require review. A title-level review mode overrides all rule modes. Title overrides include Never Purge, thresholds, minimum footprint, and review behavior. The review queue offers Delete, Keep, Snooze, and Never Purge. Quota rules require high-water bytes strictly above low-water bytes. Disk-pressure rules require an explicit path as seen by Sonarr or Radarr; when that path cannot be matched to a reported volume, Curatarr does not select media on disk pressure alone. All decisions and outcomes are written to History.

Dry run is enabled by default. A library must explicitly set dry run to false before destructive arr calls are possible. Before each deletion, Curatarr rechecks mapping, activity, grace, policy, queue state, and integration health. If it cannot verify these, it blocks deletion.

## Containers

Set `CURATARR_DB_PASSWORD` and `CURATARR_SECRET_KEY` (and optionally `CURATARR_JELLYFIN_URL`) in your deployment environment, then run `docker compose up --build`. To use HTTPS, put Curatarr behind a TLS-terminating reverse proxy and set `CURATARR_SESSION_COOKIE_SECURE=true`. The compose example binds the UI to `127.0.0.1:8787` and uses PostgreSQL and persistent database/config volumes. It runs migrations and legacy-secret encryption on web startup. If running outside compose, run `flask --app curatarr db upgrade` and `flask --app curatarr encrypt-secrets` before starting each upgraded application version unless you intentionally set `CURATARR_AUTO_MIGRATE=true`.

The application requires no media volume. Poster snapshots are stored under application data and are never part of the source repository.

## API and health

Read-only endpoints (all except `/health` and `/api/v1/status` require sign-in): `/health`, `/api/v1/status`, `/api/v1/metrics`, `/api/v1/libraries`, `/api/v1/review/summary`, and `/api/v1/history`. The webhook endpoint is `/api/v1/webhook/jellyfin`.

## Logs and metrics

Operational log lines from the `curatarr.ops` logger are single JSON objects. They are limited to `event_id`, `action_id`, `media_identity_id`, `candidate_id`, `integration`, `operation`, `duration_ms`, `result`, and `status_code`, and never include API keys or payloads. Each external call, processed event, executed action, and reconciliation is logged. Successful GET requests log at DEBUG, failures at WARNING, and everything else at INFO. Set the level with `CURATARR_LOG_LEVEL`.

`/api/v1/metrics` and the Operations table on Overview report: events processed, duplicate events ignored, acquisition actions, purge candidates created, rescues, bytes proposed and actually reclaimed, external API failures per integration, last reconciliation duration, pending actions, and oldest pending action age. Most values are computed from stored history. Duplicate-event and API-failure counts are held in memory and written to the `metric_counters` table when each request or worker cycle ends.

## Tests

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Tests use synthetic media names and mocked integration boundaries. No real credentials or private media data belong in this repository.
PostgreSQL contention tests are opt-in: point `CURATARR_TEST_POSTGRES_URL` at an isolated, migrated test database and run `pytest tests/test_postgres_concurrency.py`. They skip during the normal SQLite test run.

## Working on Curatarr

Configuration comes from the environment; `.env.example` lists every variable. The database URL is read from `DATABASE_URL`. If it is unset, Curatarr uses SQLite at `instance/curatarr.db`, and poster snapshots go to `instance/posters/` (or `CURATARR_DATA_DIR`). Both are git-ignored local state. Before running migrations, downgrades, or demo seeding for verification, point `DATABASE_URL` and `CURATARR_DATA_DIR` at a scratch location so your working database is not modified. The test suite already uses temporary databases and data directories.

`IMPLEMENTATION_STATUS.md` tracks the current phase, open work, known issues, and blockers. `docs/decisions/` records design decisions. Update both alongside behavior changes.

Release checklist (spec section 92), run against a scratch database:

```bash
export DATABASE_URL=sqlite:////tmp/curatarr-release.db CURATARR_DATA_DIR=/tmp/curatarr-release
.venv/bin/pytest && .venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/flask --app curatarr db upgrade
.venv/bin/flask --app curatarr db check            # models match migrations
.venv/bin/flask --app curatarr db downgrade base
.venv/bin/flask --app curatarr db upgrade
```

Then start the app on a fresh database and in demo mode (`demo-seed`, `worker --once`), confirm `/health` and the main pages load, update `CHANGELOG.md`, and create an annotated `vX.Y.Z` tag.
