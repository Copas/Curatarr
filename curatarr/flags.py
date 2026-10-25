"""Flags on a film or show, a season, or one episode or file.

**Leave after watched** ("I'm watching this one last time, then it can go"):
setting it starts nothing. Once it's been watched after the flag was set (by
whoever set it, or by anyone), the title is marked Leaving Soon as if asked
for on its page, and the usual notice and rescues apply from then on. A film
is watched when Jellyfin marks it played; a show when its last episode on
disk is played (it's then trimmed to its always-keep episodes, like Reset to
minimum). Playing it before then leaves the flag alone; clearing it cancels.

**Never compress**: files an external transcoder (Tdarr, Unmanic, FileFlows)
must leave exactly as they are. Curatarr never re-encodes video itself; it
lists the protected files' paths for the transcoder to skip, at
``/api/v1/protected-paths?kind=compress`` and, if set, in a text file.
Favourites can be protected automatically.
"""

from datetime import UTC
from pathlib import Path

from . import db
from .integrations import IntegrationError
from .models import (
    EpisodeUserState,
    MediaFlag,
    MediaIdentity,
    MediaPart,
    UserMediaState,
    utcnow,
)

LEAVE = "leave_after_watched"
NEVER_COMPRESS = "never_compress"
FLAGS = (LEAVE, NEVER_COMPRESS)
# {"favourites": protect favourites too, "file": where to write the list}
SETTINGS_KEY = "never_compress"


def settings() -> dict:
    from .services import setting

    return {"favourites": False, "file": ""} | (setting(SETTINGS_KEY) or {})


def save_settings(favourites: bool, file: str) -> dict:
    from .services import set_setting

    file = (file or "").strip()
    if file and not Path(file).is_absolute():
        raise ValueError("The list's file needs a full path, starting with /.")
    options = {"favourites": bool(favourites), "file": file}
    set_setting(SETTINGS_KEY, options)
    db.session.commit()
    write_protected_file()
    return options


def _aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def flags_for(media) -> list[MediaFlag]:
    return (
        db.session.query(MediaFlag)
        .filter_by(media_identity_id=media.id)
        .order_by(MediaFlag.created_at)
        .all()
    )


def find(media, flag, *, season=None, part=None) -> MediaFlag | None:
    return (
        db.session.query(MediaFlag)
        .filter_by(
            media_identity_id=media.id,
            flag=flag,
            season_number=season,
            media_part_id=part.id if part else None,
        )
        .first()
    )


def set_flag(media, flag, *, season=None, part=None, user_id=None, set_by=None):
    """Set a flag on a title, one of its seasons or one episode. Leave after
    watched is for a whole film or show; ``user_id`` is whose watch counts
    (None: anyone's)."""
    from .lifecycle import _active_candidate
    from .services import audit, resolved_policy

    if flag not in FLAGS:
        raise ValueError(f"Unknown flag: {flag}")
    if media.media_type not in {"movie", "series"}:
        raise ValueError(f"{media.title} isn't a film or a TV show.")
    if part is not None and part.media_identity_id != media.id:
        raise ValueError("That episode isn't part of this title.")
    if flag == LEAVE:
        if season is not None or part is not None:
            raise ValueError("Leave after watched is for a whole film or show for now.")
        policy, _ = resolved_policy(media)
        if policy["never_purge"]:
            raise ValueError(f"Never Purge is on for {media.title}.")
        existing = _active_candidate(media.id)
        if existing and existing.state in {"LEAVING_SOON", "APPROVED", "EXECUTING"}:
            raise ValueError(f"{media.title} is already on its way out.")
    row = find(media, flag, season=season, part=part)
    if row is None:
        row = MediaFlag(
            media_identity_id=media.id,
            flag=flag,
            season_number=season,
            media_part_id=part.id if part else None,
        )
        db.session.add(row)
    row.jellyfin_user_id = user_id if flag == LEAVE else None
    row.set_by = set_by
    row.created_at = utcnow()  # watching counts from now
    db.session.flush()
    audit(
        f"flag_{flag}_set",
        media.id,
        _describe(media, row) + ".",
        f"flag:{row.id}:{row.created_at.isoformat()}",
        payload={"user": user_id, "season": season, "part": row.media_part_id},
    )
    db.session.commit()
    if flag == NEVER_COMPRESS:
        write_protected_file()
    return row


def clear_flag(media, flag, *, season=None, part=None, why="cleared") -> bool:
    from .services import audit

    row = find(media, flag, season=season, part=part)
    if row is None:
        return False
    audit(
        f"flag_{flag}_cleared",
        media.id,
        f"{media.title}: {why} ({describe(row)}).",
        f"flag-cleared:{row.id}:{utcnow().isoformat()}",
    )
    db.session.delete(row)
    db.session.commit()
    if flag == NEVER_COMPRESS:
        write_protected_file()
    return True


def _where(row) -> str:
    if row.media_part_id:
        part = db.session.get(MediaPart, row.media_part_id)
        if part and part.season_number is not None:
            return f" (S{part.season_number:02d}E{part.episode_number or 0:02d})"
        return " (one file)"
    if row.season_number is not None:
        return f" (season {row.season_number})"
    return ""


