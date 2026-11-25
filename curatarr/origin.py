"""Where a title came from, and why its current copy is here (its page).

Radarr and Sonarr record when a title was added but not how, so the answer is
put together from what does know: Seerr (who asked for it), the import lists
(which ones have it now), Curatarr's own Activity (Find Favorites, seasonal
returns, new seasons for shows people follow, replaced files) and, for a
film, Radarr's download history (what found this copy). Whatever can't be
reached is left out rather than guessed.
"""

from datetime import timedelta

from flask import current_app

from . import db
from .integrations import IntegrationError
from .models import ChapterState, LifecycleAction, MediaPart
from .services import _as_datetime, client, configured

# Radarr's names for what started a download.
SOURCES = {
    "RssSync": "Radarr spotted it among new releases",
    "Search": "Radarr's automatic search found it",
    "UserInvokedSearch": "someone started a search for it",
    "InteractiveSearch": "someone picked it from Radarr's search results",
    "ReleasePush": "another app sent it to Radarr",
}
REPLACED = ("file_replaced", "dub_replaced", "wrong_version_replaced")
# A file first seen this long before the current one arrived was an earlier copy.
SEEN_BEFORE = timedelta(hours=1)
LISTS_FRESH_FOR = timedelta(minutes=10)
# Recording where titles came from (record, sweep): a few each worker cycle,
# newest first. Recorded within a day of arriving, the lists and requests
# still say what brought it; later, it's today's best answer.
RECORD_BATCH = 40
RECORD_SECONDS = 20
ON_ARRIVAL = timedelta(days=1)
HAND = "Added by hand"
UNKNOWN = "Requested or added by hand"  # Seerr not connected: can't tell
# Looked at long after it was added: an import list that brought it may have
# dropped it since (trending and upcoming lists turn over), so "by hand"
# would be a guess.
LATE = "Can't tell"


def describe(media) -> dict:
    """{"app", "added", "reasons": [{"text", "at"}], "guess", "this_copy"}.

    Not "copy": in a template, o.copy finds the dict's copy() method."""
    film = media.media_type == "movie"
    app_name = "Radarr" if film else "Sonarr"
    result = {
        "app": app_name,
        "added": None,
        "reasons": [],
        "guess": None,
        "this_copy": None,
    }
    arr_item, arr = _arr_item(media, film)
    if arr_item:
        result["added"] = _as_datetime(arr_item.get("added"))
    reasons = result["reasons"]
    for request in _requests(media):
        who = request.get("user") or "someone"
        reasons.append(
            {
                "text": f"Requested in Seerr by {who}",
                "at": _as_datetime(request.get("at")),
            }
        )
    for action in _curatarr_adds(media):
        reasons.append(
            {"text": action.reason_text, "at": _as_datetime(action.created_at)}
        )
    lists = _lists(media, film, arr, arr_item)
    if lists:
        reasons.append(
            {
                "text": f"On {app_name}'s import list{'s' if len(lists) > 1 else ''}: "
                + ", ".join(lists),
                "at": None,
            }
        )
    elif lists is None and _listed_at_last_sync(media):
        reasons.append(
            {"text": f"Added by one of {app_name}'s import lists", "at": None}
        )
    if not reasons and not configured("seerr"):
        # Without Seerr a request can't be ruled out: "by hand" would be a guess.
        result["guess"] = (
            f"Not on any of {app_name}'s import lists and not added by Curatarr. "
            "Seerr isn't connected to Curatarr, so whether someone requested it "
            f"there can't be told; otherwise it was added in {app_name} by hand."
        )
        result["unknown"] = True
    elif not reasons and _late(media, result["added"]):
        result["guess"] = (
            f"Added to {app_name} more than a day before Curatarr looked. It isn't "
            f"requested in Seerr or on any of {app_name}'s import lists now, but "
            "lists change: one may have brought it and dropped it since, or it "
            "was added by hand."
        )
        result["late"] = True
    elif not reasons:
        result["guess"] = (
            f"Not requested in Seerr, not on any of {app_name}'s import lists, and "
            f"not added by Curatarr, so most likely added in {app_name} by hand "
            f"(or it was already in the library when {app_name} took it over)."
        )
    if not film:
        follow = _latest(media, ("sonarr_season_search",))
        if follow:
            reasons.append(
                {"text": follow.reason_text, "at": _as_datetime(follow.created_at)}
            )
    else:
        result["this_copy"] = _copy(media, arr, arr_item)
    kept = media.origin or {}
    result["recorded"] = (
        {
            "short": kept.get("short"),
            "on_arrival": kept.get("on_arrival"),
            "at": _as_datetime(kept.get("recorded_at")),
        }
        if kept.get("short")
        else None
    )
    return result


