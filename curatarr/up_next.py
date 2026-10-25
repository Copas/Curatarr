"""Get what people will watch next ready first.

Every ten minutes Curatarr asks Jellyfin what each person is in the middle
of: the next episode of each show they're watching, and films and episodes
they stopped partway through. Every worker cycle it also looks at what's
playing right now, so pressing play moves a file up within seconds. Each
episode brings the few after it (into the next season too), so a binge
doesn't catch up with the queue. Those files' jobs (chapters, subtitles,
skip markers, TV-friendly audio, the seek index repair) go to the front of
the queue, instead of waiting behind a backlog of the library's back
catalogue.
"""

from datetime import timedelta

from sqlalchemy import and_, or_

from . import db
from .integrations import IntegrationError
from .models import MediaIdentity, MediaPart, utcnow

KEY = "watching_next"
EVERY = timedelta(minutes=10)
AFTER = 5  # episodes past the next one (or the one playing)


def _norm(item_id) -> str:
    return str(item_id or "").replace("-", "").lower()


def _files(wanted: dict[str, set]) -> tuple[list, dict]:
    """The files of Jellyfin items (each episode followed by the ones after
    it), and which of them each person is about to watch."""
    items = set(wanted)
    parts: dict[str, None] = {}  # in order, once each
    by_user: dict[str, set] = {}

    def add(part_id, item):
        parts.setdefault(part_id, None)
        for user in wanted.get(item, ()):
            by_user.setdefault(user, set()).add(part_id)

    if not items:
        return [], by_user
    for part in db.session.query(MediaPart).filter(
        MediaPart.jellyfin_id.isnot(None), MediaPart.has_file.is_(True)
    ):
        item = _norm(part.jellyfin_id)
        if item not in items:
            continue
        add(part.id, item)
        if part.kind == "episode" and part.episode_number is not None:
            season = part.season_number or 0
            following = (
                db.session.query(MediaPart.id)
                .filter(
                    MediaPart.media_identity_id == part.media_identity_id,
                    MediaPart.kind == "episode",
                    MediaPart.has_file.is_(True),
                    or_(
                        MediaPart.season_number > season,
                        and_(
                            MediaPart.season_number == part.season_number,
                            MediaPart.episode_number > part.episode_number,
                        ),
                    ),
                )
                .order_by(MediaPart.season_number, MediaPart.episode_number)
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
            MediaIdentity.jellyfin_id.in_(sorted(items)),
        )
    ):
        add(part_id, _norm(item))
    return list(parts), by_user


def _note_playing(jellyfin) -> None:
    """Every cycle: what's playing right now (and the episodes after it)."""
    from .services import set_setting, setting

    try:
        sessions = jellyfin.sessions() or []
    except IntegrationError:
        return
    wanted = {}
    for session in sessions:
        item = (session.get("NowPlayingItem") or {}).get("Id")
        if item:
            wanted.setdefault(_norm(item), set()).add(_norm(session.get("UserId")))
    db.session.rollback()  # asked Jellyfin outside any transaction
    parts, by_user = _files(wanted)
    stored = setting(KEY) or {}
    playing = {
        "playing": parts,  # what's playing first, then what follows it
        "playing_by_user": {user: sorted(ids) for user, ids in by_user.items()},
    }
    if any(stored.get(k) != v for k, v in playing.items()):
        set_setting(KEY, stored | playing)
        db.session.commit()


def refresh(now=None) -> int:
    """Worker cycle: remember the files people will watch next. Returns how many."""
    from .services import _as_datetime, client, counted_users, set_setting, setting

    now = now or utcnow()
    jellyfin = client("jellyfin")
    _note_playing(jellyfin)
    stored = setting(KEY) or {}
    checked = _as_datetime(stored.get("at"))
    if checked and now - checked < EVERY:
        return len(stored.get("parts") or [])
    wanted = {}  # Jellyfin item id: the users about to watch it
    try:
        for user in counted_users(jellyfin.users()):
            for item in jellyfin.next_up(user["Id"]) + jellyfin.resume(user["Id"]):
                wanted.setdefault(_norm(item.get("Id")), set()).add(_norm(user["Id"]))
    except IntegrationError:
        return len(stored.get("parts") or [])
    db.session.rollback()  # asked Jellyfin outside any transaction
    parts, by_user = _files(wanted)
    set_setting(
        KEY,
        (setting(KEY) or {})
        | {
            "at": now.isoformat(),
            "parts": sorted(parts),
            "by_user": {user: sorted(ids) for user, ids in by_user.items()},
        },
    )
    db.session.commit()
    return len(parts)


# How many of the front of the line are ordered one by one; the rest of what
# people will watch next follows as one group.
RANKED = 60


def rank(column):
    """For ORDER BY: a file's place in the line, what's playing first, then
    the episodes after it and what people will watch next; everything else
    after them."""
    from sqlalchemy import case, literal

    ids = part_ids()
    if not ids:
        return literal(0)
    whens = [(column == part_id, place) for place, part_id in enumerate(ids[:RANKED])]
    if ids[RANKED:]:
        whens.append((column.in_(ids[RANKED:]), RANKED))
    return case(*whens, else_=RANKED + 1)


def part_ids(users=None) -> list[str]:
    """Files someone will watch next; with ``users``, only those people."""
    from .services import setting

    stored = setting(KEY) or {}
    playing = list(stored.get("playing") or [])
    if users is None:
        return playing + [p for p in stored.get("parts") or [] if p not in playing]
    found = set()
    for key in ("by_user", "playing_by_user"):
        by_user = stored.get(key) or {}
        found |= {p for user in users for p in by_user.get(_norm(user), [])}
    return sorted(found)