def describe(row) -> str:
    """How a flag reads on the title's page."""
    from .services import user_display_name

    if row.flag == LEAVE:
        who = (
            f"once {user_display_name(row.jellyfin_user_id)} has watched it"
            if row.jellyfin_user_id
            else "once someone has watched it"
        )
        return f"Leaving Soon {who}"
    return "Never compress" + _where(row)


def _describe(media, row) -> str:
    return f"{media.title}: {describe(row)}"


# --- Leave after watched ----------------------------------------------------


def _last_episode(media):
    """The show's last episode with a file (specials aside)."""
    parts = [
        p
        for p in media.parts
        if p.kind == "episode" and p.has_file and (p.season_number or 0) > 0
    ]
    return max(
        parts, key=lambda p: (p.season_number, p.episode_number or 0), default=None
    )


def _watched_since(media, row, jellyfin) -> bool:
    since = _aware(row.created_at)
    if media.media_type == "series":
        last = _last_episode(media)
        if last is None:
            return False
        query = db.session.query(EpisodeUserState).filter_by(
            media_part_id=last.id, played=True
        )
        if row.jellyfin_user_id:
            query = query.filter_by(jellyfin_user_id=row.jellyfin_user_id)
        return any(s.last_played_at and _aware(s.last_played_at) > since for s in query)
    # A film: Jellyfin knows whether it was played to the end, and when.
    if not media.jellyfin_id:
        return False
    users = (
        [row.jellyfin_user_id]
        if row.jellyfin_user_id
        else [
            u
            for (u,) in db.session.query(UserMediaState.jellyfin_user_id).filter_by(
                media_identity_id=media.id
            )
            if u
        ]
    )
    for user_id in users:
        data = jellyfin.user_data(user_id, media.jellyfin_id)
        played_at = _as_datetime(data.get("LastPlayedDate"))
        if data.get("Played") and played_at and played_at > since:
            return True
    return False


def _as_datetime(value):
    from .services import _as_datetime as parse

    return _aware(parse(value))


def sweep() -> int:
    """Mark Leaving Soon what's been watched since it was flagged. Returns
    how many were marked."""
    from .lifecycle import request_removal
    from .services import client

    rows = db.session.query(MediaFlag).filter_by(flag=LEAVE).all()
    if not rows:
        return 0
    try:
        jellyfin = client("jellyfin")
    except IntegrationError:
        return 0
    marked = 0
    for row in rows:
        media = db.session.get(MediaIdentity, row.media_identity_id)
        if media is None or media.missing_since:
            continue
        try:
            watched = _watched_since(media, row, jellyfin)
        except IntegrationError:
            continue  # Jellyfin is away: look again next time
        if not watched:
            continue
        try:
            request_removal(media, where="Leave after watched")
        except ValueError as exc:
            db.session.rollback()
            clear_flag(media, LEAVE, why=f"not marked Leaving Soon: {exc}")
            continue
        clear_flag(media, LEAVE, why="watched, so marked Leaving Soon")
        marked += 1
    return marked


# --- Never compress -----------------------------------------------------------


def protected_parts() -> list[MediaPart]:
    """Every file a transcoder must leave alone: flagged ones, and
    favourites when that's on."""
    titles, seasons, parts = set(), set(), set()
    for row in db.session.query(MediaFlag).filter_by(flag=NEVER_COMPRESS):
        if row.media_part_id:
            parts.add(row.media_part_id)
        elif row.season_number is not None:
            seasons.add((row.media_identity_id, row.season_number))
        else:
            titles.add(row.media_identity_id)
    if settings()["favourites"]:
        titles |= {
            m
            for (m,) in db.session.query(UserMediaState.media_identity_id).filter_by(
                favorite=True
            )
        }
        parts |= {
            p
            for (p,) in db.session.query(EpisodeUserState.media_part_id).filter_by(
                favorite=True
            )
        }
    if not (titles or seasons or parts):
        return []
    show_ids = titles | {m for m, _s in seasons}
    found = (
        db.session.query(MediaPart)
        .filter(MediaPart.has_file.is_(True))
        .filter(
            MediaPart.media_identity_id.in_(show_ids or {""})
            | MediaPart.id.in_(parts or {""})
        )
        .all()
    )
    return [
        p
        for p in found
        if p.media_identity_id in titles
        or p.id in parts
        or (p.media_identity_id, p.season_number) in seasons
    ]


def protected_paths() -> list[str]:
    """The protected files' paths, as Sonarr and Radarr report them."""
    return sorted({p.file_path for p in protected_parts() if p.file_path})


def write_protected_file() -> None:
    """Keep the list's file (when one is set) up to date; never raises."""
    target = settings()["file"]
    if not target:
        return
    text = "".join(f"{path}\n" for path in protected_paths())
    path = Path(target)
    try:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return
        partial = path.with_name(path.name + ".partial")
        partial.write_text(text, encoding="utf-8")
        partial.replace(path)
    except OSError:
        from flask import current_app

        current_app.logger.warning("Couldn't write the protected paths to %s", path)
