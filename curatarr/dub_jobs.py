"""Finding films whose "English" audio isn't, and fixing them (see dubs.py).

The sweep runs in its own background thread from the worker cycle, a film at
a time: films made in English are only marked checked, and films made in
another language have their audio tracks listened to. Fixes that only edit
the file's track labels are applied straight away; a film with no English
audio at all is listed for review, and replaced through Radarr (its release
blocklisted, the file deleted, a new search started) when approved or when
"replace automatically" is on.
"""

import threading
from datetime import timedelta

from flask import current_app
from sqlalchemy import case, or_

from . import db, dubs
from .chapters import GENERATED_STATUSES, ChapterConfigError
from .integrations import IntegrationError
from .mediafile import (
    MediaToolError,
    can_write,
    probe,
    remove_tracks,
    set_audio_language,
    set_default_audio,
)
from .models import ChapterState, Library, MediaIdentity, MediaPart, utcnow
from .services import audit, client, setting

FOREIGN_PER_RUN = 3  # films listened to per run; each takes ~30 s of GPU
ENGLISH_PER_RUN = 500  # films made in English, only marked as checked

_thread = None
_lock = threading.Lock()


def _enrichment():
    from . import enrichment

    return enrichment


def enabled() -> bool:
    try:
        return bool(_enrichment().options()["dub_check"])
    except ChapterConfigError:
        return False


def _due():
    """Movie files not yet checked, or replaced since they were."""
    return (
        db.session.query(MediaPart)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .join(Library, MediaIdentity.library_id == Library.id)
        .outerjoin(ChapterState, ChapterState.media_part_id == MediaPart.id)
        .filter(
            MediaPart.kind == "movie_file",
            MediaPart.has_file.is_(True),
            MediaPart.file_path.isnot(None),
            MediaIdentity.radarr_id.isnot(None),
            MediaIdentity.missing_since.is_(None),
            Library.media_type == "movies",
            or_(
                ChapterState.id.is_(None),
                ChapterState.dub_checked_at.is_(None),
                ChapterState.dub_file_size.is_(None),
                ChapterState.dub_file_size != MediaPart.size_bytes,
            ),
        )
        # Films whose chapter job is waiting on this check go first.
        .order_by(case((ChapterState.job_state == "queued", 0), else_=1))
    )


CHAPTER_WAIT = timedelta(hours=6)


def holds_chapters(state, enabled_now: bool) -> bool:
    """Whether a movie's chapter job should wait for its dub check.

    Chapters and subtitles are transcribed from the default audio track; if
    the check is about to change which track that is, transcribing first
    would mean doing it twice. The wait is capped, so a film the check can't
    get to (Radarr unreachable, say) isn't held forever.
    """
    if not enabled_now or state.requested_action == "subtitles":
        return False
    part = state.part
    if part is None or part.kind != "movie_file" or not state.media.radarr_id:
        return False
    if state.dub_checked_at and state.dub_file_size == part.size_bytes:
        return False
    requested = state.requested_at
    if requested is None:
        return False
    if requested.tzinfo is None:
        requested = requested.replace(tzinfo=utcnow().tzinfo)
    return utcnow() - requested < CHAPTER_WAIT


def start(app=None):
    """Start a sweep in the background unless one is running (worker cycle)."""
    global _thread
    if not enabled():
        return False
    with _lock:
        if _thread and _thread.is_alive():
            return False
        if not _due().first():
            return False
        app = app or current_app._get_current_object()
        _thread = threading.Thread(
            target=_run, args=(app,), name="curatarr-dubs", daemon=True
        )
        _thread.start()
    return True


def _run(app):
    with app.app_context():
        try:
            sweep()
        except Exception:
            db.session.rollback()
            app.logger.exception("Dub check failed")
        finally:
            db.session.remove()


def _codes(text) -> set:
    return {dubs.code(part) for part in str(text or "").split("/")} - {None}


