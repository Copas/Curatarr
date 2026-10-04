# Changelog

## 0.1.0 (2026-10-04)

First tagged development release. Validated against demo and mocked integrations only; see IMPLEMENTATION_STATUS.md before connecting real services.

- Initial Flask application, database migration, integration clients, event ingestion, policy engine, review queue, guarded dry-run deletion, and worker.
- Per-rule automatic review modes for inactivity and quota cleanup.
- Quota cleanup continues to the low-water target instead of stopping at high water.
- Late playback rescues expiring Leaving Soon notices; deferred approvals are retried by the worker.
- Demo Sonarr adapter serves episode inventory so demo TV deletions are fully validated.
