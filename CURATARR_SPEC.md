<!--
Curatarr implementation specification.
This file is intentionally deployment-neutral and suitable for a public repository.
Do not add real credentials, private hostnames, user names, library IDs, or personal deployment data.
-->

# Curatarr — Product and Implementation Specification

**Status:** Implementation-ready specification  
**Audience:** Codex / implementation agent  
**Product:** Curatarr  
**Type:** Standalone media lifecycle manager for Jellyfin, Sonarr, and Radarr

---

## 1. Product Summary

Curatarr is a standalone media lifecycle manager that uses Jellyfin viewing activity to control staged acquisition and retention of television and movie content through Sonarr and Radarr.

Core principle:

> Keep a small immediately playable media footprint, automatically acquire more content when users demonstrate interest, and reclaim storage when content becomes inactive.

Curatarr is intended to be publishable and usable by other Jellyfin users. It must not contain developer-specific, household-specific, network-specific, or deployment-specific assumptions.

Curatarr may be linked from another dashboard, but it must remain an independent application with its own UI, configuration, persistence, API integrations, migrations, background jobs, and service lifecycle.

---

## 2. Autonomous Codex Execution Contract

Implement this project autonomously for long uninterrupted periods.

Do **not** stop for routine approval, confirmation, or preference questions when a reasonable engineering decision can be made from this specification.

### Expected behavior

Codex should:

- Work through implementation phases continuously.
- Make conventional, maintainable engineering decisions when details are unspecified.
- Prefer simple, explicit designs over speculative abstraction.
- Create and modify files without asking for approval for ordinary repository work.
- Initialize and use Git from the beginning.
- Make small logical commits as milestones are completed.
- Run tests, linters, migrations, and local validation as needed.
- Fix failures and continue instead of stopping after the first error.
- Refactor when necessary to keep the implementation maintainable.
- Add tests for important behavior while implementing it.
- Update documentation as behavior changes.
- Keep a running implementation checklist in the repository.
- Continue to the next unblocked task after completing each task.
- Preserve backwards compatibility with already-implemented behavior unless the specification requires a change.
- Prefer dry-run behavior whenever a destructive integration is not yet fully validated.

### Do not ask for approval for

- File creation or edits inside the Curatarr repository
- Dependency installation inside the project environment
- Database migrations
- Test execution
- Local development server changes
- Refactoring
- Git commits
- Normal branch creation
- UI implementation details consistent with this specification
- Schema details that can be reasonably inferred
- API wrapper design
- Test fixtures
- Internal naming
- Error-handling implementation
- Logging implementation
- Routine dependency/version choices
- Formatting/linting configuration

### Stop only for a true blocker

Examples:

- Required credentials or API keys are unavailable.
- An external service cannot be reached and no mock/dry-run path can continue development.
- A destructive action outside the repository would be irreversible and is not explicitly authorized.
- Two requirements directly conflict and choosing either would materially change product behavior.
- A required external API capability appears not to exist and no safe fallback is available.

When blocked, document:

1. What is blocked.
2. What was attempted.
3. What remains independently implementable.
4. The smallest specific input required.

Continue all other unblocked work before requesting input.

---

## 3. Primary Goals

Curatarr must:

1. React to Jellyfin playback activity.
2. Expand TV availability progressively through Sonarr.
3. Keep approximately one season ahead of demonstrated viewing.
4. Collapse inactive TV shows back to a configurable minimum footprint.
5. Manage movie retention through Radarr.
6. Support time-based and size-based cleanup policies.
7. Use Jellyfin favorites as a strong retention signal.
8. Support explicit Never Purge protection.
9. Provide a review queue before destructive actions when configured.
10. Support fully automatic cleanup when configured.
11. Show Leaving Soon status inside Jellyfin before scheduled deletion.
12. Explain every automated decision.
13. Maintain an audit trail.
14. Fail safe when external services are unavailable.
15. Begin destructive behavior in dry-run mode.
16. Be suitable for a public Git repository without exposing private deployment details.

---

## 4. Non-Goals

Initial versions do not need to:

- Replace Jellyfin.
- Replace Sonarr.
- Replace Radarr.
- Replace Seerr or other request managers.
- Directly download media.
- Directly delete files from the filesystem.
- Automatically reacquire deleted movies.
- Manage Season 0 / Specials by default.
- Predict what users may want to watch.
- Use opaque ML-based retention decisions.

---

## 5. Recommended Technical Shape

Prefer the existing development ecosystem:

- Python 3.12+
- Flask
- SQLAlchemy
- Alembic
- PostgreSQL for production
- SQLite permitted for development/testing
- Bootstrap 5
- Minimal JavaScript; use lightweight vanilla JS or jQuery where useful
- Background scheduler appropriate for a single-node deployment initially
- REST-style internal API boundaries

Keep integrations isolated behind service/client classes so future API changes do not leak throughout the application.

Recommended repository structure:

```text
curatarr/
├── curatarr/
│   ├── api/
│   ├── core/
│   ├── integrations/
│   │   ├── jellyfin/
│   │   ├── sonarr/
│   │   └── radarr/
│   ├── models/
│   ├── policies/
│   ├── services/
│   ├── tasks/
│   ├── web/
│   └── branding/
├── migrations/
├── tests/
├── docs/
├── static/
├── templates/
├── .env.example
├── .gitignore
├── pyproject.toml
├── README.md
├── CHANGELOG.md
└── LICENSE
```

---

## 6. Publishability and Privacy Requirements

Curatarr must be safe to publish publicly.

Never commit:

- Real API keys
- Tokens
- Passwords
- Cookies
- Session secrets
- Private certificates
- Personal names
- Household member names
- Private hostnames
- Private IP addresses
- Public IP addresses tied to a real deployment
- Real Jellyfin usernames
- Real Jellyfin/Sonarr/Radarr IDs
- Local filesystem paths
- Personal domains
- Device names
- NAS/server names
- Database files
- Runtime-generated artwork
- Logs containing deployment identifiers
- Screenshots containing private environment information

All deployment-specific values must come from configuration or environment variables.

Commit:

```text
.env.example
```

Do not commit:

```text
.env
```

Example values must be generic:

```dotenv
CURATARR_SECRET_KEY=change-me
DATABASE_URL=sqlite:///curatarr.db

JELLYFIN_URL=http://jellyfin.example.local:8096
JELLYFIN_API_KEY=replace-me

SONARR_URL=http://sonarr.example.local:8989
SONARR_API_KEY=replace-me

RADARR_URL=http://radarr.example.local:7878
RADARR_API_KEY=replace-me
```

All fixtures, tests, screenshots, documentation, and demo records must use generic names.

Add secret scanning or an equivalent pre-commit safeguard if practical.

Before every commit, inspect staged changes for credentials and deployment-specific identifiers.

---

## 7. Git Development Requirements

Initialize Git before implementation begins.

Use small logical commits.

Suggested early history:

```text
chore: initialize Curatarr project
feat: add configuration model
feat: add Jellyfin integration
feat: add playback event ingestion
feat: add Sonarr discovery
feat: add staged TV acquisition
feat: add retention policy engine
feat: add purge review queue
feat: add Leaving Soon artwork
feat: add Radarr cleanup
```

Use semantic version tags when meaningful:

```text
v0.1.0
v0.2.0
v0.3.0
```

Maintain:

- `README.md`
- `CHANGELOG.md`
- `LICENSE`
- `.gitignore`
- `.env.example`

Do not commit private deployment configuration.

---

## 8. Configuration Hierarchy

Configuration precedence:

1. Per-title override
2. Per-library policy
3. Global Curatarr defaults

A title inherits higher-level values unless explicitly overridden.

Curatarr must support independent policies for each Jellyfin library.

Examples:

- TV Library A: size limit enabled
- TV Library B: no quota
- Movie Library: separate quota
- Archive Library: cleanup disabled

---

## 9. Jellyfin Integration

Jellyfin is the source of:

- Libraries
- Media items
- Users
- Playback activity
- Played/completed state
- Favorite state
- Artwork
- Provider IDs
- Library membership

Use Jellyfin webhook/event capability for immediate event handling where available.

Do not depend solely on event delivery.

A scheduled reconciliation task must compare Curatarr state against Jellyfin, Sonarr, and Radarr.

Default reconciliation interval:

```text
1 hour
```

Make configurable.

---

## 10. Media Identity Mapping

Never rely only on title strings.

Persist mappings among available identifiers:

- Jellyfin item ID
- Sonarr series ID
- Sonarr episode ID
- Radarr movie ID
- TMDB ID
- TVDB ID
- IMDb ID
- Other stable provider IDs

Prefer stable provider identifiers.

Mappings must survive deletion and later re-addition where possible.

---

# TV Lifecycle

## 11. Minimum Retained Footprint

TV libraries must support a configurable minimum retained footprint.

Options:

### First N Episodes

Examples:

```text
Keep first 1 episode
Keep first 3 episodes
```

### Entire Season 1

Retain the full first season.

### Entire Series

Disables content collapse for that title while allowing other lifecycle behavior.

Configure globally, per library, or per series.

Season 0 / Specials are excluded from lifecycle logic by default.

---

## 12. TV Acquisition Trigger

Use Jellyfin's played/completed state rather than inventing another completion threshold.

Default acquisition trigger:

