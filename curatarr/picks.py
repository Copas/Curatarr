"""Picks for each person: a Jellyfin playlist of unwatched films they'd like.

Each account gets a private playlist, "Picks for <name>", which shows on
every Jellyfin app, Roku included. It's made from what that account has
favourited and finished:

- Each film is described by its genres and TMDB keywords, weighted by how
  rare they are in the library (a keyword most films carry says little).
- A candidate scores by how close it is to the films the person liked: the
  mean of its closest few, so it takes several to agree, and one much-loved
  film can't fill the list with lookalikes. A good rating counts a little.
- Each liked film explains at most two picks, and no genre takes more than
  four places, so the list has some range; when that leaves it short, the
  best of the rest fill it.

Candidates come from Jellyfin, asked as that person: only films in their
libraries, within their parental rating, not played and not already started
(those are in Continue Watching). Films finished come off the list within a
few minutes. Once a week the list is made again; a film that stayed on it
unplayed counts for less each week, so others get a turn.

Accounts hidden from Jellyfin's sign-in screen (an admin account, a spare
one) never get a list. Accounts with fewer than MIN_LIKED films to go on get
no playlist until they have them.
"""

import json
import math
from collections import Counter
from datetime import timedelta

from . import db
from .integrations import IntegrationError
from .models import MediaIdentity, UserMediaState, utcnow

KEY = "picks"  # {user id: {"playlist", "items", "built_at", "shown", "because"}}
COUNT = 10
MIN_LIKED = 5
REBUILD = timedelta(days=7)
CHECK_PLAYED = timedelta(minutes=10)
CLOSEST = 3  # liked films whose similarity is averaged
PER_LIKED = 2  # picks one liked film may explain
PER_GENRE = 4
COMMON = 0.2  # a keyword on more than this share of films says nothing
WEEKLY_FADE = 0.7  # a pick left unplayed counts this much less each week
NAME = "Picks for {name}"


def enabled() -> bool:
    from .curation import options

    return bool(options().get("picks", False))


def skipped_accounts() -> set[str]:
    from .curation import options

    return set(options().get("picks_skip") or [])


def offered_accounts() -> dict[str, str]:
    """{account id: name} that can have picks: those Curatarr's pages show,
    less accounts hidden from Jellyfin's sign-in screen (admin and spare
    accounts), whatever Curatarr shows."""
    from .services import account_names, setting

    hidden = {_norm(i) for i in setting("jellyfin_hidden_user_ids", []) or []}
    return {i: n for i, n in account_names().items() if _norm(i) not in hidden}


def eligible_accounts() -> set[str]:
    """Accounts that get picks: offered and not unticked."""
    skip = {_norm(i) for i in skipped_accounts()}
    return {_norm(i) for i in offered_accounts()} - skip


def _norm(item_id: str) -> str:
    return (item_id or "").replace("-", "").lower()


# --- Scoring ---------------------------------------------------------------------


def features(media: MediaIdentity) -> set[str]:
    def listed(value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return []
        return value or []

    return {f"g:{g}" for g in listed(media.genres)} | {
        f"k:{k}" for k in listed(media.keywords)
    }


def vectors(films: dict) -> dict:
    """{key: unit vector} from {key: features}: rare features weigh more,
    genres twice as much as keywords, features on one film or on most are
    left out."""
    count = len(films) or 1
    df = Counter(f for fs in films.values() for f in fs)
    result = {}
    for key, fs in films.items():
        v = {
            f: (2.0 if f.startswith("g:") else 1.0) * math.log(count / df[f])
            for f in fs
            if df[f] > 1 and (f.startswith("g:") or df[f] <= COMMON * count)
        }
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        result[key] = {f: x / norm for f, x in v.items()}
    return result


def _similarity(a: dict, b: dict) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(f, 0.0) for f, x in a.items())


