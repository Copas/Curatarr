"""Checking every film and episode for burned-in subtitles and broken files.

A sweep runs in its own background thread from the worker cycle, a few files
at a time, and stands aside while playback has priority (it reads from the
same disks people are watching from). Films are checked first. A file is
checked again only when it's replaced. What's found is listed on Enhance →
Problems, where a file can be replaced through Radarr or Sonarr (its release
blocklisted, the file deleted, a new search started) or kept.
"""

import re
import threading

from flask import current_app
from sqlalchemy import case, func, or_

from . import db, filecheck, throttle
from .chapters import ChapterConfigError
from .integrations import IntegrationError
from .mediafile import MediaToolError, probe
from .models import ChapterState, Library, MediaIdentity, MediaPart, utcnow
from .services import audit, client, setting

PER_RUN = 4

_thread = None
_lock = threading.Lock()


def _enrichment():
    from . import enrichment

    return enrichment


def enabled() -> bool:
    try:
        return bool(_enrichment().options()["file_check"])
    except ChapterConfigError:
        return False


def _due():
    """Files not yet checked, or replaced since they were; films first."""
    return (
        db.session.query(MediaPart)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .join(Library, MediaIdentity.library_id == Library.id)
        .outerjoin(ChapterState, ChapterState.media_part_id == MediaPart.id)
        .filter(
            MediaPart.kind.in_(("movie_file", "episode")),
            MediaPart.has_file.is_(True),
            MediaPart.file_path.isnot(None),
            MediaIdentity.missing_since.is_(None),
            Library.media_type.in_(("tv", "movies")),
            or_(
                ChapterState.id.is_(None),
                ChapterState.file_checked_at.is_(None),
                ChapterState.file_check_size.is_(None),
                ChapterState.file_check_size != MediaPart.size_bytes,
            ),
        )
        .order_by(case((MediaPart.kind == "movie_file", 0), else_=1), MediaPart.id)
    )


def start(app=None):
    """Start a sweep in the background unless one is running (worker cycle)."""
    global _thread
    if not enabled() or setting("chapters_paused_since"):
        return False
    with _lock:
        if _thread and _thread.is_alive():
            return False
        if not _due().first():
            return False
        app = app or current_app._get_current_object()
        _thread = threading.Thread(
            target=_run, args=(app,), name="curatarr-filecheck", daemon=True
        )
        _thread.start()
    return True


def _run(app):
    with app.app_context():
        try:
            sweep()
        except throttle.Interrupted:
            db.session.rollback()  # all work paused; it carries on after resuming
        except Exception:
            db.session.rollback()
            app.logger.exception("File check failed")
        finally:
            db.session.remove()


def _runtimes() -> dict:
    """Listed film runtimes in minutes, by ("movie", Radarr id). Episodes are
    looked up per show as they're checked (_episode_runtime)."""
    found = {}
    for kind, name, fetch in (("movie", "radarr", lambda c: c.movies()),):
        try:
            for row in fetch(client(name)) or []:
                if row.get("runtime"):
                    found[(kind, row["id"])] = row["runtime"]
        except IntegrationError:
            continue  # checked without a runtime; the rest still applies
    return found


def _episode_runtime(part, media, runtimes):
    """The episode's own listed runtime. Not the show's: episodes of one show
    vary a lot (a 22-minute episode of a show listed at 44), and comparing
    with the show's had good copies replaced as "cut short" over and over.
    None when Sonarr has no runtime for the episode: then length isn't judged."""
    if not media.sonarr_id or not part.sonarr_episode_id:
        return None
    key = ("episodes", media.sonarr_id)
    if key not in runtimes:
        try:
            runtimes[key] = {
                e["id"]: e.get("runtime")
                for e in client("sonarr").episodes(media.sonarr_id) or []
            }
        except IntegrationError:
            runtimes[key] = {}
    return runtimes[key].get(part.sonarr_episode_id) or None


