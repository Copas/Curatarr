# Curatarr Implementation Status

## Current Phase
Phase 6 — Controlled deletion validation

## Completed
- Flask app, database schema and migrations, health/status API, Bootstrap shell, branding, and container skeleton.
- Jellyfin/Sonarr/Radarr clients and read-only discovery with stable provider-ID matching.
- Durable webhook ingestion, duplicate-event suppression, playback rescue, and episode completion state; missed-webhook reconciliation handles changed episode IDs, pagination without a reported total, and separate users.
- TV current/next-season plan generation and durable Sonarr actions.
- Policy inheritance, inactivity/grace rules, deterministic purge ranking, quota high/low selection, mapped disk-pressure selection, review actions, and dry-run deletion guard.
- Reversible Leaving Soon poster snapshots, original-before-upload persistence, image-tag-aware restoration, snooze/notice expiration, worker commands, and synthetic demo adapters.
- Pre-delete identity/queue/activity checks, fresh Sonarr episode-file mapping checks, a final playback check before each destructive request, uncertain-response reconciliation, and preserved TV played-state restoration.
- Synthetic disk-pressure acceptance checks cover normal/low/critical free-space, insufficient reclaimable bytes, a changed queue, cleared pressure, and quota falling below its trigger. Malformed disk/queue/provider responses fail closed.
- Renewable database leases for event processing and library policy evaluation, plus atomic action claims; PostgreSQL checks pass for fresh/expired leases, work lasting past the original lease, four-process contention, and single-winner action execution.
- Initial unit and integration tests, including stale/shared TV file guards, partial-deletion reconciliation, queued-delete playback rescue, playback after revalidation, and playback between TV file deletes.

## In Progress
- Checking live API compatibility when test services become available; current local fixtures cover missed-webhook playback and favorite recovery.
- Validating live test integrations and remaining concurrent playback/delete races, especially playback during an in-flight external request.

## Next
- Expand acceptance scenario coverage for remaining concurrent playback/delete races and live API failures.
- Finish full UI reporting and confirm pressure-level behavior against real disk-space responses.
- Validate image-tag behavior with a live Jellyfin version and test lease behavior across multiple hosts against a shared production-like database.
- Validate controlled and automatic cleanup against real test integrations before production use.

## Known Issues
- No real-service credentials are available for live validation.
- Authentication and encrypted-at-rest integration keys are not implemented.
- Poster restoration uses Jellyfin's primary image tag or exact badge bytes. An ambiguous upload followed by image re-encoding before the new tag is recorded still needs manual recovery.
- PostgreSQL lease renewal and same-host multi-process contention were validated locally; cross-host contention has not been validated.
- An external delete already in flight cannot be cancelled by a later playback event; the next TV file request is stopped and partial cleanup requires manual review.

## Decisions Made
- SQLite for local tests; PostgreSQL in container deployment.
- External side effects are represented by persisted actions.
- Dry run is the default; each library can explicitly override it.

## Blockers
- Live integration validation requires user-provided test services and credentials.
