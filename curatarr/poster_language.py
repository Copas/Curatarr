"""Swap posters in another language for ones in Jellyfin's metadata language.

Jellyfin takes a title's poster when the title arrives, preferring one in the
metadata language. A new release often has only foreign posters on TMDB at
first (a Chinese one, a Persian one); Jellyfin takes one of those and keeps
it, even after an English one appears.

So Curatarr looks at every film and show's poster, and again whenever
Jellyfin changes it (a refresh or a new season can bring a foreign one to an
old show): it remembers the poster's tag, which changes with the picture. It
compares the poster with the ones TMDB offers to find which it is: when
that's one in another language and there is one in the metadata language
now, Jellyfin is told to use that instead. A poster that matches none of
them (one chosen by hand, or from another source) is left alone, as are
posters with a Leaving Soon badge. A title without a poster in the metadata
language yet is looked at again every few days (every month once it's over
a year old).
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
# How posters are matched. Raised when that changes: every title is looked at
# again at once, since "kept" is only rechecked when its poster changes.
#   2: Jellyfin's copies in another shape are cut to fit (_fitted), and every
#      offer is compared (the first pass with tags ran before this and kept
#      foreign posters it couldn't recognise)
RULE = 2
WINDOW = timedelta(days=365)  # released within it: new posters come often
EVERY = timedelta(hours=24)
RECHECK = timedelta(days=3)
RECHECK_OLDER = timedelta(days=30)
RUN_SECONDS = 20
CANDIDATES = 15  # of the provider's posters, the first (most liked) this many
MORE = 200  # …and when it's none of those, the rest (a show can have 100+)
SAME = 2.5  # mean grey-level difference below which two posters are one
SIZE = (24, 36)
STEPS = 20  # positions tried when one poster is a crop of the other's shape


def enabled() -> bool:
    from .curation import options

    return bool(options().get("poster_language", True))


def _norm(item_id: str) -> str:
    return item_id.replace("-", "").lower()


def _picture(image_bytes: bytes) -> Image.Image:
    image = Image.open(BytesIO(image_bytes)).convert("L")
    image.thumbnail((240, 360))  # plenty to compare; keeps the shape
    return image


def _grid(image) -> list[int]:
    return list(image.resize(SIZE).tobytes())


def _distance(a, b) -> float:
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


def _fitted(mine: Image.Image, theirs: Image.Image) -> float:
    """How far apart two posters are, allowing for Jellyfin keeping a copy
    of another shape (1000x1426 of TMDB's 2:3, cropped where it chose):
    the taller is cut to the other's shape at several heights, the best
    kept. Same shape: compared as they are."""
    ratio_mine = mine.height / mine.width
    ratio_theirs = theirs.height / theirs.width
    if abs(ratio_mine - ratio_theirs) < 0.01:
        return _distance(_grid(mine), _grid(theirs))
    fixed, cut = (mine, theirs) if ratio_theirs > ratio_mine else (theirs, mine)
    target = _grid(fixed)
    height = round(cut.width * fixed.height / fixed.width)
    best = float("inf")
    for step in range(STEPS + 1):
        top = round((cut.height - height) * step / STEPS)
        part = cut.crop((0, top, cut.width, top + height))
        best = min(best, _distance(target, _grid(part)))
    return best


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
    mine = _picture(current)
    best, nearest = None, SAME
    for n, image in enumerate(offered[:MORE]):
        if n == CANDIDATES and best is not None:
            break  # found among the most liked; the rest needn't be fetched
        data = fetch(image.get("Url") or "")
        if not data:
            continue
        image["_distance"] = distance = _fitted(mine, _picture(data))
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


def _titles(jellyfin) -> dict[str, dict]:
    """{item: {"name", "tag" (its poster's, None without one), "premiered"}}."""
    titles, start = {}, 0
    while True:
        page = jellyfin.titles_with_posters(start=start) or {}
        items = page.get("Items") or []
        for item in items:
            titles[_norm(item["Id"])] = {
                "name": item.get("Name") or "",
                "tag": (item.get("ImageTags") or {}).get("Primary"),
                "premiered": (item.get("PremiereDate") or "")[:10],
            }
        start += len(items)
        if not items or start >= (page.get("TotalRecordCount") or 0):
            return titles


def _due(waiting_since, premiered: str, now) -> bool:
    """A title waiting for a poster in our language: looked at again after a
    few days, or a month when it's over a year old (new posters are rarer)."""
    if waiting_since is None:
        return True
    recent = premiered >= (now - WINDOW).date().isoformat()
    return now - waiting_since >= (RECHECK if recent else RECHECK_OLDER)


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
    if state.get("rule") != RULE:
        state = {"waiting": state.get("waiting") or {}, "rule": RULE}
    finished = _as_datetime(state.get("finished_at"))
    if finished and now - finished < EVERY:
        return 0
    jellyfin = client("jellyfin")
    started = time.monotonic()
    try:
        language = _language(jellyfin)
        titles = _titles(jellyfin)
    except IntegrationError:
        return 0
    # kept: {item: the tag of the poster found fine}; a new poster (another
    # tag) is looked at again. (Before tags, it held a time: looked at again.)
    kept = {i: tag for i, tag in (state.get("kept") or {}).items() if i in titles}
    waiting = {i: at for i, at in (state.get("waiting") or {}).items() if i in titles}
    badged = _badged()
    replaced = 0
    todo = [
        item
        for item, info in sorted(titles.items())
        if info["tag"]
        and kept.get(item) != info["tag"]
        and item not in badged
        and _due(_as_datetime(waiting.get(item)), info["premiered"], now)
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
        # A replaced poster has a new tag: the next pass confirms it.
        kept[item] = titles[item]["tag"] if outcome == "kept" else None
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
                f"The poster of {titles[item]['name']} was in another language; "
                "it now has one in the metadata language.",
                f"poster-language:{item}:{now.date().isoformat()}",
            )
    else:
        state["finished_at"] = now.isoformat()
    state["kept"], state["waiting"] = kept, waiting
    # What Jellyfin listed, for checking the pass sees the whole library.
    state["titles"] = len(titles)
    state["with_poster"] = sum(1 for info in titles.values() if info["tag"])
    set_setting(KEY, state)
    db.session.commit()
    return replaced


def _dashed(item: str) -> str:
    if len(item) != 32:
        return item
    return f"{item[:8]}-{item[8:12]}-{item[12:16]}-{item[16:20]}-{item[20:]}"