```text
1 completed episode
```

Make configurable.

Any Jellyfin user counts.

---

## 13. Any Playback Resets Retention

Any playback event for a title must:

1. Reset inactivity timing.
2. Cancel pending purge.
3. Cancel scheduled deletion.
4. Remove Leaving Soon state.
5. Restore normal artwork if modified.
6. Record the event.
7. Re-evaluate acquisition rules.

This applies even if playback does not satisfy the completed-episode acquisition trigger.

---

## 14. Progressive TV Acquisition

Curatarr keeps one complete season ahead of demonstrated viewing.

Example initial state:

```text
S1E1 only
```

User completes:

```text
S1E1
```

Curatarr should:

1. Reset inactivity state.
2. Cancel pending cleanup.
3. Tell Sonarr to monitor the remainder of Season 1.
4. Search for missing Season 1 episodes.
5. Monitor Season 2.
6. Search for Season 2.
7. Leave Season 3 untouched.

When a qualifying episode from Season 2 is completed:

```text
Acquire Season 3
```

When a qualifying episode from Season 3 is completed:

```text
Acquire Season 4
```

Continue progressively.

---

## 15. Partial Current Season

If only part of the current season is retained, playback should fill the rest of that season.

Example:

```text
Stored: S1E1-S1E3
User completes: S1E1
```

Request:

```text
Remaining Season 1
Season 2
```

This gives users enough content while staying one season ahead.

---

## 16. Future / Airing Seasons

If the triggered next season exists but is still airing:

- Mark that season monitored in Sonarr.
- Search for currently available episodes.
- Allow Sonarr to acquire future episodes normally.
- Do not monitor the following season until viewing activity reaches the currently triggered season.

---

## 17. Meaningful Viewing Threshold

Cleanup ranking needs a separate concept of meaningful engagement.

Default:

```text
2 completed episodes
```

Make configurable.

A show below this threshold may be treated similarly to unwatched media for cleanup ranking.

Any playback still resets inactivity.

---

## 18. TV Inactivity

Measure TV inactivity from the later of:

- Last playback date
- Most recent acquisition date

Example default:

```text
90 days
```

Make configurable globally, per library, and per title.

---

## 19. TV Cleanup

Normal TV cleanup collapses a show back to its configured minimum footprint rather than deleting it entirely.

Example:

Current:

```text
Seasons 1-6
```

Configured minimum:

```text
First 3 episodes
```

Cleanup result:

```text
Keep S1E1-S1E3
Remove all other managed episodes through Sonarr
```

If configured minimum is full Season 1, retain Season 1 and remove later seasons.

Never directly delete files from the filesystem.

---

# Movie Lifecycle

## 20. Movie Behavior

Movies do not use staged acquisition.

Curatarr manages:

- Retention
- Library-size enforcement
- Favorite signals
- Never Purge
- Grace period
- Leaving Soon
- Review queue
- Scheduled deletion
- Deletion through Radarr

Deleted movies are not automatically reacquired.

Users may request them again through their normal request application.

---

## 21. Favorites

Jellyfin favorite state is a strong retention signal.

Rules:

- Favorite from any user counts.
- Favorite does not equal Never Purge.
- Favorites should rank substantially lower for deletion.
- Favorite media may still be purgeable if policy permits and lower-priority candidates are exhausted.

Explicit protection:

```text
Never Purge
```

Never Purge content must never be automatically queued or deleted.

---

## 22. Per-Title Overrides

Allow title-level overrides for at least:

- Never Purge
- Inactivity period
- Minimum retained footprint
- Acquisition trigger
- Meaningful-watch threshold
- Grace period
- Review vs automatic behavior
- Purge strategy participation

The UI must distinguish inherited values from overridden values.

---

# Retention and Capacity

## 23. Purge Strategy Framework

Purge ordering must be configurable per library.

Do not hard-code one strategy.

### Strategy A — Oldest Unwatched First

Priority:

1. Unwatched non-favorites
2. Oldest acquired first
3. Lightly watched non-favorites
4. Oldest watched non-favorites
5. Favorites
6. Never Purge excluded

### Strategy B — Oldest Watched Non-Favorites First

Preferred initial movie strategy.

Priority:

1. Watched non-favorites
2. Oldest last-watch first
3. Unwatched non-favorites
4. Favorites
5. Never Purge excluded

### Strategy C — Smart / Weighted

Support a transparent score using inputs such as:

- Last watched age
- Acquisition age
- Watched/unwatched state
- Completed episode count
- Favorite state
- Media size
- Grace period
- Current disk pressure

Every score must be explainable.

Do not use opaque ranking logic.

---

## 24. Library Size Management

Support independent size limits for each Jellyfin library.

Configuration:

- Disabled
- High-water mark
- Low-water target

Example:

```text
Cleanup begins: 8.0 TB
Cleanup stops: 7.5 TB
```

When the high-water mark is exceeded:

1. Calculate required reclaimed storage.
2. Rank candidates using the selected strategy.
3. Select enough candidates to reach the low-water target.
4. Queue or schedule them according to policy.
5. Stop selecting once expected reclaimed space reaches the target.

This avoids constant churn around a single threshold.

---

## 25. Disk Pressure Policies

Optionally support free-space thresholds in addition to library quotas.

Example:

```text
Normal:   >15% free
Low:      <15% free
Critical: <8% free
```

Allow different policies by pressure level.

Disk-pressure cleanup must still respect:

- Never Purge
- Active download/import protection
- Grace periods unless explicitly configured otherwise
- Pre-delete validation

Library quotas and free-space limits are independently configurable.

---

## 26. Grace Period

Newly acquired media receives cleanup protection.

Default example:

```text
30 days
```

Make configurable.

During grace period:

- Do not select for cleanup.
- Do not schedule deletion.

---

## 27. Active Download / Import Protection

Media associated with active Sonarr or Radarr download/import activity must be excluded from purge consideration.

Validate again immediately before deletion.

---

# Review and Deletion

## 28. Review Modes

Support per-library/per-rule modes:

```text
Recommend only
Require review
Automatic
```

---

## 29. Review Queue

Review candidates should display:

- Poster/thumbnail
- Title
- Library
- Media type
- Recoverable size
- Last played date
- Last watcher when available
- Acquisition date
- Favorite state
- Meaningful-watch state
- Never Purge state
- Purge reason
- Proposed action
- Scheduled deletion date

Actions:

```text
Delete
Keep
Snooze 30 days
Snooze 90 days
Never Purge
```

Snooze suppresses eligibility until the selected date.

---

## 30. Review Queue Expiration

Support:

### Manual Forever

Candidate remains until explicitly acted upon.

### Timed Automatic Delete

Candidate receives a future deletion date.

Example:

```text
Delete after 14 days unless rescued
```

Make configurable.

---

## 31. Leaving Soon

Curatarr should visibly mark media inside Jellyfin before scheduled deletion.

Badge text:

```text
LEAVING SOON
```

Preferred implementation:

1. Preserve enough information to restore original artwork.
2. Generate a temporary poster variant containing the Leaving Soon badge.
3. Apply it through Jellyfin.
4. Restore original artwork when the item is rescued, snoozed, kept, or removed from the deletion path.

Leaving Soon begins:

- Immediately when entering a manual review queue, or
- X configurable days before automatic deletion

Default example:

```text
14 days
```

Any playback rescues the content and removes Leaving Soon.

---

## 32. Watch-State Preservation

Before deleting managed TV content, snapshot relevant Jellyfin user state.

Store at least:

- Jellyfin user ID
- Stable media identity
- Series identity
- Season
- Episode
- Played state
- Playback position if useful
- Provider IDs

If media later returns, Curatarr should be capable of restoring played state.

Do not depend exclusively on Jellyfin retaining deleted-item history.

---

## 33. Deletion Mechanics

Curatarr must never directly remove media files.

Use:

```text
Sonarr API -> TV
Radarr API -> Movies
```

This keeps filesystem operations synchronized with media managers.

---

## 34. Pre-Delete Validation

Immediately before deletion, verify:

- Item still exists.
- Identity still matches.
- Rule still applies.
- No playback occurred since candidate creation.
- Item is not Never Purge.
- Grace period expired.
- Item is not downloading/importing.
- Candidate is not snoozed.
- Required warning period expired.
- Library is still above the relevant threshold if quota-driven.
- External service state is sufficiently healthy to make the decision.

If uncertain:

```text
DO NOT DELETE
```

Record the reason.

---

## 35. Failure Handling

Fail safe.

If Jellyfin, Sonarr, or Radarr is unavailable:

- Record the failure.
- Do not infer success.
- Retry safely.
- Do not delete based on incomplete information.
- Surface integration health in the UI.

Destructive operations default to no-op when state cannot be confirmed.

---

## 36. Idempotency

Duplicate events must not produce duplicate actions.

Prevent duplicate:

- Searches
- Monitoring changes
- Purge candidates
- Scheduled deletions
- Review entries
- Audit actions

Use stable event/action keys where possible.

---

## 37. Scheduled Reconciliation

Webhooks provide responsiveness.

Reconciliation provides correctness.

Default:

```text
Every 1 hour
```

Reconciliation should detect:

- Missed playback
- Deleted media
- Newly added media
- Favorite changes
- Sonarr/Radarr monitoring changes
- Library-size changes
- Stale purge candidates
- Failed artwork restoration
- Invalid identity mappings
- Orphaned scheduled deletions

