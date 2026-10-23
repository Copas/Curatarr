"""Deletes made in Jellyfin, sent here by the Curatarr plugin for Jellyfin.

With the plugin installed, Delete in any Jellyfin app asks Curatarr first.
A film or show Curatarr looks after is marked Leaving Soon instead of
deleted, the same as Remove on its title page: its poster says so, playing it
or Keep cancels it, and at the end of the notice period it goes through
Radarr or Sonarr, which a delete in Jellyfin never told. Anything else (an
episode, a season, a title Curatarr doesn't know) is left to Jellyfin.
"""

from . import db
from .models import MediaIdentity, utcnow


def _norm(item_id) -> str | None:
    text = str(item_id or "").replace("-", "").strip().lower()
    if len(text) == 32 and all(char in "0123456789abcdef" for char in text):
        return text
    return None


def handle(item_id, user_name=None) -> tuple[int, dict]:
    """Answer the plugin: ``handled`` false lets Jellyfin delete as usual;
    true with ``refused`` means the delete should fail with that message."""
    from .lifecycle import request_removal
    from .services import audit

    item = _norm(item_id)
    if item is None:
        return 400, {"error": "itemId must be a Jellyfin item id"}
    media = (
        db.session.query(MediaIdentity)
        .filter(
            MediaIdentity.jellyfin_id == item, MediaIdentity.missing_since.is_(None)
        )
        .first()
    )
    if media is None or media.media_type not in {"movie", "series"}:
        return 200, {"handled": False}
    who = (user_name or "").strip()[:100] or "someone"
    try:
        candidate = request_removal(media, where=f"a delete in Jellyfin by {who}")
    except ValueError as exc:
        db.session.rollback()
        message = str(exc)
        if message.endswith("is already on its way out."):
            return 200, {"handled": True, "result": "already", "message": message}
        audit(
            "jellyfin_delete_refused",
            media.id,
            f"{who} deleted {media.title} in Jellyfin; it was kept: {message}",
            f"jellyfin-delete-refused:{media.id}:{utcnow():%Y-%m-%dT%H:%M}",
        )
        db.session.commit()
        return 200, {"handled": True, "refused": message}
    when = candidate.scheduled_delete_at
    return 200, {
        "handled": True,
        "result": "leaving_soon",
        "title": media.title,
        "deleteAt": when.isoformat() if when else None,
        "message": f"{media.title} is Leaving Soon instead.",
    }
