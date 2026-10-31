# Developing Curatarr

Everything here uses a source checkout. The database and posters it creates under `instance/` belong to the checkout only: a disposable development database, separate from any installation (see [Why install from a release](INSTALL.md#why-install-from-a-release)).

## Local development

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

In a second shell, run the worker with `.venv/bin/flask --app curatarr worker`. The development server listens on port 8787.

### Keeping personal data out

The repository is public, and a name or path committed by mistake stays in GitHub's caches even after history is rewritten. Turn on the commit checks once per checkout (the same hooks also run ruff, the formatter check and the whole test suite before every commit, and refuse it if anything fails or isn't staged):

```bash
git config core.hooksPath scripts/git-hooks
mkdir -p ~/.config/curatarr-dev
$EDITOR ~/.config/curatarr-dev/private-words   # your household's names, server names, paths, addresses
```

One word or phrase per line, matched as a whole word with its capitals as written; lines starting with `#` are notes. The list stays outside the repository. Commits whose changes or message contain one of its words are refused, and so are commits under an email other than GitHub's private `…@users.noreply.github.com` one, and messages with AI co-author or session lines. `tests/test_no_personal_data.py`, part of the test suite, catches the generic kinds (home folders, private network addresses, real email addresses) for everyone.

## Demo mode

Demo mode uses synthetic adapters and never contacts private services. After creating the database, set `CURATARR_DEMO_MODE=true` and run `flask --app curatarr demo-seed`. Sign in as `demo-admin` with password `demo`, and enable `demo-viewer` from the household list to try that view. The overview includes controls for playback, favorites, time and size changes, and simulated outages. Run `flask --app curatarr worker --once` or use Reconcile in the UI to evaluate the sample libraries.

## Tests

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Tests use synthetic media names and mocked integration boundaries. A few use real FFmpeg and MKVToolNix on generated files when those are installed, and skip otherwise. No real credentials or private media data belong in this repository.

PostgreSQL contention tests are opt-in: point `CURATARR_TEST_POSTGRES_URL` at an isolated, migrated test database and run `pytest tests/test_postgres_concurrency.py`. They skip during the normal SQLite run.

`tests/test_repository_hygiene.py` keeps the repository publishable (spec section 99). It fails if a tracked file is an `.env`, database, log, key or `instance/` file, is larger than 1 MB, or contains a private IPv4 address or 32-hex-character key. Run the full suite before every push.

## Configuration and records

Configuration comes from the environment; [`.env.example`](../.env.example) lists every variable. The database URL is read from `DATABASE_URL`. If it's unset, Curatarr uses SQLite at `instance/curatarr.db`, and poster snapshots go to `instance/posters/` (or `CURATARR_DATA_DIR`). Both are git-ignored local state. Before running migrations, downgrades or demo seeding for verification, point `DATABASE_URL` and `CURATARR_DATA_DIR` at a scratch location so your working database isn't modified. The test suite already uses temporary databases and data directories.

[IMPLEMENTATION_STATUS.md](../IMPLEMENTATION_STATUS.md) tracks the current phase, open work, known issues and blockers. [docs/decisions/](decisions/) records design decisions. Update both alongside behaviour changes.

## Release checklist

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

Then start the app on a fresh database and in demo mode (`demo-seed`, `worker --once`), confirm `/health` and the main pages load. Move **Unreleased** in `CHANGELOG.md` into a dated `## X.Y.Z` section, bump the version in `pyproject.toml` and in the install instructions (README and `docs/INSTALL.md`), and push an annotated `vX.Y.Z` tag (`git tag -a vX.Y.Z -m "Curatarr X.Y.Z"`).

The Jellyfin plugin is released on its own, with `plugin-v…` tags: see [Releasing](../jellyfin-plugin/README.md#releasing) in its README. Its notes go in `CHANGELOG.md` under **Jellyfin plugin**, and its release workflow won't run without them.

GitHub runs the tests and linters on every push (`.github/workflows/tests.yml`), and the plugin's tests, with a check that its version has notes, when the plugin changes.

## Screenshots

The Overview and Review images in `docs/images/` come from demo mode (`CURATARR_DEMO_MODE=true`, `demo-seed`, `worker --once`), captured at 1280 px wide. Demo mode has no media files, so the Chapter maintenance image shows counts from a real library.
