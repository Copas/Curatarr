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
export CURATARR_ALLOW_UNAUTHENTICATED=true  # trusted loopback/LAN only
.venv/bin/flask --app curatarr db upgrade
.venv/bin/flask --app curatarr encrypt-secrets
.venv/bin/python -m curatarr
```

In a second shell, run the persistent worker:

```bash
.venv/bin/flask --app curatarr worker
```

The web app listens on port 8787 by default. Open Settings to enter Jellyfin, Sonarr, and Radarr URLs and keys, then run Discover from Overview. Saved keys are masked in the UI. The first Settings visit generates a webhook token and shows it once. Configure Jellyfin's webhook plugin to send JSON to `/api/v1/webhook/jellyfin` with that token in the `X-Curatarr-Token` request header. The webhook stays closed until the token exists.

Use a unique, stable random `CURATARR_SECRET_KEY` before starting. Curatarr currently has no login system: unauthenticated operation must be explicitly enabled and the UI must be restricted to a trusted loopback/LAN or an authenticated reverse proxy. Browser mutations use CSRF tokens, which do not replace authentication. Integration keys and the webhook token are encrypted in the database using a key derived from `CURATARR_SECRET_KEY`; losing or changing that key makes them unreadable. Back up the key separately from the database. Existing installations should back up the database, run `db upgrade`, then run `encrypt-secrets` once; the command is safe to repeat.

## Retention behavior

The default TV minimum is the first three episodes of Season 1. A completed episode may request missing episodes in that season and the next known season. Any playback resets inactivity and rescues pending cleanup. The default grace period is 30 days and inactivity period is 90 days. Movie cleanup is retention only; deleted movies are never automatically reacquired.

Each library can override acquisition, retention, quota, disk-pressure, review mode, and dry-run settings. Review mode can also be set per cleanup rule: inactivity, quota, and low/critical disk pressure each have their own mode that inherits the library review mode when left blank, so one rule can run automatically while the others still require review. A title-level review mode overrides all rule modes. Title overrides include Never Purge, thresholds, minimum footprint, and review behavior. The review queue offers Delete, Keep, Snooze, and Never Purge. Quota rules require high-water bytes strictly above low-water bytes. Disk-pressure rules require an explicit path as seen by Sonarr or Radarr; when that path cannot be matched to a reported volume, Curatarr does not select media on disk pressure alone. All decisions and outcomes are written to History.

Dry run is enabled by default. A library must explicitly set dry run to false before destructive arr calls are possible. Before each deletion, Curatarr rechecks mapping, activity, grace, policy, queue state, and integration health. If it cannot verify these, it blocks deletion.

## Containers

Set `CURATARR_DB_PASSWORD`, `CURATARR_SECRET_KEY`, and `CURATARR_ALLOW_UNAUTHENTICATED=true` in your deployment environment only after protecting access to the UI, then run `docker compose up --build`. The compose example binds the UI to `127.0.0.1:8787` and uses PostgreSQL and persistent database/config volumes. It runs migrations and legacy-secret encryption on web startup. If running outside compose, run `flask --app curatarr db upgrade` and `flask --app curatarr encrypt-secrets` before starting each upgraded application version unless you intentionally set `CURATARR_AUTO_MIGRATE=true`.

The application requires no media volume. Poster snapshots are stored under application data and are never part of the source repository.

## API and health

Read-only endpoints: `/health`, `/api/v1/status`, `/api/v1/libraries`, `/api/v1/review/summary`, and `/api/v1/history`. The webhook endpoint is `/api/v1/webhook/jellyfin`.

## Tests

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Tests use synthetic media names and mocked integration boundaries. No real credentials or private media data belong in this repository.
PostgreSQL contention tests are opt-in: point `CURATARR_TEST_POSTGRES_URL` at an isolated, migrated test database and run `pytest tests/test_postgres_concurrency.py`. They skip during the normal SQLite test run.