def short(info) -> str | None:
    """Where it came from in a few words: the import list's name, who asked,
    what Curatarr did, or "Added by hand"."""
    for reason in info["reasons"]:
        text = reason["text"]
        if "import list" in text and ": " in text:
            return text.split(": ", 1)[1]
        if text.startswith("Requested in Seerr by "):
            return "Requested by " + text.removeprefix("Requested in Seerr by ")
        if text.startswith("Added by one of"):
            return "An import list"
    if info["reasons"]:
        text = info["reasons"][0]["text"]
        return text if len(text) <= 60 else text[:57].rstrip() + "…"
    if info.get("unknown"):
        return UNKNOWN
    if info.get("late"):
        return LATE
    if info["guess"]:
        return HAND
    return None


def record(media, now=None) -> bool:
    """Work out where a title came from and keep it. False (nothing kept,
    tried again later) when Radarr or Sonarr couldn't be asked: without them
    "added by hand" would be a guess."""
    now = now or _now()
    info = describe(media)
    if info["added"] is None:
        return False
    media.origin = {
        "short": short(info),
        "reasons": [
            {"text": r["text"], "at": r["at"].isoformat() if r.get("at") else None}
            for r in info["reasons"]
        ],
        "guess": info["guess"],
        "added": info["added"].isoformat(),
        "recorded_at": now.isoformat(),
        "on_arrival": now - info["added"] <= ON_ARRIVAL,
    }
    return True


def sweep(now=None) -> int:
    """Record where the titles without a record came from, newest first, a
    few at a time; returns how many were recorded."""
    import time

    from .models import MediaIdentity

    started, done = time.monotonic(), 0
    rows = (
        db.session.query(MediaIdentity)
        .filter(MediaIdentity.origin.is_(None))
        .order_by(MediaIdentity.added_at.desc())
        .limit(RECORD_BATCH)
        .all()
    )
    for media in rows:
        if time.monotonic() - started > RECORD_SECONDS:
            break
        try:
            done += record(media, now)
        except (IntegrationError, OSError, ValueError, TypeError):
            continue
    db.session.commit()
    return done


def _arr_item(media, film):
    kind, arr_id = ("radarr", media.radarr_id) if film else ("sonarr", media.sonarr_id)
    if not arr_id or not configured(kind):
        return None, None
    try:
        arr = client(kind)
        path = f"/api/v3/{'movie' if film else 'series'}/{arr_id}"
        item = arr.request("GET", path)
        return (item if isinstance(item, dict) else None), arr
    except IntegrationError:
        return None, None


def _requests(media):
    from .curation import _context

    return _context()["requests"].get(media.id) or []


def _curatarr_adds(media):
    """Find Favorites adding a film, or a seasonal title added back."""
    found = (
        db.session.query(LifecycleAction)
        .filter(
            LifecycleAction.media_identity_id == media.id,
            LifecycleAction.action_type == "film_added",
            LifecycleAction.state == "SUCCEEDED",
        )
        .all()
    )
    if media.tmdb_id:
        found += [
            action
            for action in db.session.query(LifecycleAction).filter(
                LifecycleAction.action_type == "seasonal_returned"
            )
            if (action.payload_json or {}).get("tmdb_id") == media.tmdb_id
        ]
    return sorted(found, key=lambda a: _as_datetime(a.created_at))


def _lists(media, film, arr, arr_item) -> list[str] | None:
    """Names of the import lists that have the title now; None when they
    can't be read."""
    if arr is None or arr_item is None:
        return None
    cache = current_app.extensions.setdefault("curatarr_origin_lists", {})
    kind = "radarr" if film else "sonarr"
    entry = cache.get(kind)
    now = _now()
    try:
        if not entry or now - entry["at"] > LISTS_FRESH_FOR:
            lists = arr.import_lists() or []
            listed = {}
            if film:
                for movie in arr.list_movies() or []:
                    if movie.get("tmdbId"):
                        listed.setdefault(int(movie["tmdbId"]), set()).update(
                            movie.get("lists") or []
                        )
            entry = cache[kind] = {"at": now, "lists": lists, "listed": listed}
    except (IntegrationError, TypeError, ValueError):
        return None
    names = {
        entry_["id"]: entry_.get("name") for entry_ in entry["lists"] if "id" in entry_
    }
    if film:
        ids = entry["listed"].get(int(arr_item.get("tmdbId") or 0), set())
        return sorted(names[i] for i in ids if names.get(i))
    tags = set(arr_item.get("tags") or [])
    return sorted(
        lst.get("name")
        for lst in entry["lists"]
        if lst.get("name") and tags & set(lst.get("tags") or [])
    )


