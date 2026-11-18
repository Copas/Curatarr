"""What a title is and who's watched it, for its page (templates/title.html).

About: the plot, genres, year, age rating, community rating, length,
tagline and studios, from Jellyfin, kept a while so the page doesn't wait on
Jellyfin each time; the genres Sonarr or Radarr gave when Jellyfin can't be
reached.

Watching: each account's progress (a show's episodes played, a film played or
not, how often), favourites and when they last watched, and a show's most
recently watched episodes. Curatarr keeps the latest play per person and
episode (from Jellyfin), not a log of every play, so that's what it shows.
"""

import time

from flask import current_app

from . import db
from .models import EpisodeUserState, MediaPart, UserMediaState
from .services import IntegrationError, account_names

ABOUT_FRESH_FOR = 1800  # s: a plot or rating rarely changes
ABOUT_KEPT = 300  # titles, in memory
RECENT = 8  # a show's most recently watched episodes listed


def about(media) -> dict:
    """{"overview", "genres", "year", "rating", "score", "minutes", "tagline",
    "studios", "status", "years", "reached"}; whatever Jellyfin doesn't say is
    missing."""
    kept = current_app.extensions.setdefault("curatarr_about", {})
    now = time.monotonic()
    found = kept.get(media.id)
    if found and now - found[0] < ABOUT_FRESH_FOR:
        return found[1]
    item, reached = {}, False
    if media.jellyfin_id:
        try:
            from .services import client

            item = client("jellyfin").item_about(media.jellyfin_id) or {}
            reached = True
        except (IntegrationError, OSError, ValueError, AttributeError):
            item = {}
    info = {
        "overview": (item.get("Overview") or "").strip(),
        "genres": item.get("Genres") or list(media.genres or []),
        "year": item.get("ProductionYear") or media.year,
        "rating": item.get("OfficialRating"),
        "score": item.get("CommunityRating"),
        "minutes": round(item["RunTimeTicks"] / 600_000_000)
        if item.get("RunTimeTicks")
        else None,
        "tagline": (item.get("Taglines") or [None])[0],
        "studios": [s.get("Name") for s in item.get("Studios") or [] if s.get("Name")],
        "status": item.get("Status"),  # a show: Continuing or Ended
        "years": _years(item),
        "reached": reached,  # False: Jellyfin couldn't be asked
    }
    if item:  # don't keep a miss: Jellyfin may be back in a moment
        if len(kept) >= ABOUT_KEPT:
            kept.pop(next(iter(kept)))
        kept[media.id] = (now, info)
    return info


def _years(item) -> str | None:
    """A show's run: "2019–2023", or "2019–" while it's still going."""
    start, end = item.get("PremiereDate"), item.get("EndDate")
    if not start:
        return None
    first = start[:4]
    if item.get("Status") == "Continuing":
        return f"{first}–"
    if end and end[:4] != first:
        return f"{first}–{end[:4]}"
    return first


def watching(media) -> dict:
    """{"people": [{"name", "played", "of", "last", "favorite", "times"}],
    "recent": [{"name", "season", "episode", "at"}], "episodes": count}.
    people are those who've played it or favourited it, most recent first."""
    names = account_names()
    series = media.media_type == "series"
    episodes = (
        db.session.query(MediaPart)
        .filter_by(media_identity_id=media.id, kind="episode", has_file=True)
        .count()
        if series
        else 0
    )
    played = {}
    if series:
        rows = (
            db.session.query(EpisodeUserState.jellyfin_user_id, db.func.count())
            .join(MediaPart, EpisodeUserState.media_part_id == MediaPart.id)
            .filter(
                MediaPart.media_identity_id == media.id,
                MediaPart.kind == "episode",
                EpisodeUserState.played.is_(True),
            )
            .group_by(EpisodeUserState.jellyfin_user_id)
        )
        played = dict(rows)
    people = []
    for state in db.session.query(UserMediaState).filter_by(media_identity_id=media.id):
        if state.jellyfin_user_id not in names:
            continue
        count = played.get(state.jellyfin_user_id, 0)
        watched = (
            count if series else (1 if state.last_played_at or state.play_count else 0)
        )
        if not (watched or state.last_played_at or state.favorite):
            continue
        people.append(
            {
                "name": names[state.jellyfin_user_id],
                "played": watched,
                "of": episodes if series else 1,
                "last": state.last_played_at,
                "favorite": state.favorite,
                "times": state.play_count,
            }
        )
    people.sort(
        key=lambda p: (p["last"] is None, -(p["last"].timestamp() if p["last"] else 0))
    )
    recent = []
    if series:
        rows = (
            db.session.query(EpisodeUserState, MediaPart)
            .join(MediaPart, EpisodeUserState.media_part_id == MediaPart.id)
            .filter(
                MediaPart.media_identity_id == media.id,
                EpisodeUserState.last_played_at.isnot(None),
            )
            .order_by(EpisodeUserState.last_played_at.desc())
            .limit(RECENT * 3)
        )
        for state, part in rows:
            if state.jellyfin_user_id not in names:
                continue
            recent.append(
                {
                    "name": names[state.jellyfin_user_id],
                    "season": part.season_number,
                    "episode": part.episode_number,
                    "at": state.last_played_at,
                    "finished": state.played,
                }
            )
            if len(recent) == RECENT:
                break
    return {"people": people, "recent": recent, "episodes": episodes}