---

# Auditability

## 38. Audit History

Retain audit history indefinitely by default.

Record:

- Playback received
- Episode completed
- Inactivity reset
- Favorite state changed
- Acquisition triggered
- Sonarr monitoring changed
- Sonarr search triggered
- Radarr state observed
- Purge candidate created
- Candidate reprioritized
- Candidate rescued
- Leaving Soon applied
- Leaving Soon removed
- Candidate snoozed
- Never Purge applied
- Delete approved
- Delete executed
- Delete blocked by validation
- Reconciliation correction
- Integration/API failure

Every automated decision must include a human-readable explanation.

Examples:

```text
Season 2 search triggered because S1E4 was completed.
```

```text
Queued for cleanup because no playback occurred for 103 days.
```

```text
Selected because the Movies library exceeded its high-water mark and this was the oldest watched non-favorite movie.
```

```text
Scheduled deletion cancelled because playback occurred.
```

---

# User Interface

## 39. Navigation

Curatarr should be operationally focused rather than exposing the entire library by default.

### Overview

Show:

- Integration status
- Library sizes
- High/low water state
- Pending review count
- Scheduled deletions
- Recent acquisitions
- Recent cleanup
- Storage reclaimed
- Recent errors

### Acquisition Rules

Configure:

- Episode completion trigger
- Minimum retained footprint
- One-season-ahead behavior
- Grace periods
- Library inheritance

### Retention Rules

Configure:

- Inactivity periods
- Meaningful-view threshold
- Purge strategy
- Favorite behavior
- Library size limits
- High/low watermarks
- Disk-pressure thresholds
- Review/automatic behavior
- Leaving Soon period

### Review Queue

Manage pending cleanup candidates.

### Overrides

Search for a title and modify title-specific behavior.

### History

Filterable event/decision log.

### Settings

Configure:

- Jellyfin
- Sonarr
- Radarr
- Scheduler
- Application behavior
- Theme
- Authentication if implemented

---

## 40. Setup Experience

The target UX should not require editing source code.

Initial development may use `.env`, but implement a first-run setup workflow when practical:

1. Jellyfin URL
2. Jellyfin API key
3. Sonarr URL
4. Sonarr API key
5. Radarr URL
6. Radarr API key
7. Connectivity validation
8. Jellyfin library discovery
9. Library-policy setup
10. Dry-run enablement

Secrets must never be rendered back in full after saving.

---

## 41. External Dashboard Integration

Curatarr must remain independent of any parent/home dashboard.

Provide:

- A stable web URL
- A health endpoint
- A concise status endpoint suitable for dashboard integrations
- Optional future API endpoints for summary data

An external dashboard may simply link to Curatarr initially.

Curatarr must not depend on that dashboard to function.

---

# Branding

## 42. Product Name

Application name:

```text
Curatarr
```

The name should visually fit the broader `*arr` ecosystem without copying another project's branding.

---

## 43. Logo Concept

The `C` in `Curatarr` is formed from two curved arrows.

Requirements:

- Two curved arrow segments together read clearly as the letter `C`.
- The mark should suggest cycling/reuse without looking like a generic recycling-bin icon.
- Arrowheads should be subtle.
- The negative space must preserve the readability of the `C`.
- The remaining `yclearr` wordmark should use a clean sans-serif typeface.
- Must work in light and dark themes.
- Must remain recognizable at favicon size.

Required variants:

- Full horizontal wordmark
- Icon-only `C`
- Static SVG
- Animated SVG/web implementation
- Favicon/app icon

Prefer vector SVG as the canonical source.

---

## 44. Logo Load Animation

On a full page load:

1. Both arrow segments begin in their normal resting position.
2. The arrows rotate around their shared center so they appear to chase one another.
3. They complete exactly one revolution.
4. They settle precisely back into the static `C`.
5. They remain static afterward.

Recommended default:

```text
Duration: 1.2 seconds
Iterations: 1
Delay: 100 ms
Easing: cubic-bezier(0.4, 0, 0.2, 1)
```

Do not:

- Loop continuously.
- Bounce.
- Overshoot.
- Replay on ordinary component rerenders.
- Replay on every internal navigation event.

Run once per full page load.

Respect reduced-motion preferences:

```css
@media (prefers-reduced-motion: reduce) {
    .curatarr-logo .curatarr-arrow {
        animation: none;
    }
}
```

Recommended SVG structure:

```html
<svg class="curatarr-logo" viewBox="0 0 100 100" aria-hidden="true">
    <g class="curatarr-arrow curatarr-arrow-top">
        <!-- SVG path -->
    </g>
    <g class="curatarr-arrow curatarr-arrow-bottom">
        <!-- SVG path -->
    </g>
</svg>
```

Recommended animation approach:

```css
.curatarr-logo .curatarr-arrow {
    transform-box: view-box;
    transform-origin: 50% 50%;
    animation: curatarr-spin 1.2s cubic-bezier(0.4, 0, 0.2, 1) 100ms 1;
}

@keyframes curatarr-spin {
    from {
        transform: rotate(0deg);
    }

    to {
        transform: rotate(360deg);
    }
}

@media (prefers-reduced-motion: reduce) {
    .curatarr-logo .curatarr-arrow {
        animation: none;
    }
}
```

The final SVG geometry should be custom paths rather than relying on a text glyph for the `C`.

---

# Persistence

## 45. Recommended Data Model

Create equivalent persistent entities for:

### GlobalSettings

Global defaults and integration configuration.

### LibraryPolicy

Per-library acquisition, retention, quota, and review policy.

### TitleOverride

Per-series/movie overrides.

### MediaIdentity

Cross-system identity mapping.

### PlaybackState

Latest playback/activity information.

### FavoriteState

Per-user or aggregated favorite state.

### AcquisitionState

Current staged-acquisition state for TV.

### PurgeCandidate

Current cleanup candidate and explanation.

### ScheduledDeletion

Future destructive action and warning period.

### WatchStateSnapshot

Playback history preserved before media removal.

### CuratarrEvent

Observed external event.

### CuratarrAction

Decision/action produced by Curatarr.

Exact schema may evolve as implementation proceeds.

---

# Safety and Deployment

## 46. Initial Dry-Run Mode

Initial deployment should enable:

- Real Jellyfin event processing
- Real Jellyfin metadata reads
- Real Sonarr/Radarr reads
- Real Sonarr acquisition behavior after it is validated
- Real candidate scoring
- Real review queue
- Real Leaving Soon behavior once artwork restoration is proven

But:

```text
AUTOMATIC DELETION MUST START DISABLED
```

Deletion begins in dry-run mode.

Dry-run should show exactly what would have been deleted and why.

Enable deletion independently per library after validation.

---

## 47. Health and Observability

Provide at minimum:

```text
GET /health
```

Health should distinguish:

- Application health
- Database health
- Jellyfin connectivity
- Sonarr connectivity
- Radarr connectivity
- Scheduler status

Logs should be useful but must not unnecessarily expose secrets.

Never log API keys.

---

## 48. Authentication

Design the application so authentication can be added cleanly.

For early trusted-LAN development, local unauthenticated operation may be acceptable if deployment configuration explicitly allows it.

Do not bake a private network assumption into the architecture.

---

# Implementation Phases

## 49. Phase 0 — Repository and Foundations

Implement:

- Git repository
- Python project
- Flask app factory
- Configuration loading
- Database
- Alembic
- Base templates
- Bootstrap UI shell
- Health endpoint
- Test framework
- `.env.example`
- `.gitignore`
- README
- CHANGELOG
- License placeholder/selection
- Secret/privacy safeguards
- Curatarr branding shell

Exit criteria:

- App starts.
- Tests run.
- Migration works.
- No private environment values exist in tracked files.

---

## 50. Phase 1 — Read-Only Discovery

Implement:

- Jellyfin client
- Sonarr client
- Radarr client
- Connectivity tests
- Library discovery
- Media discovery
- Identity mapping
- Library statistics
- Integration status UI

No destructive actions.

---

## 51. Phase 2 — Playback and State

Implement:

- Jellyfin webhook/event endpoint
- Playback ingestion
- Completion ingestion
- Favorite ingestion/reconciliation
- Playback state
- Audit history
- Hourly reconciliation
- Duplicate-event protection

---

## 52. Phase 3 — TV Acquisition

Implement:

- Minimum footprint model
- Current-season filling
- One-season-ahead logic
- Sonarr monitoring changes
- Sonarr searches
- Airing-season behavior
- Per-library and per-title configuration

Acquisition actions must be explainable and idempotent.

---

## 53. Phase 4 — Retention Simulation

Implement:

- Inactivity calculations
- Meaningful-watch threshold
- Favorite weighting
- Grace periods
- Purge strategies
- Library high/low watermarks
- Free-space pressure policies
- Candidate scoring
- Review queue
- Dry-run actions

No automatic deletion yet.

---

## 54. Phase 5 — Leaving Soon

Implement:

- Scheduled deletion model
- Poster preservation
- Leaving Soon badge generation
- Jellyfin artwork update
- Artwork restoration
- Playback rescue
- Snooze behavior

Validate artwork restoration carefully before proceeding.

---