def _late(media, added) -> bool:
    """Added more than a day before now, with nothing recorded on arrival
    that already said "by hand" (that answer was made while the lists still
    showed what brought it)."""
    kept = media.origin or {}
    if kept.get("on_arrival") and kept.get("short") == HAND:
        return False
    return bool(added) and _now() - added > ON_ARRIVAL


def _listed_at_last_sync(media) -> bool:
    from .curation import _context

    return media.id in _context()["lists"]


def _latest(media, types):
    return (
        db.session.query(LifecycleAction)
        .filter(
            LifecycleAction.media_identity_id == media.id,
            LifecycleAction.action_type.in_(types),
            LifecycleAction.state == "SUCCEEDED",
        )
        .order_by(LifecycleAction.created_at.desc())
        .first()
    )


def _copy(media, arr, movie) -> dict | None:
    """When the film's current file arrived, whether it replaced another,
    and what found it."""
    part = next((p for p in media.parts if p.kind == "movie_file" and p.has_file), None)
    if part is None or not part.acquired_at:
        return None
    arrived = _as_datetime(part.acquired_at)
    replaced = _latest(media, REPLACED)
    if (
        replaced
        and _as_datetime(replaced.created_at) <= arrived + timedelta(days=1)
        and (_as_datetime(replaced.created_at) >= arrived - timedelta(days=14))
    ):
        why = f"Curatarr replaced the copy before it. {replaced.reason_text}"
    elif seen_before(part):
        why = (
            "It replaced an earlier copy: an upgrade by Radarr, or a copy "
            "someone picked."
        )
    elif arrived >= _first_scan():
        why = "The first copy in the library."
    else:
        why = ""  # arrived before Curatarr was looking: it can't tell
    source = release = None
    if arr is not None and movie:
        try:
            grab = arr.current_grab(movie)
        except IntegrationError:
            grab = False
        if grab:
            data = grab.get("data") or {}
            said = SOURCES.get(data.get("releaseSource"))
            source = (
                (said[0].upper() + said[1:] if said else "Downloaded")
                + (f" on {data['indexer']}" if data.get("indexer") else "")
                + "."
            )
            release = grab.get("sourceTitle")
        elif grab is None:
            source = (
                "No download in Radarr's history matches this file: it was "
                "imported by hand, or arrived before that history begins."
            )
    return {"arrived": arrived, "why": why, "source": source, "release": release}


def _first_scan():
    """When Curatarr first looked at any file: before then it can't know
    whether a copy replaced another."""
    from sqlalchemy import func

    first = db.session.query(func.min(ChapterState.created_at)).scalar()
    return _as_datetime(first) or _now()


def seen_before(part) -> bool:
    """Whether Curatarr had already looked at a file in this place well
    before the current one arrived: the current file is a later copy."""
    first = (
        db.session.query(ChapterState.created_at)
        .filter(ChapterState.media_part_id == part.id)
        .scalar()
    )
    arrived = _as_datetime(part.acquired_at)
    return bool(first and arrived and _as_datetime(first) < arrived - SEEN_BEFORE)


def first_copies(parts) -> list[MediaPart]:
    """Of these files, the ones that are the first in their place (not a
    replacement or an upgrade): what's new to the library."""
    ids = [p.id for p in parts]
    first_seen = (
        dict(
            db.session.query(ChapterState.media_part_id, ChapterState.created_at)
            .filter(ChapterState.media_part_id.in_(ids))
            .all()
        )
        if ids
        else {}
    )
    new = []
    for part in parts:
        seen = _as_datetime(first_seen.get(part.id))
        arrived = _as_datetime(part.acquired_at)
        if not (seen and arrived and seen < arrived - SEEN_BEFORE):
            new.append(part)
    return new


def _now():
    from .models import utcnow

    return utcnow()
