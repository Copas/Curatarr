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
from .mediafile import (
    MediaToolError,
    add_compatible_audio,
    browser_audio_source,
    can_write,
    probe,
)
from .mediafile import compatible_audio_source as source_track
from .models import ChapterState, Library, MediaIdentity, MediaPart, utcnow

# Radarr's and Sonarr's names for audio Curatarr adds a TV-friendly track beside.
HARD_CODECS = ("DTS", "TrueHD", "FLAC", "PCM")
# And for audio web browsers can't play, given a stereo AAC track beside it.
BROWSER_HARD_CODECS = ("AC3", "EAC3", "DTS", "TrueHD", "PCM")
PER_RUN = 2  # each rewrites a whole file
_lock = threading.Lock()
_thread = None


def _enrichment():
    from . import enrichment

    return enrichment


def tv_enabled() -> bool:
    try:
        return bool(_enrichment().options()["compat_audio"])
    except Exception:  # noqa: BLE001  an unreadable config turns nothing on
        return False


def browser_enabled() -> bool:
    """AAC tracks for browsers: only with the Jellyfin plugin, which points
    browsers at them (Jellyfin itself would keep playing the default track)."""
    from . import jellyfin_plugin

    try:
        on = bool(_enrichment().options()["browser_audio"])
    except Exception:  # noqa: BLE001
        return False
    return on and jellyfin_plugin.active()


def enabled() -> bool:
    return tv_enabled() or browser_enabled()


SONARR_SCAN_SECONDS = 6 * 3600  # episode files are read per show: not every cycle
_sonarr_cache = {}


def _needs(info) -> str | None:
    """From Radarr's or Sonarr's media info: "tv" when TVs can't play the
    audio, "browser" when only browsers can't, else None."""
    codec = str((info or {}).get("audioCodec") or "").upper()  # "DTS-HD MA", "EAC3"
    if tv_enabled() and any(codec.startswith(c.upper()) for c in HARD_CODECS):
        return "tv"
    if browser_enabled() and any(
        codec.startswith(c.upper()) for c in BROWSER_HARD_CODECS
    ):
        return "browser"
    return None


def _hard(info) -> bool:
    return _needs(info) is not None


def browser_parts() -> set[str]:
    """The files an AAC track is worth adding to: the next episodes, and what
    they're partway through, of the people who mostly watch in a web browser
    (viewers.py), topped up as they go. Adding one to every file would take
    weeks, each rebuilt behind Jellyfin's thumbnails, for tracks only
    browsers use."""
    from . import up_next, viewers

    who = viewers.browser_viewers()
    return set(up_next.part_ids(users=who)) if who else set()


def hard_files() -> tuple[set, set]:
    """(Radarr film ids, Sonarr episode-file ids) whose audio is hard to play,
    from Radarr's and Sonarr's media info. Files only browsers can't play
    count only when browser viewers are about to watch them (browser_parts)."""
    import time

    from .services import client, configured

    films, film_browser = set(), set()
    for movie in client("radarr").movies() or []:
        need = _needs((movie.get("movieFile") or {}).get("mediaInfo"))
        if need:
            (films if need == "tv" else film_browser).add(movie["id"])
    episodes, episode_browser = set(), set()
    if configured("sonarr"):
        hit = _sonarr_cache.get("files")
        if hit and time.monotonic() - hit[0] < SONARR_SCAN_SECONDS:
            episodes, episode_browser = hit[1]
        else:
            sonarr = client("sonarr")
            for show in sonarr.series() or []:
                for entry in sonarr.episode_files(show["id"]) or []:
                    need = _needs(entry.get("mediaInfo"))
                    if need:
                        (episodes if need == "tv" else episode_browser).add(entry["id"])
            _sonarr_cache["files"] = (time.monotonic(), (episodes, episode_browser))
    if film_browser or episode_browser:
        wanted = browser_parts()
        films |= {
            radarr_id
            for (radarr_id,) in db.session.query(MediaIdentity.radarr_id)
            .join(MediaPart, MediaPart.media_identity_id == MediaIdentity.id)
            .filter(
                MediaIdentity.radarr_id.in_(film_browser),
                MediaPart.id.in_(wanted or {""}),
            )
        }
        episodes |= {
            file_id
            for (file_id,) in db.session.query(MediaPart.arr_file_id).filter(
                MediaPart.arr_file_id.in_(episode_browser),
                MediaPart.id.in_(wanted or {""}),
            )
        }
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
        # What people watch next first; then films, where DTS and TrueHD
        # mostly are.
        .order_by(
            _watching_rank(),
            MediaPart.kind.desc(),
        )
    )


def _watching_rank():
    from . import up_next

    return up_next.rank(MediaPart.id)


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


BROWSER_RESET = "browser_audio_started"


def _look_again_for_browsers():
    """Once, when AAC tracks for browsers start: files already looked at for
    TV-friendly audio are looked at again (they may need an AAC track)."""
    from .services import set_setting, setting

    if not browser_enabled() or setting(BROWSER_RESET):
        return
    db.session.query(ChapterState).filter(
        ChapterState.compat_audio_size.isnot(None)
    ).update({ChapterState.compat_audio_size: None}, synchronize_session=False)
    _sonarr_cache.clear()
    set_setting(BROWSER_RESET, utcnow().isoformat())
    db.session.commit()


def sweep(limit=PER_RUN) -> int:
    from .filecheck_jobs import _dry_run

    _look_again_for_browsers()
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


def _add(state, part, path) -> dict:
    try:
        info = probe(path)
    except MediaToolError as exc:
        return {"error": str(exc)}
    tv = source_track(info) if tv_enabled() else None
    browser = browser_audio_source(info) if browser_enabled() else None
    if tv is None and browser is None:
        return {"needed": False, "size": info.size}
    writable, reason = can_write(info)
    if not writable:
        return {"error": reason, "size": info.size}
    codec = next(s["codec_name"] for s in info.streams if s["index"] == (tv or browser))
    in_sync = (state.chapter_file_size, state.chapter_file_mtime) == (
        info.size,
        info.mtime,
    )
    tracks = []
    try:
        if tv is not None:
            info = add_compatible_audio(path, info, tv)
            tracks.append("ac3")
            # An AC3 track doesn't play in a browser either.
            browser = browser_audio_source(info) if browser_enabled() else None
        if browser is not None:
            info = add_compatible_audio(path, info, browser, for_browsers=True)
            tracks.append("aac")
    except MediaToolError as exc:
        if not tracks:
            return {"error": str(exc), "size": info.size}
    if in_sync:  # Curatarr's chapters are still its own
        state.chapter_file_size, state.chapter_file_mtime = info.size, info.mtime
    return {"added": True, "from": codec, "tracks": tracks, "size": info.size}


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
    tracks = result.get("tracks") or ["ac3"]
    what = " and ".join(
        {"ac3": "a TV-friendly AC3 track", "aac": "a stereo AAC track for browsers"}[t]
        for t in tracks
    )
    audit(
        "compat_audio_added",
        part.media.id,
        f"Added {what} to {e.file_label(state)}, beside its "
        f"{result['from'].upper()} audio.",
        f"compat-audio:{state.id}:{state.compat_audio['at']}",
        payload={"file": path, "from": result["from"]},
    )
    db.session.commit()
