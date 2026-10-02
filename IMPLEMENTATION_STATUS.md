# Curatarr Implementation Status

## Current Phase
Phase 6 — Controlled deletion validation

## Completed
- Flask app, database schema and migrations, health/status API, Bootstrap shell, branding, and container skeleton.
- Jellyfin/Sonarr/Radarr clients and read-only discovery with stable provider-ID matching.
- Durable webhook ingestion, duplicate-event suppression, playback rescue, and episode completion state.
- TV current/next-season plan generation and durable Sonarr actions.
- Policy inheritance, inactivity/grace rules, deterministic purge ranking, quota high/low selection, mapped disk-pressure selection, review actions, and dry-run deletion guard.
- Reversible Leaving Soon poster snapshots, snooze/notice expiration, worker commands, and synthetic demo adapters.
- Pre-delete identity/queue/activity checks, fresh Sonarr episode-file mapping checks, uncertain-response reconciliation, and preserved TV played-state restoration.
- Database leases for event processing and library policy evaluation, plus atomic action claims; PostgreSQL contention checks pass for fresh/expired leases and single-winner action execution.
- Initial unit and integration tests, including stale/shared TV file guards, partial-deletion reconciliation, and queued-delete playback rescue.

## In Progress
- Expanding missed-webhook reconciliation coverage and live API compatibility checks.
- Validating live test integrations and remaining concurrent playback/delete races.

## Next
- Expand acceptance scenario coverage for remaining concurrent playback/delete races and live API failures.
- Finish full UI reporting and confirm pressure-level behavior against real disk-space responses.
- Harden artwork metadata comparison and validate longer-running or multi-node lease behavior.
- Validate controlled and automatic cleanup against real test integrations before production use.

## Known Issues
- No real-service credentials are available for live validation.
- Authentication and encrypted-at-rest integration keys are not implemented.
- Poster restoration currently depends on exact image bytes returned by Jellyfin.
- PostgreSQL lease/action contention was validated locally; longer-running and multi-node behavior has not been validated.

## Decisions Made
- SQLite for local tests; PostgreSQL in container deployment.
- External side effects are represented by persisted actions.
- Dry run is the default; each library can explicitly override it.

## Blockers
- Live integration validation requires user-provided test services and credentials.
