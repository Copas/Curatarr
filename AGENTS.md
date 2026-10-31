# Working on Curatarr

Notes for anyone changing this repository, people and coding agents alike (Codex, Claude Code and others read this file). [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) has the setup, tests and release checklist.

## Before every commit

- Run the whole test suite and the linters, and commit only when all pass:
  `.venv/bin/pytest && .venv/bin/ruff check . && .venv/bin/ruff format --check .`
  In a pipeline, use `set -o pipefail` so a failing test isn't hidden by `tail`.
- The pre-commit hook in `scripts/git-hooks/` runs exactly these checks (after the personal-data check) and refuses the commit if any fails. It also refuses when changes are left unstaged or new files aren't added, because then the tests wouldn't run on exactly what's committed. Turn it on once per checkout with `git config core.hooksPath scripts/git-hooks`. **Never bypass it** (`git commit --no-verify`, `-n`, or changing `core.hooksPath` for one commit): a failing check means the commit isn't ready.
- After pushing, check the **Tests** run on GitHub (`gh run list --workflow tests.yml --limit 1`, or `gh run watch`): it runs on a clean machine and catches what a local checkout hides. If it fails, fixing it comes before anything else.
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

This applies to every example too: a changelog line, a test fixture, a code comment or a commit message describing what you just did on your own server. Write it with made-up names and paths from the start; don't copy a real one and plan to fix it later. Once pushed, it's public for good: rewriting history doesn't remove GitHub's cached copies.

Two checks back this up:

- **`tests/test_no_personal_data.py`** (part of the test suite) fails on home folders, private network addresses, real email addresses and AI session links, in tracked files and in every commit message.
- **The commit hooks in `scripts/git-hooks/`** refuse a commit whose changes or message contain a word from your own list of names, server names and paths, kept outside the repository at `~/.config/curatarr-dev/private-words`. They also refuse an author email that isn't GitHub's private one, and AI co-author or session lines. Turn them on once per checkout: `git config core.hooksPath scripts/git-hooks`. Agents: if `git config core.hooksPath` doesn't print `scripts/git-hooks`, tell the person before committing.

## Releases

- **Curatarr**: the checklist in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#release-checklist). Move **Unreleased** in `CHANGELOG.md` into a dated `## X.Y.Z` section, bump `pyproject.toml` and the version in the install instructions, and push an annotated `vX.Y.Z` tag.
- **The Jellyfin plugin**: [jellyfin-plugin/README.md](jellyfin-plugin/README.md#releasing). Each version needs notes in `CHANGELOG.md` under **Jellyfin plugin**; they become the GitHub release's notes and the changelog Jellyfin shows, and the release workflow refuses a version without them.
- Never rewrite published history or move a published tag unless the repository's owner asks for it.
