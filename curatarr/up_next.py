"""Get what people will watch next ready first.

Every ten minutes Curatarr asks Jellyfin what each person is in the middle
of: the next episode of each show they're watching (and the two after it),
and films and episodes they stopped partway through. Those files' jobs
(chapters, subtitles, skip markers) go to the front of the queue, so a new
episode has its chapters and recap marker by the time someone reaches it,
instead of waiting behind a backlog of the library's back catalogue.
"""

from datetime import timedelta

from . import db
from .integrations import IntegrationError
from .models import MediaIdentity, MediaPart, utcnow

KEY = "watching_next"
EVERY = timedelta(minutes=10)
AFTER = 2  # episodes past the next one


def _norm(item_id) -> str:
    return str(item_id or "").replace("-", "").lower()


def refresh(now=None) -> int:
    """Worker cycle: remember the files people will watch next. Returns how many."""
    from .services import _as_datetime, client, counted_users, set_setting, setting

    now = now or utcnow()
    stored = setting(KEY) or {}
    checked = _as_datetime(stored.get("at"))
    if checked and now - checked < EVERY:
        return len(stored.get("parts") or [])
    jellyfin = client("jellyfin")
    wanted = {}  # Jellyfin item id: the users about to watch it
    try:
        for user in counted_users(jellyfin.users()):
            for item in jellyfin.next_up(user["Id"]) + jellyfin.resume(user["Id"]):
                wanted.setdefault(_norm(item.get("Id")), set()).add(_norm(user["Id"]))
    except IntegrationError:
        return len(stored.get("parts") or [])
    items = set(wanted)
    db.session.rollback()  # asked Jellyfin outside any transaction
    parts, by_user = set(), {}

    def add(part_id, item):
        parts.add(part_id)
        for user in wanted.get(item, ()):
            by_user.setdefault(user, set()).add(part_id)

    for part in db.session.query(MediaPart).filter(
        MediaPart.jellyfin_id.isnot(None), MediaPart.has_file.is_(True)
    ):
        item = _norm(part.jellyfin_id)
        if item not in items:
            continue
        add(part.id, item)
        if part.kind == "episode" and part.episode_number is not None:
            # And the next couple, so a binge doesn't catch up with the queue.
            following = (
                db.session.query(MediaPart.id)
                .filter(
                    MediaPart.media_identity_id == part.media_identity_id,
                    MediaPart.season_number == part.season_number,
                    MediaPart.episode_number > part.episode_number,
                    MediaPart.has_file.is_(True),
                )
                .order_by(MediaPart.episode_number)
                .limit(AFTER)
            )
            for row in following:
                add(row.id, item)
    # Films: Jellyfin's id is the title's (their files aren't linked one by one).
    for part_id, item in (
        db.session.query(MediaPart.id, MediaIdentity.jellyfin_id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            MediaPart.kind == "movie_file",
            MediaPart.has_file.is_(True),
            MediaIdentity.jellyfin_id.in_(sorted(items) or [""]),
        )
    ):
        add(part_id, _norm(item))
    set_setting(
        KEY,
        {
            "at": now.isoformat(),
            "parts": sorted(parts),
            "by_user": {user: sorted(ids) for user, ids in by_user.items()},
        },
    )
    db.session.commit()
    return len(parts)


def part_ids(users=None) -> list[str]:
    """Files someone will watch next; with ``users``, only those people."""
    from .services import setting

    stored = setting(KEY) or {}
    if users is None:
        return list(stored.get("parts") or [])
    by_user = stored.get("by_user") or {}
    return sorted({p for user in users for p in by_user.get(_norm(user), [])})
