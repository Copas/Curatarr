"""What a household's choices say about a title, and how cleanup treats it.

Four signals, each read from an app the household already uses:

- **Requested** (Seerr): someone asked for it. It's held from cleanup until
  that person has played it, or until ``request_hold_days`` pass.
- **Rewatched** (Jellyfin play counts): a film played twice or more, or a show
  someone rewatches or several people follow. It goes to the back of the
  line, like a favourite.
- **From a list** (Sonarr/Radarr import lists): added by a list rather than by
  a person. Until someone plays it, it gets a shorter grace period
  (``list_grace_days``) and goes first.
- **Seasonal**: a Halloween or Christmas title, by its TMDB keywords or because
  it was played in that season in an earlier year. It's held through the season
  and the weeks before it; and if Curatarr removed it, it can be added back to
  Radarr before the season starts (``seasonal_return``, off by default).

Cleanup reads them through ``signals()`` (see lifecycle._candidate_input).
"""

from datetime import UTC, date, datetime, timedelta

from . import db
from .integrations import IntegrationError
from .models import LifecycleEvent, MediaIdentity, UserMediaState, utcnow

SETTINGS_KEY = "curation"
DEFAULTS = {
    "request_hold_days": 90,
    "list_grace_days": 7,
    "seasonal": True,
    "seasonal_return": False,
    "season_lead_days": 14,
    "leaving_collection": True,
}
BOUNDS = {
    "request_hold_days": (0, 3650),
    "list_grace_days": (0, 3650),
    "season_lead_days": (0, 90),
}
REQUESTS_KEY = "seerr_requests"  # {media id: [{"user_id", "user", "at"}]}
LISTS_KEY = "list_arrivals"  # [media id]
RETURNS_KEY = "seasonal_returns"  # {tmdb id: {...}} films to bring back
SEASONS = (
    {
        "key": "halloween",
        "name": "Halloween",
        "start": (10, 1),
        "end": (10, 31),
        "keywords": {"halloween", "trick or treating", "jack-o'-lantern"},
    },
    {
        "key": "christmas",
        "name": "Christmas",
        "start": (12, 1),
        "end": (12, 31),
        "keywords": {
            "christmas",
            "christmas eve",
            "christmas party",
            "christmas tree",
            "santa claus",
        },
    },
)
PLAY_EVENTS = ("playback_started", "playback_stopped", "item_played")
REWATCHED_EPISODES = 3  # a show someone has rewatched this many episodes of
SHARED_FOLLOWERS = 2  # or that this many people have each watched...
SHARED_EPISODES = 5  # ...this many episodes of


def options():
    from .services import setting

    stored = setting(SETTINGS_KEY, {}) or {}
    return DEFAULTS | {k: v for k, v in stored.items() if k in DEFAULTS}


def save_options(form):
    """Settings → Curation. Raises ValueError with a message for the page."""
    from .services import set_setting

    values = {}
    for key, (low, high) in BOUNDS.items():
        try:
            number = int((form.get(key) or "").strip())
        except ValueError as exc:
            raise ValueError("Enter whole numbers of days") from exc
        if not low <= number <= high:
            raise ValueError(f"Days must be between {low} and {high}")
        values[key] = number
    values["seasonal"] = form.get("seasonal") == "on"
    values["seasonal_return"] = form.get("seasonal_return") == "on"
    values["leaving_collection"] = form.get("leaving_collection") == "on"
    set_setting(SETTINGS_KEY, values)
    db.session.commit()


def _aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _parse(value):
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


# --- Sonarr/Radarr tags, genres and import lists (during discovery) ---------


def remember_tags(media, arr_item):
    """Genres and TMDB keywords from Sonarr/Radarr, lower-cased."""
    media.genres = sorted({str(g).lower() for g in arr_item.get("genres") or []})
    media.keywords = sorted({str(k).lower() for k in arr_item.get("keywords") or []})


