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
- Integration API keys and the webhook token are now encrypted at rest with an authenticated cipher derived from a stable application key. Existing plaintext rows have an idempotent `encrypt-secrets` upgrade command, run automatically after container migrations. Unauthenticated operation requires an explicit opt-in and compose binds the UI to loopback.
- Renewable database leases for event processing and library policy evaluation, plus atomic action claims; PostgreSQL checks pass for fresh/expired leases, work lasting past the original lease, four-process contention, and single-winner action execution.
- Automatic lifecycle: per-rule review modes (inactivity, quota, low/critical disk pressure) inherit the library mode and stay opt-in alongside dry run. Notice expiry and approved deletes process pending playback first; deletes deferred by unprocessed events are retried by the worker. Quota runs continue to the low-water target after Curatarr's own deletes, while external drops below high water still invalidate queued candidates. Confirmed movie deletes mark files absent immediately.
- End-to-end demo lifecycle: the reconcile CLI creates, expires, validates, and deletes quota candidates to the low-water target with reclaimed-space reporting; a simulated Radarr outage blocks every automatic delete.
- All Section 98 acceptance edge cases have explicit tests.
- Acquisition and Retention rule pages edit global defaults or one library. Saves merge only the shown fields, library-only fields (size limits, disk pressure, dry run) are excluded globally, and global changes are validated against every library. Navigation follows section 39 (Overrides is the title search).
- Section 94 observability: JSON operational logs limited to the specified fields, durable metric counters, `/api/v1/metrics`, and an Operations table on Overview. Tests use temporary data directories, so they no longer write posters into `instance/`.
- Initial unit and integration tests, including stale/shared TV file guards, partial-deletion reconciliation, queued-delete playback rescue, playback after revalidation, and playback between TV file deletes.

## In Progress
- Checking live API compatibility when test services become available; current local fixtures cover missed-webhook playback and favorite recovery.
- Validating live test integrations and remaining concurrent playback/delete races, especially playback during an in-flight external request.

## Next
- Expand live API failure coverage once test services are available.
- Confirm pressure-level behavior against real disk-space responses and expand synthetic UI state coverage.
- Validate image-tag behavior with a live Jellyfin version and test lease behavior across multiple hosts against a shared production-like database.
- Validate controlled and automatic cleanup against real test integrations before production use.

## Known Issues
- No real-service credentials are available for live validation.
- Local account authentication is not implemented. The specification permits explicitly opted-in unauthenticated operation for a protected trusted network; do not expose the UI directly to an untrusted network.
- Poster restoration uses Jellyfin's primary image tag or exact badge bytes. An ambiguous upload followed by image re-encoding before the new tag is recorded still needs manual recovery.
- PostgreSQL lease renewal and same-host multi-process contention were validated locally; cross-host contention has not been validated.
- An external delete already in flight cannot be cancelled by a later playback event; the next TV file request is stopped and partial cleanup requires manual review.

## Decisions Made
- SQLite for local tests; PostgreSQL in container deployment.
- Rule-specific review modes and the inactivity > quota > disk-pressure reason order (docs/decisions/006-automatic-lifecycle.md).
- Quota high/low-water hysteresis applies only to Curatarr's own deletions (docs/decisions/005-capacity-policy.md).
- Favorites rank last but do not protect a title; only Never Purge does.
- External side effects are represented by persisted actions.
- Dry run is the default; each library can explicitly override it.

## Blockers
- Live integration validation requires user-provided test services and credentials.
