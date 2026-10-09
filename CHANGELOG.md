# Changelog

## Unreleased

- New "Shows by size" page (navigation bar): every TV show on disk, largest first, with what Reset to minimum would free, last watched and by whom, a library filter, totals, and a Reset… button that opens the confirmation page. Daily shows are flagged on this page and on the confirmation page, because a reset keeps none of their episodes and Curatarr never refills daily shows.

- Title page actions are grouped in an Actions panel, one row per action with its description beside the button, instead of help text trailing after the button. Reset to minimum opens a confirmation page that lists what is deleted and kept, with Cancel and a red confirm button.

- Reset to minimum and the Review queue's Delete no longer run the deletions inside the web request: they approve the cleanup and the worker carries it out within a cycle (about 15 seconds). A long show (six seasons of I Love Lucy) outlasted gunicorn's 30-second request timeout, the page showed Internal Server Error, and the reset stopped after one file. An interrupted deletion is still reconciled against Sonarr and marked blocked rather than repeated.

- Works with Jellyfin 12.2: looking up one Jellyfin item now uses the item list (`/Items?Ids=`) because 12.2 rejects `GET /Items/{id}` from an API key with no user ("Guid can't be empty"). Before this fix every deletion check failed with "External integration cannot be verified", and poster badge updates could not read items.

- Reset to minimum now works on any show, including ones Sonarr has unmonitored (for example by a nightly job that unmonitors ended, complete shows) and ones with an always-keep episode missing. A show Curatarr has reset or cleaned up is refilled when someone watches it even if Sonarr has it unmonitored: the request monitors the series in Sonarr first, since Sonarr ignores unmonitored series. Other unmonitored shows are still left alone. The reset button on the title page is now a clearly separate red button.

- Title pages for TV shows have "Reset to minimum": trims the show to its always-keep episodes as if nobody had watched it. It runs through the normal approved-deletion path with every check except the grace period and recent viewing, then unmonitors all seasons in Sonarr (keeping the always-keep episodes monitored) and records the reset so earlier viewing no longer requests seasons. Watching again after a reset works as usual. Dry run is respected. Requires `db upgrade` (migration `d2f6a8c41e93` adds `viewing_reset_at`).

- A show whose latest season viewers have watched (by the acquisition threshold) gets its next season whenever Sonarr lists it, no matter how long ago that viewing was; before, the next season was only requested within the recent-viewing window (90 days by default), so a season premiering a year later was missed. A show not continued although the next season was already out is still treated as abandoned.
- While viewers are on a season that is still airing (its latest episode aired in the last 30 days), Curatarr makes sure Sonarr monitors that season even if every episode so far is downloaded, so new episodes download when they air. Queued once per season.

- Withdrew the `curatarr-pilot` Sonarr tag rule added earlier the same day: shows added by a list get Curatarr's configured always-keep fill like any other show. Migration `a1e5d9c3b7f2` drops the `arr_tags` column again (`db upgrade`).

- When Curatarr requests a whole season (the rest of the season being watched, or the next season), it now also monitors that season in Sonarr. Sonarr monitors an episode it learns about later only when the season is monitored, so episodes announced after the request (common for a season still airing) were added unmonitored and never downloaded. Partial requests, such as the first episodes of Season 1, still leave the season alone.

- Discovery and watch-history reconciliation no longer miss movies that belong to a Jellyfin collection. Jellyfin collapsed collection members into the collection in library listings, which hid 849 of 979 movies in the owner's Movies library (and 92 of 199 Classic Movies) from tracking, sizes, and cleanup.
- Reloading Settings after generating a webhook token no longer replaces the token.

- The stored policy format is versioned (migration `c4e8a1f07d36`); a database with settings from a newer version is refused with a clear message instead of being reinterpreted.

- Cleanup preview (Retention → "Preview what free-space cleanup would pick"): pretend free space is at a given level and see, per disk, the titles that would be picked in order with sizes and a running total, using the same rules and live queue as the worker. Nothing is changed.

