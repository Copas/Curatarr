"""Don't play a commentary, an audio description or another language by default.

Some releases make the director's commentary or the audio description (a
narrator describing the picture, for blind viewers) the default audio track,
so that's what Jellyfin plays. When the default track is one of those, by its
flags or its name, and the file has its own soundtrack in the same language,
that soundtrack becomes the default instead: in place, with mkvpropedit, no
re-encode (Settings → Enhance). Unless the fix is automatic (Settings →
Automatic, off by default), a file found like this is listed on Problems with
Fix and Leave it, and changed only once Fix is pressed.

Another language works the same way (its own switch, on by default): when the
default track is tagged as another language and a track is tagged English, the
English track becomes the default. Some players, Jellyfin's Roku app among
them, play the first audio track whatever the default flag says, so English
also has to be first: a file with another language ahead of it is copied with
English moved up, nothing re-encoded, checked, and moved over the original,
even when English is already its default. Tags can be wrong, so that track is
listened to first, a few files each worker cycle in the background, and nothing
changes unless it's heard clearly as English. Curatarr's own chapters and subtitles
came from the old default, so they're queued to be redone from the new one, and
the file is looked at again for a TV-friendly copy of its English track.

Every file is looked at once, a few each worker cycle (it means reading each
file's track list), and again when it's replaced. Files being played, and
files in dry-run libraries, wait.
"""

import re
import threading
import time

from flask import current_app
from sqlalchemy import or_

from . import db, dubs, throttle
from .integrations import IntegrationError
from .mediafile import EASY_AUDIO, ENGLISH
from .models import ChapterState, MediaIdentity, MediaPart, utcnow

RUN_SECONDS = 20
HEARD_PER_RUN = 3  # each is a few clips of one track through Whisper
MOVES_PER_RUN = 2  # each copies a whole file
HEARING_TRIES = 3
LANGUAGE_RESET = "default_audio_first_track_started"
UNTAGGED = {"", "und"}
EXTRA_NAME = re.compile(
    r"comment|descri|narrat|visually impaired|\bad\b|\bdvs\b", re.IGNORECASE
)


def enabled() -> bool:
    from .enrichment import options

    try:
        return bool(options()["fix_default_audio"])
    except Exception:  # noqa: BLE001  an unreadable config turns nothing on
        return False


def english_enabled() -> bool:
    from .enrichment import options

    try:
        return bool(options()["english_default_audio"])
    except Exception:  # noqa: BLE001  an unreadable config turns nothing on
        return False


def _extra(stream) -> bool:
    return bool(stream.get("extra") or EXTRA_NAME.search(stream.get("title") or ""))


def better_default(streams) -> int | None:
    """The track to make the default, or None when the default is fine: the
    file's own soundtrack in the default track's language (most channels),
    when the default is a commentary or an audio description."""
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if len(audio) < 2:
        return None
    current = next((s for s in audio if s.get("default")), audio[0])
    if not _extra(current):
        return None
    own = [
        s
        for s in audio
        if not _extra(s)
        and (s.get("language") or "") == (current.get("language") or "")
    ]
    if not own:
        return None
    return max(own, key=lambda s: s.get("channels") or 0)["index"]


def english_instead(streams) -> int | None:
    """The English track that should be the default and first: the default
    when that's already English but another language is ahead of it, else (the
    default being tagged as another language) the English track with the most
    channels, one TVs can play when that's level. None when English already
    leads, or the default or first track is untagged, or the default is a
    commentary or description (better_default looks after those)."""
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if len(audio) < 2:
        return None
    language = lambda s: str(s.get("language") or "").lower()
    current = next((s for s in audio if s.get("default")), audio[0])
    if _extra(current) or language(current) in UNTAGGED:
        return None
    if language(current) in ENGLISH:
        lead = language(audio[0])
        return None if lead in ENGLISH or lead in UNTAGGED else current["index"]
    english = [s for s in audio if language(s) in ENGLISH and not _extra(s)]
    if not english:
        return None
    return max(
        english,
        key=lambda s: (s.get("channels") or 0, s.get("codec_name") in EASY_AUDIO),
    )["index"]


def _label(stream, why) -> str:
    name = stream.get("title") or stream.get("codec_name") or "audio"
    if why == "language":
        return f"{stream.get('language')} · {name}"
    return name


def _look_again_for_language():
    """Once, when this check first runs: files already looked at are looked at
    again, except those waiting for Fix or Leave it."""
    from .services import set_setting, setting

    if not english_enabled() or setting(LANGUAGE_RESET):
        return
    found = ChapterState.default_audio["found"].as_boolean()
    db.session.query(ChapterState).filter(
        ChapterState.default_audio_size.isnot(None),
        or_(found.is_(None), found.is_(False)),
    ).update({ChapterState.default_audio_size: None}, synchronize_session=False)
    set_setting(LANGUAGE_RESET, utcnow().isoformat())
    db.session.commit()


