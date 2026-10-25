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
- ``status`` (the web app, opening a menu): whether Leaving Soon applies,
  and whether Leave after watched can be set or is set for this account;
- ``leave_after_watched`` / ``keep_after_watched`` (the web app's Leave after
  I watch, and undoing it): for a film, show, season or episode, counting the
  watch of the account that chose it.
"""

from . import db
from .models import MediaIdentity, utcnow

MODES = {
    "delete",
    "now",
    "leaving_soon",
    "status",
    "leave_after_watched",
    "keep_after_watched",
}
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
    if mode in {"leave_after_watched", "keep_after_watched"}:
        return _leave_after(item, user_name, mode)
    leave = _leave_status(item, user_name) if mode == "status" else {}
    media = (
        db.session.query(MediaIdentity)
        .filter(
            MediaIdentity.jellyfin_id == item, MediaIdentity.missing_since.is_(None)
        )
        .first()
    )
    if media is None or media.media_type not in {"movie", "series"}:
        return 200, {"handled": False} | leave
    existing = _active_candidate(media.id)
    if existing is not None and existing.target_part_ids:
        existing = None  # only some episodes are leaving: the show isn't
    state = existing.state if existing else None
    if mode == "status":
        return 200, {
            "handled": True,
            "title": media.title,
            "leavingSoon": state == "LEAVING_SOON",
            "deleting": state in GOING,
        } | leave
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
        where = "Leaving Soon" if mode == "leaving_soon" else "a delete"
        candidate = request_removal(media, where=f"{where} in Jellyfin by {who}")
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


def _account(user_name) -> str | None:
    """The Jellyfin account id for a user name the plugin sent."""
    from .services import setting

    wanted = (user_name or "").strip().lower()
    for user_id, name in (setting("jellyfin_user_names", {}) or {}).items():
        if str(name).strip().lower() == wanted:
            return user_id
    return None


def _flag_target(item):
    """(title, season, episode) for Leave after watched, or None."""
    from .flag_routes import resolve

    try:
        media, season, part = resolve(item)
    except LookupError:
        return None
    if media is None or media.media_type not in {"movie", "series"}:
        return None
    return media, season, part


def _leave_status(item, user_name) -> dict:
    from . import flags
    from .services import resolved_policy

    target = _flag_target(item)
    if target is None:
        return {"canLeaveAfterWatched": False, "leavesAfterWatched": False}
    media, season, part = target
    row = flags.find(media, flags.LEAVE, season=season, part=part)
    policy, _ = resolved_policy(media)
    return {
        "canLeaveAfterWatched": row is None and not policy["never_purge"],
        "leavesAfterWatched": row is not None,
    }


def _leave_after(item, user_name, mode) -> tuple[int, dict]:
    from . import flags

    target = _flag_target(item)
    if target is None:
        return 200, {"handled": False}
    media, season, part = target
    who = (user_name or "").strip()[:100] or "someone"
    if mode == "keep_after_watched":
        flags.clear_flag(media, flags.LEAVE, season=season, part=part)
        return 200, {
            "handled": True,
            "result": "cleared",
            "message": f"{media.title} won't leave after it's watched.",
        }
    try:
        row = flags.set_flag(
            media,
            flags.LEAVE,
            season=season,
            part=part,
            user_id=_account(user_name),
            set_by=f"{who} in Jellyfin",
        )
    except ValueError as exc:
        return 200, {"handled": True, "refused": str(exc)}
    return 200, {
        "handled": True,
        "result": "leave_after_watched",
        "message": f"{media.title}: {flags.describe(row)}.",
    }
