"""Repair MKV files whose seek index Roku players can't reach.

When an in-place edit (chapters, track flags) doesn't fit at the start of a
file, mkvpropedit moves the seek head to the end and leaves one at the start
that only points there. Roku players don't follow it, so seeking starts the
episode over (mediafile.seek_index_hidden). Curatarr's own edits now rebuild
such files straight away; this finds the ones already like that.

When Settings → Enhance → Seeking on Roku is on (the default), each MKV file's
first few hundred kilobytes are read once (again only if the file changes),
and affected files are rebuilt with mkvmerge, one or two per worker cycle, in
the background, never one that's playing. Nothing is re-encoded.
"""

import threading

from flask import current_app
from sqlalchemy import case, func, or_
from sqlalchemy.exc import OperationalError

from . import db, throttle, thumbnails
from .mediafile import MediaToolError, rebuild_layout, seek_index_hidden
from .models import ChapterState, MediaIdentity, MediaPart, UserMediaState, utcnow

CHECKS_PER_RUN = 300  # a quick read of each file's start
REBUILDS_PER_RUN = 2  # each copies a whole file
REMAINING_KEY = "seek_repair_remaining"
_lock = threading.Lock()
_thread = None


def _enrichment():
    from . import enrichment

    return enrichment


def enabled() -> bool:
    try:
        return bool(_enrichment().options()["seek_repair"])
    except Exception:  # noqa: BLE001  an unreadable config turns nothing on
        return False


def _due():
    """MKV files not looked at yet, or changed since: what's being watched
    first, then chaptered ones (Curatarr's own edits are where this mostly
    came from)."""
    watched = (
        db.session.query(
            UserMediaState.media_identity_id.label("media_id"),
            func.max(UserMediaState.last_played_at).label("last"),
        )
        .group_by(UserMediaState.media_identity_id)
        .subquery()
    )
    return (
        db.session.query(MediaPart)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .outerjoin(ChapterState, ChapterState.media_part_id == MediaPart.id)
        .outerjoin(watched, watched.c.media_id == MediaPart.media_identity_id)
        .filter(
            MediaPart.has_file.is_(True),
            MediaPart.file_path.isnot(None),
            func.lower(MediaPart.file_path).like("%.mkv"),
            MediaIdentity.missing_since.is_(None),
            or_(
                ChapterState.id.is_(None),
                ChapterState.seek_layout_size.is_(None),
                ChapterState.seek_layout_size != MediaPart.size_bytes,
            ),
        )
        .order_by(
            case((watched.c.last.is_(None), 1), else_=0),
            watched.c.last.desc(),
            case((ChapterState.chapter_status.like("generated%"), 0), else_=1),
            MediaPart.id,
        )
    )


def start(app=None) -> bool:
    """Start in the background unless already running (worker cycle)."""
    global _thread
    if not enabled():
        return False
    with _lock:
        if _thread and _thread.is_alive():
            return False
        app = app or current_app._get_current_object()
        _thread = threading.Thread(
            target=_run, args=(app,), name="curatarr-seek-repair", daemon=True
        )
        _thread.start()
    return True


def _run(app):
    with app.app_context():
        try:
            sweep()
        except throttle.Interrupted:
            db.session.rollback()  # all work paused; carries on after resuming
        except Exception:
            db.session.rollback()
            app.logger.exception("Repairing seek indexes failed")
        finally:
            db.session.remove()


def sweep(checks=CHECKS_PER_RUN, rebuilds=REBUILDS_PER_RUN) -> int:
    """Check up to ``checks`` files and rebuild up to ``rebuilds``; returns how
    many were rebuilt."""
    e = _enrichment()
    done = 0
    for part in _due().limit(checks).all():
        path = e.local_path(part.file_path)
        if not path or not e.file_exists(path):
            continue  # unreachable for now; try again later
        state = e.state_for(part)
        if not seek_index_hidden(path):
            state.seek_layout_size = part.size_bytes
            state.seek_layout = {"ok": True, "at": utcnow().isoformat()}
            db.session.commit()
            continue
        if (
            done >= rebuilds
            or _playing(part)
            or not thumbnails.room(leave=_audio_waiting())
        ):
            db.session.commit()
            continue  # next cycle (or once Jellyfin has caught up on thumbnails)
        db.session.commit()  # never hold the write lock while a file is copied
        state_id, part_id = state.id, part.id
        had_thumbnails = thumbnails.has_thumbnails(part, part.media)
        result = _rebuild(part, path)
        if result.get("rebuilt"):
            thumbnails.note_rebuilt(part, part.media, had_thumbnails)
        try:
            _record(state, part, result)
        except OperationalError:
            # Busy past the timeout; the file is already rebuilt: once more.
            db.session.rollback()
            _record(
                db.session.get(ChapterState, state_id),
                db.session.get(MediaPart, part_id),
                result,
            )
        if result.get("rebuilt"):
            _announce(state, part, path)
            done += 1
    _remember_remaining()
    return done


