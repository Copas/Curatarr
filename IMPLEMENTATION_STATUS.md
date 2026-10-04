# Curatarr Implementation Status

## Current Phase
Phase 7 — Automatic lifecycle (implemented against demo/mocked integrations; live validation pending)

## Completed
- v0.1.0 tagged 2026-10-04 after the Section 92 release checks: tests and lint, migration upgrade/check/downgrade/re-upgrade on a fresh database, and fresh-install and demo-mode startup with all main pages and API endpoints returning 200.
- Flask app, database schema and migrations, health/status API, Bootstrap shell, branding, and container skeleton.
- Jellyfin/Sonarr/Radarr clients and read-only discovery with stable provider-ID matching.
- Durable webhook ingestion, duplicate-event suppression, playback rescue, and episode completion state; missed-webhook reconciliation handles changed episode IDs, pagination without a reported total, and separate users.
- TV current/next-season plan generation and durable Sonarr actions.
- Policy inheritance, inactivity/grace rules, deterministic purge ranking, quota high/low selection, mapped disk-pressure selection, review actions, and dry-run deletion guard.
- Reversible Leaving Soon poster snapshots, original-before-upload persistence, image-tag-aware restoration, snooze/notice expiration, worker commands, and synthetic demo adapters.
- Pre-delete identity/queue/activity checks, fresh Sonarr episode-file mapping checks, a final playback check before each destructive request, uncertain-response reconciliation, and preserved TV played-state restoration.
- Sonarr search timeout-after-acceptance and Radarr delete timeout-after-completion are exercised with stateful fixtures; retries/reconciliation do not repeat the external write.
- Rescans preserve media identity across Jellyfin/Sonarr ID changes and title renames; externally removed files rescue pending candidates. Invalid file sizes are discarded on discovery and changed Radarr/Sonarr sizes block deletion. Future unaired episodes and specials do not trigger premature searches.
- Synthetic disk-pressure acceptance checks cover normal/low/critical free-space, insufficient reclaimable bytes, a changed queue, cleared pressure, and quota falling below its trigger. Malformed disk/queue/provider responses fail closed.
- Overview reports current stored size, high/low-water state, recent acquisition decisions, errors, and integration health; Review shows playback/favorite and Leaving Soon state; title details show decision score and effective/inherited policy. History filters by date, library, title, action/state, and user, with linked decision context and candidate timeline. Demo fixtures include review, recommend-only, and Leaving Soon candidates plus visible simulated outages.
- Integration API keys and the webhook token are now encrypted at rest with an authenticated cipher derived from a stable application key. Existing plaintext rows have an idempotent `encrypt-secrets` upgrade command, run automatically after container migrations. Compose binds the UI to loopback.
- Renewable database leases for event processing and library policy evaluation, plus atomic action claims; PostgreSQL checks pass for fresh/expired leases, work lasting past the original lease, four-process contention, and single-winner action execution.
- Automatic lifecycle: per-rule review modes (inactivity, quota, low/critical disk pressure) inherit the library mode and stay opt-in alongside dry run. Notice expiry and approved deletes process pending playback first; deletes deferred by unprocessed events are retried by the worker. Quota runs continue to the low-water target after Curatarr's own deletes, while external drops below high water still invalidate queued candidates. Confirmed movie deletes mark files absent immediately.
- End-to-end demo lifecycle: the reconcile CLI creates, expires, validates, and deletes quota candidates to the low-water target with reclaimed-space reporting; a simulated Radarr outage blocks every automatic delete.
- All Section 98 acceptance edge cases have explicit tests.
- Acquisition and Retention rule pages edit global defaults or one library. Saves merge only the shown fields, library-only fields (size limits, disk pressure, dry run) are excluded globally, and global changes are validated against every library. Navigation follows section 39 (Overrides is the title search).
- Source and installed copies are separate (docs/decisions/008-source-and-installed-copies.md, docs/INSTALL.md). The package now ships templates, static files, and migrations, so it runs without a checkout. `scripts/install.sh` and `scripts/uninstall.sh` manage native systemd installs under `/opt/curatarr`, `/etc/curatarr`, and `/var/lib/curatarr`, with database backup before upgrade and the previous release kept for rollback. The Docker image runs the installed package as a non-root user. A hygiene test keeps tracked files publishable. Verified: wheel installed outside the repo, installer fresh install and upgrade into scratch locations without systemd, Docker image build and start.
- Live discovery (2026-10-04, read-only against the owner's Jellyfin/Sonarr/Radarr into a scratch database): 6 libraries, 202 series (197 matched to Sonarr), 237 movies (233 matched to Radarr), about 31k parts, in 44.5 seconds. Pressing Discover in the installed copy hit the 30-second gunicorn timeout, so Discover and Reconcile now queue work for the worker. Libraries of type "other" (adult, collections) receive no media. Unmatched titles stay ineligible.
- Webhook payloads checked against the installed Webhook plugin (v22) field names. It has no ItemPlayed event; completion is PlaybackStop with PlayedToCompletion or UserDataSaved with Played and SaveReason. Template values are strings. The parser and Settings instructions handle this, but a live delivery from the plugin has not been observed yet.
- Live Jellyfin 12.1 check (2026-10-04): saving a valid API key failed because Jellyfin returns 401 for the bare `X-Emby-Token` header, even on `/System/Info`. The client now uses the MediaBrowser Authorization scheme for every call, and version, users, libraries, item listing, and user lookup were verified read-only against the live server.
- First real install (2026-10-04) on the owner's host: `scripts/install.sh` created the service user, units, and services, and Jellyfin administrator sign-in worked against the live server. The install exposed a worker bug: before API keys exist, every 15-second cycle failed reconciliation, logged a warning, and skipped the heartbeat, so health showed the scheduler offline. Fixed: the heartbeat is written at the start of each cycle, reconciliation waits quietly until all three API keys are saved, failed reconciliations retry after 5 minutes, and a key-less integration reports "unconfigured".
- Sign-in with Jellyfin administrator accounts (docs/decisions/007-jellyfin-sign-in.md): password verified by Jellyfin and never stored, the Jellyfin session ended immediately, administrator status re-checked every five minutes with a one-hour outage grace period, rate-limited failures, safe post-sign-in redirects, and first-run server binding. `/health`, `/api/v1/status`, and the webhook remain public.
- Review queue shows every section 29 field: library, media type, proposed action (movie delete, or the number of episode files outside the footprint), last watched and by whom (Jellyfin display names cached during reconciliation), acquisition date, favorite and meaningful-watch state.
- First-run `/setup` checklist derived from stored state, with an Overview prompt until required steps are done. Overview also lists scheduled deletions and recent cleanup (deleted vs dry run).
- Section 94 observability: JSON operational logs limited to the specified fields, durable metric counters, `/api/v1/metrics`, and an Operations table on Overview. Tests use temporary data directories, so they no longer write posters into `instance/`.
- Initial unit and integration tests, including stale/shared TV file guards, partial-deletion reconciliation, queued-delete playback rescue, playback after revalidation, and playback between TV file deletes.

## In Progress
- Checking live API compatibility when test services become available; current local fixtures cover missed-webhook playback and favorite recovery.
- Validating live test integrations and remaining concurrent playback/delete races, especially playback during an in-flight external request.

## Next
- Finish first-run setup on the owner's installed copy (2026-10-04: installed with `scripts/install.sh`, services running, Jellyfin administrator sign-in working): add Jellyfin, Sonarr, and Radarr API keys, configure the webhook, run Discover, and begin live validation in dry run.
- Publish the clean repository to GitHub once a remote exists; tag the next release after the installer has been run for real.
- Expand live API failure coverage once test services are available.
- Confirm pressure-level behavior against real disk-space responses and expand synthetic UI state coverage.
- Validate image-tag behavior with a live Jellyfin version and test lease behavior across multiple hosts against a shared production-like database.
- Validate controlled and automatic cleanup against real test integrations before production use.

## Known Issues
- No real-service credentials are available for live validation.
- Jellyfin sign-in is verified against mocked and demo Jellyfin only; confirm `AuthenticateByName`, `Sessions/Logout`, and `Users/{id}` behavior against a live server. The first-run server URL can be bound by whoever signs in first unless `CURATARR_JELLYFIN_URL` is set. The sign-in rate limit is per process and per client address, so behind a reverse proxy it applies to all users together.
- Poster restoration uses Jellyfin's primary image tag or exact badge bytes. An ambiguous upload followed by image re-encoding before the new tag is recorded still needs manual recovery.
- PostgreSQL lease renewal and same-host multi-process contention were validated locally; cross-host contention has not been validated.
- An external delete already in flight cannot be cancelled by a later playback event; the next TV file request is stopped and partial cleanup requires manual review.

## Decisions Made
- SQLite for local tests; PostgreSQL in container deployment.
- Rule-specific review modes and the inactivity > quota > disk-pressure reason order (docs/decisions/006-automatic-lifecycle.md).
- Quota high/low-water hysteresis applies only to Curatarr's own deletions (docs/decisions/005-capacity-policy.md).
- Favorites rank last but do not protect a title; only Never Purge does.
- First sync acts on existing watch history: past completions trigger acquisition like new ones (owner's choice, 2026-10-04; docs/decisions/006). Acquisition is not dry-run gated, and prior Sonarr monitored state is not recorded before Curatarr changes it.
- Authentication uses Jellyfin administrator accounts (owner's choice, like Seerr) behind an isolated adapter, rather than local accounts. Non-administrators cannot sign in.
- External side effects are represented by persisted actions.
- Dry run is the default; each library can explicitly override it.

## Blockers
- Live integration validation requires user-provided test services and credentials.
