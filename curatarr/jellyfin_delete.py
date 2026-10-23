"""Deletes made in Jellyfin, sent here by the Curatarr plugin for Jellyfin.

With the plugin installed, Delete in any Jellyfin app asks Curatarr first.
A film or show Curatarr looks after is marked Leaving Soon instead of
deleted, the same as Remove on its title page: its poster says so, playing it
or Keep cancels it, and at the end of the notice period it goes through
Radarr or Sonarr, which a delete in Jellyfin never told. Deleting a title
that is already Leaving Soon deletes it now, through Radarr or Sonarr, so
every app can do both. Jellyfin's web app has a Leaving Soon menu item of its
own, so Delete there deletes straight away. Anything else (an episode, a
season, a title Curatarr doesn't know) is left to Jellyfin.

Modes, from the plugin:
- ``delete`` (Delete in an app): Leaving Soon, or now if it already is;
- ``now`` (Delete in the web app): now;
- ``leaving_soon`` (the web app's Leaving Soon): Leaving Soon only;
- ``status`` (the web app, opening a menu): whether Leaving Soon applies.
"""

from . import db
from .models import MediaIdentity, utcnow

MODES = {"delete", "now", "leaving_soon", "status"}
GOING = {"APPROVED", "EXECUTING"}


def _norm(item_id) -> str | None:
    text = str(item_id or "").replace("-", "").strip().lower()
    if len(text) == 32 and all(char in "0123456789abcdef" for char in text):
        return text
    return None


def _refused(media, who, message) -> tuple[int, dict]:
    from .services import audit

    db.session.rollback()
    audit(
        "jellyfin_delete_refused",
        media.id,
        f"{who} deleted {media.title} in Jellyfin; it was kept: {message}",
        f"jellyfin-delete-refused:{media.id}:{utcnow():%Y-%m-%dT%H:%M}",
    )
    db.session.commit()
    return 200, {"handled": True, "refused": message}


def _delete_now(media, candidate, who) -> tuple[int, dict]:
    """Approve the title's Leaving Soon notice, as Delete in Review does: the
    worker deletes it within a minute, through Radarr or Sonarr."""
    from .lifecycle import review_candidate
    from .services import audit, resolved_policy

    review_candidate(candidate.id, "delete")
    audit(
        "jellyfin_delete_now",
        media.id,
        f"{who} deleted {media.title} in Jellyfin; it's deleted now, "
        "through Radarr or Sonarr.",
        f"jellyfin-delete-now:{candidate.id}",
        candidate_id=candidate.id,
    )
    db.session.commit()
    policy, _ = resolved_policy(media)
    if policy["dry_run"]:
        message = f"Dry run: {media.title} isn't deleted; History says what would be."
    elif media.media_type == "series":
        message = f"{media.title} is being trimmed to its always-keep episodes."
    else:
        message = f"{media.title} is being deleted."
    return 200, {"handled": True, "result": "deleting", "message": message}


def handle(item_id, user_name=None, mode=None) -> tuple[int, dict]:
    """Answer the plugin: ``handled`` false lets Jellyfin delete as usual;
    true with ``refused`` means the delete should fail with that message."""
    from .lifecycle import request_removal
    from .services import _active_candidate

    mode = mode or "delete"
    if mode not in MODES:
        return 400, {"error": f"mode must be one of {', '.join(sorted(MODES))}"}
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
    existing = _active_candidate(media.id)
    state = existing.state if existing else None
    if mode == "status":
        return 200, {
            "handled": True,
            "title": media.title,
            "leavingSoon": state == "LEAVING_SOON",
            "deleting": state in GOING,
        }
    who = (user_name or "").strip()[:100] or "someone"
    if state in GOING:
        return 200, {
            "handled": True,
            "result": "already",
            "message": f"{media.title} is already being deleted.",
        }
    if state == "LEAVING_SOON":
        if mode == "leaving_soon":
            return 200, {
                "handled": True,
                "result": "already",
                "message": f"{media.title} is already Leaving Soon.",
            }
        return _delete_now(media, existing, who)
    try:
        candidate = request_removal(media, where=f"a delete in Jellyfin by {who}")
    except ValueError as exc:
        return _refused(media, who, str(exc))
    if mode == "now":
        return _delete_now(media, candidate, who)
    when = candidate.scheduled_delete_at
    return 200, {
        "handled": True,
        "result": "leaving_soon",
        "title": media.title,
        "deleteAt": when.isoformat() if when else None,
        "message": f"{media.title} is Leaving Soon. Delete it again to delete it now.",
    }