def _films() -> dict:
    """Radarr id -> (original language, audio languages, subtitle languages),
    as ISO 639-1 codes, the file's languages from Radarr's media info."""
    found = {}
    for movie in client("radarr").movies() or []:
        language = (movie.get("originalLanguage") or {}).get("name")
        info = (movie.get("movieFile") or {}).get("mediaInfo") or {}
        found[movie["id"]] = (
            dubs.code(language) or (language or None),
            _codes(info.get("audioLanguages")),
            _codes(info.get("subtitles")),
        )
    return found


def _needs_listening(film, remove_others: bool) -> bool:
    """Films made in English are checked only when their file has audio (or,
    when removing other languages, subtitles) in another language."""
    original, audio, subtitles = film
    if original != "en":
        return True
    return bool(audio - {"en"}) or (remove_others and bool(subtitles - {"en"}))


def sweep(foreign_limit=FOREIGN_PER_RUN) -> dict:
    parts = _due().limit(ENGLISH_PER_RUN + foreign_limit).all()
    counts = {"english": 0, "foreign": 0}
    if not parts:
        return counts
    films = _films()
    e = _enrichment()
    remove_others = bool(e.options()["dub_remove_other_languages"])
    for part in parts:
        film = films.get(part.media.radarr_id)
        if film is None:
            continue  # not in Radarr any more; the reconciler will catch up
        state = e.state_for(part)
        original = film[0]
        if not _needs_listening(film, remove_others):
            state.dub_checked_at = utcnow()
            state.dub_file_size = part.size_bytes
            state.dub_result = {"original": original}
            db.session.commit()
            counts["english"] += 1
            continue
        if counts["foreign"] >= foreign_limit or setting("chapters_paused_since"):
            continue  # listening needs the GPU: not while playback has priority
        try:
            check(state, part, original)
        except (MediaToolError, ValueError, OSError) as exc:
            db.session.rollback()
            state = e.state_for(part)
            state.dub_checked_at = utcnow()
            state.dub_file_size = part.size_bytes
            state.dub_result = {"original": original, "error": str(exc)[:300]}
            db.session.commit()
        counts["foreign"] += 1
    return counts


def _audio_tracks(info):
    return [s["index"] for s in info.streams if s.get("codec_type") == "audio"]


def check(state, part, original) -> dict:
    """Listen to a film's audio tracks and apply the label/default fixes."""
    e = _enrichment()
    path = e.local_path(part.file_path)
    if not path or not e.file_exists(path):
        raise ValueError("The file wasn't found")
    info = probe(path)
    tracks = _audio_tracks(info)[: dubs.MAX_TRACKS]
    if not tracks:
        raise ValueError("The file has no audio")
    config, opts = e.config_for(part.media.library_id), e.options()
    model = e._language_model(config, opts)
    samples = dubs.listen(
        info,
        tracks,
        opts["whisper_device"],
        config["semantic"]["model"],
        make_room=getattr(model, "unload", None),
    )
    heard = {track: dubs.heard(samples.get(track, [])) for track in tracks}
    result = dubs.plan(info.streams, heard)
    result.update(original=original, applied=[], samples=samples)
    if result["actions"]:
        writable, reason = can_write(info)
        if writable:
            result["applied"] = _apply(state, part, path, info, result["actions"])
            for action in result["applied"]:
                if action[0] == "label":
                    track = next(t for t in result["tracks"] if t["index"] == action[1])
                    track["was"], track["label"] = track["label"], action[2]
        else:
            result["not_applied"] = reason
    state.dub_checked_at = utcnow()
    # Radarr's size for the file: a different one later means it was replaced.
    state.dub_file_size = part.size_bytes
    if opts["dub_remove_other_languages"] and result["verdict"] == "english":
        removed = _remove_others(state, part, path, result)
        if removed:
            result["removed"] = removed
            state.dub_file_size = removed["size"]  # what Radarr will now report
    state.dub_result = _jsonable(result)
    db.session.commit()
    if result.get("removed"):
        r = result["removed"]
        audit(
            "dub_tracks_removed",
            part.media.id,
            f"Removed {_removed_words(r)} from {e.file_label(state)}.",
            f"dub-removed:{state.id}:{state.dub_checked_at.isoformat()}",
            payload={"file": path, **r},
        )
    if result["applied"]:
        audit(
            "dub_fixed",
            part.media.id,
            f"Fixed the audio tracks of {e.file_label(state)}: "
            + "; ".join(_describe(a) for a in result["applied"])
            + ".",
            f"dub-fixed:{state.id}:{state.dub_checked_at.isoformat()}",
            payload={"file": path, "actions": state.dub_result["applied"]},
        )
    if result["verdict"] == "no_english":
        e._log("dub_no_english", state_id=state.id)
        if opts["dub_replace_automatically"] and not _dry_run(part.media):
            try:
                replace(state)
            except (IntegrationError, ValueError) as exc:
                db.session.rollback()
                e._log("dub_replace_failed", state_id=state.id, error=str(exc))
    return state.dub_result


