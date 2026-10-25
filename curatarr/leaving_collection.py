"""A Jellyfin collection of the titles Leaving Soon, kept in step by Curatarr.

Titles join when they're marked Leaving Soon and leave when they're kept,
watched or deleted. The collection is created once and then known by its id,
so renaming it in Jellyfin is fine. Settings → Household turns it off; the
collection is then emptied.
"""

from . import db
from .integrations import IntegrationError
from .models import MediaIdentity, PurgeCandidate

NAME = "Leaving Soon"
KEY = "leaving_collection"


def enabled() -> bool:
    from .curation import options

    return bool(options().get("leaving_collection", True))


def _wanted() -> set[str]:
    if not enabled():
        return set()
    rows = (
        db.session.query(MediaIdentity.jellyfin_id)
        .join(PurgeCandidate, PurgeCandidate.media_identity_id == MediaIdentity.id)
        .filter(
            PurgeCandidate.state == "LEAVING_SOON",
            PurgeCandidate.target_part_ids.is_(None),  # the title, not episodes
            MediaIdentity.jellyfin_id.isnot(None),
            MediaIdentity.missing_since.is_(None),
        )
    )
    return {_norm(jellyfin_id) for (jellyfin_id,) in rows}


def _norm(item_id: str) -> str:
    return item_id.replace("-", "").lower()


def sync() -> dict | None:
    """Make the collection hold exactly the titles Leaving Soon. Asks Jellyfin
    only when that set changed since the last sync (or the collection is new)."""
    from .services import client, set_setting, setting

    stored = setting(KEY, {}) or {}
    wanted = _wanted()
    if not wanted and not stored.get("id"):
        return None  # nothing to show, and no collection yet
    if (
        stored.get("id")
        and set(stored.get("items", [])) == wanted
        and stored.get("poster")
    ):
        return stored
    jellyfin = client("jellyfin")
    try:
        collection = stored.get("id")
        if collection and not jellyfin.collection_exists(collection):
            collection = None  # deleted in Jellyfin: make a new one
        if collection is None:
            if not wanted:
                set_setting(KEY, {})
                db.session.commit()
                return None
            collection = jellyfin.create_collection(NAME, sorted(wanted))
            have = set(wanted)
        else:
            have = {_norm(i) for i in jellyfin.collection_items(collection)}
            if wanted - have:
                jellyfin.add_to_collection(collection, sorted(wanted - have))
            if have - wanted:
                jellyfin.remove_from_collection(collection, sorted(have - wanted))
    except IntegrationError:
        return stored  # try again next pass
    stored = {
        "id": collection,
        "items": sorted(wanted),
        "poster": _set_poster(jellyfin, collection, sorted(wanted)),
    }
    set_setting(KEY, stored)
    db.session.commit()
    return stored


def poster(images: list[bytes]) -> bytes:
    """The collection's poster: up to four of its titles' posters in a grid,
    under a red LEAVING SOON band like the one on their own posters."""
    from io import BytesIO

    from PIL import Image, ImageDraw

    from .artwork import BADGE_RED, _fit

    width, height = 1000, 1500
    canvas = Image.new("RGB", (width, height), "#111418")
    tiles = []
    for data in images[:4]:
        try:
            tiles.append(Image.open(BytesIO(data)).convert("RGB"))
        except OSError:
            continue
    cells = [(0, 0), (1, 0), (0, 1), (1, 1)]
    for tile, (col, row) in zip(tiles, cells, strict=False):
        tile = tile.resize((width // 2, height // 2))
        canvas.paste(tile, (col * width // 2, row * height // 2))
    drawing = ImageDraw.Draw(canvas)
    band = round(height * 0.16)
    top = (height - band) // 2
    drawing.rectangle((0, top, width, top + band), fill=BADGE_RED)
    font, box = _fit(drawing, "LEAVING SOON", round(band * 0.5), width * 0.9)
    drawing.text(
        (
            (width - (box[2] - box[0])) / 2 - box[0],
            top + (band - (box[3] - box[1])) / 2 - box[1],
        ),
        "LEAVING SOON",
        fill="#FFFFFF",
        font=font,
    )
    output = BytesIO()
    canvas.save(output, format="JPEG", quality=90)
    return output.getvalue()


def _set_poster(jellyfin, collection, items) -> bool:
    """Jellyfin draws nothing for a collection made through its API."""
    images = []
    for item in items[:4]:
        try:
            images.append(jellyfin.image(item))
        except IntegrationError:
            continue
    try:
        jellyfin.put_image(collection, poster(images))
    except IntegrationError:
        return False  # tried again next pass
    return True
