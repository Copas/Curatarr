# Working on Curatarr

Notes for anyone changing this repository, people and coding agents alike (Codex, Claude Code and others read this file). [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) has the setup, tests and release checklist.

## Before every commit

- Run the whole test suite and the linters, and commit only when all pass:
  `.venv/bin/pytest && .venv/bin/ruff check . && .venv/bin/ruff format --check .`
  In a pipeline, use `set -o pipefail` so a failing test isn't hidden by `tail`.
- For the Jellyfin plugin: `dotnet test jellyfin-plugin/Jellyfin.Plugin.Curatarr.Tests`.
- Add a line to `CHANGELOG.md` under **Unreleased** for anything a user would notice: what changed and why, in plain words (a fix says what went wrong). Update `docs/FEATURES.md`, and the README's feature list when a feature is added or its default changes.

## Commits

- A commit is authored by the person the work is for, under their own name and email (GitHub's private `…@users.noreply.github.com` address keeps a real one out of the history). An agent doesn't commit under its own name.
- No `Co-Authored-By:` or session trailers naming an AI tool.
- The message says what changed and why, in a short subject and a wrapped body.

## A public, anonymous repository

Curatarr is open source and runs on anyone's server, so nothing in code, tests, docs, commit messages or screenshots may identify a particular household:

- no real people's names (use made-up ones such as Alex, Sam and Pat), no real server names, addresses or paths (use `/mnt/media`, `/path/to/…`);
- no keys, tokens or passwords, even as examples that look real;
- no photos of real people, posters made from them included;
- nothing that depends on tools outside this repository: Curatarr works on its own.

## Releases

- **Curatarr**: the checklist in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#release-checklist). Move **Unreleased** in `CHANGELOG.md` into a dated `## X.Y.Z` section, bump `pyproject.toml` and the version in the install instructions, and push an annotated `vX.Y.Z` tag.
- **The Jellyfin plugin**: [jellyfin-plugin/README.md](jellyfin-plugin/README.md#releasing). Each version needs notes in `CHANGELOG.md` under **Jellyfin plugin**; they become the GitHub release's notes and the changelog Jellyfin shows, and the release workflow refuses a version without them.
- Never rewrite published history or move a published tag unless the repository's owner asks for it.