class ListArrivals:
    """Which titles an import list added: they carry a tag one of the lists
    applies, or (Radarr) are on one of its lists right now. Kept unchanged when
    the lists can't be read."""

    def __init__(self, sonarr, radarr):
        self.found, self.ok = set(), True
        self.tags = {"sonarr": set(), "radarr": set()}
        self.listed = set()
        for kind, arr in (("sonarr", sonarr), ("radarr", radarr)):
            if arr is None:
                continue
            try:
                for entry in arr.import_lists() or []:
                    if entry.get("enabled", True) is not False:
                        self.tags[kind] |= set(entry.get("tags") or [])
                if kind == "radarr":
                    self.listed = {
                        str(m.get("tmdbId"))
                        for m in arr.list_movies() or []
                        if m.get("tmdbId") and (m.get("lists") or [])
                    }
            except (IntegrationError, AttributeError, TypeError):
                self.ok = False

    def observe(self, media, arr_item):
        kind = "sonarr" if media.media_type == "series" else "radarr"
        if self.tags[kind] & set(arr_item.get("tags") or []) or (
            kind == "radarr" and str(arr_item.get("tmdbId")) in self.listed
        ):
            self.found.add(media.id)

    def save(self):
        from .services import set_setting

        if self.ok:
            set_setting(LISTS_KEY, sorted(self.found))
            forget()


# --- Seerr requests ----------------------------------------------------------


def sync_requests():
    """Read every Seerr request and match it to a title. Returns how many matched."""
    from .services import client, configured, set_setting

    if not configured("seerr"):
        return 0
    try:
        rows = client("seerr").requests()
    except IntegrationError:
        return 0  # keep what was read last time
    by_tmdb, by_tvdb = {}, {}
    for media in db.session.query(MediaIdentity).filter(
        MediaIdentity.missing_since.is_(None)
    ):
        if media.tmdb_id:
            by_tmdb[(media.media_type, media.tmdb_id)] = media
        if media.tvdb_id:
            by_tvdb[media.tvdb_id] = media
    found = {}
    for row in rows:
        if row.get("status") == 3:  # declined
            continue
        item = row.get("media") or {}
        kind = "movie" if row.get("type") == "movie" else "series"
        media = by_tmdb.get((kind, item.get("tmdbId")))
        if media is None and kind == "series":
            media = by_tvdb.get(item.get("tvdbId"))
        if media is None:
            continue
        who = row.get("requestedBy") or {}
        user_id = (who.get("jellyfinUserId") or "").replace("-", "").lower()
        found.setdefault(media.id, []).append(
            {
                "user_id": user_id or None,
                "user": who.get("displayName") or who.get("jellyfinUsername"),
                "at": row.get("createdAt"),
            }
        )
    set_setting(REQUESTS_KEY, found)
    db.session.commit()
    forget()
    return len(found)


def requests_for(media_id):
    from .services import setting

    return (setting(REQUESTS_KEY, {}) or {}).get(media_id) or []


def _played_since(states, user_id, since):
    for state in states:
        if (state.jellyfin_user_id or "").replace("-", "").lower() != user_id:
            continue
        if state.last_played_at and (
            since is None or _aware(state.last_played_at) >= since
        ):
            return True
    return False


def request_hold(requests, states, now, opts):
    """(until, the request) while a request still protects the title."""
    best = None
    for entry in requests:
        at = _parse(entry.get("at"))
        if at is None:
            continue
        until = at + timedelta(days=opts["request_hold_days"])
        if until <= now:
            continue
        if entry.get("user_id") and _played_since(states, entry["user_id"], at):
            continue  # they've watched what they asked for
        if not entry.get("user_id") and any(s.last_played_at for s in states):
            continue
        if best is None or until > best[0]:
            best = (until, entry)
    return best


# --- Rewatched -----------------------------------------------------------------


