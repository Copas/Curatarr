"""Films with no English audio that can't be put right: Leaving Soon.

Radarr's import lists add films by what's popular or acclaimed, whatever their
language, and a quality profile's "not English" rule only judges release
names: a release that doesn't name its language counts as English. So a film
in another language can arrive with no English audio. Curatarr's dub check
hears that, and (with Replace films with no English audio on) looks for an
English copy. When none can be found, this deletes the film through Radarr
straight away (approved like Delete in Review, so Never Purge and dry run
still apply), and adds a Radarr import-list exclusion so no list adds it
back. Films it used to mark Leaving Soon instead are deleted too.

Left alone: films on a quality profile that doesn't reject other languages
(a profile kept for foreign films on purpose), films someone has watched or
favourited, and films Curatarr's own dub page was told to keep. Settings →
Automatic, off by default.
"""

from . import db
from .integrations import IntegrationError
from .models import ChapterState, UserMediaState, utcnow


def enabled() -> bool:
    from .autofix import automatic

    return automatic("no_english")


def blocks_other_languages(profile: dict, formats: dict) -> bool:
    """Whether a Radarr quality profile scores down releases not in English:
    a custom format matching "not English" (a negated English language
    condition) with a negative score."""
    for item in profile.get("formatItems") or []:
        if (item.get("score") or 0) >= 0:
            continue
        spec_list = (formats.get(item.get("format")) or {}).get("specifications") or []
        for spec in spec_list:
            if spec.get("implementation") != "LanguageSpecification" or not spec.get(
                "negate"
            ):
                continue
            values = [f.get("value") for f in spec.get("fields") or []]
            if 1 in values:  # Radarr's id for English
                return True
    return False


def _watched(media) -> bool:
    return (
        db.session.query(UserMediaState.id)
        .filter(
            UserMediaState.media_identity_id == media.id,
            (UserMediaState.play_count > 0)
            | UserMediaState.favorite.is_(True)
            | UserMediaState.last_played_at.isnot(None),
        )
        .first()
        is not None
    )


def due() -> list:
    """Dub-check states of films heard to have no English, whose automatic
    search for an English copy found none, not yet handled here."""
    from . import better_copy

    rows = db.session.query(ChapterState).filter(ChapterState.dub_result.isnot(None))
    found = []
    for state in rows:
        result = state.dub_result or {}
        if result.get("verdict") != "no_english" or result.get("english_only"):
            continue
        if result.get("replaced") or not better_copy.was_automatic(state, "dub"):
            continue  # replaced, or someone chose by hand on the dub page
        if better_copy.status(state, "dub") != "no_better":
            continue
        media = state.media
        if media is None or media.media_type != "movie" or media.missing_since:
            continue
        found.append(state)
    return found


WHERE = "no English audio, and no English copy"


def _delete(media, candidate=None) -> str:
    """Approve the film's deletion now (the worker carries it out within a
    minute); its Leaving Soon entry is made first if it hasn't one."""
    from .lifecycle import _active_candidate, request_removal, review_candidate

    candidate = candidate or _active_candidate(media.id)
    if candidate and candidate.state in {"APPROVED", "EXECUTING"}:
        return "already being deleted"
    if not (candidate and candidate.state == "LEAVING_SOON"):
        candidate = request_removal(media, where=WHERE)
    review_candidate(candidate.id, "delete")
    return "deleted"


def _earlier() -> int:
    """Films this marked Leaving Soon before it deleted them outright: deleted
    now. Returns how many."""
    from .models import PurgeCandidate
    from .services import audit

    done = 0
    for candidate in (
        db.session.query(PurgeCandidate)
        .filter(
            PurgeCandidate.state == "LEAVING_SOON",
            PurgeCandidate.reason_code == "requested",
            PurgeCandidate.reason_text == f"Marked Leaving Soon from {WHERE}.",
        )
        .all()
    ):
        media = candidate.media
        if media is None:
            continue
        try:
            _delete(media, candidate)
        except ValueError:
            continue
        audit(
            "no_english_deleted",
            media.id,
            f"{media.title} has no English audio and no English copy could be "
            "found: deleted through Radarr.",
            f"no-english-delete:{candidate.id}",
            candidate_id=candidate.id,
        )
        done += 1
    db.session.commit()
    return done


def sweep() -> int:
    """Worker cycle: delete such films. Returns how many."""
    if not enabled():
        return 0
    done = _earlier()
    states = due()
    if not states:
        return done
    from .services import audit, client

    try:
        radarr = client("radarr")
        formats = {f["id"]: f for f in radarr.custom_formats() or []}
    except IntegrationError:
        return 0
    profiles: dict = {}
    marked = 0
    for state in states:
        media = state.media
        outcome = "kept"
        try:
            movie = radarr.movie(media.radarr_id) if media.radarr_id else None
            profile_id = (movie or {}).get("qualityProfileId")
            if profile_id not in profiles:
                profiles[profile_id] = (
                    radarr.quality_profile(profile_id) if profile_id else {}
                )
            if not blocks_other_languages(profiles[profile_id] or {}, formats):
                outcome = "profile allows other languages"
            elif _watched(media):
                outcome = "watched or favourited"
            else:
                # Excluded first: a film already Leaving Soon (or Never Purge,
                # which keeps it) still shouldn't come back from a list.
                if media.tmdb_id:
                    try:
                        radarr.add_exclusion(
                            media.tmdb_id, media.title, (movie or {}).get("year")
                        )
                    except IntegrationError:
                        pass  # already excluded, or Radarr refused: still leaving
                outcome = _delete(media)
                audit(
                    "no_english_deleted",
                    media.id,
                    f"{media.title} has no English audio and no English copy "
                    "could be found: deleted through Radarr, and Radarr's import "
                    "lists won't add it again.",
                    f"no-english:{media.id}",
                )
                marked += 1
        except IntegrationError as exc:
            if exc.status_code != 404:
                continue  # Radarr unreachable: next time
            outcome = "no longer in Radarr"
        except ValueError as exc:  # Never Purge, already leaving
            outcome = str(exc)
        state.dub_result = {
            **(state.dub_result or {}),
            "english_only": {"at": utcnow().isoformat(), "outcome": outcome},
        }
        db.session.commit()
    return marked + done