def _removed_words(removed) -> str:
    parts = []
    for kind, noun in (("audio", "audio"), ("subtitles", "subtitle")):
        languages = sorted({dubs.name(lang) for _index, lang in removed[kind]})
        if languages:
            count = len(removed[kind])
            parts.append(
                f"{count} {noun} track{'s' if count != 1 else ''} ({', '.join(languages)})"
            )
    return " and ".join(parts)


def _remove_others(state, part, path, result):
    """Rewrite the file without audio and subtitles in other languages."""
    e = _enrichment()
    if _dry_run(part.media):
        result["not_removed"] = "dry run is on for this library"
        return None
    info = probe(path)
    writable, reason = can_write(info)
    if not writable:
        result["not_removed"] = reason
        return None
    drop = dubs.removable(result, info.streams)
    if not drop["audio"] and not drop["subtitles"]:
        return None
    english = [t["index"] for t in result["tracks"] if t["heard"] == "en"]
    default = next(
        (
            s["index"]
            for s in info.streams
            if s.get("default") and s["index"] in english
        ),
        english[0],
    )
    in_sync = (state.chapter_file_size, state.chapter_file_mtime) == (
        info.size,
        info.mtime,
    )
    heard = {t["index"]: t["heard"] for t in result["tracks"]}
    labels = {s["index"]: dubs.code(s.get("language")) for s in info.streams}
    try:
        after = remove_tracks(path, info, drop["audio"] + drop["subtitles"], default)
    except MediaToolError as exc:
        result["not_removed"] = str(exc)
        return None
    if in_sync:
        state.chapter_file_size, state.chapter_file_mtime = after.size, after.mtime
    for track in result["tracks"]:
        track["removed"] = track["index"] in drop["audio"]
    e._refresh_jellyfin(part, part.media)
    try:
        client("radarr").rescan_movie(part.media.radarr_id)
    except IntegrationError:
        pass  # Radarr notices on its own next rescan
    return {
        "audio": [[i, heard.get(i)] for i in drop["audio"]],
        "subtitles": [[i, labels.get(i)] for i in drop["subtitles"]],
        "size": after.size,
    }


def _jsonable(result):
    return {
        **result,
        "actions": [list(a) for a in result["actions"]],
        "applied": [list(a) for a in result["applied"]],
        "samples": {str(k): [list(s) for s in v] for k, v in result["samples"].items()},
    }


def _describe(action) -> str:
    if action[0] == "label":
        return f"track {action[1]} relabelled {dubs.name(action[2])}"
    return f"track {action[1]} (English) made the default"


