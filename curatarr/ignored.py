"""Jellyfin libraries Curatarr leaves alone entirely.

A library switched to Ignore on Setup isn't read in, cleaned up or enhanced,
and the features that work from Jellyfin directly (rather than from
Curatarr's own records) skip what's in it too: Recently watched, Finished at
the credits, playback trouble, poster languages and movie skip markers. Which
items are in an ignored library is asked of Jellyfin at most every half hour.
"""

import time

from . import db
from .integrations import IntegrationError
from .models import Library

REFRESH_SECONDS = 1800
PAGE = 500
_cache = {"at": None, "key": None, "ids": frozenset()}


def bare(item_id) -> str:
    return str(item_id or "").replace("-", "").lower()


def library_ids() -> set:
    """Jellyfin ids of the ignored libraries."""
    return {
        jid
        for (jid,) in db.session.query(Library.jellyfin_library_id).filter(
            Library.enabled.is_(False)
        )
    }


def item_ids(jellyfin=None) -> frozenset:
    """Every item (films, shows, seasons, episodes) in an ignored library, as
    bare ids. When Jellyfin can't be asked, the last list stands."""
    libraries = library_ids()
    if not libraries:
        return frozenset()
    key = frozenset(libraries)
    now = time.monotonic()
    if (
        _cache["key"] == key
        and _cache["at"] is not None
        and now - _cache["at"] < REFRESH_SECONDS
    ):
        return _cache["ids"]
    if jellyfin is None:
        from .services import client

        try:
            jellyfin = client("jellyfin")
        except IntegrationError:
            return _cache["ids"]
    found = set()
    try:
        for library in libraries:
            start = 0
            while True:
                page = jellyfin.items(library, start, PAGE) or {}
                items = page.get("Items") or []
                found |= {bare(i.get("Id")) for i in items}
                start += len(items)
                if not items or start >= (page.get("TotalRecordCount") or 0):
                    break
    except IntegrationError:
        return _cache["ids"]
    _cache.update(at=now, key=key, ids=frozenset(found - {""}))
    return _cache["ids"]


def drop(items, jellyfin=None, key="Id") -> list:
    """``items`` (Jellyfin's dicts) without those in an ignored library."""
    items = list(items or [])
    ignored = item_ids(jellyfin) if items else frozenset()
    if not ignored:
        return items
    return [i for i in items if bare(i.get(key)) not in ignored]


def forget() -> None:
    """Look again next time (a library was just switched)."""
    _cache.update(at=None, key=None, ids=frozenset())