def _due():
    return (
        db.session.query(MediaPart)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .outerjoin(ChapterState, ChapterState.media_part_id == MediaPart.id)
        .filter(
            MediaPart.kind.in_(("movie_file", "episode")),
            MediaPart.has_file.is_(True),
            MediaPart.file_path.isnot(None),
            MediaIdentity.missing_since.is_(None),
            (ChapterState.id.is_(None))
            | ChapterState.default_audio_size.is_(None)
            | (ChapterState.default_audio_size != MediaPart.size_bytes),
        )
        .order_by(MediaPart.kind.desc(), MediaPart.id)  # films first
    )


def _automatic() -> bool:
    from . import autofix

    return autofix.automatic("default_audio")


def found_rows() -> list:
    """Files found playing an extra first, waiting for Fix (or Leave it), when
    the fix isn't automatic (Settings → Automatic)."""
    from sqlalchemy.orm import contains_eager

    rows = (
        db.session.query(ChapterState)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .options(contains_eager(ChapterState.part))
        .filter(
            ChapterState.default_audio["found"].as_boolean().is_(True),
            MediaIdentity.missing_since.is_(None),
        )
        .all()
    )
    return [r for r in rows if not (r.default_audio or {}).get("dismissed")]


def _mark(state_id, **changes) -> None:
    state = db.session.get(ChapterState, state_id)
    if state is None or not (state.default_audio or {}).get("found"):
        raise ValueError("Nothing waiting for that file")
    state.default_audio = dict(state.default_audio) | changes
    db.session.commit()


def approve(state_id) -> None:
    """Fix it on the next worker cycle."""
    _mark(state_id, approved=True)


def approve_all() -> int:
    rows = found_rows()
    for state in rows:
        state.default_audio = dict(state.default_audio) | {"approved": True}
    db.session.commit()
    return len(rows)


def dismiss(state_id) -> None:
    _mark(state_id, dismissed=True)


def sweep() -> int:
    """Worker cycle: look at a few files; returns how many were fixed."""
    from .compat_audio import _playing
    from .enrichment import (
        _refresh_jellyfin,
        can_write,
        curatarr_wrote,
        file_exists,
        file_label,
        local_path,
        probe,
        state_for,
    )
    from .filecheck_jobs import _dry_run
    from .mediafile import MediaToolError, set_default_audio
    from .services import audit

    if not enabled() and not english_enabled():
        return 0
    _look_again_for_language()
    started, fixed = time.monotonic(), 0
    automatic = _automatic()
    waiting = [
        s.part
        for s in found_rows()
        if automatic or (s.default_audio or {}).get("approved")
    ]
    seen = set()
    for part in waiting[:20] + _due().limit(200).all():
        if time.monotonic() - started > RUN_SECONDS:
            break
        if part.id in seen:
            continue  # approved, and due as well: once is enough
        seen.add(part.id)
        path = local_path(part.file_path)
        if not path or not file_exists(path):
            continue  # unreachable for now
        state = state_for(part)
        try:
            info = probe(path)
        except (MediaToolError, OSError):
            continue
        choice, why = better_default(info.streams) if enabled() else None, "extra"
        if choice is None and english_enabled():
            choice, why = english_instead(info.streams), "language"
        record = {"at": utcnow().isoformat()}
        redo_chapters = False
        previous = state.default_audio or {}
        if state.default_audio_size != info.size:
            previous = {}  # a new copy of the file: an earlier Fix or Leave it was for the old one
        if choice is not None:
            writable, reason = can_write(info)
            audio = [s for s in info.streams if s.get("codec_type") == "audio"]
            before = next((s for s in audio if s.get("default")), audio[0])
            if not writable or _dry_run(part.media):
                record["skipped"] = reason or "dry run"
            elif why == "language" and previous.get("not_english"):
                # Already listened to. Radarr's or Sonarr's size for a file can
                # differ from what's on disk: that mustn't mean hearing it again.
                record["not_english"] = previous["not_english"]
            elif why == "language" and previous.get("error"):
                record["error"] = previous["error"]
            elif why == "language" and not previous.get("heard_english"):
                record["listen"] = True  # hear() confirms it's English first
                if previous.get("tries"):
                    record["tries"] = previous["tries"]
            elif not automatic and not previous.get("approved"):
                # Ask first: listed on Problems with Fix and Leave it.
                after = next(s for s in info.streams if s["index"] == choice)
                record |= {
                    "found": True,
                    "from": _label(before, why),
                    "to": _label(after, why),
                    "why": why,
                    "dismissed": previous.get("dismissed", False),
                }
                if why == "language":
                    record["heard_english"] = True
            elif why == "language" and choice != audio[0]["index"]:
                # A whole file to copy: not in this cycle (move_first).
                if previous.get("move"):
                    continue
                after = next(s for s in info.streams if s["index"] == choice)
                record |= {
                    "move": True,
                    "heard_english": True,
                    "why": why,
                    "from": _label(before, why),
                    "to": _label(after, why),
                }
            elif _playing(part):
                continue  # look again once it's stopped
            else:
                in_sync = (state.chapter_file_size, state.chapter_file_mtime) == (
                    info.size,
                    info.mtime,
                )
                try:
                    set_default_audio(path, info, choice)
                    info = probe(path)
                except (MediaToolError, OSError) as exc:
                    record["error"] = str(exc)[:200]
                else:
                    if in_sync:  # Curatarr's chapters are still its own
                        state.chapter_file_size = info.size
                        state.chapter_file_mtime = info.mtime
                    now_default = next(s for s in info.streams if s["index"] == choice)
                    record |= {
                        "fixed": True,
                        "from": _label(before, why),
                        "to": _label(now_default, why),
                        "why": why,
                    }
                    if why == "language":
                        state.compat_audio_size = None  # see _after_english
                        redo_chapters = (
                            curatarr_wrote(state, info) and state.job_state == "idle"
                        )
                    try:
                        _refresh_jellyfin(part, part.media)
                    except IntegrationError:
                        pass
                    audit(
                        "default_audio_fixed",
                        part.media_identity_id,
                        f"{file_label(state)} played “{record['from']}” by default; "
                        + (
                            "its English track, heard to be English, is the default "
                            "now."
                            if why == "language"
                            else "its own soundtrack is the default now."
                        ),
                        f"default-audio-extra:{state.id}:{record['at']}",
                        payload={"file": path, "stream": choice},
                    )
                    fixed += 1
        state.default_audio_size = info.size
        if part.size_bytes != info.size and record.get("fixed"):
            part.size_bytes = info.size
        state.default_audio = record
        db.session.commit()
        if redo_chapters:
            _redo_chapters(state, part)
    return fixed


