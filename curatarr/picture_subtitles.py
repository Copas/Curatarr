"""Text subtitles for files whose English subtitles are only pictures.

Blu-ray (PGS) and DVD (VobSub) subtitles are pictures. TV apps such as
Roku can't draw them, so Jellyfin burns them into the video: turning them
on restarts it, it's transcoded, and seeking is slow. With the subtitle
option "Also when the English ones are only pictures" (on by default), a
subtitle job gives such files a generated text track as well.

Files checked before that option existed were recorded as having English
subtitles, whatever kind. This pass looks at them again, a few each worker
cycle, and queues the picture-only ones. It also queues files recorded as
"pictures" (checked while the option was off) once it's switched on.
"""

import time

from . import db
from .models import ChapterState, MediaIdentity, MediaPart, utcnow

KEY = "picture_subtitles"
RUN_SECONDS = 20
BATCH = 200


def _wanted(library_id, configs) -> bool:
    from .enrichment import config_for

    if library_id not in configs:
        subtitles = config_for(library_id)["subtitles"]
        configs[library_id] = bool(
            subtitles["enabled"] and subtitles.get("replace_pictures", True)
        )
    return configs[library_id]


def _queue(state):
    state.subtitle_status = None
    state.subtitle_error = None
    state.job_state = "queued"
    state.requested_action = "subtitles"
    state.requested_at = utcnow()


def _only_pictures(path) -> bool:
    from .enrichment import file_exists, probe
    from .subtitles import embedded_english, sidecar_english

    if not path or not file_exists(path):
        return False
    streams = probe(path).streams
    if embedded_english(streams, text_only=True) or sidecar_english(
        path, text_only=True
    ):
        return False
    return embedded_english(streams) or bool(sidecar_english(path))


def _rows(status, after=None, limit=BATCH):
    query = (
        db.session.query(ChapterState, MediaPart.file_path, MediaIdentity.library_id)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            ChapterState.subtitle_status == status,
            ChapterState.subtitle_fingerprint.is_(None),
            ChapterState.job_state == "idle",
            MediaPart.has_file.is_(True),
        )
        .order_by(ChapterState.id)
    )
    if after:
        query = query.filter(ChapterState.id > after)
    return query.limit(limit).all()


def sweep() -> int:
    """Returns how many files were queued."""
    from .mediafile import MediaToolError
    from .services import set_setting, setting

    configs, queued = {}, 0
    for state, _path, library_id in _rows("pictures", limit=None):
        if _wanted(library_id, configs):
            _queue(state)
            queued += 1
    progress = dict(setting(KEY, {}) or {})
    if not progress.get("finished_at"):
        started = time.monotonic()
        rows = _rows("existing", after=progress.get("after"))
        for state, path, library_id in rows:
            if time.monotonic() - started > RUN_SECONDS:
                break
            progress["after"] = state.id
            if not _wanted(library_id, configs):
                continue
            try:
                if _only_pictures(path):
                    _queue(state)
                    queued += 1
            except (MediaToolError, OSError):
                continue  # unreadable now: its next subtitle check decides
        else:
            if len(rows) < BATCH:
                progress["finished_at"] = utcnow().isoformat()
        progress["queued"] = progress.get("queued", 0) + queued
        set_setting(KEY, progress)
    db.session.commit()
    return queued
