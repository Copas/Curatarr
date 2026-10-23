# API, dashboards, logs and metrics

## Endpoints

| Endpoint | Access | Returns |
|---|---|---|
| `GET /health` | Public | Health of the app, database, scheduler and integrations |
| `GET /api/v1/status` | Public | Review and Leaving Soon counts, dry-run state, integration health, version (no secrets) |
| `GET /api/v1/metrics` | Sign-in or API key | The counters listed under [Metrics](#metrics) |
| `GET /api/v1/libraries` | Sign-in or API key | Managed libraries |
| `GET /api/v1/review/summary` | Sign-in or API key | The review queue in brief |
| `GET /api/v1/history` | Sign-in or API key | Newest actions, with title, library, Jellyfin item ID and links to Curatarr's pages |
| `GET /api/v1/transcripts/search?q=…` | Sign-in or API key | Episodes and films where the words were said, with timestamps |
| `GET /api/v1/identity-flags` | Sign-in or API key | Episodes flagged as possibly the wrong episode (filed as one, sounding like another), with the Sonarr series, whether the two are swapped and whether the other episode's file is confirmed. Empty while the episode check is off |
| `POST /api/v1/webhook/jellyfin` | `X-Curatarr-Token` header | Jellyfin Webhook plugin events |
| `POST /api/v1/jellyfin/delete` | `X-Curatarr-Token` header | Used by the Curatarr plugin for Jellyfin before Jellyfin deletes an item. Body `{"itemId", "userName"}`. A film or show Curatarr manages is marked Leaving Soon: `{"handled": true, "result": "leaving_soon" or "already"}`. One it must keep (Never Purge, no file in Radarr) gives `{"handled": true, "refused": "why"}`, and the delete should fail. Anything else gives `{"handled": false}`, and Jellyfin deletes it as usual. `{"check": true}` answers `{"ok": true}` |

`/api/v1/history?limit=20&exclude=admin_signed_in` returns the 20 newest actions without sign-ins. Repeat `type=` or `exclude=` to filter by action type (for example `type=dub_fixed`).

### Read-only API key

Another tool, such as a home dashboard, can read the `/api/v1/` endpoints without signing in by sending a key in an `X-Api-Key` header:

```bash
curl -H "X-Api-Key: $CURATARR_API_KEY" http://127.0.0.1:8787/api/v1/history?limit=5
```

Generate the key under **Settings → Integrations → API key**. It's shown once, stored only as a hash, and grants nothing but these GET requests.

## Adding Curatarr to a dashboard

Curatarr runs on its own and doesn't depend on any dashboard. To add it to one, link to:

- `/` for the overview and `/review` for the review queue (both require sign-in);
- `/health` for a health check and `/api/v1/status` for counts and health (both public);
- `/static/logo.svg` for the app icon, also in the repository at `curatarr/static/logo.svg`.

## Logs

Operational log lines from the `curatarr.ops` logger are single JSON objects, limited to `event_id`, `action_id`, `media_identity_id`, `candidate_id`, `integration`, `operation`, `duration_ms`, `result` and `status_code`. They never include API keys or payloads. Each external call, processed event, executed action and reconciliation is logged. Successful GET requests log at DEBUG, failures at WARNING, everything else at INFO. Set the level with `CURATARR_LOG_LEVEL`.

On a native install: `journalctl -u curatarr-worker -f` (or `curatarr-web`). In Docker: `docker compose logs -f worker`.

## Metrics

`/api/v1/metrics` and Settings → Operations report:

- events processed, and duplicate events ignored;
- acquisition actions, purge candidates created, and rescues;
- bytes proposed and actually reclaimed;
- external API failures per integration;
- last reconciliation duration, pending actions, and the oldest pending action's age.

Most values are computed from stored history. Duplicate-event and API-failure counts are held in memory and written to the `metric_counters` table at the end of each request or worker cycle.
