"""Watch a replaced film until its new copy arrives.

Replacing a film (no English audio, burned-in subtitles, a broken file)
deletes the file and asks Radarr for another. If that download fails, or the
film is unmonitored in Radarr, nothing tries again, and Jellyfin is left with
a title that won't play.

So before the file is deleted the film is monitored, and until a copy
arrives the worker checks it now and then: nothing on disk and nothing
downloading means another search, a few times over about a day. When a copy
arrives, monitoring goes back to how it was; when none does, Activity says so.
"""

from datetime import timedelta

from . import db
from .integrations import IntegrationError
from .models import utcnow

KEY = "replacements_watched"
SEARCH_EVERY = timedelta(hours=6)
SEARCHES = 3


def watch(media, radarr, movie) -> None:
    """Call before deleting a film's file to replace it."""
    from .services import set_setting, setting

    was_monitored = bool(movie.get("monitored"))
    if not was_monitored:
        radarr.set_monitored(media.radarr_id, True)
    watched = dict(setting(KEY, {}) or {})
    watched[str(media.radarr_id)] = {
        "media": media.id,
        "title": media.title,
        "was_monitored": was_monitored,
        "searched": [utcnow().isoformat()],
    }
    set_setting(KEY, watched)
    db.session.commit()


def _queued(radarr) -> set:
    rows = radarr.queue() or []
    if isinstance(rows, dict):
        rows = rows.get("records") or []
    return {row.get("movieId") for row in rows}


def sweep(now=None) -> int:
    """Follow up on every watched film; returns how many were searched again."""
    from .services import _as_datetime, audit, client, set_setting, setting

    watched = dict(setting(KEY, {}) or {})
    if not watched:
        return 0
    now = now or utcnow()
    radarr = client("radarr")
    try:
        queued = _queued(radarr)
    except IntegrationError:
        return 0
    searched = 0
    for radarr_id, entry in list(watched.items()):
        try:
            movie = radarr.movie(int(radarr_id))
        except IntegrationError:
            continue
        if movie.get("hasFile"):
            if not entry["was_monitored"]:
                radarr.set_monitored(int(radarr_id), False)
            del watched[radarr_id]
            audit(
                "replacement_arrived",
                entry["media"],
                f"The replacement copy of {entry['title']} arrived.",
                f"replacement-arrived:{radarr_id}:{entry['searched'][0]}",
            )
            continue
        if int(radarr_id) in queued:
            continue  # downloading
        last = _as_datetime(entry["searched"][-1]) or now
        if now - last < SEARCH_EVERY:
            continue
        if len(entry["searched"]) >= SEARCHES:
            del watched[radarr_id]
            audit(
                "replacement_failed",
                entry["media"],
                f"No replacement copy of {entry['title']} arrived after "
                f"{SEARCHES} searches, so it has no file now. It stays monitored "
                "in Radarr, which will take one if it appears.",
                f"replacement-failed:{radarr_id}:{entry['searched'][0]}",
                state="FAILED_FINAL",
            )
            continue
        radarr.set_monitored(int(radarr_id), True)
        radarr.search_movie(int(radarr_id))
        entry["searched"].append(now.isoformat())
        searched += 1
    set_setting(KEY, watched)
    db.session.commit()
    return searched
