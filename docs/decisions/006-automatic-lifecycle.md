# 006: Automatic lifecycle is opt-in per rule

Each library has a general `review_mode` plus optional per-rule modes: `inactivity_review_mode`, `quota_review_mode`, and the low/critical disk-pressure modes. A blank rule mode inherits the library mode. A title-level `review_mode` overrides all rule modes. The defaults are `require_review` with dry run enabled, so no automatic deletion happens without two explicit library changes.

When a title qualifies under several rules, the reason is chosen in a fixed order: inactivity, then quota, then disk pressure. The candidate follows that rule's mode. Enabling automatic quota cleanup therefore does not automatically delete titles that are also inactive; those keep the inactivity mode.

Automatic mode with a zero-day notice still creates a `LEAVING_SOON` candidate that expires on the next worker cycle, rather than jumping straight to `APPROVED`. This gives playback received in between a chance to rescue the title, and keeps one code path for timed deletion.

Before a notice expires and before any approved deletion runs, pending playback events are processed and the candidate is read again. If events are still unprocessed, the deletion is deferred (the candidate stays `APPROVED`) rather than blocked, and the worker retries it with `resume_approved()`.

A favorite is a strong ranking signal but not protection. A candidate favorited after entering review stays in review; capacity re-ranking moves it behind non-favorites before deletion. Only Never Purge removes a title from cleanup.

## Watch history found on first sync

The first reconciliation turns every user's existing Played state into completion events, and those trigger the normal acquisition rule: fill the rest of that season and request the next. The owner chose this on 2026-10-04 after seeing it happen on the first live sync (11 Sonarr requests), over recording history as a baseline only. Acquisition is not covered by dry run, which applies to deletion. Later reconciliations do not repeat this, because a completion already recorded generates no new event.

## Cleanup only when free space runs low (2026-10-04)

The owner's rule, which replaces inactivity as a cleanup trigger by default:

- Not being played (`tv_inactivity_days`/`movie_inactivity_days`) does not by itself make a title a cleanup candidate. A per-library `inactivity_cleanup` switch, off by default, restores the old behavior. Unwatched time still shapes the purge strategy ranking, so long-unwatched titles are chosen first under pressure. Recently played TV stays ineligible.
- Below the low free-space threshold, Curatarr selects in ranking order only enough to bring free space back to that threshold. The default `low_pressure_review_mode` is `automatic`: titles get a Leaving Soon notice for `notice_days` (14 by default) and are removed when it expires unless rescued by playback. Each hourly re-run counts already-selected titles, so it does not select more, and each removal re-checks pressure and ranking, so the rest are dropped once space recovers.
- Below the critical threshold, the default `critical_pressure_review_mode` is `automatic` with no notice: removal is attempted on the next worker cycle, with the same pre-delete validation.
- Dry run remains the final gate. A library must turn it off before anything is actually removed.
- Free-space enforcement still requires `free_space_enabled` and an arr-side `disk_path` per library.
