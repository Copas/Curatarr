"""A Jellyfin collection of the titles Leaving Soon, kept in step by Curatarr.

Titles join when they're marked Leaving Soon and leave when they're kept,
watched or deleted. The collection is created once and then known by its id,
so renaming it in Jellyfin is fine. Settings → Household turns it off; the
collection is then emptied.
"""

from . import db
from .integrations import IntegrationError
from .models import MediaIdentity, PurgeCandidate

NAME = "Leaving Soon"
KEY = "leaving_collection"


def enabled() -> bool:
    from .curation import options

    return bool(options().get("leaving_collection", True))


def _wanted() -> set[str]:
    if not enabled():
        return set()
    rows = (
        db.session.query(MediaIdentity.jellyfin_id)
        .join(PurgeCandidate, PurgeCandidate.media_identity_id == MediaIdentity.id)
        .filter(
            PurgeCandidate.state == "LEAVING_SOON",
            MediaIdentity.jellyfin_id.isnot(None),
            MediaIdentity.missing_since.is_(None),
        )
    )
    return {_norm(jellyfin_id) for (jellyfin_id,) in rows}


def _norm(item_id: str) -> str:
    return item_id.replace("-", "").lower()


def sync() -> dict | None:
    """Make the collection hold exactly the titles Leaving Soon. Asks Jellyfin
    only when that set changed since the last sync (or the collection is new)."""
    from .services import client, set_setting, setting

    stored = setting(KEY, {}) or {}
    wanted = _wanted()
    if not wanted and not stored.get("id"):
        return None  # nothing to show, and no collection yet
    if stored.get("id") and set(stored.get("items", [])) == wanted:
        return stored
    jellyfin = client("jellyfin")
    try:
        collection = stored.get("id")
        if collection and not jellyfin.collection_exists(collection):
            collection = None  # deleted in Jellyfin: make a new one
        if collection is None:
            if not wanted:
                set_setting(KEY, {})
                db.session.commit()
                return None
            collection = jellyfin.create_collection(NAME, sorted(wanted))
            have = set(wanted)
        else:
            have = {_norm(i) for i in jellyfin.collection_items(collection)}
            if wanted - have:
                jellyfin.add_to_collection(collection, sorted(wanted - have))
            if have - wanted:
                jellyfin.remove_from_collection(collection, sorted(have - wanted))
    except IntegrationError:
        return stored  # try again next pass
    stored = {"id": collection, "items": sorted(wanted)}
    set_setting(KEY, stored)
    db.session.commit()
    return stored
