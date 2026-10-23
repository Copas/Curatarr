"""Swap posters in another language for ones in Jellyfin's metadata language.

Jellyfin takes a title's poster when the title arrives, preferring one in the
metadata language. A new release often has only foreign posters on TMDB at
first (a Chinese one, a Persian one); Jellyfin takes one of those and keeps
it, even after an English one appears.

So once a day Curatarr looks at the films and shows released in the last
year. It compares each poster with the ones TMDB offers to find which it is:
when that's one in another language and there is one in the metadata
language now, Jellyfin is told to use that instead. A poster that matches
none of them (one chosen by hand, or from another source) is left alone, as
are posters with a Leaving Soon badge. A title without a poster in the
metadata language yet is looked at again every few days.
"""

import time
from datetime import timedelta
from io import BytesIO

import requests
from PIL import Image

from . import db
from .integrations import IntegrationError
from .models import MediaIdentity, PosterSnapshot, utcnow

KEY = "poster_languages"
WINDOW = timedelta(days=365)
EVERY = timedelta(hours=24)
RECHECK = timedelta(days=3)
RUN_SECONDS = 20
CANDIDATES = 15  # of the provider's posters, the first (most liked) this many
SAME = 2.0  # mean grey-level difference below which two posters are one
SIZE = (24, 36)


def enabled() -> bool:
    from .curation import options

    return bool(options().get("poster_language", True))


def _norm(item_id: str) -> str:
    return item_id.replace("-", "").lower()


def _pixels(image_bytes: bytes) -> list[int]:
    image = Image.open(BytesIO(image_bytes)).convert("L").resize(SIZE)
    return list(image.tobytes())


def _distance(a, b) -> float:
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


def _small(url: str) -> str:
    # TMDB serves every poster in several sizes; a small one is enough to compare.
    return url.replace("/t/p/original/", "/t/p/w185/")


def _fetch(url: str) -> bytes | None:
    if not url.startswith("https://"):
        return None
    try:
        response = requests.get(_small(url), timeout=(5, 15))
        response.raise_for_status()
        return response.content
    except requests.RequestException:
        return None


def which(current: bytes, offered: list[dict], fetch=_fetch) -> dict | None:
    """The offered poster the current one is, or None when it's none of them."""
    mine = _pixels(current)
    best, nearest = None, SAME
    for image in offered[:CANDIDATES]:
        data = fetch(image.get("Url") or "")
        if not data:
            continue
        image["_distance"] = distance = _distance(mine, _pixels(data))
        if distance < nearest:
            best, nearest = image, distance
    return best


def check(jellyfin, item_id: str, language: str, fetch=_fetch) -> str:
    """Look at one title's poster: "kept", "replaced" or "waiting" (it's in
    another language and there's none in ours yet)."""
    offered = jellyfin.remote_images(item_id)
    if not offered:
        return "kept"
    match = which(jellyfin.image(item_id), offered, fetch)
    if match is None or match.get("Language") in (language, "", None):
        return "kept"
    # The same artwork is sometimes offered under several languages.
    if any(
        image.get("Language") in (language, "", None)
        and image.get("_distance", SAME) - match["_distance"] < 0.5
        for image in offered
    ):
        return "kept"
    ours = [image for image in offered if image.get("Language") == language]
    if not ours:
        return "waiting"
    jellyfin.download_remote_image(item_id, ours[0])
    return "replaced"


def _language(jellyfin) -> str:
    try:
        configured = (jellyfin.configuration() or {}).get("PreferredMetadataLanguage")
    except IntegrationError:
        configured = None
    return (configured or "en").split("-")[0].lower()


def _titles(jellyfin, now) -> dict[str, str]:
    titles, start = {}, 0
    while True:
        page = jellyfin.released_since((now - WINDOW).date(), start=start) or {}
        items = page.get("Items") or []
        for item in items:
            titles[_norm(item["Id"])] = item.get("Name") or ""
        returned = page.get("Returned", len(items))
        start += returned
        if not returned or start >= (page.get("TotalRecordCount") or 0):
            return titles


def _badged() -> set[str]:
    rows = db.session.query(PosterSnapshot.jellyfin_item_id).filter(
        PosterSnapshot.active.is_(True)
    )
    return {_norm(item) for (item,) in rows}


def sweep(now=None, fetch=_fetch) -> int:
    """The daily pass, a few titles each worker cycle; returns how many
    posters were replaced."""
    from .services import _as_datetime, audit, client, set_setting, setting

    if not enabled():
        return 0
    now = now or utcnow()
    state = dict(setting(KEY, {}) or {})
    finished = _as_datetime(state.get("finished_at"))
    if finished and now - finished < EVERY:
        return 0
    jellyfin = client("jellyfin")
    started = time.monotonic()
    try:
        language = _language(jellyfin)
        titles = _titles(jellyfin, now)
    except IntegrationError:
        return 0
    # Only titles still in the window are remembered.
    kept = {i: at for i, at in (state.get("kept") or {}).items() if i in titles}
    waiting = {i: at for i, at in (state.get("waiting") or {}).items() if i in titles}
    badged = _badged()
    replaced = 0
    todo = [
        item
        for item in sorted(titles)
        if item not in kept
        and item not in badged
        and now - (_as_datetime(waiting.get(item)) or now - RECHECK) >= RECHECK
    ]
    for item in todo:
        if time.monotonic() - started > RUN_SECONDS:
            break
        try:
            outcome = check(jellyfin, item, language, fetch)
        except (IntegrationError, OSError, ValueError):
            continue  # unreachable, or an unreadable picture: next pass
        if outcome == "waiting":
            waiting[item] = now.isoformat()
            continue
        waiting.pop(item, None)
        kept[item] = now.isoformat()
        if outcome == "replaced":
            replaced += 1
            media = (
                db.session.query(MediaIdentity.id)
                .filter(MediaIdentity.jellyfin_id.in_([item, _dashed(item)]))
                .scalar()
            )
            audit(
                "poster_language",
                media,
                f"The poster of {titles[item]} was in another language; "
                "it now has one in the metadata language.",
                f"poster-language:{item}:{now.date().isoformat()}",
            )
    else:
        state["finished_at"] = now.isoformat()
    state["kept"], state["waiting"] = kept, waiting
    set_setting(KEY, state)
    db.session.commit()
    return replaced


def _dashed(item: str) -> str:
    if len(item) != 32:
        return item
    return f"{item[:8]}-{item[8:12]}-{item[12:16]}-{item[16:20]}-{item[20:]}"