def _redo_chapters(state, part):
    from .enrichment import request

    if state.subtitle_fingerprint:
        state.subtitle_status = None  # redone after the chapters
    db.session.commit()
    request(part, "regenerate")
    db.session.commit()


def _waiting_to_move():
    from . import up_next

    return (
        db.session.query(ChapterState)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            ChapterState.default_audio["move"].as_boolean().is_(True),
            MediaPart.has_file.is_(True),
            MediaIdentity.missing_since.is_(None),
        )
        .order_by(up_next.rank(MediaPart.id), MediaPart.id)
    )


def move_first(limit=MOVES_PER_RUN) -> int:
    """Copy a few files with their English track first and default (never one
    that's playing); returns how many were done."""
    from . import enrichment, thumbnails
    from .compat_audio import _playing
    from .filecheck_jobs import _dry_run
    from .mediafile import MediaToolError, file_identity_of, remove_tracks
    from .services import audit, client

    done = 0
    for state in _waiting_to_move().limit(limit * 3).all():
        if done >= limit:
            break
        part = state.part
        path = enrichment.local_path(part.file_path)
        if not path or not enrichment.file_exists(path):
            continue  # unreachable for now
        if _dry_run(part.media) or _playing(part):
            continue
        if not thumbnails.room():
            break  # Jellyfin is still making thumbnails for earlier ones
        try:
            info = enrichment.probe(path)
        except (MediaToolError, OSError):
            continue
        choice = english_instead(info.streams)
        if info.size != state.default_audio_size or choice is None:
            state.default_audio, state.default_audio_size = None, None
            db.session.commit()
            continue  # changed since: looked at again from the start
        record = {"at": utcnow().isoformat()}
        waiting = dict(state.default_audio)
        in_sync = (state.chapter_file_size, state.chapter_file_mtime) == (
            info.size,
            info.mtime,
        )
        had_thumbnails = thumbnails.has_thumbnails(part, part.media)
        db.session.commit()  # never hold the write lock while a file is copied
        try:
            after = remove_tracks(path, info, [], choice, first=True)
        except (MediaToolError, OSError) as exc:
            state.default_audio = record | {"error": str(exc)[:200]}
            db.session.commit()
            continue
        thumbnails.note_rebuilt(part, part.media, had_thumbnails)
        if in_sync:  # Curatarr's chapters are still its own
            state.chapter_file_size, state.chapter_file_mtime = file_identity_of(path)
        for column in ("seek_layout_size", "subtitle_tracks_size"):
            if getattr(state, column) == info.size:
                setattr(state, column, after.size)
        part.size_bytes = after.size  # as Radarr or Sonarr will report it
        state.default_audio_size = after.size
        state.compat_audio_size = None  # see compat_audio._due
        state.default_audio = record | {
            "fixed": True,
            "why": "language",
            "moved": True,
            "from": waiting.get("from"),
            "to": waiting.get("to"),
        }
        db.session.commit()
        try:
            enrichment._refresh_jellyfin(part, part.media)
            if part.kind == "movie_file":
                client("radarr").rescan_movie(part.media.radarr_id)
            elif part.media.sonarr_id:
                client("sonarr").request(
                    "POST",
                    "/api/v3/command",
                    json={"name": "RescanSeries", "seriesId": part.media.sonarr_id},
                )
        except IntegrationError:
            pass  # they notice on their next rescan
        audit(
            "default_audio_fixed",
            part.media_identity_id,
            f"{enrichment.file_label(state)} played “{waiting.get('from')}” first; "
            "its English track, heard to be English, is first and the default now "
            "(nothing re-encoded).",
            f"default-audio-first:{state.id}:{record['at']}",
            payload={"file": path, "stream": choice},
        )
        db.session.commit()
        done += 1
        if enrichment.curatarr_wrote(state, after) and state.job_state == "idle":
            _redo_chapters(state, part)
    return done