def _segments(part, info):
    """The saved transcript, if Curatarr has made one; never a new one."""
    e = _enrichment()
    from .transcribe import cached

    try:
        model = e.config_for(part.media.library_id)["semantic"]["model"]
    except ChapterConfigError:
        return []
    hit = cached(info, model, e.cache_dir() / "transcripts")
    return hit[0] if hit else []


RECHECKED = "short_episodes_rechecked"


def _recheck_short_episodes():
    """Once: episodes flagged "cut short" by the show's runtime are checked
    again by their own (and any Replace waiting on that verdict is dropped)."""
    if setting(RECHECKED):
        return
    from .services import set_setting

    rows = (
        db.session.query(ChapterState)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .filter(MediaPart.kind == "episode", ChapterState.file_problem.is_(True))
        .all()
    )
    for state in rows:
        if any(
            i.get("kind") == "short"
            for i in (state.file_check or {}).get("issues") or []
        ):
            state.file_checked_at = None
            state.file_problem = False
            state.file_check = {
                k: v
                for k, v in (state.file_check or {}).items()
                if k not in ("replace_requested", "replace_auto", "blocklisting")
            }
    set_setting(RECHECKED, utcnow().isoformat())
    db.session.commit()


def sweep(limit=PER_RUN) -> int:
    _recheck_short_episodes()
    # Look further than `limit`: files that can't be reached are skipped.
    parts = _due().limit(limit * 10).all()
    if not parts:
        return 0
    runtimes = _runtimes()
    e = _enrichment()
    checked = 0
    for part in parts:
        if checked >= limit:
            break
        if setting("chapters_paused_since") or throttle.stopping():
            break  # someone started watching, or all work is paused
        path = e.local_path(part.file_path)
        if not path or not e.file_exists(path):
            continue  # unreachable for now (a share offline?): check it later
        state = e.state_for(part)
        # Save a new record now: the check reads frames for up to a minute, and
        # holding SQLite's write lock that long stalls the web app and webhooks.
        db.session.commit()
        try:
            check(state, part, runtimes)
        except (MediaToolError, OSError, ValueError) as exc:
            db.session.rollback()
            state = e.state_for(part)
            state.file_checked_at = utcnow()
            state.file_check_size = part.size_bytes
            state.file_check = {"error": str(exc)[:300], "issues": []}
            state.file_problem = False
            db.session.commit()
        checked += 1
    return checked


def check(state, part, runtimes=None) -> dict:
    e = _enrichment()
    path = e.local_path(part.file_path)
    if not path or not e.file_exists(path):
        raise ValueError("The file wasn't found")
    info = probe(path)
    if not info.duration:
        raise ValueError("The file has no length")
    media = part.media
    film = part.kind == "movie_file"
    runtimes = {} if runtimes is None else runtimes
    listed = (
        runtimes.get(("movie", media.radarr_id))
        if film
        else _episode_runtime(part, media, runtimes)
    )
    segments = _segments(part, info)
    result = {
        "issues": filecheck.health(path, info.duration, listed, segments, film=film),
        "transcript": bool(segments),
    }
    _trust_agreeing_lengths(state, result, info.duration)
    if film:
        result["subtitles"] = filecheck.burned_subtitles(path, info.duration, segments)
    state.file_checked_at = utcnow()
    state.file_check_size = part.size_bytes
    state.file_check = result
    state.file_problem = _needs_action(result)
    db.session.commit()
    if state.file_problem:
        e._log("file_problem", state_id=state.id, kinds=_kinds(result))
        if e.options()["file_replace_automatically"] and not _dry_run(media):
            from . import better_copy

            better_copy.request(
                state, "file", auto=True
            )  # only if a better copy exists
    return result


SAME_LENGTH_MINUTES = 2


