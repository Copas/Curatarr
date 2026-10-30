"""English subtitle files beside the video that Curatarr didn't write: the
background pass that checks their sync, what Enhance → Subtitles shows, and
its buttons.

Files checked before the sync check existed were recorded as having English
subtitles and left alone, however far out of step those were. The pass looks
at them again: files with an English .srt beside them, not Curatarr's, that
hasn't been checked in its present form get a subtitle job, which listens to
a few minutes of the film and re-times or sets aside the file (see
subtitle_sync.py). Only a few wait at a time, so the work spreads out behind
everything else.

Each file's outcome is kept in ChapterState.subtitle_sync["files"][name]:
in_sync, resynced, set_aside, unknown, unchecked, or what someone chose on
the page instead (undone: re-timing put back; kept: set-aside file brought
back). Those choices stand until the file changes.
"""

import os
import time
from pathlib import Path

from . import db
from .models import ChapterState, MediaIdentity, MediaPart, utcnow

KEY = "subtitle_sync_sweep"
RUN_SECONDS = 20
BATCH = 200
WAITING = 4  # at most this many of its subtitle jobs waiting at once
CHOSEN = {"undone", "kept"}  # someone decided: not checked again as it is
LISTED = 200  # per group on the page


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


def _candidates():
    return (
        db.session.query(ChapterState, MediaPart.file_path, MediaIdentity.library_id)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            ChapterState.subtitle_status.in_(("existing", "resynced")),
            ChapterState.subtitle_fingerprint.is_(None),
            MediaPart.has_file.is_(True),
        )
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
    query = _candidates().filter(ChapterState.job_state == "idle")
    if progress.get("after"):
        query = query.filter(ChapterState.id > progress["after"])
    rows = query.order_by(ChapterState.id).limit(BATCH).all()
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
        _queue(state)
        queued += 1
    else:
        if len(rows) < BATCH:
            progress["finished_at"] = utcnow().isoformat()
    progress["queued"] = progress.get("queued", 0) + queued
    set_setting(KEY, progress)
    db.session.commit()
    return queued


def _queue(state):
    state.job_state = "queued"
    state.requested_action = "subtitles"
    state.requested_at = utcnow()
    state.attempts = 0
    state.next_attempt_at = None


# --- What Enhance → Subtitles shows -----------------------------------------


def _checked_states():
    return (
        db.session.query(ChapterState)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .filter(ChapterState.subtitle_sync.isnot(None), MediaPart.has_file.is_(True))
        .all()
    )


def _when(entry) -> str:
    return entry.get("checked_at") or ""


def overview() -> dict:
    """Counts of each outcome, the files under each, and the pass's progress."""
    from .services import setting

    groups = {
        name: []
        for name in ("resynced", "set_aside", "unknown", "in_sync", "chosen", "own")
    }
    for state in _checked_states():
        record = state.subtitle_sync or {}
        if record.get("own"):
            groups["own"].append((state, None, {}))
        for name, entry in (record.get("files") or {}).items():
            outcome = entry.get("outcome")
            if outcome in CHOSEN:
                group = "chosen"
            elif outcome == "unchecked":
                group = "unknown"
            else:
                group = outcome
            if group in groups:
                groups[group].append((state, name, entry))
    for items in groups.values():
        items.sort(key=lambda item: _when(item[2]), reverse=True)
    progress = dict(setting(KEY, {}) or {})
    total = _candidates().count()
    looked = (
        total
        if progress.get("finished_at")
        else _candidates().filter(ChapterState.id <= progress["after"]).count()
        if progress.get("after")
        else 0
    )
    return {
        "counts": {name: len(items) for name, items in groups.items()},
        "groups": {name: items[:LISTED] for name, items in groups.items()},
        "pass": {
            "total": total,
            "looked": looked,
            "finished_at": progress.get("finished_at"),
            "waiting": _waiting(),
        },
    }