def rewatched(media, states, episodes_played_by_user=None):
    """Watched more than once, by one person or several."""
    if media.media_type == "movie":
        return sum(max(0, s.play_count or 0) for s in states) >= 2
    if any((s.play_count or 0) >= REWATCHED_EPISODES for s in states):
        return True
    followers = sum(
        1 for n in (episodes_played_by_user or {}).values() if n >= SHARED_EPISODES
    )
    return followers >= SHARED_FOLLOWERS


# --- Seasons -----------------------------------------------------------------


def _window(season, year):
    start = date(year, *season["start"])
    end = date(year, *season["end"])
    return start, end


def current_window(season, today, lead_days):
    """(start, end) of the season's window that contains today, counting the
    lead-in, or None."""
    for year in (today.year - 1, today.year, today.year + 1):
        start, end = _window(season, year)
        if start - timedelta(days=lead_days) <= today <= end:
            return start, end
    return None


def seasons_for(media, today=None, learned=None):
    """The seasons a title belongs to: by keyword, or by when it's been watched."""
    if learned is None:
        learned = _context()["learned"]
    words = set(media.keywords or [])
    return [
        season
        for season in SEASONS
        if words & season["keywords"] or season["key"] in learned.get(media.id, ())
    ]


def seasonal_hold(media, now, opts, learned=None):
    """(until, season) while a title's season, or the weeks before it, is on."""
    if not opts["seasonal"]:
        return None
    today = _aware(now).date()
    for season in seasons_for(media, today, learned):
        window = current_window(season, today, opts["season_lead_days"])
        if window:
            until = datetime.combine(
                window[1] + timedelta(days=1), datetime.min.time(), UTC
            )
            return until, season
    return None


def remember_for_return(media, movie):
    """Called when Curatarr removes a seasonal film from Radarr."""
    from .services import set_setting, setting

    seasons = seasons_for(media)
    if not seasons or not media.tmdb_id:
        return
    stored = dict(setting(RETURNS_KEY, {}) or {})
    stored[str(media.tmdb_id)] = {
        "title": media.title,
        "seasons": [s["key"] for s in seasons],
        "profile": (movie or {}).get("qualityProfileId"),
        "root": (movie or {}).get("rootFolderPath"),
        "removed_at": utcnow().isoformat(),
    }
    set_setting(RETURNS_KEY, stored)


def return_seasonal(now=None):
    """Add removed seasonal films back to Radarr as their season approaches."""
    from .services import audit, client, configured, set_setting, setting

    opts = options()
    if not (opts["seasonal"] and opts["seasonal_return"] and configured("radarr")):
        return 0
    stored = dict(setting(RETURNS_KEY, {}) or {})
    if not stored:
        return 0
    today = _aware(now or utcnow()).date()
    by_key = {s["key"]: s for s in SEASONS}
    radarr, added = client("radarr"), 0
    for tmdb_id, entry in list(stored.items()):
        seasons = [by_key[k] for k in entry.get("seasons") or [] if k in by_key]
        if not any(current_window(s, today, opts["season_lead_days"]) for s in seasons):
            continue
        try:
            film = radarr.lookup_tmdb(int(tmdb_id))
            if film and not film.get("id"):
                if not entry.get("profile") or not entry.get("root"):
                    from .franchises import _placement

                    entry["profile"], entry["root"] = _placement(radarr)
                radarr.add_movie(
                    film
                    | {
                        "qualityProfileId": entry["profile"],
                        "rootFolderPath": entry["root"],
                        "monitored": True,
                        "minimumAvailability": "released",
                        "addOptions": {"searchForMovie": True},
                    }
                )
                season = seasons[0]["name"]
                audit(
                    "seasonal_returned",
                    None,
                    f"Added {entry.get('title')} back to Radarr for {season}.",
                    f"seasonal-return:{tmdb_id}:{today.year}:{seasons[0]['key']}",
                    payload={"tmdb_id": int(tmdb_id), "season": seasons[0]["key"]},
                )
                added += 1
        except (IntegrationError, ValueError):
            continue  # try again next time
        stored.pop(tmdb_id)
    set_setting(RETURNS_KEY, stored)
    db.session.commit()
    return added