- Settings shows the last webhook delivery (what it was, who, and whether it matched a title), the total received, and the last rejected or ignored delivery with the reason. Notification types Curatarr does not use are acknowledged and recorded as ignored instead of rejected.

- Title pages have "Request now", which applies the always-keep and season-ahead rules to one show immediately and sends the result to Sonarr, plus the Review queue's Delete/Keep/Snooze/Never Purge buttons for a title in Review or Leaving Soon.

- Jellyfin libraries that are not TV or movies (e.g. collections, mixed libraries) are listed on Overview as not managed instead of showing as empty 0 TB libraries, and are left out of rule scopes, the dry-run banner, and library counts.

- Download requests explain themselves, e.g. "Kelden finished S01E01 of XYZ, so Curatarr is searching for Season 2." Overview lists recent requests under "Downloads requested", with their status.

- Every reconciliation now requests missing always-keep episodes (new setting "Download always-keep episodes that are missing", on by default) and catches up the next season for shows watched in the last 90 days, skipping shows Sonarr has unmonitored and daily shows, with at most 5 new searches per run. Requires `db upgrade` (adds `arr_monitored` and `series_type`).

- Size limits are entered in MB, GB, or TB (TB by default) instead of bytes, and every size in the interface uses the same units (binary, matching Sonarr, Radarr, Jellyfin, and df).

- TV cleanup unmonitors each trimmed episode in Sonarr just before deleting its file. Previously the episodes stayed monitored, so Sonarr's missing-episode search would have downloaded them straight back (Sonarr's own "unmonitor deleted episodes" does not apply to API deletions and is off here).

- Library-size limits, free-space enforcement, and dry run can be set as global defaults. The dry-run banner states exactly which libraries are in dry run and which can delete, and explains that dry run rehearses everything but never asks Sonarr/Radarr to delete.

- Libraries on the same disk share one free-space selection, so four libraries on one NAS no longer each pick enough titles to cover the whole shortfall.

- Disk-pressure checks no longer fall back to the `/` record for other paths. Sonarr/Radarr omit network mounts, so a NAS library was being measured against the local drive. When the arr apps report no matching disk, free space is measured directly if the path is visible to Curatarr.

- Discovery records each file's acquisition time from Sonarr/Radarr `dateAdded` instead of the discovery time, which had made an entire existing library look newly acquired. Existing installs are corrected at the next discovery.

- Cleanup is driven by free space. Unwatched titles are no longer cleanup candidates on their own (new per-library switch `inactivity_cleanup`, off by default). Low free space selects only enough to return to the low threshold, with a Leaving Soon notice (14 days by default) before removal. Critical free space removes without a notice. Dry run still gates all removal.

- The top navigation highlights the section being viewed, including sub-pages (title pages under Overrides, history entries under History, library policy under Retention, setup under Settings).
- The global rules page shows real values instead of "Built-in default" placeholders and saves only values that differ from the built-in defaults. Blank options remain only where they mean something: the media-type-specific purge strategy and rule review modes that follow the main review mode. Library pages read "Inherit (value)".
- Policy choices use plain labels: "Always keep for each show" offers "First N episodes of Season 1", "All of Season 1", and "Entire series (never trimmed)"; review modes, expiry, strategies, and Yes/No read as words.
- Rule pages name what a blank field resolves to: "Built-in default (value)" on global defaults, which have nothing to inherit from, and "Inherit global default (value)" or "Inherit built-in default (value)" on a library.
- Acquisition ignores episodes that exist only in Jellyfin. Split or differently numbered episodes Sonarr does not track looked missing and triggered season searches that re-downloaded seasons already present (seen live with Tires S2).
- Acquisition actions record which episodes were actually switched to monitored (`newly_monitored`). Episodes Sonarr already monitored are no longer implied as changes.
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