def sounds_english(samples) -> bool:
    """Whether what was heard backs up a track's English tag: at least one clip
    clearly English, and none clearly anything else. Stricter would miss quiet
    films, where most clips have no speech in them (dubs.heard, with no tag to
    go on, needs more)."""
    confident = [lang for lang, chance in samples if chance >= dubs.CONFIDENT]
    return bool(confident) and all(lang == "en" for lang in confident)


def _waiting_to_hear():
    from . import up_next

    return (
        db.session.query(ChapterState)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            ChapterState.default_audio["listen"].as_boolean().is_(True),
            MediaPart.has_file.is_(True),
            MediaIdentity.missing_since.is_(None),
        )
        .order_by(up_next.rank(MediaPart.id), MediaPart.id)
    )


def hear(limit=HEARD_PER_RUN) -> int:
    """Listen to the English track of a few files; those heard clearly as
    English go on to be fixed (or listed on Problems) like any other. Returns
    how many were listened to."""
    from . import enrichment
    from .mediafile import MediaToolError

    listened = 0
    for state in _waiting_to_hear().limit(limit).all():
        part = state.part
        path = enrichment.local_path(part.file_path)
        if not path or not enrichment.file_exists(path):
            continue  # unreachable for now
        try:
            info = enrichment.probe(path)
        except (MediaToolError, OSError):
            continue
        choice = english_instead(info.streams)
        now = utcnow().isoformat()
        if info.size != state.default_audio_size or choice is None:
            # Replaced or changed since: looked at again from the start.
            state.default_audio, state.default_audio_size = None, None
            db.session.commit()
            continue
        config, opts = (
            enrichment.config_for(part.media.library_id),
            enrichment.options(),
        )
        model = enrichment._language_model(config, opts)
        tries = (state.default_audio or {}).get("tries", 0) + 1
        db.session.commit()  # never hold the write lock while listening
        try:
            samples = dubs.listen(
                info,
                [choice],
                opts["whisper_device"],
                config["semantic"]["model"],
                make_room=getattr(model, "unload", None),
            )
        except (MediaToolError, OSError) as exc:
            state.default_audio = (
                {"at": now, "listen": True, "tries": tries}
                if tries < HEARING_TRIES
                else {"at": now, "error": str(exc)[:200]}
            )
            db.session.commit()
            continue
        listened += 1
        heard = samples.get(choice, [])
        if sounds_english(heard):
            audio = [s for s in info.streams if s.get("codec_type") == "audio"]
            before = next((s for s in audio if s.get("default")), audio[0])
            after = next(s for s in audio if s["index"] == choice)
            state.default_audio = {
                "at": now,
                "found": True,
                "heard_english": True,
                "why": "language",
                "from": _label(before, "language"),
                "to": _label(after, "language"),
                "dismissed": False,
            }
        else:
            state.default_audio = {
                "at": now,
                "not_english": dubs.heard(heard) or "unclear",
            }
        db.session.commit()
    return listened


_lock = threading.Lock()
_thread = None


def start(app=None) -> bool:
    """Start listening in the background unless already running (worker cycle)."""
    global _thread
    if not english_enabled():
        return False
    with _lock:
        if _thread and _thread.is_alive():
            return False
        if not _waiting_to_hear().first() and not _waiting_to_move().first():
            return False
        app = app or current_app._get_current_object()
        _thread = threading.Thread(
            target=_run, args=(app,), name="curatarr-default-audio", daemon=True
        )
        _thread.start()
    return True


def _run(app):
    with app.app_context():
        try:
            hear()
            move_first()
        except throttle.Interrupted:
            db.session.rollback()  # all work paused; carries on after resuming
        except Exception:
            db.session.rollback()
            app.logger.exception("Listening for English audio failed")
        finally:
            db.session.remove()