## 55. Phase 6 — Controlled Deletion

Implement:

- Manual approval
- Pre-delete revalidation
- Sonarr-based TV deletion/collapse
- Radarr-based movie deletion
- Watch-state snapshot
- Audit records
- Reclaimed-space accounting

Automatic deletion remains disabled by default.

---

## 56. Phase 7 — Automatic Lifecycle

Enable per library:

- Automatic inactivity cleanup
- Automatic quota cleanup
- Timed Leaving Soon deletion
- Configurable review bypass

Retain fail-safe pre-delete validation.

---

# Acceptance Scenarios

## 57. TV Minimal Footprint Expansion

Configuration:

```text
Minimum footprint: S1E1 only
Acquisition trigger: 1 completed episode
```

Initial media:

```text
S1E1
```

User completes S1E1.

Expected:

- Remaining Season 1 requested.
- Season 2 requested/monitored.
- Season 3 untouched.
- Decision logged.

---

## 58. TV Progression

User completes a qualifying episode in Season 2.

Expected:

- Season 3 acquisition begins.
- Season 4 remains untouched.

---

## 59. TV Inactivity

A series exceeds its inactivity policy.

Expected:

- Candidate created.
- Proposed action collapses it to configured minimum.
- Explanation displayed.
- Leaving Soon applied if policy requires.

---

## 60. Playback Rescue

A Leaving Soon series receives playback.

Expected:

- Candidate removed or invalidated.
- Scheduled deletion cancelled.
- Normal artwork restored.
- Inactivity timer reset.
- Rescue logged.

---

## 61. Movie Quota Cleanup

Movie library exceeds high-water mark.

Configured strategy:

```text
Oldest watched non-favorites first
```

Expected:

- Watched non-favorites ranked by oldest last watch.
- Enough candidates selected to reach low-water target.
- Favorites ranked behind eligible non-favorites.
- Never Purge excluded.
- Reasons displayed.

---

## 62. Grace Period

Content is inside its configured acquisition grace period.

Expected:

- Not selected for cleanup.

---

## 63. Active Download Protection

Candidate has active Sonarr/Radarr download/import activity.

Expected:

- Excluded from deletion.
- Reason recorded.

---

## 64. Review Approval

User approves Delete.

Expected:

- Candidate is fully revalidated.
- Destructive call goes through Sonarr/Radarr.
- Result is recorded.
- Storage accounting is updated.

---

## 65. Dry Run

Deletion disabled.

Expected:

- Entire decision process executes.
- Proposed destructive action appears in history/review.
- No destructive external API call is issued.

---

## 66. Duplicate Playback Event

Same webhook/event is delivered twice.

Expected:

- One logical acquisition/decision.
- No duplicate search.
- No duplicate review candidate.

---

## 67. Integration Failure During Deletion

Sonarr/Radarr/Jellyfin state cannot be verified.

Expected:

- No deletion.
- Candidate remains safe.
- Failure is logged and surfaced.

---

# Testing Requirements

## 68. Automated Tests

At minimum cover:

- Configuration inheritance
- Per-title overrides
- Playback reset behavior
- Episode completion trigger
- One-season-ahead acquisition
- Partial-season completion
- Meaningful-watch threshold
- Favorite ranking
- Never Purge exclusion
- Grace period
- High/low watermark calculation
- Each purge strategy
- Candidate storage calculation
- Snooze
- Playback rescue
- Duplicate event handling
- Pre-delete revalidation
- Dry-run blocking
- External API error handling
- Identity mapping
- Artwork restore state
- Reduced-motion logo behavior where practical

Use mocked Jellyfin/Sonarr/Radarr responses for unit/integration tests.

Do not require real private services for the automated test suite.

---

# Documentation Requirements

## 69. README

README should contain:

- What Curatarr does
- Screenshots using generic/demo data
- Supported integrations
- Installation
- Configuration
- First-run setup
- Docker instructions when available
- Development setup
- Dry-run warning
- How retention/acquisition works
- Privacy/security notes

---

## 70. Decision Documentation

For nontrivial architecture choices, create short ADR-style documentation under:

```text
docs/decisions/
```

Examples:

```text
001-media-identity.md
002-deletion-through-arr-apps.md
003-purge-strategy-model.md
004-leaving-soon-artwork.md
```

Do not stop implementation to request approval for ordinary ADR decisions. Document the choice and continue.

---

# Guiding Principles

## 71. Explainability

Every action must answer one of:

```text
Why are we acquiring this?
```

or:

```text
Why are we deleting this?
```

The answer must be visible in the UI and audit history.

---

## 72. Safety

When uncertain about a destructive action:

```text
Do not delete.
```

Acquisition may fail noisy.

Deletion must fail safe.

---

## 73. Independence

Curatarr must remain:

- Standalone
- Configurable
- Publishable
- Deployment-neutral
- Free of private environment assumptions

---

## 74. Completion Standard

Do not consider Curatarr complete because screens exist.

A feature is complete only when:

- Core behavior works.
- Relevant tests pass.
- Errors fail safely.
- Decisions are auditable.
- Configuration is not hard-coded.
- No private identifiers are committed.
- Documentation reflects actual behavior.

Codex should proceed through the implementation phases autonomously, committing stable progress as it goes, and should request user input only when genuinely blocked according to Section 2.
---

# Implementation Determinism Addendum

The following sections are normative. If an earlier section leaves behavior ambiguous,
these sections control. If a minor implementation detail remains unspecified, make a
reasonable conventional engineering decision, document it, implement it, and continue.
Do not request user input merely because multiple technically valid approaches exist.

# 75. Canonical Defaults and Configuration Schema

Use typed configuration. Persist policy configuration in the database. Use environment
variables only for bootstrap/runtime concerns and secrets.

All durations are stored internally as seconds or timestamps, but the UI should expose
human-friendly units.

## 75.1 Runtime / Bootstrap Configuration

| Setting | Type | Default | Required | Notes |
|---|---|---:|---|---|
| `CURATARR_SECRET_KEY` | secret string | none | production | Never log or return to browser |
| `DATABASE_URL` | string | `sqlite:///curatarr.db` | no | PostgreSQL recommended for production |
| `CURATARR_HOST` | string | `0.0.0.0` | no | Runtime bind only |
| `CURATARR_PORT` | integer | `8787` | no | Avoid assuming external reverse proxy |
| `CURATARR_LOG_LEVEL` | enum | `INFO` | no | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `CURATARR_BASE_URL` | URL | empty | no | Used for callback/link generation when needed |
| `CURATARR_DEMO_MODE` | boolean | `false` | no | Synthetic integrations only |
| `CURATARR_ALLOW_UNAUTHENTICATED` | boolean | `true` | no | Suitable for trusted-LAN early deployments |
| `CURATARR_SESSION_COOKIE_SECURE` | boolean | `false` | no | Enable behind HTTPS |
| `CURATARR_CSRF_ENABLED` | boolean | `true` | no | Disable only in tests |

## 75.2 Integration Configuration

Integration secrets must be write-only after save in the normal UI.

| Setting | Type | Default | Validation |
|---|---|---:|---|
| Jellyfin URL | URL | none | Must respond to system/info or equivalent health call |
| Jellyfin API key | secret | none | Validate before saving as connected |
| Sonarr URL | URL | none | Validate with `/api/v3/system/status` |
| Sonarr API key | secret | none | Send using `X-Api-Key` |
| Radarr URL | URL | none | Validate with `/api/v3/system/status` |
| Radarr API key | secret | none | Send using `X-Api-Key` |
| Webhook shared token | secret | generated | Optional defense-in-depth token on Curatarr webhook URL |

Store URLs normalized without a trailing slash.

## 75.3 Global Policy Defaults

| Setting | Type | Default | Allowed |
|---|---|---:|---|
| Reconciliation interval | duration | 1 hour | 5 minutes–24 hours |
| TV acquisition completed-episode threshold | integer | 1 | 1–100 |
| Meaningful TV watch threshold | integer | 2 | 1–100 |
| TV inactivity period | duration | 90 days | 1 day–10 years |
| Movie inactivity period | duration | 90 days | 1 day–10 years |
| New-media grace period | duration | 30 days | 0–3650 days |
| Minimum TV footprint mode | enum | `first_n_episodes` | `first_n_episodes`, `season_1`, `entire_series` |
| Minimum TV episode count | integer | 3 | 1–100; ignored outside `first_n_episodes` |
| Keep one season ahead | boolean | true | v1 should remain true unless future policy adds N seasons |
| Season 0/Specials managed | boolean | false | false by default |
| Review mode | enum | `require_review` | `recommend`, `require_review`, `automatic` |
| Review expiry mode | enum | `manual_forever` | `manual_forever`, `auto_delete_after_notice` |
| Leaving Soon notice | duration | 14 days | 0–365 days |
| Purge strategy | enum | media-type default | See Section 80 |
| Library-size enforcement | boolean | false | per library |
| High-water bytes | integer | null | Must exceed low-water bytes |
| Low-water bytes | integer | null | Must be below high-water bytes |
| Free-space enforcement | boolean | false | per library/root volume |
| Low free-space threshold | percentage | 15% | 1–99% |
| Critical free-space threshold | percentage | 8% | 1–99%; less than low |
| Never Purge | boolean | false | title override |
| Dry-run destructive actions | boolean | true | default true for fresh installs |