def _replaced_lengths(state) -> list[int]:
    """How long earlier copies of this file ran, in minutes, from the "runs N
    min" of each time one was replaced for being short."""
    from .models import LifecycleAction

    texts = db.session.query(LifecycleAction.reason_text).filter(
        LifecycleAction.action_type == "file_replaced",
        LifecycleAction.idempotency_key.like(f"file-replaced:{state.id}:%"),
    )
    return [
        int(found.group(1))
        for (text,) in texts
        if (found := re.search(r"runs (\d+) min", text or "", re.IGNORECASE))
    ]


def _trust_agreeing_lengths(state, result, duration):
    """A copy that runs as long as one already replaced for being short isn't
    cut short: the listed length is wrong (Sonarr lists one runtime for a
    whole show, and a finale may be shorter). Replacing it again only throws
    away good copies, one release after another."""
    minutes = round(duration / 60)
    earlier = _replaced_lengths(state)
    if not any(abs(minutes - m) <= SAME_LENGTH_MINUTES for m in earlier):
        return
    kept = [i for i in result["issues"] if i.get("kind") != "short"]
    if len(kept) != len(result["issues"]):
        result["issues"] = kept
        result["length_note"] = (
            f"Runs {minutes} min, like the copy it replaced, so the listed "
            "length is wrong"
        )


def _kinds(result) -> list[str]:
    kinds = [issue["kind"] for issue in result.get("issues") or []]
    if (result.get("subtitles") or {}).get("verdict") == "burned":
        kinds.insert(0, "burned_subtitles")
    return kinds


def _needs_action(result) -> bool:
    return bool(_kinds(result)) and not result.get("dismissed")


def describe(result) -> list[str]:
    """What's wrong, in words, for the review page and Overview."""
    lines = []
    subtitles = result.get("subtitles") or {}
    if subtitles.get("verdict") == "burned":
        lines.append(
            "Subtitles are burned into the picture "
            f"(on {subtitles['hits']} of {subtitles['samples']} frames checked)"
        )
    lines.extend(issue["text"] for issue in result.get("issues") or [])
    return lines


def _dry_run(media) -> bool:
    from .services import resolved_policy

    return bool(resolved_policy(media)[0]["dry_run"])


# Release names marking one piece of a split film ("Part.1", "CD2"), as a
# Radarr/Sonarr release-profile regular expression.
SPLIT_RELEASE = (
    r"/(^|[ ._\-\[(])(part|pt|cd|disc|disk)[ ._\-]?(\d|i{1,3}|iv)([ ._\-\])]|$)/i"
)


def _blocklisted_before(state) -> bool:
    """A replace that failed after blocklisting (a slow delete) mustn't
    blocklist again when it's retried: by then the latest grab is the new
    copy, not the bad one."""
    return bool((state.file_check or {}).get("blocklisting"))


def _note_blocklisted(state, title):
    state.file_check = {**(state.file_check or {}), "blocklisting": title or True}
    db.session.commit()


def _already_replaced(state, media) -> str:
    """The file was replaced since the problem was found (a retry after a slow
    delete, or by hand): nothing to blocklist or delete; the new file is
    checked on its own."""
    state.file_check = {
        **{k: v for k, v in (state.file_check or {}).items() if k != "blocklisting"},
        "replaced": {"at": utcnow().isoformat(), "blocklisted": None},
    }
    state.file_problem = False
    db.session.commit()
    return f"{media.title} already has a new file; nothing more to do."