def entries(state) -> list[tuple[str, dict]]:
    """One file's checked subtitles, newest first."""
    files = (state.subtitle_sync or {}).get("files") or {}
    return sorted(files.items(), key=lambda item: _when(item[1]), reverse=True)


# --- Its buttons --------------------------------------------------------------


def _record(state, name, entry):
    record = dict(state.subtitle_sync or {})
    files = dict(record.get("files") or {})
    files[name] = entry
    state.subtitle_sync = record | {"files": files}


def undo(state, name) -> str:
    """Puts back the subtitles as they were before re-timing."""
    from .enrichment import _sidecar_key

    entry = dict(((state.subtitle_sync or {}).get("files") or {}).get(name) or {})
    if entry.get("outcome") != "resynced" or not entry.get("backup"):
        raise ValueError("Those subtitles weren't re-timed by Curatarr")
    video = Path(state.part.file_path)
    backup, target = video.with_name(entry["backup"]), video.with_name(name)
    if not backup.exists():
        raise ValueError(f"The original ({backup.name}) is no longer there")
    os.replace(backup, target)
    entry |= {"outcome": "undone", "key": _sidecar_key(target)}
    entry.pop("backup", None)
    _record(state, name, entry)
    if state.subtitle_status == "resynced":
        state.subtitle_status = "existing"
    db.session.commit()
    return f"Put back the original timing of {name}."


def restore(state, name) -> str:
    """Brings back subtitles that were set aside as out of sync."""
    from .enrichment import _sidecar_key

    entry = dict(((state.subtitle_sync or {}).get("files") or {}).get(name) or {})
    if entry.get("outcome") != "set_aside" or not entry.get("aside"):
        raise ValueError("Those subtitles weren't set aside by Curatarr")
    video = Path(state.part.file_path)
    aside, target = video.with_name(entry["aside"]), video.with_name(name)
    if target.exists():
        raise ValueError(f"There's a new {name} there already")
    if not aside.exists():
        raise ValueError(f"The file set aside ({aside.name}) is no longer there")
    os.replace(aside, target)
    entry |= {"outcome": "kept", "key": _sidecar_key(target)}
    entry.pop("aside", None)
    _record(state, name, entry)
    db.session.commit()
    return f"Brought back {name}. Curatarr's own subtitles stay beside it."


def recheck(state) -> str:
    """Checks a file's subtitles again, whatever was decided before."""
    record = dict(state.subtitle_sync or {})
    files = {
        name: {k: v for k, v in entry.items() if k != "key"}
        for name, entry in (record.get("files") or {}).items()
    }
    state.subtitle_sync = record | {"files": files}
    _queue(state)
    db.session.commit()
    return "Queued to be checked again."


def check_all() -> int:
    """Every file's subtitles checked again from the start, in the background,
    except where someone chose otherwise. Returns how many were checked before."""
    from .services import set_setting

    count = 0
    for state in _checked_states():
        record = dict(state.subtitle_sync or {})
        files = {}
        for name, entry in (record.get("files") or {}).items():
            if entry.get("outcome") not in CHOSEN:
                entry = {k: v for k, v in entry.items() if k != "key"}
                count += 1
            files[name] = entry
        state.subtitle_sync = record | {"files": files}
    set_setting(KEY, {})
    db.session.commit()
    return count


def make_own_for_all() -> int:
    """Curatarr's own subtitles for every file, even those with English ones.
    Returns how many were queued."""
    from .enrichment import _parts_query, config_for, state_for

    configs, count = {}, 0
    for part in _parts_query(None):
        library_id = part.media.library_id
        if library_id not in configs:
            configs[library_id] = config_for(library_id)["subtitles"]["enabled"]
        if not configs[library_id]:
            continue
        state = state_for(part)
        if state.subtitle_fingerprint or state.job_state != "idle":
            continue
        if state.subtitle_status in ("skipped_language", "no_speech"):
            continue
        state.subtitle_sync = dict(state.subtitle_sync or {}) | {"own": True}
        _queue(state)
        count += 1
    db.session.commit()
    return count
