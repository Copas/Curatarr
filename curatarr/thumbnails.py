"""Pace file rebuilds to Jellyfin's thumbnails (trickplay).

A rebuilt file (TV-friendly audio, the Roku seek repair, removing other
languages) has the same picture, but to Jellyfin it's a new file: it drops
the item's scrubbing thumbnails and makes them again, one file at a time,
minutes each. Rebuilding faster than that leaves hundreds of titles without
thumbnails for days, and the nightly task runs for a day.

So Curatarr remembers the files it rebuilt that had thumbnails before, and
starts another rebuild only while fewer than a few of those are still
waiting for Jellyfin to make theirs. A file waits at most two days.
"""

from datetime import timedelta

from .integrations import IntegrationError
from .models import utcnow

KEY = "trickplay_waiting"
MAX_WAITING = 3
GIVE_UP = timedelta(days=2)


def enabled() -> bool:
    from . import enrichment

    try:
        return bool(enrichment.options()["pace_rebuilds"])
    except Exception:  # noqa: BLE001  an unreadable config paces as by default
        return True


def _item(part, media):
    from .enrichment import jellyfin_item_id

    return jellyfin_item_id(part, media)


def _with_thumbnails(item_ids) -> set[str]:
    """The ones Jellyfin has thumbnails for (raises IntegrationError)."""
    from .services import client

    if not item_ids:
        return set()
    found = client("jellyfin").items_trickplay(sorted(item_ids))
    return {
        item.get("Id", "").replace("-", "").lower()
        for item in found
        if item.get("Trickplay")
    }


def has_thumbnails(part, media) -> bool:
    """Whether Jellyfin has thumbnails for this file now (False if unsure)."""
    item = _item(part, media)
    if not item:
        return False
    try:
        return _normal(item) in _with_thumbnails([item])
    except IntegrationError:
        return False


def _normal(item_id: str) -> str:
    return item_id.replace("-", "").lower()


def note_rebuilt(part, media, had_thumbnails: bool) -> None:
    """Remember a rebuilt file Jellyfin has to make thumbnails for again."""
    from . import db
    from .services import set_setting, setting

    item = _item(part, media)
    if not had_thumbnails or not item:
        return  # nothing was lost (or thumbnails are off for its library)
    waiting = [w for w in setting(KEY, []) or [] if w["item"] != _normal(item)]
    waiting.append({"item": _normal(item), "at": utcnow().isoformat()})
    set_setting(KEY, waiting)
    db.session.commit()


def before_rebuild(path):
    """For a rebuild Curatarr makes while editing a file in place (see
    mediafile._mkvpropedit): the file's part and whether it has thumbnails
    now, or None when that can't be told (no app context, unknown file)."""
    from . import db
    from .models import MediaPart

    try:
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        part = (
            db.session.query(MediaPart)
            .filter(MediaPart.has_file.is_(True), MediaPart.file_path.like(f"%/{name}"))
            .first()
        )
        if part is None:
            return None
        return part, has_thumbnails(part, part.media)
    except Exception:  # noqa: BLE001  outside the app (tools, tests): nothing to note
        return None


def after_rebuild(noted) -> None:
    if noted:
        part, had_thumbnails = noted
        note_rebuilt(part, part.media, had_thumbnails)


def waiting() -> list[dict]:
    """Rebuilt files still without thumbnails; checks with Jellyfin and
    forgets the ones that have them again, or waited too long."""
    from . import db
    from .services import _as_datetime, set_setting, setting

    entries = setting(KEY, []) or []
    if not entries:
        return []
    now = utcnow()
    recent = [w for w in entries if (_as_datetime(w["at"]) or now) > now - GIVE_UP]
    done = _with_thumbnails({w["item"] for w in recent})
    still = [w for w in recent if w["item"] not in done]
    if still != entries:
        set_setting(KEY, still)
        db.session.commit()
    return still


def room() -> bool:
    """Whether another rebuild may start now."""
    if not enabled():
        return True
    try:
        return len(waiting()) < MAX_WAITING
    except IntegrationError:
        return False  # Jellyfin is unreachable: its refresh would fail too


def status() -> dict | None:
    """For Enhance: how many rebuilt files await thumbnails (no Jellyfin call)."""
    from .services import setting

    if not enabled():
        return None
    count = len(setting(KEY, []) or [])
    return {"waiting": count, "paused": count >= MAX_WAITING}