def choose(liked: dict, candidates: dict, vecs: dict, ratings=None, shown=None):
    """[(key, score, because)] best first. ``liked`` is {key: weight};
    ``candidates`` {key: genres}; ``ratings`` {key: 0-10}; ``shown`` {key:
    weeks it stayed on the list unplayed}."""
    ratings, shown = ratings or {}, shown or {}
    scored = []
    for key, genres in candidates.items():
        v = vecs.get(key)
        if not v:
            continue
        near = sorted(
            (
                (weight * _similarity(v, vecs[m]), m)
                for m, weight in liked.items()
                if m in vecs
            ),
            reverse=True,
        )[:CLOSEST]
        if not near:
            continue
        score = sum(s for s, _m in near) / len(near)
        rating = ratings.get(key)
        if rating:
            score *= 0.7 + 0.3 * min(max((rating - 5) / 3, 0.0), 1.0)
        score *= WEEKLY_FADE ** shown.get(key, 0)
        # only liked films it's actually like can be what it's credited to
        scored.append((score, key, [m for sim, m in near if sim > 0], genres))
    scored.sort(key=lambda row: row[0], reverse=True)
    scored = [row for row in scored if row[0] > 0]
    picks, by_liked, by_genre = [], Counter(), Counter()
    for score, key, nearest, genres in scored:
        main = genres[0] if genres else None
        if main and by_genre[main] >= PER_GENRE:
            continue
        # credited to the closest liked film that hasn't explained enough yet
        because = next((m for m in nearest if by_liked[m] < PER_LIKED), None)
        if because is None:
            continue
        picks.append((key, score, because))
        by_liked[because] += 1
        if main:
            by_genre[main] += 1
        if len(picks) == COUNT:
            return picks
    # Too few with some range: the best of the rest fill the list.
    taken = {key for key, _s, _b in picks}
    for score, key, nearest, _genres in scored:
        if len(picks) == COUNT:
            break
        if key not in taken:
            picks.append((key, score, nearest[0]))
    picks.sort(key=lambda row: row[1], reverse=True)
    return picks


# --- What each account liked -----------------------------------------------------


def liked_films(user_id: str) -> dict:
    """{media id: weight}: favourite 3, finished 1, rewatched 1 more."""
    rows = (
        db.session.query(UserMediaState)
        .join(MediaIdentity, UserMediaState.media_identity_id == MediaIdentity.id)
        .filter(
            UserMediaState.jellyfin_user_id == _norm(user_id),
            MediaIdentity.media_type == "movie",
        )
    )
    liked = {}
    for row in rows:
        plays = row.play_count or 0
        weight = (3 if row.favorite else 0) + (plays > 0) + (plays > 1)
        if weight:
            liked[row.media_identity_id] = weight
    return liked


def plan(jellyfin, user_id: str, shown=None):
    """(picks [(jellyfin id, score, because media id)], liked count) for one
    account, or (None, liked count) when there's too little to go on."""
    liked = liked_films(user_id)
    if len(liked) < MIN_LIKED:
        return None, len(liked)
    # Films since deleted still say what someone likes; only those on disk
    # can be picked (Jellyfin lists only those).
    films = {
        m.id: m
        for m in db.session.query(MediaIdentity).filter(
            MediaIdentity.media_type == "movie"
        )
    }
    by_jellyfin = {
        _norm(m.jellyfin_id): m.id
        for m in films.values()
        if m.jellyfin_id and m.missing_since is None
    }
    candidates, ratings = {}, {}
    for item in jellyfin.unplayed_movies(user_id):
        media_id = by_jellyfin.get(_norm(item.get("Id")))
        user_data = item.get("UserData") or {}
        if (
            media_id is None
            or media_id in liked
            or user_data.get("PlaybackPositionTicks")
        ):
            continue
        candidates[media_id] = [g.lower() for g in item.get("Genres") or []]
        ratings[media_id] = item.get("CommunityRating")
    vecs = vectors({key: features(m) for key, m in films.items()})
    picks = choose(liked, candidates, vecs, ratings, shown)
    return [(films[k].jellyfin_id, s, because) for k, s, because in picks], len(liked)


# --- Keeping the playlists ------------------------------------------------------