## 75.4 Media-Type Default Purge Strategy

Default for TV:

```text
oldest_unwatched_first
```

Default for Movies:

```text
oldest_watched_nonfavorite_first
```

These are defaults only and remain configurable per library.

## 75.5 Inheritance Semantics

Each policy field must store one of:

```text
INHERIT
EXPLICIT VALUE
```

Resolution order:

```text
title override -> library policy -> global policy -> compiled application default
```

The UI must show both the effective value and its source.

---

# 76. Canonical State Machines

State changes must occur inside database transactions. Side effects against external
systems are represented as durable actions and must be idempotent.

## 76.1 TV Acquisition State

Per series, maintain:

```text
DORMANT
READY
EXPANDING_CURRENT
PREFETCHING_NEXT
ACTIVE
WAITING_FOR_FUTURE
ERROR_RETRY
```

Definitions:

| State | Meaning |
|---|---|
| `DORMANT` | Series exists at minimum retained footprint and has no pending expansion |
| `READY` | Playback interest exists; acquisition evaluation is required |
| `EXPANDING_CURRENT` | Missing episodes in the viewer's current season are being searched/monitored |
| `PREFETCHING_NEXT` | Next season is being monitored/searched |
| `ACTIVE` | Required current and one-season-ahead content is satisfied or commands completed |
| `WAITING_FOR_FUTURE` | Next triggered season exists but unreleased episodes remain; Sonarr monitoring handles arrivals |
| `ERROR_RETRY` | External action failed; safe retry pending |

Transitions:

| Current | Event | Guard | Action | Next |
|---|---|---|---|---|
| any | any playback | valid mapped TV item | reset retention, cancel purge | unchanged or `READY` |
| `DORMANT` | completed episode threshold reached | current season exists | calculate current/next acquisition plan | `READY` |
| `READY` | current season incomplete | not already searched for same plan revision | monitor/search missing current season | `EXPANDING_CURRENT` |
| `READY` | current season complete | next season exists | monitor/search next season | `PREFETCHING_NEXT` |
| `EXPANDING_CURRENT` | command accepted | next season exists | enqueue next-season action | `PREFETCHING_NEXT` |
| `PREFETCHING_NEXT` | command accepted | future episodes unaired | persist monitoring state | `WAITING_FOR_FUTURE` |
| `PREFETCHING_NEXT` | command accepted | acquisition target satisfied | persist state | `ACTIVE` |
| `WAITING_FOR_FUTURE` | qualifying playback in triggered season | later season exists | new plan revision | `READY` |
| any action state | retryable integration error | retry budget available | schedule exponential retry | `ERROR_RETRY` |
| `ERROR_RETRY` | retry succeeds | — | continue stored plan | prior target state |

A user watching Season N must never automatically cause Season N+2 to be searched.
Curatarr may fill Season N and acquire Season N+1.

If users watch different seasons concurrently, the highest demonstrated season may drive
the one-season-ahead target, but Curatarr must not remove or skip content needed by a
lower-season active viewer.

## 76.2 Purge Candidate State

```text
NONE
ELIGIBLE
REVIEW
LEAVING_SOON
SNOOZED
APPROVED
EXECUTING
COMPLETED
RESCUED
BLOCKED
FAILED
```

Transitions:

| Current | Event | Guard | Action | Next |
|---|---|---|---|---|
| `NONE` | policy evaluation | eligible | create candidate and reason snapshot | `ELIGIBLE` |
| `ELIGIBLE` | review required | — | queue candidate, apply badge if configured | `REVIEW` |
| `ELIGIBLE` | automatic with notice | — | schedule deletion, apply badge | `LEAVING_SOON` |
| `ELIGIBLE` | automatic no notice | prechecks pass | approve | `APPROVED` |
| `REVIEW` | user snoozes | — | set snooze-until, remove badge | `SNOOZED` |
| `REVIEW` | user keeps | — | invalidate candidate | `RESCUED` |
| `REVIEW` | user marks Never Purge | — | persist override, invalidate | `RESCUED` |
| `REVIEW` | user approves | prechecks pass | record approval | `APPROVED` |
| `LEAVING_SOON` | notice expires | prechecks pass | approve | `APPROVED` |
| `SNOOZED` | snooze expires | policy still eligible | reevaluate from scratch | `ELIGIBLE` |
| `APPROVED` | execution begins | destructive mode enabled | create durable action | `EXECUTING` |
| `APPROVED` | dry run | — | record would-delete result | `COMPLETED` |
| `EXECUTING` | arr deletion succeeds | — | reconcile size/state | `COMPLETED` |
| any pending | playback occurs | — | cancel schedule and badge | `RESCUED` |
| any pending | policy no longer applies | — | invalidate | `RESCUED` |
| any pending | hard safety guard fails | — | preserve media and reason | `BLOCKED` |
| `EXECUTING` | retryable external failure | — | preserve action for retry | `FAILED` |

`RESCUED`, `BLOCKED`, `FAILED`, and `COMPLETED` remain in history but are not active candidates.

## 76.3 Movie Lifecycle State

Movies do not have acquisition expansion states.

```text
ACTIVE -> ELIGIBLE -> REVIEW/LEAVING_SOON -> APPROVED -> EXECUTING -> COMPLETED
```

Playback follows the same rescue/reset semantics.

---

# 77. Event Normalization Contract

External events must be normalized before entering policy code.

Canonical event object:

```json
{
  "event_id": "stable-id-or-derived-hash",
  "source": "jellyfin",
  "event_type": "playback_started|playback_progress|playback_stopped|item_played|favorite_changed",
  "occurred_at": "UTC timestamp",
  "received_at": "UTC timestamp",
  "user_external_id": "string|null",
  "item_external_id": "string",
  "item_type": "episode|movie|series|season",
  "series_external_id": "string|null",
  "season_number": 1,
  "episode_number": 1,
  "played": true,
  "position_ticks": 0,
  "runtime_ticks": 0,
  "payload_hash": "sha256",
  "raw_payload_ref": "optional"
}
```

Rules:

- Store timestamps in UTC.
- Preserve raw payload only when needed for diagnostics; do not persist secrets.
- If source has no stable event ID, derive one from normalized identifying fields and payload hash.
- Duplicate normalized event IDs are ignored after the first successful ingest.
- HTTP webhook handlers should validate, persist, and acknowledge quickly. Long policy work occurs after persistence.

---

# 78. External API Contract

Build thin integration clients. Policy code must not call HTTP directly.

All clients need:

```python
health()
version()
request(...)
```

with bounded timeouts, structured errors, and sanitized logging.

Do not assume all installations run identical versions. On connection, store detected
product/version and gate optional capabilities.

## 78.1 Jellyfin Required Operations

Curatarr requires the functional equivalent of:

- Read server/system information.
- List users.
- List libraries/views.
- Query series, seasons, episodes, movies, provider IDs, runtime, dates, and image tags.
- Query per-user played state.
- Query per-user favorite state.
- Query latest/recent media when useful for reconciliation.
- Mark an item played / update user item data when restoring preserved watch state.
- Read primary poster image.
- Replace/upload primary poster image.
- Restore original primary poster image.
- Receive playback/notification events through Jellyfin's webhook notification plugin when configured.

Current Jellyfin SDK exposes user-data operations including getting item user data,
marking played/unplayed, favorite/unfavorite, and updating item user data. Image APIs
support item image manipulation. The webhook integration must remain optional because
scheduled reconciliation is authoritative for recovery.

Curatarr must not assume a webhook alone is a complete durable event stream.

## 78.2 Sonarr Required API Surface

Use Sonarr API v3 semantics and `X-Api-Key` authentication.

Required operations include:

```text
GET  /api/v3/system/status
GET  /api/v3/series
GET  /api/v3/series/{id}
PUT  /api/v3/series/{id}
GET  /api/v3/episode
GET  /api/v3/episode/{id}
PUT  /api/v3/episode/{id}
PUT  /api/v3/episode/monitor
GET  /api/v3/episodefile
DELETE /api/v3/episodefile/{id}
GET  /api/v3/queue
GET  /api/v3/queue/details
POST /api/v3/command
GET  /api/v3/command/{id}
```

Use command operations for explicit searches. Prefer a season-scoped search command
(e.g. `SeasonSearch` with `seriesId` and `seasonNumber`) where supported. If a connected
version differs, capability-detect and use the narrowest safe equivalent.

Do not issue a broad missing-library search when a season-scoped search can satisfy the request.

TV collapse should remove only files outside the configured retained footprint while
keeping Sonarr's series record unless policy explicitly evolves to remove the series.

## 78.3 Radarr Required API Surface

Use Radarr API v3 semantics and `X-Api-Key` authentication.

Required operations include:

```text
GET  /api/v3/system/status
GET  /api/v3/movie
GET  /api/v3/movie/{id}
PUT  /api/v3/movie/{id}
DELETE /api/v3/movie/{id}
GET  /api/v3/moviefile
DELETE /api/v3/moviefile/{id}
GET  /api/v3/queue
GET  /api/v3/queue/details
POST /api/v3/command
GET  /api/v3/command/{id}
GET  /api/v3/diskspace
```

When deleting a movie, use Radarr's delete operation with file deletion enabled. Do not
add an import exclusion unless explicitly configured in a future feature, because users
may want to request the movie again later.

