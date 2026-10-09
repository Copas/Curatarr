# 008: Source checkouts and installed copies are separate

The repository is published on GitHub and must stay free of deployment data. A real installation must also be unaffected by development work in a checkout. The two are therefore kept apart:

- The Python package contains everything needed to run: templates, static files, and Alembic migrations (`curatarr/migrations`). An installed copy never needs the source tree. Earlier, migrations lived at the repository root and package data was undeclared. A wheel installed elsewhere had no templates and reported "migration required" on every request, and the Docker image only worked because it ran from a copy of the source.
- `scripts/install.sh` installs a release as native systemd services. Code goes to `/opt/curatarr/releases/<timestamp>-<suffix>` with a `current` symlink, because virtualenvs cannot be moved. Configuration goes to `/etc/curatarr/curatarr.env`, generated once with a random secret key and never overwritten. Data goes to `/var/lib/curatarr`. The installer builds the new code before stopping services, backs up the default SQLite database with SQLite's backup API, migrates using the new release, and only then switches `current`. If an upgrade fails, it restores the previous code and default SQLite database and restarts previously active services. External databases require their own backup and recovery. The script builds from a temporary copy so it never writes to the checkout, and refuses code or data locations inside it. The previous successful release is kept for rollback.
- The Docker image installs the package and runs as a non-root user from `/config`, without the source tree.
- Data in a checkout (`instance/`) is a disposable development database.
- `tests/test_repository_hygiene.py` fails the suite if tracked files include environment files, databases, logs, keys, large files, private IPv4 addresses, or API-key-shaped strings.

Native installs default to SQLite in `/var/lib/curatarr`, which suits one host. PostgreSQL remains available through `DATABASE_URL` and is the Docker default.