def replace(state) -> str:
    """Blocklist the release, delete the file, and search again."""
    e = _enrichment()
    part, media = state.part, state.media
    if _dry_run(media):
        raise ValueError(
            "dry run is on for this library, so Curatarr won't delete the file. "
            "Turn dry run off, or replace it yourself"
        )
    blocklisted = None
    # One part of a release split in several files: the same release's other
    # parts, and other split releases, would come straight back.
    split = any(
        "split into parts" in (issue.get("text") or "")
        for issue in (state.file_check or {}).get("issues") or []
    ) and not re.search(  # "Deathly Hallows – Part 2": every release says so
        r"(?i)\b(part|pt|cd|disc|disk)[ ._\-]?\d", media.title or ""
    )
    label = f"curatarr-split-{media.radarr_id or media.sonarr_id}"
    name = f"Curatarr: whole {media.title}, not one part"
    if part.kind == "movie_file":
        if not media.radarr_id:
            raise ValueError("This film isn't in Radarr")
        radarr = client("radarr")
        if split:
            radarr.ignore_for_movie(media.radarr_id, label, SPLIT_RELEASE, name)
        movie = radarr.movie(media.radarr_id)
        current = (movie.get("movieFile") or {}).get("id")
        if part.arr_file_id and current and current != part.arr_file_id:
            return _already_replaced(state, media)
        grabs = radarr.grabs(media.radarr_id)
        if grabs and not _blocklisted_before(state):
            radarr.mark_failed(grabs[0]["id"])
            blocklisted = grabs[0].get("sourceTitle")
            _note_blocklisted(state, blocklisted)
        file_id = (movie.get("movieFile") or {}).get("id")
        from . import replacements

        replacements.watch(media, radarr, movie)  # until the new copy arrives
        if file_id:
            radarr.delete_movie_file(file_id)
        radarr.search_movie(media.radarr_id)
        service = "Radarr"
    else:
        if not part.sonarr_episode_id:
            raise ValueError("This episode isn't in Sonarr")
        sonarr = client("sonarr")
        if split and media.sonarr_id:
            sonarr.ignore_for_series(media.sonarr_id, label, SPLIT_RELEASE, name)
        current = (sonarr.episode(part.sonarr_episode_id) or {}).get("episodeFileId")
        if part.arr_file_id and current and current != part.arr_file_id:
            return _already_replaced(state, media)
        grabs = sonarr.episode_grabs(part.sonarr_episode_id)
        if grabs and not _blocklisted_before(state):
            sonarr.mark_failed(grabs[0]["id"])
            blocklisted = grabs[0].get("sourceTitle")
            _note_blocklisted(state, blocklisted)
        if part.arr_file_id:
            sonarr.delete_episode_file(part.arr_file_id)
        sonarr.episode_search([part.sonarr_episode_id])
        service = "Sonarr"
    at = utcnow().isoformat()
    blocklisted = blocklisted or ((state.file_check or {}).get("blocklisting") or None)
    if blocklisted is True:
        blocklisted = None
    state.file_check = {
        **{k: v for k, v in (state.file_check or {}).items() if k != "blocklisting"},
        "replaced": {"at": at, "blocklisted": blocklisted},
    }
    state.file_problem = False
    db.session.commit()
    audit(
        "file_replaced",
        media.id,
        f"Replaced {e.file_label(state)}: "
        + "; ".join(describe(state.file_check)).lower()
        + "."
        + (f" Blocklisted {blocklisted}." if blocklisted else ""),
        f"file-replaced:{state.id}:{at}",
        payload={"blocklisted": blocklisted, "kinds": _kinds(state.file_check)},
    )
    db.session.commit()
    message = f"Deleted the file and asked {service} to find another release."
    if split:
        message += f" {service} now ignores releases split into parts for this title."
    if blocklisted:
        message += " The old release is blocklisted so it won't come back."
    else:
        message += (
            f" {service} had no download record for it, so nothing was blocklisted."
        )
    return message


def dismiss(state) -> None:
    state.file_check = {**(state.file_check or {}), "dismissed": True}
    state.file_problem = False
    db.session.commit()


def recheck(state) -> None:
    state.file_checked_at = None
    db.session.commit()


def review() -> dict:
    """Files to act on, and how many have been checked."""
    to_act = (
        db.session.query(ChapterState)
        .filter(ChapterState.file_problem.is_(True))
        .order_by(ChapterState.file_checked_at.desc())
        .all()
    )
    return {"to_act": to_act, **counts()}


def counts() -> dict:
    problems, checked = db.session.query(
        func.count(case((ChapterState.file_problem.is_(True), 1))),
        func.count(ChapterState.file_checked_at),
    ).one()
    return {"problems_to_act": problems, "files_checked": checked}
