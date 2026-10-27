"""Film collections with gaps, and suggestions that fit what the household likes.

Both come from Radarr: its collections (TMDB's film series, such as "Saw
Collection") list every film in a series and whether it's in the library, and
its recommendations are films TMDB links to the ones already there. Curatarr
adds what Radarr doesn't know: which films the household has favourited in
Jellyfin, so suggestions are ranked by how well their genres match those.

Adding a film goes through Radarr with the quality profile and root folder of
the series' films already in the library (or the library's most common ones),
and starts a search. "Follow" turns on Radarr's own collection monitoring, so
later films in the series are added when they're released.
"""

import time
from collections import Counter
from datetime import UTC, datetime

from . import db
from .models import MediaIdentity, UserMediaState
from .services import audit, client, set_setting, setting

CACHE_SECONDS = 600
SUGGESTIONS = 24
_cache: dict = {}


def _cached(key, fetch):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = fetch()
    _cache[key] = (time.monotonic(), value)
    return value


def forget():
    _cache.clear()


def _dismissed() -> dict:
    found = setting("franchise_dismissed", {}) or {}
    return {
        "collections": set(found.get("collections", [])),
        "films": set(found.get("films", [])),
    }


def dismiss(kind: str, tmdb_id: int) -> None:
    if kind not in {"collections", "films"}:
        raise ValueError("Unknown kind")
    found = setting("franchise_dismissed", {}) or {}
    ids = set(found.get(kind, [])) | {int(tmdb_id)}
    set_setting("franchise_dismissed", found | {kind: sorted(ids)})
    db.session.commit()
    forget()


def _released(film, today) -> bool:
    if film.get("status") == "released":
        return True
    for key in ("digitalRelease", "physicalRelease"):
        when = film.get(key)
        if when and when[:10] <= today:
            return True
    return False


def _poster(film):
    for image in film.get("images") or []:
        if image.get("coverType") == "poster":
            return image.get("remoteUrl") or image.get("url")
    return film.get("remotePoster")


def _film(film) -> dict:
    ratings = film.get("ratings") or {}
    rating = (ratings.get("tmdb") or ratings.get("imdb") or {}).get("value")
    return {
        "tmdb_id": film.get("tmdbId"),
        "title": film.get("title"),
        "year": film.get("year"),
        "rating": round(rating, 1) if rating else None,
        "runtime": film.get("runtime"),
        "genres": film.get("genres") or [],
        "overview": film.get("overview") or "",
        "poster": _poster(film),
    }


def gaps() -> list[dict]:
    """Collections the library has some but not all released films of."""
    raw = _cached("collections", lambda: client("radarr").collections() or [])
    dismissed = _dismissed()
    today = datetime.now(UTC).date().isoformat()
    found = []
    for collection in raw:
        if collection.get("tmdbId") in dismissed["collections"]:
            continue
        films = collection.get("movies") or []
        have = [f for f in films if f.get("isExisting")]
        if not have:
            continue
        missing, coming = [], []
        for film in films:
            if film.get("isExisting") or film.get("isExcluded"):
                continue
            if film.get("tmdbId") in dismissed["films"]:
                continue
            (missing if _released(film, today) else coming).append(_film(film))
        if not missing:
            continue
        total = len(have) + len(missing)
        found.append(
            {
                "id": collection.get("id"),
                "tmdb_id": collection.get("tmdbId"),
                "title": collection.get("title") or "",
                "have": len(have),
                "total": total,
                "missing": sorted(missing, key=lambda f: f["year"] or 0),
                "coming": sorted(coming, key=lambda f: f["year"] or 0),
                "following": bool(collection.get("monitored")),
            }
        )
    # Nearly complete series first: they're the ones a gap stands out in.
    found.sort(key=lambda c: (-c["have"] / c["total"], -c["have"], c["title"]))
    return found


def _favourite_genres(library_movies) -> Counter:
    """Genres of the films anyone in the household has favourited."""
    favourite_ids = {
        row.radarr_id
        for row in db.session.query(MediaIdentity.radarr_id)
        .join(UserMediaState, UserMediaState.media_identity_id == MediaIdentity.id)
        .filter(UserMediaState.favorite.is_(True), MediaIdentity.radarr_id.isnot(None))
    }
    genres = Counter()
    for movie in library_movies:
        if movie.get("id") in favourite_ids:
            genres.update(movie.get("genres") or [])
    return genres


def suggestions(limit=SUGGESTIONS) -> dict:
    """Radarr's recommendations, ranked by fit with the household's favourites."""
    radarr = client("radarr")
    library = _cached("movies", lambda: radarr.movies() or [])
    raw = _cached("recommendations", lambda: radarr.recommendations() or [])
    genres = _favourite_genres(library)
    weight = sum(genres.values()) or 1
    dismissed = _dismissed()["films"]
    today = datetime.now(UTC).date().isoformat()
    ranked = []
    for film in raw:
        if film.get("isExisting") or film.get("isExcluded"):
            continue
        if film.get("tmdbId") in dismissed or not _released(film, today):
            continue
        item = _film(film)
        fit = sum(genres[g] for g in item["genres"]) / weight
        item["matches"] = [g for g in item["genres"] if genres[g]][:3]
        item["score"] = fit + (item["rating"] or 0) / 10
        ranked.append(item)
    ranked.sort(key=lambda f: -f["score"])
    return {"films": ranked[:limit], "genres": [g for g, _ in genres.most_common(5)]}


