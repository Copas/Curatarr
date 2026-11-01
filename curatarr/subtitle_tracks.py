"""Keep only the subtitle tracks in the languages wanted.

Releases often carry a dozen subtitle tracks (Chinese, Arabic, Portuguese…).
Jellyfin lists them all, and a player set to pick subtitles by itself can pick
the wrong one. When Settings → Enhance → Subtitle tracks is on (it's off by
default), each MKV file is looked at once (again only if it changes), and the
subtitle tracks not in a kept language are removed: a copy without them,
checked, then moved over the original, so nothing is re-encoded and video,
audio, chapters and attachments stay as they are. One or two files per worker
cycle, in the background, never one that's playing.

A track is kept when it's tagged with a kept language, or when it has no
language tag and its title names one ("English"). Untagged tracks titled
otherwise ("Simplified", "Traditional": releases often leave Chinese ones
untagged) or with no title are removed. Subtitle files beside the video
(Bazarr's) aren't touched. MP4 files are left alone: Curatarr only rewrites
Matroska, and MP4 releases rarely carry other languages' subtitles.

This is the job an external transcoder (Tdarr, with an "English subtitles
only" plugin) can do; done here, it knows what's playing and shows in History.
"""

import re
import threading

from flask import current_app
from sqlalchemy import case, func, or_
from sqlalchemy.exc import OperationalError

from . import db, throttle, thumbnails
from .mediafile import MediaToolError, file_identity_of, probe, remove_tracks
from .models import ChapterState, MediaIdentity, MediaPart, UserMediaState, utcnow

CHECKS_PER_RUN = 200  # a probe of each file
REWRITES_PER_RUN = 2  # each copies a whole file
REMAINING_KEY = "subtitle_tracks_remaining"
UNTAGGED = {"", "und"}
# Language codes as files tag them, and the words their titles use.
NAMES = {
    "eng": ("en", "english"),
    "spa": ("es", "spanish", "español", "espanol"),
    "fre": ("fr", "fra", "french", "français", "francais"),
    "ger": ("de", "deu", "german", "deutsch"),
    "ita": ("it", "italian", "italiano"),
    "por": ("pt", "portuguese", "português", "portugues"),
    "dut": ("nl", "nld", "dutch", "nederlands"),
    "jpn": ("ja", "japanese"),
}
_lock = threading.Lock()
_thread = None


def _enrichment():
    from . import enrichment

    return enrichment


def enabled() -> bool:
    try:
        return bool(_enrichment().options()["subtitle_tracks"])
    except Exception:  # noqa: BLE001  an unreadable config turns nothing on
        return False


def languages() -> list[str]:
    """The languages kept, as three-letter codes (English when none are set)."""
    try:
        wanted = _enrichment().options()["subtitle_languages"]
    except Exception:  # noqa: BLE001
        wanted = ["eng"]
    return list(wanted) or ["eng"]


def _codes(language: str) -> tuple[set[str], re.Pattern]:
    """The tags meaning a language, and a pattern for its name in a title."""
    language = language.lower()
    names = NAMES.get(language, ())
    tags = {language, *(n for n in names if len(n) <= 3)}
    # Two-letter codes aren't looked for in titles: "en" is a French word.
    words = [language, *(n for n in names if len(n) > 3)]
    return tags, re.compile(
        r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE
    )


def unwanted(info, keep=None) -> list[int]:
    """The subtitle streams (ffprobe indexes) not in a kept language."""
    keep = keep or languages()
    rules = [_codes(language) for language in keep]
    drop = []
    for stream in info.streams:
        if stream.get("codec_type") != "subtitle":
            continue
        language = str(stream.get("language") or "").lower()
        title = str(stream.get("title") or "")
        if language in UNTAGGED:
            kept = any(pattern.search(title) for _tags, pattern in rules)
        else:
            kept = any(language in codes for codes, _pattern in rules)
        if not kept:
            drop.append(stream["index"])
    return drop


def _default_audio(info) -> int | None:
    audio = [s for s in info.streams if s.get("codec_type") == "audio"]
    for stream in audio:
        if stream.get("default"):
            return stream["index"]
    return audio[0]["index"] if audio else None


