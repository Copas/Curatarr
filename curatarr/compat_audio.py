"""TV-friendly audio: add an AC3 track to files whose audio many TVs can't play.

Roku players, many smart TVs and their soundbars can't decode DTS, TrueHD or
FLAC, so Jellyfin converts the audio while it plays. Some clients handle that
badly: the Roku app can get stuck restarting the stream at one spot.

When Settings → Enhance → TV-friendly audio is on (off by default), Curatarr
adds an AC3 copy of the main audio track (5.1 at most), first and default,
beside the original, which is kept. Only that new track is encoded: picture,
other tracks, subtitles and chapters are copied untouched, and the file is
replaced only after the copy checks out (mediafile.add_compatible_audio).

Films and episodes are found from Radarr's and Sonarr's media info (no file
is opened until then; Sonarr's is read every six hours), one at a time in the
background, never one that's being played, and not in libraries in dry run.
"""

import threading

from flask import current_app
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import OperationalError

from . import db, throttle, thumbnails
from .integrations import IntegrationError
from .mediafile import MediaToolError, add_compatible_audio, can_write, probe
from .mediafile import compatible_audio_source as source_track
from .models import ChapterState, Library, MediaIdentity, MediaPart, utcnow

# Radarr's and Sonarr's names for audio Curatarr adds a TV-friendly track beside.
HARD_CODECS = ("DTS", "TrueHD", "FLAC", "PCM")
PER_RUN = 2  # each rewrites a whole file
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


SONARR_SCAN_SECONDS = 6 * 3600  # episode files are read per show: not every cycle
_sonarr_cache = {}


def _hard(info) -> bool:
    codec = str((info or {}).get("audioCodec") or "")
    return any(codec.upper().startswith(c.upper()) for c in HARD_CODECS)


def hard_files() -> tuple[set, set]:
    """(Radarr film ids, Sonarr episode-file ids) whose audio is hard to play,
    from Radarr's and Sonarr's media info."""
    import time

    from .services import client, configured

    films = {
        movie["id"]
        for movie in client("radarr").movies() or []
        if _hard((movie.get("movieFile") or {}).get("mediaInfo"))
    }
    episodes = set()
    if configured("sonarr"):
        hit = _sonarr_cache.get("files")
        if hit and time.monotonic() - hit[0] < SONARR_SCAN_SECONDS:
            episodes = hit[1]
        else:
            sonarr = client("sonarr")
            for show in sonarr.series() or []:
                for entry in sonarr.episode_files(show["id"]) or []:
                    if _hard(entry.get("mediaInfo")):
                        episodes.add(entry["id"])
            _sonarr_cache["files"] = (time.monotonic(), episodes)
    return films, episodes