def sweep(now=None) -> int:
    """Worker cycle: drop films people finished from their picks (every few
    minutes), make each account's list again once a week, and remove the
    playlists when switched off. Returns how many lists were made."""
    from .services import _as_datetime, client, set_setting, setting

    now = now or utcnow()
    stored = dict(setting(KEY, {}) or {})
    on = enabled()
    if not on and not stored:
        return 0
    if on:
        last = _as_datetime(setting("picks_checked_at"))
        if last and now - last < CHECK_PLAYED:
            return 0
    try:
        jellyfin = client("jellyfin")
    except IntegrationError:
        return 0
    if not on:
        for entry in stored.values():
            try:
                jellyfin.delete_item(entry["playlist"])
            except (IntegrationError, KeyError):
                pass
        set_setting(KEY, {})
        db.session.commit()
        return 0
    set_setting("picks_checked_at", now.isoformat())
    try:
        users = jellyfin.users()
    except IntegrationError:
        return 0
    wanted = eligible_accounts()
    made = 0
    for user in users:
        user_id = _norm(user["Id"])
        entry = dict(stored.get(user_id) or {})
        if user_id not in wanted or (user.get("Policy") or {}).get("IsDisabled"):
            # Left out, hidden or disabled: no list, and any made goes.
            if entry.get("playlist"):
                try:
                    jellyfin.delete_item(entry["playlist"])
                except IntegrationError:
                    continue
            stored.pop(user_id, None)
            continue
        try:
            made += _keep(jellyfin, user, entry, now)
        except IntegrationError:
            continue
        stored[user_id] = entry
    set_setting(KEY, stored)
    db.session.commit()
    return made


def _keep(jellyfin, user, entry: dict, now) -> int:
    """Bring one account's playlist up to date (``entry`` is updated)."""
    from .services import _as_datetime

    user_id = _norm(user["Id"])
    playlist = entry.get("playlist")
    items = jellyfin.playlist_items(playlist, user_id) if playlist else None
    if playlist and items is None:
        playlist = entry["playlist"] = None  # deleted in Jellyfin: made again
    if items:
        unplayed = [
            i["Id"] for i in items if not (i.get("UserData") or {}).get("Played")
        ]
        if len(unplayed) < len(items):
            jellyfin.set_playlist(playlist, unplayed)
            entry["items"] = [_norm(i) for i in unplayed]
    built = _as_datetime(entry.get("built_at"))
    if playlist and built and now - built < REBUILD:
        return 0
    shown = _shown_weeks(entry)
    picks, liked = plan(jellyfin, user_id, shown)
    entry["liked"] = liked
    entry["built_at"] = now.isoformat()
    if not picks:
        if playlist:
            jellyfin.set_playlist(playlist, [])
        entry["items"], entry["because"] = [], {}
        return 0
    ids = [jellyfin_id for jellyfin_id, _s, _b in picks]
    if playlist:
        jellyfin.set_playlist(playlist, ids)
    else:
        entry["playlist"] = jellyfin.create_playlist(
            NAME.format(name=user["Name"]), user_id, ids
        )
    entry["items"] = [_norm(i) for i in ids]
    entry["because"] = {_norm(j): because for j, _s, because in picks}
    entry["shown"] = _next_shown(shown, picks)
    return 1


def _shown_weeks(entry) -> dict:
    """{media id: weeks on the list unplayed} for the films still on it."""
    still = set(entry.get("items") or [])
    weeks = entry.get("shown") or {}
    by_jellyfin = {
        _norm(m.jellyfin_id): m.id
        for m in db.session.query(MediaIdentity).filter(
            MediaIdentity.jellyfin_id.isnot(None)
        )
        if _norm(m.jellyfin_id) in still
    }
    return {
        media_id: weeks.get(media_id, 0) + 1
        for jellyfin_id, media_id in by_jellyfin.items()
    }


def _next_shown(shown: dict, picks) -> dict:
    """Weeks shown, kept for the films picked again."""
    by_jellyfin = {
        _norm(m.jellyfin_id): m.id
        for m in db.session.query(MediaIdentity).filter(
            MediaIdentity.jellyfin_id.in_([p[0] for p in picks])
        )
    }
    return {
        media_id: shown.get(media_id, 0)
        for media_id in by_jellyfin.values()
        if media_id in shown
    }


def summary() -> list[dict]:
    """For the settings page: each account's list and why each film is on it."""
    from .services import setting

    stored = setting(KEY, {}) or {}
    media = {
        _norm(m.jellyfin_id): m
        for m in db.session.query(MediaIdentity).filter(
            MediaIdentity.jellyfin_id.isnot(None)
        )
    }
    by_id = {m.id: m for m in media.values()}
    rows = []
    for user_id, entry in stored.items():
        because = entry.get("because") or {}
        rows.append(
            {
                "user_id": user_id,
                "liked": entry.get("liked"),
                "built_at": entry.get("built_at"),
                "picks": [
                    {
                        "media": media.get(j),
                        "because": by_id.get(because.get(j)),
                    }
                    for j in entry.get("items") or []
                    if media.get(j)
                ],
            }
        )
    return rows