def _audio_waiting() -> int:
    """1 when TV-friendly audio has files to do: it then gets the last spot
    under the thumbnail limit, or this repair (far more files) takes them all."""
    from . import compat_audio
    from .services import setting

    if not compat_audio.enabled():
        return 0
    left = setting(compat_audio.REMAINING_KEY, {}) or {}
    return 1 if left.get("films") or left.get("episodes") else 0


def _rebuild(part, path) -> dict:
    from .mediafile import file_identity_of

    before = file_identity_of(path)
    try:
        after = rebuild_layout(path)
    except MediaToolError as exc:
        return {"error": str(exc), "size": part.size_bytes}
    return {"rebuilt": True, "before": before, "size": after.size, "mtime": after.mtime}


def _record(state, part, result):
    if result.get("rebuilt"):
        before_size, before_mtime = result["before"]
        # Keep Curatarr's own records of this file pointing at the new copy.
        if (state.chapter_file_size, state.chapter_file_mtime) == (
            before_size,
            before_mtime,
        ):
            state.chapter_file_size = result["size"]
            state.chapter_file_mtime = result["mtime"]
        if state.compat_audio_size == before_size:
            state.compat_audio_size = result["size"]
        part.size_bytes = result["size"]  # as Radarr/Sonarr will report it
    state.seek_layout_size = result["size"]
    state.seek_layout = {
        k: v for k, v in result.items() if k in {"rebuilt", "error"}
    } | {"at": utcnow().isoformat()}
    db.session.commit()


def _playing(part) -> bool:
    from .enrichment import jellyfin_item_id, playback_sessions

    item = jellyfin_item_id(part, part.media)
    name = (part.file_path or "").replace("\\", "/").rsplit("/", 1)[-1]
    for session in playback_sessions():
        playing = session.get("NowPlayingItem") or {}
        if not playing:
            continue  # an app that's open but not playing anything
        if item and playing.get("Id") == item:
            return True
        # Not linked to Jellyfin yet: the file name of what's playing will do.
        if name and str(playing.get("Path") or "").replace("\\", "/").endswith(
            "/" + name
        ):
            return True
    return False


def _announce(state, part, path):
    from .integrations import IntegrationError
    from .services import audit, client

    e = _enrichment()
    e._refresh_jellyfin(part, part.media)
    try:
        if part.kind == "movie_file":
            client("radarr").rescan_movie(part.media.radarr_id)
        elif part.media.sonarr_id:
            client("sonarr").request(
                "POST",
                "/api/v3/command",
                json={"name": "RescanSeries", "seriesId": part.media.sonarr_id},
            )
    except IntegrationError:
        pass  # Radarr/Sonarr notice on their next rescan
    audit(
        "seek_index_repaired",
        part.media.id,
        f"Rebuilt {e.file_label(state)} so Roku players can seek in it "
        "(nothing re-encoded).",
        f"seek-repair:{state.id}:{state.seek_layout['at']}",
        payload={"file": path},
    )
    db.session.commit()


def _remember_remaining():
    from .services import set_setting

    set_setting(
        REMAINING_KEY,
        {"files": _due().order_by(None).count(), "at": utcnow().isoformat()},
    )
    db.session.commit()


def progress() -> dict | None:
    """Files checked, rebuilt, and still to check; None when it's off and
    nothing was ever rebuilt."""
    from .services import setting

    rows = db.session.query(ChapterState.seek_layout).filter(
        ChapterState.seek_layout.isnot(None)
    )
    checked = rebuilt = 0
    for (result,) in rows:
        checked += 1
        rebuilt += bool((result or {}).get("rebuilt"))
    on = enabled()
    if not on and not rebuilt:
        return None
    left = (setting(REMAINING_KEY, {}) or {}).get("files", 0) if on else 0
    return {
        "on": on,
        "checked": checked,
        "rebuilt": rebuilt,
        "left": left,
        "total": checked + left,
    }
