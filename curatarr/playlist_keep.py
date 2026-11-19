"""Titles in a Jellyfin playlist are kept from automatic cleanup.

Someone who puts a film in a playlist means to watch it, so space-saving and
inactivity cleanup leave it alone while it's there. It can still be removed
by hand (from its page, Free up space, or Delete in Jellyfin): those don't go
through the automatic rules. A show counts when any of its episodes is in a
playlist. Curatarr's own Picks playlists don't count: they're its
suggestions, and would otherwise keep every film they suggest.

Every half hour the worker reads which titles the playlists hold
(``KEY``); cleanup and the title pages read that.
"""

from datetime import timedelta

from . import db
from .integrations import IntegrationError
from .models import MediaIdentity, utcnow

KEY = "playlist_titles"  # {"titles": {media id: [playlist names]}, "at": iso}
EVERY = timedelta(minutes=30)


def enabled() -> bool:
    from .curation import options

    return bool(options().get("playlist_keeps", True))


def _norm(item_id) -> str:
    return (item_id or "").replace("-", "").lower()


def _ours() -> set[str]:
    """The Picks playlists Curatarr keeps (picks.py)."""
    from .services import setting

    return {
        _norm(entry.get("playlist"))
        for entry in (setting("picks", {}) or {}).values()
        if entry.get("playlist")
    }


def _items(jellyfin, playlist_id, users):
    """A playlist's items, read as the first account allowed to (a private
    playlist is only readable as its owner); None when none could."""
    for user in users:
        try:
            items = jellyfin.playlist_items(playlist_id, user["Id"])
        except IntegrationError:
            continue
        if items:
            return items
    return None


def sweep(now=None) -> int:
    """Read the playlists again when due; returns how many titles they keep."""
    from .services import _as_datetime, client, set_setting, setting

    if not enabled():
        return 0
    now = now or utcnow()
    state = setting(KEY, {}) or {}
    at = _as_datetime(state.get("at"))
    if at and now - at < EVERY:
        return len(state.get("titles") or {})
    jellyfin = client("jellyfin")
    try:
        playlists = jellyfin.playlists() or []
        users = jellyfin.users() or []
    except IntegrationError:
        return len(state.get("titles") or {})
    ours = _ours()
    names: dict[str, set] = {}  # jellyfin id of a film or show: playlist names
    for playlist in playlists:
        if _norm(playlist.get("Id")) in ours:
            continue
        for item in _items(jellyfin, playlist["Id"], users) or []:
            episode = item.get("Type") in ("Episode", "Season")
            target = _norm(item.get("SeriesId") if episode else item.get("Id"))
            if target:
                names.setdefault(target, set()).add(
                    playlist.get("Name") or "a playlist"
                )
    titles = {}
    if names:
        rows = db.session.query(MediaIdentity.id, MediaIdentity.jellyfin_id).filter(
            MediaIdentity.jellyfin_id.isnot(None)
        )
        for media_id, jellyfin_id in rows:
            found = names.get(_norm(jellyfin_id))
            if found:
                titles[media_id] = sorted(found)
    set_setting(KEY, {"titles": titles, "at": now.isoformat()})
    db.session.commit()
    return len(titles)


def kept() -> dict:
    """{media id: [playlist names]} as last read."""
    from .services import setting

    return (setting(KEY, {}) or {}).get("titles") or {}
