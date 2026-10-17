"""TV-friendly audio: add an AC3 track to films whose audio many TVs can't play.

Roku players, many smart TVs and their soundbars can't decode DTS, TrueHD or
FLAC, so Jellyfin converts the audio while it plays. Some clients handle that
badly: the Roku app can get stuck restarting the stream at one spot.

When Settings → Enhance → TV-friendly audio is on (off by default), Curatarr
adds an AC3 copy of the main audio track (5.1 at most), first and default,
beside the original, which is kept. Only that new track is encoded: picture,
other tracks, subtitles and chapters are copied untouched, and the file is
replaced only after the copy checks out (mediafile.add_compatible_audio).

Films are found from Radarr's media info (no file is opened until then), one
at a time in the background, never one that's being played, and not in
libraries in dry run.
"""

import threading

from flask import current_app
from sqlalchemy import or_

from . import db, throttle
from .integrations import IntegrationError
from .mediafile import MediaToolError, add_compatible_audio, can_write, probe
from .mediafile import compatible_audio_source as source_track
from .models import ChapterState, Library, MediaIdentity, MediaPart, utcnow

# Radarr's names for audio Curatarr adds a TV-friendly track beside.
HARD_CODECS = ("DTS", "TrueHD", "FLAC", "PCM")
PER_RUN = 2  # each rewrites a whole film
_lock = threading.Lock()
_thread = None


def _enrichment():
    from . import enrichment

    return enrichment


def enabled() -> bool:
    try:
        return bool(_enrichment().options()["compat_audio"])
    except Exception:  # noqa: BLE001  an unreadable config turns nothing on
        return False


def hard_films() -> set:
    """Radarr ids of films whose file has hard-to-play audio and no easy track."""
    from .services import client

    found = set()
    for movie in client("radarr").movies() or []:
        info = (movie.get("movieFile") or {}).get("mediaInfo") or {}
        codec = str(info.get("audioCodec") or "")
        if any(codec.upper().startswith(c.upper()) for c in HARD_CODECS):
            found.add(movie["id"])
    return found


def _due(radarr_ids):
    """Their film files not yet looked at, or replaced since."""
    return (
        db.session.query(MediaPart)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .join(Library, MediaIdentity.library_id == Library.id)
        .outerjoin(ChapterState, ChapterState.media_part_id == MediaPart.id)
        .filter(
            MediaPart.kind == "movie_file",
            MediaPart.has_file.is_(True),
            MediaPart.file_path.isnot(None),
            MediaIdentity.radarr_id.in_(radarr_ids),
            MediaIdentity.missing_since.is_(None),
            or_(
                ChapterState.id.is_(None),
                ChapterState.compat_audio_size.is_(None),
                ChapterState.compat_audio_size != MediaPart.size_bytes,
            ),
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
            target=_run, args=(app,), name="curatarr-compat-audio", daemon=True
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
            app.logger.exception("Adding TV-friendly audio failed")
        finally:
            db.session.remove()


def sweep(limit=PER_RUN) -> int:
    from .filecheck_jobs import _dry_run

    try:
        ids = hard_films()
    except IntegrationError:
        return 0
    if not ids:
        return 0
    e = _enrichment()
    done = 0
    for part in _due(ids).limit(limit * 3).all():
        if done >= limit:
            break
        path = e.local_path(part.file_path)
        if not path or not e.file_exists(path):
            continue  # unreachable for now; try again later
        if _dry_run(part.media) or _playing(part):
            continue
        state = e.state_for(part)
        db.session.commit()  # never hold the write lock while a film is copied
        result = _add(state, part, path)
        state.compat_audio_size = result.pop("size", part.size_bytes)
        if result.get("added"):
            part.size_bytes = state.compat_audio_size  # as Radarr will report it
        state.compat_audio = result | {"at": utcnow().isoformat()}
        db.session.commit()
        if result.get("added"):
            _announce(state, part, path, result)
            done += 1
    return done


def _playing(part) -> bool:
    from .enrichment import jellyfin_item_id, playback_sessions

    item = jellyfin_item_id(part, part.media)
    return any(
        (s.get("NowPlayingItem") or {}).get("Id") == item for s in playback_sessions()
    )


def _add(state, part, path) -> dict:
    try:
        info = probe(path)
    except MediaToolError as exc:
        return {"error": str(exc)}
    source = source_track(info)
    if source is None:
        return {"needed": False, "size": info.size}
    writable, reason = can_write(info)
    if not writable:
        return {"error": reason, "size": info.size}
    codec = next(s["codec_name"] for s in info.streams if s["index"] == source)
    in_sync = (state.chapter_file_size, state.chapter_file_mtime) == (
        info.size,
        info.mtime,
    )
    try:
        after = add_compatible_audio(path, info, source)
    except MediaToolError as exc:
        return {"error": str(exc), "size": info.size}
    if in_sync:  # Curatarr's chapters are still its own
        state.chapter_file_size, state.chapter_file_mtime = after.size, after.mtime
    return {"added": True, "from": codec, "size": after.size}


def _announce(state, part, path, result):
    from .services import audit, client

    e = _enrichment()
    e._refresh_jellyfin(part, part.media)
    try:
        client("radarr").rescan_movie(part.media.radarr_id)
    except IntegrationError:
        pass  # Radarr notices on its next rescan
    audit(
        "compat_audio_added",
        part.media.id,
        f"Added a TV-friendly AC3 track to {e.file_label(state)}, beside its "
        f"{result['from'].upper()} audio.",
        f"compat-audio:{state.id}:{state.compat_audio['at']}",
        payload={"file": path, "from": result["from"]},
    )
    db.session.commit()
