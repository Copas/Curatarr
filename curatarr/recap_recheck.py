"""Recaps a check couldn't place yet, looked at again.

An episode that opens with "Previously on…" has a recap, but its end is
found by matching the recap's clips with the earlier episodes' transcripts.
When those weren't transcribed yet (episodes are checked in any order), the
check finds nothing to match and marks no recap, which hid the skip button
on recaps that are plainly there. Such an episode is now recorded as
``unclear:<n>``, n being how many earlier episodes had a transcript then,
and checked again once more of them do.

Episodes checked before this were recorded as having none; a one-time pass
reads their transcripts and checks again those that open with a recap.
"""

import time

from . import db
from .models import ChapterState, MediaPart, utcnow

UNCLEAR = "unclear:"
KEY = "recap_unclear_scan"
RUN_SECONDS = 20
BATCH = 300
RECHECKS_PER_RUN = 50


def unclear_source(state, part) -> str:
    """What to record for an episode whose recap opener was heard but whose
    end couldn't be found: ``unclear:<earlier episodes transcribed>``."""
    return f"{UNCLEAR}{earlier_transcribed(state, part)}"


def earlier_transcribed(state, part) -> int:
    season, episode = part.season_number or 0, part.episode_number or 0
    return (
        db.session.query(ChapterState)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .filter(
            ChapterState.media_identity_id == state.media_identity_id,
            ChapterState.id != state.id,
            ChapterState.transcript_key.isnot(None),
            (MediaPart.season_number < season)
            | (
                (MediaPart.season_number == season)
                & (MediaPart.episode_number < episode)
            ),
        )
        .count()
    )


def _queue(state):
    state.job_state = "queued"
    state.requested_action = "recap"
    state.requested_at = utcnow()
    state.recap_checked_at = None  # only the recap is looked at again


def sweep() -> int:
    """Worker cycle. Returns how many episodes were queued for a recheck."""
    return _recheck_unclear() + _scan_history()


def _recheck_unclear() -> int:
    """Unclear recaps whose series has more earlier episodes transcribed now."""
    queued = 0
    rows = (
        db.session.query(ChapterState, MediaPart)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .filter(
            ChapterState.recap_source.like(f"{UNCLEAR}%"),
            ChapterState.job_state == "idle",
            MediaPart.has_file.is_(True),
        )
        .limit(500)
        .all()
    )
    for state, part in rows:
        try:
            before = int(state.recap_source.removeprefix(UNCLEAR))
        except ValueError:
            before = 0
        if earlier_transcribed(state, part) > before:
            _queue(state)
            queued += 1
            if queued >= RECHECKS_PER_RUN:
                break
    db.session.commit()
    return queued


def _scan_history() -> int:
    """Once: episodes recorded as having no recap that open with one."""
    from . import recaps
    from .enrichment import _transcript
    from .services import set_setting, setting

    progress = dict(setting(KEY, {}) or {})
    if progress.get("finished_at"):
        return 0
    query = (
        db.session.query(ChapterState)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .filter(
            MediaPart.kind == "episode",
            MediaPart.has_file.is_(True),
            ChapterState.transcript_key.isnot(None),
            ChapterState.recap_start.is_(None),
            ChapterState.recap_source.in_(("none", "rechecked")),
            ChapterState.job_state == "idle",
        )
        .order_by(ChapterState.id)
    )
    if progress.get("after"):
        query = query.filter(ChapterState.id > progress["after"])
    states = query.limit(BATCH).all()
    queued, started = 0, time.monotonic()
    for state in states:
        if time.monotonic() - started > RUN_SECONDS:
            break
        progress["after"] = state.id
        segments = _transcript(state.transcript_key) or []
        opening = [s for s in segments if s.start < recaps.OPENER_WITHIN + 1]
        if recaps.opener(opening) is not None:
            _queue(state)
            queued += 1
    else:
        if len(states) < BATCH:
            progress["finished_at"] = utcnow().isoformat()
    progress["queued"] = progress.get("queued", 0) + queued
    set_setting(KEY, progress)
    db.session.commit()
    return queued
