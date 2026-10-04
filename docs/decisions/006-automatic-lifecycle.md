# 006: Automatic lifecycle is opt-in per rule

Each library has a general `review_mode` plus optional per-rule modes: `inactivity_review_mode`, `quota_review_mode`, and the low/critical disk-pressure modes. A blank rule mode inherits the library mode. A title-level `review_mode` overrides all rule modes. The defaults are `require_review` with dry run enabled, so no automatic deletion happens without two explicit library changes.

When a title qualifies under several rules, the reason is chosen in a fixed order: inactivity, then quota, then disk pressure. The candidate follows that rule's mode. Enabling automatic quota cleanup therefore does not automatically delete titles that are also inactive; those keep the inactivity mode.

Automatic mode with a zero-day notice still creates a `LEAVING_SOON` candidate that expires on the next worker cycle, rather than jumping straight to `APPROVED`. This gives playback received in between a chance to rescue the title, and keeps one code path for timed deletion.

Before a notice expires and before any approved deletion runs, pending playback events are processed and the candidate is read again. If events are still unprocessed, the deletion is deferred (the candidate stays `APPROVED`) rather than blocked, and the worker retries it with `resume_approved()`.

A favorite is a strong ranking signal but not protection. A candidate favorited after entering review stays in review; capacity re-ranking moves it behind non-favorites before deletion. Only Never Purge removes a title from cleanup.