def _apply(state, part, path, info, actions) -> list:
    e = _enrichment()
    in_sync = (state.chapter_file_size, state.chapter_file_mtime) == (
        info.size,
        info.mtime,
    )
    applied = []
    for action in actions:
        current = probe(path)
        if action[0] == "label":
            three = dubs.LANGUAGES[action[2]][0]
            set_audio_language(path, current, action[1], three, action[2])
        else:
            set_default_audio(path, current, action[1])
        applied.append(action)
    after = probe(path)
    if in_sync:
        # Only labels changed; the chapters Curatarr wrote are still its own.
        state.chapter_file_size, state.chapter_file_mtime = after.size, after.mtime
    if any(a[0] == "default" for a in applied) and (
        state.chapter_status in GENERATED_STATUSES.values()
        and state.job_state == "idle"
    ):
        # Curatarr's chapters and subtitles came from the foreign track.
        if state.subtitle_fingerprint:
            state.subtitle_status = None
        db.session.commit()
        e.request(part, "regenerate")
    e._refresh_jellyfin(part, part.media)
    return applied


def _dry_run(media) -> bool:
    from .services import resolved_policy

    return bool(resolved_policy(media)[0]["dry_run"])


def replace(state) -> str:
    """Blocklist the release in Radarr, delete the file, and search again."""
    e = _enrichment()
    media = state.media
    if not media.radarr_id:
        raise ValueError("This film isn't in Radarr")
    if _dry_run(media):
        raise ValueError(
            "dry run is on for this library, so Curatarr won't delete the file. "
            "Turn dry run off, or replace it in Radarr yourself"
        )
    radarr = client("radarr")
    movie = radarr.movie(media.radarr_id)
    grabs = radarr.grabs(media.radarr_id)
    blocklisted = None
    if grabs:
        radarr.mark_failed(grabs[0]["id"])
        blocklisted = grabs[0].get("sourceTitle")
    file_id = (movie.get("movieFile") or {}).get("id")
    if file_id:
        radarr.delete_movie_file(file_id)
    radarr.search_movie(media.radarr_id)
    state.dub_result = {
        **(state.dub_result or {}),
        "replaced": {"at": utcnow().isoformat(), "blocklisted": blocklisted},
    }
    db.session.commit()
    audit(
        "dub_replaced",
        media.id,
        f"Replaced {e.file_label(state)}: it had no English audio."
        + (f" Blocklisted {blocklisted}." if blocklisted else ""),
        f"dub-replaced:{state.id}:{state.dub_result['replaced']['at']}",
        payload={"blocklisted": blocklisted, "file_id": file_id},
    )
    message = "Deleted the file and asked Radarr to find another release."
    if blocklisted:
        message += " The old release is blocklisted so it won't come back."
    else:
        message += " Radarr had no download record for it, so nothing was blocklisted."
    return message


def dismiss(state) -> None:
    state.dub_result = {**(state.dub_result or {}), "dismissed": True}
    db.session.commit()


def recheck(state) -> None:
    state.dub_checked_at = None
    db.session.commit()


def review() -> dict:
    """Films for the dub page: ones to act on, and recent fixes to spot-check."""
    rows = (
        db.session.query(ChapterState)
        .filter(ChapterState.dub_result.isnot(None))
        .order_by(ChapterState.dub_checked_at.desc())
        .all()
    )
    to_act, fixed, unsure, checked = [], [], [], 0
    for state in rows:
        result = state.dub_result or {}
        if result.get("original") in (None, "en") and "verdict" not in result:
            continue
        checked += 1
        if result.get("dismissed") or result.get("replaced"):
            continue
        if result.get("verdict") == "no_english":
            to_act.append(state)
        elif result.get("applied") or result.get("removed"):
            fixed.append(state)
        elif result.get("verdict") == "unsure" or result.get("error"):
            unsure.append(state)
    return {"to_act": to_act, "fixed": fixed, "unsure": unsure, "checked": checked}


def counts() -> dict:
    found = review()
    return {
        "dubs_to_act": len(found["to_act"]),
        "dubs_fixed": len(found["fixed"]),
        "dubs_checked": found["checked"],
    }