def _due(radarr_ids, episode_file_ids):
    """Their files not yet looked at, or replaced since."""
    return (
        db.session.query(MediaPart)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .join(Library, MediaIdentity.library_id == Library.id)
        .outerjoin(ChapterState, ChapterState.media_part_id == MediaPart.id)
        .filter(
            MediaPart.has_file.is_(True),
            MediaPart.file_path.isnot(None),
            MediaIdentity.missing_since.is_(None),
            or_(
                and_(
                    MediaPart.kind == "movie_file",
                    MediaIdentity.radarr_id.in_(radarr_ids),
                ),
                and_(
                    MediaPart.kind == "episode",
                    MediaPart.arr_file_id.in_(episode_file_ids),
                ),
            ),
            or_(
                ChapterState.id.is_(None),
                ChapterState.compat_audio_size.is_(None),
                ChapterState.compat_audio_size != MediaPart.size_bytes,
            ),
        )
        # Films first: they're where DTS and TrueHD mostly are.
        .order_by(MediaPart.kind.desc())
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
        films, episodes = hard_files()
    except IntegrationError:
        return 0
    due = _due(films, episodes) if films or episodes else None
    _remember_remaining(due)
    if due is None:
        return 0
    e = _enrichment()
    done = 0
    for part in due.limit(limit * 3).all():
        if done >= limit:
            break
        path = e.local_path(part.file_path)
        if not path or not e.file_exists(path):
            continue  # unreachable for now; try again later
        if _dry_run(part.media) or _playing(part):
            continue
        if not thumbnails.room():
            break  # Jellyfin is still making thumbnails for earlier ones
        state = e.state_for(part)
        db.session.commit()  # never hold the write lock while a film is copied
        state_id, part_id = state.id, part.id
        had_thumbnails = thumbnails.has_thumbnails(part, part.media)
        result = _add(state, part, path)
        if result.get("added"):
            thumbnails.note_rebuilt(part, part.media, had_thumbnails)
        synced = (state.chapter_file_size, state.chapter_file_mtime)
        try:
            _record(state, part, result)
        except OperationalError:
            # Busy past the timeout. The file is already rewritten, and once
            # Radarr sees the new track it's never looked at again: try once more.
            db.session.rollback()
            state = db.session.get(ChapterState, state_id)
            part = db.session.get(MediaPart, part_id)
            if result.get("added"):
                state.chapter_file_size, state.chapter_file_mtime = synced
            _record(state, part, result)
        if result.get("added"):
            _announce(state, part, path, result)
            done += 1
    return done


def _record(state, part, result):
    state.compat_audio_size = result.get("size", part.size_bytes)
    if result.get("added"):
        part.size_bytes = state.compat_audio_size  # as Radarr will report it
    record = {k: v for k, v in result.items() if k != "size"}
    state.compat_audio = record | {"at": utcnow().isoformat()}
    db.session.commit()


REMAINING_KEY = "compat_audio_remaining"


def _remember_remaining(due):
    """How many files are still to do, for the progress bars (each sweep)."""
    from .services import set_setting

    remaining = {"films": 0, "episodes": 0, "at": utcnow().isoformat()}
    if due is not None:
        for kind, count in (
            due.with_entities(MediaPart.kind, func.count())
            .group_by(MediaPart.kind)
            .order_by(None)
        ):
            remaining["films" if kind == "movie_file" else "episodes"] = count
    set_setting(REMAINING_KEY, remaining)
    db.session.commit()


def progress() -> dict | None:
    """Done and to-do counts for films and episodes, or None when it's off and
    nothing was ever done."""
    from .services import setting

    done = {"films": 0, "episodes": 0}
    rows = (
        db.session.query(MediaPart.kind, ChapterState.compat_audio)
        .join(ChapterState, ChapterState.media_part_id == MediaPart.id)
        .filter(ChapterState.compat_audio.isnot(None))
    )
    for kind, result in rows:
        if (result or {}).get("added"):
            done["films" if kind == "movie_file" else "episodes"] += 1
    on = enabled()
    if not on and not any(done.values()):
        return None
    remaining = setting(REMAINING_KEY, {}) or {}
    result = {"on": on}
    for kind in ("films", "episodes"):
        left = remaining.get(kind, 0) if on else 0
        result[kind] = {"done": done[kind], "total": done[kind] + left, "left": left}
    result["done"] = done["films"] + done["episodes"]
    result["total"] = result["films"]["total"] + result["episodes"]["total"]
    return result


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
        if part.kind == "movie_file":
            client("radarr").rescan_movie(part.media.radarr_id)
        else:
            client("sonarr").request(
                "POST",
                "/api/v3/command",
                json={"name": "RescanSeries", "seriesId": part.media.sonarr_id},
            )
    except IntegrationError:
        pass  # Radarr/Sonarr notice on their next rescan
    audit(
        "compat_audio_added",
        part.media.id,
        f"Added a TV-friendly AC3 track to {e.file_label(state)}, beside its "
        f"{result['from'].upper()} audio.",
        f"compat-audio:{state.id}:{state.compat_audio['at']}",
        payload={"file": path, "from": result["from"]},
    )
    db.session.commit()