Movie search support is not required for the initial Curatarr lifecycle because deleted
movies are expected to be re-requested through the user's request system.

## 78.4 Integration Timeouts

Defaults:

```text
connect timeout: 5 seconds
read timeout: 30 seconds
destructive call timeout: 60 seconds
```

Retries:

```text
GET/read operations: up to 3 attempts with exponential backoff
idempotent writes: up to 3 attempts if idempotency can be proven
destructive writes: never blindly retry after an unknown response
```

After an ambiguous destructive response, reconcile actual external state before deciding whether to retry.

---

# 79. TV Acquisition Algorithm

Policy code must produce an acquisition plan before issuing external actions.

Pseudocode:

```text
on playback event:
    resolve series + season + episode
    reset inactivity clock
    rescue any pending purge

    if event is not completed:
        stop after retention reset

    increment/reconcile completed episode state for the viewer
    calculate qualifying completed count for the season

    if qualifying count < effective acquisition threshold:
        stop

    current_season = played episode season
    next_season = first regular season after current_season

    target = {
        fill_missing_episodes(current_season),
        acquire(next_season) if next_season exists
    }

    remove Specials / Season 0 unless enabled
    remove episodes already present
    remove actions already satisfied
    remove actions represented by active Sonarr queue entries
    compare target with last completed acquisition plan revision

    persist plan
    execute narrow Sonarr monitoring/search operations idempotently
```

### 79.1 Minimum Footprint Edge Case

If minimum footprint is the first N episodes of Season 1 and a user completes one of
those episodes:

```text
fill remaining Season 1
acquire Season 2
```

assuming acquisition threshold is satisfied.

### 79.2 Out-of-Order Viewing

If a user watches Season 4 while only Seasons 1-2 are expected:

- Treat the valid Season 4 playback as demonstrated interest.
- Never delete lower-season content due to this event.
- Fill missing Season 4 content.
- Acquire Season 5 if it exists.
- Do not automatically fetch missing Seasons 2-3 solely to make numbering contiguous.
- Record the unusual condition in the explanation.

### 79.3 Multiple Users

Aggregate demand is union-based:

- Any user's playback resets retention.
- Any user's favorite counts.
- Any user's qualifying completion can advance acquisition.
- A purge may not remove media needed by another currently active viewing path if that
  content is within the effective inactivity window.

---

# 80. Purge Ranking Algorithms

All ranking functions must be deterministic. Add a stable final tie-breaker using
Curatarr media identity primary key.

## 80.1 Shared Exclusion Filter

Before ranking, exclude:

```text
Never Purge
inside grace period
active Sonarr/Radarr queue/import
currently executing lifecycle action
snoozed
unmapped/identity-ambiguous
external integration unhealthy when state is required
already at minimum TV footprint
```

## 80.2 Oldest Unwatched First

Sort key:

```text
favorite_bucket,
meaningful_watch_bucket,
never_played_bucket,
last_activity_or_acquired_at,
acquired_at,
media_id
```

Where lower priority number is purged first:

```text
favorite_bucket: nonfavorite=0, favorite=1
meaningful_watch_bucket: below_threshold=0, meaningful=1
never_played_bucket: never_played=0, played=1
```

Within a bucket, oldest relevant timestamp first.

## 80.3 Oldest Watched Non-Favorites First

Sort key:

```text
favorite_bucket,
watched_bucket,
last_played_at_or_epoch,
acquired_at,
media_id
```

Where:

```text
favorite_bucket: nonfavorite=0, favorite=1
watched_bucket: watched=0, unwatched=1
```

For watched content, oldest `last_played_at` first.
For unwatched content, fall back to oldest `acquired_at`.

## 80.4 Weighted Strategy

Initial transparent score:

```text
score =
    days_since_last_play * W_LAST_PLAY
  + days_since_acquired * W_AGE
  + size_gib * W_SIZE
  + below_meaningful_threshold_bonus
  + unwatched_bonus
  - favorite_penalty
```

Never Purge is not represented by a score because those items are excluded.

Initial default weights should be conservative and editable later. The UI must render
the score components, not just the total.

## 80.5 High/Low Water Selection

```text
if current_size <= high_water:
    select nothing

bytes_to_reclaim = current_size - low_water
rank candidates
selected = []

for candidate in ranked:
    selected.append(candidate)
    reclaimed += candidate.reclaimable_bytes
    if reclaimed >= bytes_to_reclaim:
        break
```

For TV, `reclaimable_bytes` means bytes outside the retained minimum footprint.

If eligible content cannot reach the low-water target, queue everything safely eligible
and report the remaining deficit. Never weaken Never Purge or safety guards silently.

---

# 81. Concurrency and Locking

Curatarr must be safe when multiple webhook events, reconciliation jobs, and UI actions occur concurrently.

Use database-backed locking/serialization, not process-only mutexes.

Required logical lock scopes:

```text
series:<media_identity_id>
movie:<media_identity_id>
purge_candidate:<candidate_id>
library_policy_eval:<library_id>
scheduled_deletion:<id>
```

Recommended implementation:

- PostgreSQL: transactional row locks (`SELECT ... FOR UPDATE`) where practical.
- SQLite development mode: transaction serialization plus unique constraints.
- Never hold a DB transaction open while waiting on a slow external HTTP request.

Pattern:

```text
transaction 1:
    lock state
    calculate next action
    persist durable action with unique idempotency key
commit

external call:
    execute

transaction 2:
    record result
    advance state
commit
```

Unique action key examples:

```text
sonarr-season-search:<series_id>:<season>:<plan_revision>
purge:<candidate_id>:<candidate_revision>
poster-leaving-soon:<jellyfin_item_id>:<candidate_revision>
```

---

# 82. Durable Action Queue

Do not make core lifecycle correctness depend on an in-memory task queue.

Persist outbound actions:

```text
PENDING
RUNNING
SUCCEEDED
FAILED_RETRYABLE
FAILED_FINAL
CANCELLED
UNKNOWN_RECONCILE
```

Each action stores:

- action type
- target identity
- idempotency key
- payload
- reason
- attempts
- last error
- created time
- next retry time
- started time
- completed time
- related event ID
- related purge/acquisition plan revision

A lightweight in-process worker is acceptable initially as long as pending actions survive restart.

---

# 83. Concrete Database Schema

Use SQLAlchemy models and Alembic migrations. UUID primary keys are preferred for
Curatarr-owned entities. External numeric/string IDs remain separate columns.

## 83.1 `app_settings`

```text
id UUID PK
key VARCHAR UNIQUE NOT NULL
value_json JSON NOT NULL
is_secret BOOLEAN NOT NULL DEFAULT false
updated_at TIMESTAMPTZ NOT NULL
```

Secrets should preferably be stored using an encrypted-at-rest abstraction when a
stable application secret is available. At minimum, never render or log them.

## 83.2 `integrations`

```text
id UUID PK
kind ENUM(jellyfin, sonarr, radarr) UNIQUE NOT NULL
base_url VARCHAR NOT NULL
secret_ref VARCHAR/ENCRYPTED NULL
detected_version VARCHAR NULL
enabled BOOLEAN NOT NULL DEFAULT true
health_state VARCHAR NOT NULL DEFAULT 'unknown'
last_health_at TIMESTAMPTZ NULL
last_error TEXT NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

## 83.3 `libraries`

```text
id UUID PK
jellyfin_library_id VARCHAR UNIQUE NOT NULL
name VARCHAR NOT NULL
media_type ENUM(tv, movies, mixed, other) NOT NULL
enabled BOOLEAN NOT NULL DEFAULT true
last_size_bytes BIGINT NULL
last_scanned_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

## 83.4 `library_policies`

```text
id UUID PK
library_id UUID FK libraries UNIQUE NOT NULL
policy_json JSON NOT NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

Use validated typed application models around `policy_json`; do not pass arbitrary JSON through policy code.

## 83.5 `media_identities`

```text
id UUID PK
library_id UUID FK libraries NOT NULL
media_type ENUM(series, movie) NOT NULL
title VARCHAR NOT NULL
sort_title VARCHAR NULL
jellyfin_id VARCHAR NULL
sonarr_id INTEGER NULL
radarr_id INTEGER NULL
tmdb_id INTEGER NULL
tvdb_id INTEGER NULL
imdb_id VARCHAR NULL
path_fingerprint VARCHAR NULL
added_at TIMESTAMPTZ NULL
last_seen_at TIMESTAMPTZ NOT NULL
missing_since TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

Indexes:

```text
(jellyfin_id)
(sonarr_id)
(radarr_id)
(tmdb_id)
(tvdb_id)
(imdb_id)
(library_id, media_type)
```

Provider IDs are preferred for remapping when Jellyfin IDs change.

## 83.6 `title_overrides`

```text
id UUID PK
media_identity_id UUID FK UNIQUE NOT NULL
override_json JSON NOT NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

## 83.7 `media_parts`

Represents seasons/episodes/movie files needed for footprint and size calculations.

```text
id UUID PK
media_identity_id UUID FK NOT NULL
kind ENUM(season, episode, movie_file) NOT NULL
season_number INTEGER NULL
episode_number INTEGER NULL
jellyfin_id VARCHAR NULL
sonarr_episode_id INTEGER NULL
arr_file_id INTEGER NULL
size_bytes BIGINT NOT NULL DEFAULT 0
has_file BOOLEAN NOT NULL DEFAULT false
air_date TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

