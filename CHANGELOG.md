# Changelog

## Unreleased

- Overview shows a live reconciliation banner (queued, running with elapsed time, failed with the reason, interrupted, or last finished with counts) and refreshes itself while work is queued or running. Health reports the worker as busy during a long reconciliation instead of offline.
- Discover and Reconcile now queue work for the background worker instead of running inside the web request. A real library's discovery (about 45 seconds) exceeded the web worker's 30-second timeout and returned Internal Server Error. Overview shows reconciliation state and the last finish time.
- Jellyfin Webhook plugin support: completion via PlaybackStop/PlayedToCompletion, marking watched and favorites via UserDataSaved, string values from templates, dashed GUIDs, and bodies not labelled as JSON. Settings shows step-by-step plugin setup with a ready-made template.
- Jellyfin API keys are sent in the `Authorization: MediaBrowser ... Token="..."` header. Jellyfin 12.1 rejects the bare `X-Emby-Token` header with 401, so saving a Jellyfin key failed with "could not be reached".
- Worker: heartbeat recorded every cycle, reconciliation waits for API keys during first-run setup, failed reconciliation retries after 5 minutes instead of every 15 seconds, and integrations without a key report "unconfigured".
- Native installer (`scripts/install.sh`, `scripts/uninstall.sh`) and `docs/INSTALL.md`. Installed copies keep code, configuration, and data outside the source checkout.
- Packaging fix: migrations moved into the package (`curatarr/migrations`) and templates, static files, and migrations are declared as package data, so installed copies run without the source tree. The Docker image now runs the installed package as a non-root user.
- Repository hygiene test for publishable tracked files.
- Review queue shows library, media type, proposed action, last watcher, acquisition date, and meaningful-watch state.
- Sign-in with Jellyfin administrator accounts. Sign-in is now required by default; `CURATARR_ALLOW_UNAUTHENTICATED=true` remains for deployments behind an authenticating proxy. New optional `CURATARR_JELLYFIN_URL`.
- First-run setup checklist; Overview shows scheduled deletions and recent cleanup.
- Acquisition and Retention rule pages with global defaults and per-library scopes. Policy saves now merge with stored values instead of replacing them.
- Structured JSON operational logs, `CURATARR_LOG_LEVEL`, durable metric counters, `/api/v1/metrics`, and an Operations table on Overview. Requires `flask --app curatarr db upgrade` (adds `metric_counters`).
- Tests keep instance data in temporary directories.

## 0.1.0 (2026-10-04)

First tagged development release. Validated against demo and mocked integrations only; see IMPLEMENTATION_STATUS.md before connecting real services.

- Initial Flask application, database migration, integration clients, event ingestion, policy engine, review queue, guarded dry-run deletion, and worker.
- Per-rule automatic review modes for inactivity and quota cleanup.
- Quota cleanup continues to the low-water target instead of stopping at high water.
- Late playback rescues expiring Leaving Soon notices; deferred approvals are retried by the worker.
- Demo Sonarr adapter serves episode inventory so demo TV deletions are fully validated.
