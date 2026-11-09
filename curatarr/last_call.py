"""Last call: a public warning, weeks before a film nobody watches is cleaned up.

Films nobody has played for a year (LAST_CALL_DAYS, 365 by default) get a
"LAST CALL · ♥ TO KEEP" banner on their poster and join a Jellyfin collection,
"Last Call", a few each week (PER_WEEK, oldest first), so the collection stays
short enough to browse on a TV. Pressing the heart (favourite) in any Jellyfin
app keeps the film, for as long as it's favourited; so does playing it, Keep
or Never Purge on its page in Curatarr. Otherwise, after NOTICE days it moves
on to Leaving Soon, with the usual notice before it goes through Radarr.
Leaving Soon titles are in the Last Call collection too, so it's the one
place to look.

Only films, only in the libraries chosen (all film libraries to start).
Films are never picked while held for a request or a season, while
favourited, with Never Purge on, or for a while after someone chose to keep
one. Settings → Household choices, off by default.
"""

from datetime import timedelta

from . import db
from .integrations import IntegrationError
from .models import Library, MediaIdentity, PurgeCandidate, utcnow

REASON = "last_call"
PICKED_KEY = "last_call_picked_at"
PICK_EVERY = timedelta(hours=1)  # looking through the library for this week's
COLLECTION_KEY = "last_call_collection"
COLLECTION_NAME = "Last Call"
DEFAULTS = {
    "last_call": False,
    "last_call_days": 365,
    "last_call_per_week": 10,
    "last_call_notice_days": 14,
    "last_call_libraries": None,  # None: every film library
}


def options() -> dict:
    from .curation import options as household

    stored = household()
    return {key: stored.get(key, value) for key, value in DEFAULTS.items()}


def film_libraries() -> list:
    return (
        db.session.query(Library)
        .filter(Library.media_type == "movies")
        .order_by(Library.name)
        .all()
    )


def chosen_libraries(opts=None) -> set[str]:
    opts = opts or options()
    chosen = opts["last_call_libraries"]
    films = {library.id for library in film_libraries()}
    return films if chosen is None else films & set(chosen)


def rescue_reason(media, item) -> str | None:
    """Why a film in Last call is kept now, or None (lifecycle's reconcile
    has already handled playing it and Never Purge)."""
    opts = options()
    if not opts["last_call"]:
        return "Last call is turned off."
    if media.library_id not in chosen_libraries(opts):
        return "Last call is turned off for this library."
    if item.favorite:
        return "Someone favourited it."
    from .policy import _aware

    if item.held_until and _aware(item.held_until) > utcnow():
        return "Held for a request or a season."
    return None


def _recently_kept(since) -> set[str]:
    """Films someone chose to keep from Last call lately: not picked again."""
    rows = db.session.query(PurgeCandidate.media_identity_id).filter(
        PurgeCandidate.reason_code == REASON,
        PurgeCandidate.state == "RESCUED",
        PurgeCandidate.updated_at >= since,
    )
    return {media_id for (media_id,) in rows}


def eligible(now, opts) -> list:
    """Films that could go into Last call, oldest first."""
    from .lifecycle import _candidate_input
    from .policy import _aware
    from .services import _active_candidate, resolved_policy

    days = timedelta(days=opts["last_call_days"])
    libraries = chosen_libraries(opts)
    kept = _recently_kept(now - days)
    found = []
    rows = (
        db.session.query(MediaIdentity)
        .filter(
            MediaIdentity.media_type == "movie",
            MediaIdentity.missing_since.is_(None),
            MediaIdentity.library_id.in_(libraries),
        )
        .order_by(MediaIdentity.added_at)
    )
    for media in rows:
        if media.id in kept or not media.added_at:
            continue
        item = _candidate_input(media)
        if not item.mapped or item.size_bytes <= 0 or item.favorite:
            continue
        if item.held_until and _aware(item.held_until) > now:
            continue
        # Since the film was added: a file upgraded or rewritten since (a
        # better copy, a TV-friendly audio track) doesn't make it new.
        added = _aware(media.added_at)
        played = _aware(item.last_played_at)
        if max(added, played or added) + days > now:
            continue
        policy, _ = resolved_policy(media)
        if policy["never_purge"] or _active_candidate(media.id):
            continue
        found.append((media, item))
    return found