# --- Everything cleanup needs, for one title --------------------------------
# Cleanup asks about every title several times a run, so what's shared is
# read once and kept briefly; forget() drops it after anything it reads changes.

CACHE_SECONDS = 30
_cache = {}


def forget():
    _cache.clear()


def _context():
    import time

    hit = _cache.get("context")
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = _load_context()
    _cache["context"] = (time.monotonic(), value)
    return value


def _load_context():
    from sqlalchemy import func

    from .models import EpisodeUserState, MediaPart
    from .services import setting

    # A title belongs to a season when it was played in that season in an
    # earlier year, and on at least as many days in the season as outside it
    # (so a film watched year-round, or once by chance in October, doesn't).
    this_year = utcnow().year
    days = {}  # media id: {season key or None: {dates}}
    for media_id, occurred in db.session.query(
        LifecycleEvent.media_identity_id, LifecycleEvent.occurred_at
    ).filter(
        LifecycleEvent.media_identity_id.isnot(None),
        LifecycleEvent.event_type.in_(PLAY_EVENTS),
    ):
        when = _aware(occurred).date()
        key = next(
            (
                season["key"]
                for season in SEASONS
                if _window(season, when.year)[0]
                <= when
                <= _window(season, when.year)[1]
            ),
            None,
        )
        days.setdefault(media_id, {}).setdefault(key, set()).add(when)
    learned = {}
    for media_id, by_season in days.items():
        outside = len(by_season.get(None, ()))
        for key, dates in by_season.items():
            earlier = any(d.year < this_year for d in dates)
            if key and earlier and len(dates) >= outside:
                learned.setdefault(media_id, set()).add(key)
    episodes = {}
    for media_id, user_id, count in (
        db.session.query(
            MediaPart.media_identity_id, EpisodeUserState.jellyfin_user_id, func.count()
        )
        .join(MediaPart, EpisodeUserState.media_part_id == MediaPart.id)
        .filter(EpisodeUserState.played.is_(True))
        .group_by(MediaPart.media_identity_id, EpisodeUserState.jellyfin_user_id)
    ):
        episodes.setdefault(media_id, {})[user_id] = count
    return {
        "options": options(),
        "learned": learned,
        "episodes": episodes,
        "lists": set(setting(LISTS_KEY, []) or []),
        "requests": setting(REQUESTS_KEY, {}) or {},
    }


def _favorite_names(states):
    from .services import setting

    names = setting("jellyfin_user_names", {})
    return sorted(
        names.get(s.jellyfin_user_id, s.jellyfin_user_id) for s in states if s.favorite
    )


def signals(media, states=None, now=None):
    """How the household's choices bear on one title (for cleanup and pages)."""
    now = _aware(now or utcnow())
    ctx = _context()
    opts = ctx["options"]
    if states is None:
        states = (
            db.session.query(UserMediaState).filter_by(media_identity_id=media.id).all()
        )
    played = any(s.last_played_at for s in states)
    requests = ctx["requests"].get(media.id) or []
    request = request_hold(requests, states, now, opts)
    season = seasonal_hold(media, now, opts, ctx["learned"])
    holds = [h for h in (request, season) if h]
    return {
        "held_until": max((h[0] for h in holds), default=None),
        "request": request[1] if request else None,
        "request_until": request[0] if request else None,
        "season": season[1]["name"] if season else None,
        "season_until": season[0] if season else None,
        "rewatched": rewatched(media, states, ctx["episodes"].get(media.id)),
        "from_list": media.id in ctx["lists"] and not played,
        "requests": requests,
        "favorites": _favorite_names(states),
        "list_grace_days": opts["list_grace_days"],
    }