def _placement(radarr, collection_tmdb_id=None) -> tuple[int, str]:
    """Quality profile and root folder for a new film: those of its series'
    films already in the library, or else the library's most common ones."""
    library = _cached("movies", lambda: radarr.movies() or [])
    pool = [
        m
        for m in library
        if collection_tmdb_id
        and (m.get("collection") or {}).get("tmdbId") == collection_tmdb_id
    ] or library
    profiles = Counter(
        m.get("qualityProfileId") for m in pool if m.get("qualityProfileId")
    )
    roots = Counter(m.get("rootFolderPath") for m in pool if m.get("rootFolderPath"))
    if not profiles or not roots:
        raise ValueError("Radarr has no films yet to copy a quality profile from")
    return profiles.most_common(1)[0][0], roots.most_common(1)[0][0]


def add(tmdb_ids, collection_tmdb_id=None, because=None) -> list[str]:
    """Add films to Radarr and search for them. Returns the titles added.
    ``because`` finishes History's sentence ("… because Sam favourited Saw")."""
    radarr = client("radarr")
    profile, root = _placement(radarr, collection_tmdb_id)
    added = []
    for tmdb_id in tmdb_ids:
        film = radarr.lookup_tmdb(int(tmdb_id))
        if not film or film.get("id"):
            continue  # unknown to TMDB, or already in Radarr
        radarr.add_movie(
            film
            | {
                "qualityProfileId": profile,
                "rootFolderPath": root,
                "monitored": True,
                "minimumAvailability": "released",
                "addOptions": {"searchForMovie": True},
            }
        )
        title = f"{film.get('title')} ({film.get('year')})"
        added.append(title)
        audit(
            "film_added",
            None,
            f"Added {title} to Radarr and started a search"
            + (f", because {because}." if because else "."),
            f"film-added:{tmdb_id}:{int(time.time())}",
            payload={"tmdb_id": int(tmdb_id), "collection": collection_tmdb_id},
        )
    db.session.commit()
    forget()
    return added


def follow(collection_id: int) -> str:
    """Turn on Radarr's monitoring for a collection, so later films are added."""
    radarr = client("radarr")
    collection = radarr.collection(collection_id)
    profile, root = _placement(radarr, collection.get("tmdbId"))
    radarr.update_collection(
        collection
        | {
            "monitored": True,
            "searchOnAdd": True,
            "qualityProfileId": profile,
            "rootFolderPath": root,
        }
    )
    forget()
    title = collection.get("title") or "the collection"
    audit(
        "collection_followed",
        None,
        f"Following {title}: Radarr adds new films in it when they're released.",
        f"collection-followed:{collection_id}:{int(time.time())}",
        payload={"collection": collection.get("tmdbId")},
    )
    db.session.commit()
    return title


# --- Find Favorites -------------------------------------------------------------


def _names(user_ids) -> str:
    from .services import account_names

    names = account_names()
    known = sorted({names.get(i) for i in user_ids if names.get(i)})
    if not known:
        return "someone"
    return known[0] if len(known) == 1 else ", ".join(known[:-1]) + " and " + known[-1]


FOUND_KEY = "find_favourites_added"  # TMDB ids Find Favorites has added, never again
FIND_PER_RUN = 5  # films added per reconciliation, so a long series can't flood


def find_favourites() -> list[str]:
    """When anyone favourites a film in a series (a TMDB collection in Radarr),
    add the series' missing released films with the favourite's quality profile
    and folder, and search for them. Films excluded in Radarr (deleting one in
    Jellyfin excludes it), dismissed on the film series page, or added by Find
    Favorites before (then removed some other way) are left out. Settings →
    Household rules, off by default. Returns the titles added."""
    from . import curation

    if not curation.options()["find_favourites"]:
        return []
    favourites = {}  # TMDB id -> (title, who favourited it)
    rows = (
        db.session.query(MediaIdentity, UserMediaState.jellyfin_user_id)
        .join(UserMediaState, UserMediaState.media_identity_id == MediaIdentity.id)
        .filter(
            UserMediaState.favorite.is_(True),
            MediaIdentity.media_type == "movie",
            MediaIdentity.tmdb_id.isnot(None),
        )
    )
    for media, user_id in rows:
        title, who = favourites.get(media.tmdb_id, (media.title, []))
        favourites[media.tmdb_id] = (title, who + [user_id])
    if not favourites:
        return []
    raw = _cached("collections", lambda: client("radarr").collections() or [])
    dismissed = _dismissed()
    done = set(setting(FOUND_KEY, []) or [])
    today = datetime.now(UTC).date().isoformat()
    wanted = []  # (collection TMDB id, film TMDB id, because)
    for collection in raw:
        if collection.get("tmdbId") in dismissed["collections"]:
            continue
        films = collection.get("movies") or []
        liked = [
            favourites[f["tmdbId"]] for f in films if f.get("tmdbId") in favourites
        ]
        if not liked:
            continue
        title, who = liked[0]
        because = f"{_names(who)} favourited {title}"
        for film in films:
            tmdb = film.get("tmdbId")
            if (
                not tmdb
                or film.get("isExisting")
                or film.get("isExcluded")
                or tmdb in dismissed["films"]
                or tmdb in done
                or not _released(film, today)
            ):
                continue
            wanted.append((collection.get("tmdbId"), tmdb, because))
    added = []
    for collection_tmdb, tmdb, because in wanted[:FIND_PER_RUN]:
        added += add([tmdb], collection_tmdb, because=because)
        done.add(tmdb)  # tried once, added or not (TMDB may not know it)
    if wanted:
        set_setting(FOUND_KEY, sorted(done))
        db.session.commit()
    return added