def sweep(now=None) -> dict:
    """Worker cycle: move films whose Last call has run out to Leaving Soon,
    and add this week's few. Returns counts."""
    from .lifecycle import request_removal
    from .policy import _aware
    from .services import _as_datetime, audit, set_setting, setting

    now = now or utcnow()
    opts = options()
    done = {"leaving": 0, "added": 0}
    if not opts["last_call"]:
        return done
    notice = timedelta(days=opts["last_call_notice_days"])
    for candidate in db.session.query(PurgeCandidate).filter_by(state="LAST_CALL"):
        if _aware(candidate.eligible_at) + notice > now:
            continue
        try:
            request_removal(candidate.media, where="Last call, which nobody stopped")
            done["leaving"] += 1
        except ValueError:
            continue  # Never Purge, or mapping changed: reconcile rescues it
    week = (
        db.session.query(PurgeCandidate)
        .filter(
            PurgeCandidate.reason_code == REASON,
            PurgeCandidate.eligible_at >= now - timedelta(days=7),
        )
        .count()
    )
    room = opts["last_call_per_week"] - week
    last = _as_datetime(setting(PICKED_KEY))
    if room <= 0 or (last and now - last < PICK_EVERY):
        db.session.commit()
        return done
    set_setting(PICKED_KEY, now.isoformat())
    for media, item in eligible(now, opts)[:room]:
        reason = (
            f"Nobody has played it in {opts['last_call_days']} days: in Last call "
            f"for {opts['last_call_notice_days']} days, then Leaving Soon unless "
            "someone favourites or plays it."
        )
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="LAST_CALL",
            reason_code=REASON,
            reason_text=reason,
            reclaimable_bytes=item.size_bytes,
            last_activity_snapshot=item.last_played_at,
            eligible_at=now,
        )
        db.session.add(candidate)
        db.session.flush()
        audit(
            "last_call",
            media.id,
            f"{media.title} is in Last call: {reason}",
            f"last-call:{candidate.id}",
            candidate_id=candidate.id,
        )
        done["added"] += 1
    db.session.commit()
    return done


def now_in() -> list[dict]:
    """For the settings page: films in Last call and when each moves on."""
    from .policy import _aware

    notice = timedelta(days=options()["last_call_notice_days"])
    rows = (
        db.session.query(PurgeCandidate)
        .filter_by(state="LAST_CALL")
        .order_by(PurgeCandidate.eligible_at)
    )
    return [
        {"media": c.media, "until": _aware(c.eligible_at) + notice}
        for c in rows
        if c.media
    ]


# --- The Last Call collection --------------------------------------------------


def _wanted() -> set[str]:
    from .leaving_collection import _norm

    if not options()["last_call"]:
        return set()
    rows = (
        db.session.query(MediaIdentity.jellyfin_id)
        .join(PurgeCandidate, PurgeCandidate.media_identity_id == MediaIdentity.id)
        .filter(
            PurgeCandidate.state.in_(("LAST_CALL", "LEAVING_SOON")),
            PurgeCandidate.target_part_ids.is_(None),
            MediaIdentity.jellyfin_id.isnot(None),
            MediaIdentity.missing_since.is_(None),
        )
    )
    return {_norm(jellyfin_id) for (jellyfin_id,) in rows}


def sync_collection() -> dict | None:
    """Make the Last Call collection hold the films in Last call and every
    title Leaving Soon. Asks Jellyfin only when that changed."""
    from .artwork import LAST_CALL_AMBER
    from .leaving_collection import _norm, poster
    from .services import client, set_setting, setting

    stored = setting(COLLECTION_KEY, {}) or {}
    wanted = _wanted()
    if not wanted and not stored.get("id"):
        return None
    if stored.get("id") and set(stored.get("items", [])) == wanted:
        return stored
    jellyfin = client("jellyfin")
    try:
        collection = stored.get("id")
        if collection and not jellyfin.collection_exists(collection):
            collection = None  # deleted in Jellyfin: made again
        if collection is None:
            if not wanted:
                set_setting(COLLECTION_KEY, {})
                db.session.commit()
                return None
            collection = jellyfin.create_collection(COLLECTION_NAME, sorted(wanted))
        else:
            have = {_norm(i) for i in jellyfin.collection_items(collection)}
            if wanted - have:
                jellyfin.add_to_collection(collection, sorted(wanted - have))
            if have - wanted:
                jellyfin.remove_from_collection(collection, sorted(have - wanted))
        images = []
        for item in sorted(wanted)[:4]:
            try:
                images.append(jellyfin.image(item))
            except IntegrationError:
                continue
        try:
            jellyfin.put_image(
                collection,
                poster(images, "LAST CALL", LAST_CALL_AMBER, "#111111"),
            )
        except IntegrationError:
            pass  # drawn again when the collection next changes
    except IntegrationError:
        return stored
    stored = {"id": collection, "items": sorted(wanted)}
    set_setting(COLLECTION_KEY, stored)
    db.session.commit()
    return stored