Unique constraint where possible:

```text
(media_identity_id, kind, season_number, episode_number)
```

## 83.8 `user_media_state`

```text
id UUID PK
media_identity_id UUID FK NOT NULL
jellyfin_user_id VARCHAR NOT NULL
last_played_at TIMESTAMPTZ NULL
completed_episode_count INTEGER NOT NULL DEFAULT 0
favorite BOOLEAN NOT NULL DEFAULT false
updated_at TIMESTAMPTZ NOT NULL
UNIQUE(media_identity_id, jellyfin_user_id)
```

## 83.9 `episode_user_state`

Needed for reliable restoration and season thresholds.

```text
id UUID PK
media_part_id UUID FK NOT NULL
jellyfin_user_id VARCHAR NOT NULL
played BOOLEAN NOT NULL DEFAULT false
last_played_at TIMESTAMPTZ NULL
playback_position_ticks BIGINT NOT NULL DEFAULT 0
favorite BOOLEAN NOT NULL DEFAULT false
updated_at TIMESTAMPTZ NOT NULL
UNIQUE(media_part_id, jellyfin_user_id)
```

## 83.10 `acquisition_states`

```text
id UUID PK
media_identity_id UUID FK UNIQUE NOT NULL
state VARCHAR NOT NULL
highest_demonstrated_season INTEGER NULL
target_next_season INTEGER NULL
plan_revision INTEGER NOT NULL DEFAULT 0
last_triggered_at TIMESTAMPTZ NULL
last_error TEXT NULL
updated_at TIMESTAMPTZ NOT NULL
```

## 83.11 `purge_candidates`

```text
id UUID PK
media_identity_id UUID FK NOT NULL
state VARCHAR NOT NULL
candidate_revision INTEGER NOT NULL DEFAULT 1
reason_code VARCHAR NOT NULL
reason_text TEXT NOT NULL
reclaimable_bytes BIGINT NOT NULL
score NUMERIC NULL
score_detail_json JSON NULL
eligible_at TIMESTAMPTZ NOT NULL
last_activity_snapshot TIMESTAMPTZ NULL
scheduled_delete_at TIMESTAMPTZ NULL
snooze_until TIMESTAMPTZ NULL
approved_at TIMESTAMPTZ NULL
completed_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

Only one active candidate per media identity:

```text
partial unique index on media_identity_id where state is active
```

## 83.12 `poster_snapshots`

```text
id UUID PK
media_identity_id UUID FK NOT NULL
jellyfin_item_id VARCHAR NOT NULL
original_image_tag VARCHAR NULL
original_image_bytes_path VARCHAR NULL
badged_image_bytes_path VARCHAR NULL
candidate_id UUID FK NULL
active BOOLEAN NOT NULL DEFAULT false
created_at TIMESTAMPTZ NOT NULL
restored_at TIMESTAMPTZ NULL
```

Runtime artwork files belong in application data storage, never Git.

## 83.13 `lifecycle_events`

```text
id UUID PK
external_event_id VARCHAR UNIQUE NULL
source VARCHAR NOT NULL
event_type VARCHAR NOT NULL
occurred_at TIMESTAMPTZ NOT NULL
received_at TIMESTAMPTZ NOT NULL
media_identity_id UUID FK NULL
jellyfin_user_id VARCHAR NULL
payload_hash VARCHAR NOT NULL
normalized_json JSON NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

## 83.14 `lifecycle_actions`

```text
id UUID PK
idempotency_key VARCHAR UNIQUE NOT NULL
action_type VARCHAR NOT NULL
state VARCHAR NOT NULL
media_identity_id UUID FK NULL
candidate_id UUID FK NULL
event_id UUID FK NULL
reason_text TEXT NOT NULL
payload_json JSON NOT NULL
attempts INTEGER NOT NULL DEFAULT 0
next_retry_at TIMESTAMPTZ NULL
started_at TIMESTAMPTZ NULL
completed_at TIMESTAMPTZ NULL
last_error TEXT NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

## 83.15 `watch_state_snapshots`

```text
id UUID PK
media_identity_id UUID FK NOT NULL
jellyfin_user_id VARCHAR NOT NULL
provider_key VARCHAR NOT NULL
season_number INTEGER NULL
episode_number INTEGER NULL
played BOOLEAN NOT NULL
playback_position_ticks BIGINT NOT NULL DEFAULT 0
date_played TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
restored_at TIMESTAMPTZ NULL
```

---

# 84. Reconciliation Algorithm

Reconciliation is the authoritative repair mechanism.

Per run:

```text
1. Check integration health and versions.
2. Discover enabled Jellyfin libraries.
3. Refresh media identities/provider IDs.
4. Refresh media parts and file sizes.
5. Refresh per-user played/favorite state.
6. Refresh Sonarr/Radarr monitored/file/queue state.
7. Reconcile acquisition-state assumptions.
8. Rescue invalid purge candidates.
9. Identify newly eligible purge candidates.
10. Repair stale Leaving Soon artwork state.
11. Retry safe durable actions.
12. Recompute library sizes and pressure status.
13. Record summary metrics and completion time.
```

Do not run two full reconciliations for the same library concurrently.

Use pagination/batching. Avoid loading an entire large library into memory when unnecessary.

---

# 85. Webhook Processing

Preferred flow:

```text
HTTP request
  -> authenticate/validate
  -> normalize
  -> INSERT event (dedupe)
  -> return 2xx
  -> worker evaluates policies
```

Target webhook response:

```text
< 500 ms under normal local conditions
```

Never wait for Sonarr/Radarr searches before acknowledging the Jellyfin webhook.

If the Jellyfin webhook payload does not provide enough state to make a reliable
completion decision, persist the event and query Jellyfin user data during processing.

---

# 86. Leaving Soon Artwork Contract

The badge is reversible state, not a destructive image replacement.

Requirements:

- Cache or preserve the source primary image before first modification.
- Associate modified image with the active candidate revision.
- Regenerate from source, never repeatedly badge an already-badged poster.
- Restore only if the current badge belongs to the candidate being cancelled.
- Reconciliation must repair artwork after crashes.
- If poster manipulation fails, do not block the candidate workflow; record the UI warning.
- Never delete media solely because artwork manipulation succeeded or failed.

Recommended badge presentation:

```text
lower or upper horizontal ribbon
high contrast
text: LEAVING SOON
avoid covering title/faces more than necessary
```

Do not hard-code brand colors into policy logic.

---

# 87. UI Wireframes

Use Bootstrap 5 and server-rendered pages initially. JavaScript may progressively
enhance interactions.

## 87.1 Overview

```text
+--------------------------------------------------------------+
| Curatarr logo                     Integrations: J S R ● ● ●  |
+--------------------------------------------------------------+
| TV Library             Movies Library                        |
| 6.4 / 8.0 TB           3.8 / 4.0 TB                         |
| NORMAL                  NEAR HIGH WATER                      |
+--------------------------------------------------------------+
| Review Queue  12 | Leaving Soon 5 | Reclaimed 420 GB         |
+--------------------------------------------------------------+
| Recent Decisions                                             |
| [time] [title] [action] [reason]                             |
+--------------------------------------------------------------+
| Recent Errors / Integration Health                           |
+--------------------------------------------------------------+
```

## 87.2 Acquisition Rules

```text
Library selector
Effective inheritance badge

Minimum retained footprint:
(o) First [3] episodes
( ) Entire Season 1
( ) Entire series

Trigger after [1] completed episode(s)
[x] Keep one season ahead
[ ] Manage Specials / Season 0

Grace period [30] days

[Save]
```

## 87.3 Retention Rules

```text
Inactivity:
TV [90 days]
Movies [90 days]

Meaningful TV watch [2 episodes]

Purge strategy [dropdown]

Library-size policy:
[x] Enabled
High water [8.0 TB]
Low water  [7.5 TB]

Free-space pressure:
[ ] Enabled

Review behavior [Require review]
Leaving Soon notice [14 days]
```

## 87.4 Review Queue

Table/cards:

```text
Poster | Title | Why | Last watched | Favorite | Size | Delete date | Actions
```

Actions:

```text
Delete
Keep
Snooze...
Never Purge
Details
```

Bulk actions may be added only after individual behavior is proven safe.

## 87.5 Title Override

```text
Search title
Title metadata + current lifecycle status

Never Purge [toggle]

