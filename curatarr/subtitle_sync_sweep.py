"""Checks the sync of English subtitles the library already had.

Files checked before the sync check existed were recorded as having English
subtitles and left alone, however far out of step those were. This pass
looks at them again: files with an English .srt beside them, not Curatarr's,
that hasn't been checked in its present form get a subtitle job, which
listens to a few minutes of the film and re-times or sets aside the file
(see subtitle_sync.py). Only a few wait at a time, so the work spreads out
behind everything else.
"""

import time
from pathlib import Path

from . import db
from .models import ChapterState, MediaIdentity, MediaPart, utcnow

KEY = "subtitle_sync_sweep"
RUN_SECONDS = 20
BATCH = 200
WAITING = 4  # at most this many of its subtitle jobs waiting at once


def _wanted(library_id, configs) -> bool:
    from .enrichment import config_for

    if library_id not in configs:
        subtitles = config_for(library_id)["subtitles"]
        configs[library_id] = bool(
            subtitles["enabled"] and subtitles.get("check_sync", True)
        )
    return configs[library_id]


def unchecked(state, path) -> bool:
    """Whether an English .srt beside the video hasn't been checked as it is."""
    from .enrichment import _sidecar_key
    from .subtitles import sidecar_english

    files = (state.subtitle_sync or {}).get("files") or {}
    video = Path(path)
    for name in sidecar_english(path, text_only=True):
        if not name.lower().endswith(".srt"):
            continue
        try:
            key = _sidecar_key(video.with_name(name))
        except OSError:
            continue
        if (files.get(name) or {}).get("key") != key:
            return True
    return False


def _waiting() -> int:
    return (
        db.session.query(ChapterState)
        .filter(
            ChapterState.requested_action == "subtitles",
            ChapterState.job_state.in_(("queued", "running")),
            ChapterState.subtitle_status.in_(("existing", "resynced")),
        )
        .count()
    )


def sweep() -> int:
    """Returns how many files were queued."""
    from .services import set_setting, setting

    progress = dict(setting(KEY, {}) or {})
    if progress.get("finished_at"):
        return 0
    room = WAITING - _waiting()
    if room <= 0:
        return 0
    query = (
        db.session.query(ChapterState, MediaPart.file_path, MediaIdentity.library_id)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            ChapterState.subtitle_status.in_(("existing", "resynced")),
            ChapterState.subtitle_fingerprint.is_(None),
            ChapterState.job_state == "idle",
            MediaPart.has_file.is_(True),
        )
        .order_by(ChapterState.id)
    )
    if progress.get("after"):
        query = query.filter(ChapterState.id > progress["after"])
    rows = query.limit(BATCH).all()
    configs, queued, started = {}, 0, time.monotonic()
    for state, path, library_id in rows:
        if queued >= room or time.monotonic() - started > RUN_SECONDS:
            break
        progress["after"] = state.id
        if not _wanted(library_id, configs):
            continue
        try:
            if not unchecked(state, path):
                continue
        except OSError:
            continue  # unreadable now: its next subtitle check decides
        state.job_state = "queued"
        state.requested_action = "subtitles"
        state.requested_at = utcnow()
        queued += 1
    else:
        if len(rows) < BATCH:
            progress["finished_at"] = utcnow().isoformat()
    progress["queued"] = progress.get("queued", 0) + queued
    set_setting(KEY, progress)
    db.session.commit()
    return queued
