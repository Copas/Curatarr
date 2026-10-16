"""Always-keep episodes that never arrived.

Every show keeps its first episodes (the minimum) even when nobody is watching.
When one of them never downloads (every release failed, or none matched the
show), nothing says so: Sonarr only retries what it monitors, and Curatarr
leaves a show Sonarr has unmonitored alone. These are listed on Enhance →
Problems, with a button to search for them once.
"""

from datetime import timedelta

from . import db
from .models import LifecycleAction, MediaIdentity, MediaPart
from .policy import retained

IGNORED_KEY = "missing_keep_ignored"
# A request this recent is still being worked on (searching, downloading).
WAITING = timedelta(days=2)
# An episode that aired this recently may simply not be released yet.
JUST_AIRED = timedelta(days=1)


def _codes(parts):
    return ", ".join(f"S{p.season_number:02}E{p.episode_number:02}" for p in parts)


def or_none(column, excluded):
    """column != excluded, counting NULL as not excluded."""
    return (column.is_(None)) | (column != excluded)


def rows():
    """Shows missing always-keep episodes, most recently requested last.

    Only Season 1 counts, as with the automatic fill ("entire series" means
    never trim, not download everything). Each row says whether a request is
    still in progress ("waiting") and whether Sonarr has the show unmonitored.
    """
    from .services import _as_datetime, resolved_policy, setting, utcnow

    now = utcnow()
    parts = (
        db.session.query(MediaPart)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            MediaIdentity.media_type == "series",
            MediaIdentity.sonarr_id.isnot(None),
            MediaIdentity.missing_since.is_(None),
            # Daily shows are numbered by date; a reset keeps none of them.
            or_none(MediaIdentity.series_type, "daily"),
            MediaPart.kind == "episode",
            MediaPart.season_number == 1,
            MediaPart.has_file.is_(False),
            MediaPart.sonarr_episode_id.isnot(None),
            MediaPart.air_date.isnot(None),
            MediaPart.air_date <= now - JUST_AIRED,
        )
        .order_by(MediaPart.episode_number)
        .all()
    )
    by_show = {}
    for part in parts:
        by_show.setdefault(part.media_identity_id, []).append(part)
    ignored = setting(IGNORED_KEY, {}) or {}
    found = []
    for media_id, missing in by_show.items():
        media = missing[0].media
        policy, _ = resolved_policy(media)
        missing = [p for p in missing if retained(p, policy)]
        ids = {p.sonarr_episode_id for p in missing}
        if not missing or ids <= set(ignored.get(media_id, [])):
            continue
        last = (
            db.session.query(LifecycleAction)
            .filter_by(media_identity_id=media_id, action_type="sonarr_season_search")
            .order_by(LifecycleAction.created_at.desc())
            .first()
        )
        searched = _as_datetime(last.created_at) if last else None
        found.append(
            {
                "media": media,
                "missing": missing,
                "codes": _codes(missing),
                "unmonitored": media.arr_monitored is False,
                "searched": searched,
                "waiting": bool(searched and now - searched < WAITING),
            }
        )
    found.sort(key=lambda r: (r["waiting"], r["media"].title.lower()))
    return found


def count():
    """Shows that need someone: missing episodes and no request in progress."""
    return sum(1 for row in rows() if not row["waiting"])


def search_once(media):
    """Ask Sonarr for the show's missing always-keep episodes, now.

    Sonarr won't search a show it has unmonitored, so the show is monitored
    (only the missing episodes are, so nothing else downloads). Returns the
    result of sending the request to Sonarr.
    """
    from .lifecycle import execute_action
    from .services import _request_season, resolved_policy, utcnow

    policy, _ = resolved_policy(media)
    action = _request_season(
        media,
        1,
        f"Search requested for {media.title}'s missing always-keep episodes",
        episode_filter=lambda part: retained(part, policy),
        attempt=utcnow().isoformat(),
    )
    if action is None:
        raise ValueError("Nothing is missing any more.")
    db.session.commit()
    return execute_action(action.id)


def ignore(media):
    """Stop listing the show for the episodes missing now (new gaps still show)."""
    from .services import set_setting, setting

    row = next((r for r in rows() if r["media"].id == media.id), None)
    if row is None:
        return
    ignored = dict(setting(IGNORED_KEY, {}) or {})
    ignored[media.id] = sorted(
        set(ignored.get(media.id, [])) | {p.sonarr_episode_id for p in row["missing"]}
    )
    set_setting(IGNORED_KEY, ignored)