Each configurable field:
[Inherit from Library] / [Override]
Effective value shown adjacent
```

## 87.6 History

Filters:

```text
date range
library
title
event/action type
success/failure
user
```

Rows link to a detail page showing input event, resolved policy, decision, and external actions.

---

# 88. Demo / Mock Mode

Curatarr must support development without private services.

`CURATARR_DEMO_MODE=true` uses deterministic synthetic adapters.

Demo dataset should include:

- 2 TV libraries
- 1 movie library
- at least 20 series
- at least 50 movies
- multiple synthetic users
- favorites
- recently watched items
- never-watched items
- active downloads
- a currently airing series
- a partially retained series
- quota pressure
- Leaving Soon candidates
- Never Purge examples

Demo mode must support simulated events such as:

```text
complete episode
start playback
favorite/unfavorite
advance clock
add media
change library size
integration outage
```

Automated tests should use the same domain interfaces as demo adapters.

Do not put real-world personal media history into committed demo fixtures.

---

# 89. Public/Internal API Boundary

Expose a small versioned API for dashboards and future integrations.

Initial endpoints:

```text
GET /api/v1/status
GET /api/v1/libraries
GET /api/v1/review/summary
GET /api/v1/history?limit=...
GET /health
```

`/api/v1/status` should return non-secret operational data:

```json
{
  "app": "curatarr",
  "version": "0.1.0",
  "dry_run": true,
  "review_count": 12,
  "leaving_soon_count": 5,
  "integrations": {
    "jellyfin": "healthy",
    "sonarr": "healthy",
    "radarr": "healthy"
  }
}
```

Mutating public API endpoints are not required in v0.1.

---

# 90. Security Model

Minimum requirements:

- CSRF protection for browser mutations.
- Secure session-cookie settings configurable for HTTPS deployments.
- No secrets in HTML, JSON status endpoints, logs, exception pages, or Git.
- API keys accepted through password-type fields and displayed thereafter only as masked/present.
- SSRF-conscious integration URL validation.
- Explicit outbound HTTP client allowlist limited to configured integrations.
- No arbitrary user-supplied fetch URL endpoint.
- Escape all titles/user-provided metadata in HTML.
- Validate webhook content type and payload size.
- Optional webhook shared token.
- Rate-limit authentication/setup endpoints if authentication is enabled.
- Destructive UI actions require POST/DELETE semantics, never GET.
- Apply Content Security Policy when practical.
- Never trust Jellyfin/Sonarr/Radarr metadata as safe HTML.
- Sanitize filenames and generated artwork paths.
- Do not allow path traversal from external metadata.

If authentication is added, prefer local Curatarr accounts or a clearly isolated auth
adapter rather than coupling directly to a specific household identity model.

---

# 91. Deployment Target

The project must support both native Python development and container deployment.

Required deliverables before first public-ready release:

```text
Dockerfile
compose.yaml
.env.example
persistent data volume documentation
healthcheck
upgrade/migration instructions
```

Container assumptions:

```text
app port: 8787
persistent application data: /config
no direct media-volume mount required for normal operation
```

Curatarr should not need filesystem access to Jellyfin media because deletion is through Sonarr/Radarr APIs.

If local poster cache is used, store it under Curatarr application data.

---

# 92. Migration and Versioning Policy

- Every schema change uses Alembic.
- Never edit an already-released migration.
- App startup checks schema revision.
- Production startup may run safe migrations automatically only if explicitly enabled;
  otherwise provide a documented migration command.
- Configuration schema gains explicit versioning once persistent policies are public.
- Unknown future policy fields must not silently change meaning.
- Changelog follows semantic versioning conventions.

Before a tagged release:

```text
tests pass
migration upgrade from previous tagged schema passes
migration downgrade is tested where supported
demo mode starts
fresh install starts
```

---

# 93. Coding Standards

Python:

- PEP 8
- Type hints on public/internal service boundaries
- `ruff` for lint/format or an equivalent single-tool approach
- `pytest`
- Avoid unnecessary docstrings; document public abstractions and non-obvious behavior
- No catch-all `except Exception` without logging/rethrow/defined recovery
- No business logic inside Flask route functions
- No raw HTTP calls outside integration clients
- No direct SQL in route handlers
- No global mutable service singletons where dependency injection/application context is clearer

Architecture:

```text
routes -> application services -> policies/domain -> integration interfaces
                                      |
                                   database
```

Policy functions should be testable with no live HTTP.

Prefer enums/value objects over magic strings in core logic.

---

# 94. Observability

Structured logs should include when available:

```text
event_id
action_id
media_identity_id
candidate_id
integration
operation
duration_ms
result
```

Never include API keys.

Metrics may initially be surfaced in the database/UI rather than Prometheus, but record:

- events processed
- duplicate events ignored
- acquisition actions
- purge candidates created
- rescues
- bytes proposed for reclaim
- bytes actually reclaimed
- external API failures
- reconciliation duration
- pending durable actions
- oldest pending action age

---

# 95. Repository Work Tracking

Maintain:

```text
IMPLEMENTATION_STATUS.md
```

Codex updates it as work progresses.

Required structure:

```markdown
# Curatarr Implementation Status

## Current Phase
Phase X — Name

## Completed
- ...

## In Progress
- ...

## Next
- ...

## Known Issues
- ...

## Decisions Made
- ...

## Blockers
- None
```

Update this file before milestone commits.

Also maintain:

```text
docs/decisions/
```

for architectural decisions that future contributors need to understand.

---

# 96. Autonomous Development Rules

When a product requirement is clear but an implementation detail is not:

```text
choose a conventional implementation
document it
test it
commit it
continue
```

Do not ask for approval simply because:

- two libraries could solve the same internal problem;
- a table/column name is not prescribed;
- CSS spacing is unspecified;
- an internal class structure has multiple valid designs;
- a conventional default can be safely chosen;
- additional tests are clearly needed;
- a dependency requires a compatible patch/minor version adjustment.

Do stop when:

- a destructive action outside the repository cannot be safely simulated;
- credentials are required to continue a live integration test;
- a direct product-behavior contradiction cannot be resolved from this specification;
- a required capability does not exist and every fallback changes user-visible behavior materially.

When stopped, finish all unrelated work first.

---

# 97. Phase Definition of Done

## Phase 0

Commands must succeed:

```bash
python -m pytest
ruff check .
ruff format --check .
flask --app curatarr db upgrade
```

Additionally:

- fresh install starts
- `/health` returns healthy application/database
- no secrets/private values are tracked
- demo shell renders
- logo animation respects reduced motion

## Phase 1

- integration clients have mocked tests
- live connection configuration validates cleanly when credentials are available
- discovery is paginated/batched
- identity mapping handles provider IDs
- API failures are sanitized

## Phase 2

- duplicate event test passes
- webhook returns quickly
- playback reset behavior passes
- favorite reconciliation passes
- restart does not lose persisted event/action state

## Phase 3

- all acquisition acceptance scenarios pass
- Sonarr action idempotency passes
- no Season N+2 prefetch occurs
- active queue prevents duplicate search
- out-of-order playback test passes

## Phase 4

- all purge strategies have deterministic unit tests
- high/low water calculation passes boundary cases
- Never Purge cannot be selected
- grace/queue exclusion tests pass
- dry-run makes zero destructive calls

## Phase 5

- original artwork restoration proven in tests
- repeated badge application does not degrade poster
- playback rescue restores poster
- restart reconciliation repairs pending artwork state

## Phase 6

- manual deletion revalidates immediately before call
- ambiguous external response reconciles before retry
- TV collapse preserves minimum footprint exactly
- movie deletion does not create import exclusion
- watch-state snapshot is persisted first

## Phase 7

- automatic deletion remains opt-in
- notice expiry race with playback is covered by tests
- integration outage blocks deletion
- end-to-end demo lifecycle succeeds

---

# 98. Additional Acceptance Edge Cases

Implement tests for:

1. Two users complete the same episode within seconds.
2. Two users watch different seasons of the same show.
3. Playback occurs while a delete action is waiting to execute.
4. Library falls below high-water mark after candidates were queued.
5. Candidate is favorited after entering review.
6. Candidate receives Never Purge after entering review.
7. Media is manually deleted outside Curatarr.
8. Sonarr/Radarr ID changes while provider ID remains stable.
9. Jellyfin item ID changes after library rescan.
10. Jellyfin webhook is delivered twice.
11. Jellyfin webhook is never delivered but reconciliation discovers the state.
12. Sonarr accepts a search command but Curatarr times out before seeing the response.
13. Radarr deletion returns an ambiguous timeout.
14. Artwork restore fails temporarily.
15. Curatarr restarts with pending actions.
16. High-water equals low-water: configuration must reject it.
17. Favorite from one user and unwatched by all others.
18. Series is already at first-N minimum footprint.
19. Missing Season 1 episodes have never aired.
20. Specials exist between regular seasons.
21. A future season exists in metadata but has no episodes yet.
22. Movie has no last-watched date.
23. Library has insufficient purgeable content to reach low-water target.
24. External services report inconsistent sizes.
25. Title is renamed but provider IDs remain stable.

---

# 99. Public Repository Hygiene

Before every commit, inspect staged files.

Automate where practical:

```text
secret scanner
large-file check
.env protection
private-IP/hostname heuristic check for docs/fixtures
```

Allow generic RFC/documentation addresses only in committed examples.

Recommended pre-commit policy should reject:

```text
.env
*.db
*.sqlite
logs/
config/runtime secrets
generated poster cache
coverage artifacts
```

Do not reject source code that legitimately contains standard localhost examples.

---

# 100. Final Product Principle

Curatarr is a deterministic curation engine, not a black-box cleanup daemon.

Every lifecycle decision must be reproducible from:

```text
observed media state
observed user activity
effective configuration
deterministic policy
```

A maintainer must be able to answer:

```text
What did Curatarr observe?
Which effective settings applied?
Why did it make this decision?
Which external actions did it issue?
What happened afterward?
```

If those questions cannot be answered from persisted state and the audit trail, the
feature is not complete.