def _due():
    """MKV files not looked at yet, or changed since: what's playing and what
    people will watch next first, then what's been watched, then the rest."""
    from . import up_next

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
                ChapterState.subtitle_tracks_size.is_(None),
                ChapterState.subtitle_tracks_size != MediaPart.size_bytes,
            ),
        )
        .order_by(
            up_next.rank(MediaPart.id),
            case((watched.c.last.is_(None), 1), else_=0),
            watched.c.last.desc(),
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
            target=_run, args=(app,), name="curatarr-subtitle-tracks", daemon=True
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
            app.logger.exception("Removing other languages' subtitle tracks failed")
        finally:
            db.session.remove()


def sweep(checks=CHECKS_PER_RUN, rewrites=REWRITES_PER_RUN) -> int:
    """Look at up to ``checks`` files and rewrite up to ``rewrites``; returns
    how many were rewritten."""
    from .seek_repair import _playing

    e = _enrichment()
    keep = languages()
    done = 0
    for part in _due().limit(checks).all():
        path = e.local_path(part.file_path)
        if not path or not e.file_exists(path):
            continue  # unreachable for now; try again later
        state = e.state_for(part)
        try:
            info = probe(path)
        except MediaToolError as exc:
            state.subtitle_tracks_size = part.size_bytes
            state.subtitle_tracks = {"error": str(exc), "at": utcnow().isoformat()}
            db.session.commit()
            continue
        drop = unwanted(info, keep)
        if not drop:
            state.subtitle_tracks_size = part.size_bytes
            state.subtitle_tracks = {"ok": True, "at": utcnow().isoformat()}
            db.session.commit()
            continue
        if done >= rewrites or _playing(part) or not thumbnails.room():
            db.session.commit()
            continue  # next cycle (or once Jellyfin has caught up on thumbnails)
        db.session.commit()  # never hold the write lock while a file is copied
        state_id, part_id = state.id, part.id
        had_thumbnails = thumbnails.has_thumbnails(part, part.media)
        result = _rewrite(part, path, info, drop)
        if result.get("removed"):
            thumbnails.note_rebuilt(part, part.media, had_thumbnails)
        try:
            _record(state, part, result)
        except OperationalError:
            # Busy past the timeout; the file is already rewritten: once more.
            db.session.rollback()
            _record(
                db.session.get(ChapterState, state_id),
                db.session.get(MediaPart, part_id),
                result,
            )
        if result.get("removed"):
            _announce(state, part, path, result)
            done += 1
    _remember_remaining()
    return done


def _rewrite(part, path, info, drop) -> dict:
    before = file_identity_of(path)
    default = _default_audio(info)
    languages_removed = sorted(
        {
            str(s.get("language") or "untagged")
            for s in info.streams
            if s["index"] in drop
        }
    )
    try:
        if default is None:
            raise MediaToolError("The file has no audio track; left unchanged")
        after = remove_tracks(path, info, drop, default)
    except MediaToolError as exc:
        return {"error": str(exc), "size": part.size_bytes}
    return {
        "removed": len(drop),
        "languages": languages_removed,
        "before": before,
        "size": after.size,
        "mtime": file_identity_of(path)[1],
    }


def _record(state, part, result):
    if result.get("removed"):
        before_size, before_mtime = result["before"]
        # Keep Curatarr's own records of this file pointing at the new copy.
        if (state.chapter_file_size, state.chapter_file_mtime) == (
            before_size,
            before_mtime,
        ):
            state.chapter_file_size = result["size"]
            state.chapter_file_mtime = result["mtime"]
        for column in ("compat_audio_size", "default_audio_size", "seek_layout_size"):
            if getattr(state, column) == before_size:
                setattr(state, column, result["size"])
        part.size_bytes = result["size"]  # as Radarr/Sonarr will report it
    state.subtitle_tracks_size = result["size"]
    state.subtitle_tracks = {
        k: v for k, v in result.items() if k in {"removed", "languages", "error"}
    } | {"at": utcnow().isoformat()}
    db.session.commit()


def _announce(state, part, path, result):
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
    count = result["removed"]
    audit(
        "subtitle_tracks_removed",
        part.media.id,
        f"Removed {count} subtitle track{'' if count == 1 else 's'} in other "
        f"languages ({', '.join(result['languages'])}) from {e.file_label(state)} "
        "(nothing re-encoded).",
        f"subtitle-tracks:{state.id}:{state.subtitle_tracks['at']}",
        payload={"file": path, "languages": result["languages"]},
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
    """Files looked at, rewritten, and still to look at; None when it's off
    and nothing was ever rewritten."""
    from .services import setting

    rows = db.session.query(ChapterState.subtitle_tracks).filter(
        ChapterState.subtitle_tracks.isnot(None)
    )
    checked = rewritten = 0
    for (result,) in rows:
        checked += 1
        rewritten += bool((result or {}).get("removed"))
    on = enabled()
    if not on and not rewritten:
        return None
    left = (setting(REMAINING_KEY, {}) or {}).get("files", 0) if on else 0
    return {
        "on": on,
        "checked": checked,
        "rewritten": rewritten,
        "left": left,
        "total": checked + left,
    }
