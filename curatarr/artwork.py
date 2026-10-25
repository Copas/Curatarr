"""Reversible Leaving Soon posters, stored only under application data."""

import hashlib
import re
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from flask import current_app
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
from sqlalchemy.exc import OperationalError

from . import db
from .integrations import IntegrationError
from .models import MediaIdentity, PosterSnapshot, PurgeCandidate, utcnow
from .services import audit, client


def badge_label(candidate, today=None) -> str:
    """The banner's words: days left when a deletion date is set (the poster
    is redrawn as they count down), else Leaving Soon."""
    when = candidate.scheduled_delete_at
    if not when:
        return "LEAVING SOON"
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    today = today or datetime.now().astimezone().date()
    days = (when.astimezone().date() - today).days
    if days <= 0:
        return "LAST DAY"
    return f"{days} DAY{'' if days == 1 else 'S'} LEFT"


BADGE_RED = "#E50914"
# Part of each snapshot's recorded label: a new style redraws every poster.
BADGE_STYLE = "red-tag-4"


def _fit(drawing, label, size, max_width):
    while True:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", size)
        box = drawing.textbbox((0, 0), label, font=font)
        if box[2] - box[0] <= max_width or size <= 10:  # longer labels fit too
            return font, box
        size -= 1


def _poster_tag_bottom(drawing, width, height, label) -> int:
    """Where a poster's Leaving Soon tag (as badge_poster draws it) ends."""
    size = max(12, round(width * 0.065))
    _font, box = _fit(drawing, label, size, width * 0.8)
    return round(height * 0.05) + (box[3] - box[1]) + 2 * round(size * 0.45)


def badge_poster(
    source: bytes,
    label: str = "LEAVING SOON",
    kind: str = "Primary",
    *,
    colour: str = BADGE_RED,
    ink: str = "#FFFFFF",
) -> bytes:
    """Posters get a red tag in the top-left corner, like a streaming
    service's, so the film's own title (usually at the bottom) stays visible.
    Wide cards (Thumb) get a shaded red ribbon along the bottom, and
    backdrops, shown full screen behind a title's page, a smaller tag in the
    top-right corner, clear of the menus and text apps draw over them."""
    image = Image.open(BytesIO(source)).convert("RGB")
    width, height = image.size
    drawing = ImageDraw.Draw(image)
    if kind != "Thumb":
        backdrop = kind == "Backdrop"
        size = max(12, round(height * 0.045 if backdrop else width * 0.065))
        font, box = _fit(drawing, label, size, width * (0.4 if backdrop else 0.8))
        text_width, text_height = box[2] - box[0], box[3] - box[1]
        pad_x, pad_y = round(size * 0.55), round(size * 0.45)
        tag_width = text_width + 2 * pad_x
        # A backdrop's top strip sits under apps' header buttons, and phones
        # crop its sides: its tag goes lower and in from the edge.
        top = round(height * (0.24 if backdrop else 0.05))
        left = width - tag_width - round(width * 0.14) if backdrop else 0
        drawing.rectangle(
            (left, top, left + tag_width, top + text_height + 2 * pad_y),
            fill=colour,
        )
        drawing.text(
            (left + pad_x - box[0], top + pad_y - box[1]),
            label,
            fill=ink,
            font=font,
        )
    else:
        ribbon = max(24, round(height * 0.14))
        top = height - ribbon
        edge = max(2, round(height * 0.006))
        for row in range(ribbon):  # bright red fading darker toward the bottom
            shade = row / ribbon
            drawing.line(
                (0, top + row, width, top + row),
                fill=(round(229 - 60 * shade), 9, round(20 - 8 * shade)),
            )
        drawing.rectangle((0, top - edge, width, top), fill="#111111")
        font, box = _fit(drawing, label, round(ribbon * 0.55), width * 0.9)
        drawing.text(
            (
                (width - (box[2] - box[0])) / 2 - box[0],
                top + (ribbon - (box[3] - box[1])) / 2 - box[1],
            ),
            label,
            fill="#FFFFFF",
            font=font,
        )
    output = BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()


# Leave after watched's tag, where Leaving Soon's would be: it isn't yet.
WAITING_AMBER = "#F28C28"
FAVOURITE_GOLD = "#E8B923"
HOUSEHOLD_RED = "#8E1424"
NEW_RED = "#D7263D"
INK = "#1A1405"
# A favourite ribbon's colours: band, writing.
RIBBON_COLOURS = {
    "gold": (FAVOURITE_GOLD, INK),
    "silver": ("#C9CED6", "#15181D"),
    "teal": ("#127C7A", "#FFFFFF"),
    "purple": ("#5B2A86", "#FFFFFF"),
}
# Initials' circles, picked by name so a person keeps their colour.
AVATAR_COLOURS = ("#2E86AB", "#7B2CBF", "#2A9D8F", "#E76F51", "#3D5A80", "#BC4749")


def initials(name: str) -> str:
    """ "Alex and Sam" → "AS", "Pat" → "P"."""
    words = [w for w in re.split(r"[\s&+,]+", name) if w and w.lower() != "and"]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def avatar_colour(name: str) -> str:
    return AVATAR_COLOURS[sum(map(ord, name)) % len(AVATAR_COLOURS)]


def _avatar(picture: bytes | None, name: str, size: int) -> Image.Image:
    """A round picture of a person, or their initials in a coloured circle."""
    scale = 4  # drawn large, then shrunk, for smooth edges
    big = size * scale
    if picture:
        face = Image.open(BytesIO(picture)).convert("RGB")
        side = min(face.size)
        left, top = (face.width - side) // 2, (face.height - side) // 3
        face = face.crop((left, top, left + side, top + side)).resize((big, big))
    else:
        face = Image.new("RGB", (big, big), avatar_colour(name))
        drawing = ImageDraw.Draw(face)
        letters = initials(name)
        font, box = _fit(drawing, letters, round(big * 0.46), big * 0.78)
        drawing.text(
            (
                (big - (box[2] - box[0])) / 2 - box[0],
                (big - (box[3] - box[1])) / 2 - box[1],
            ),
            letters,
            fill="#FFFFFF",
            font=font,
        )
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    ring = Image.new("RGB", (big, big), "#111111")
    inner = round(big * 0.06)
    ring.paste(
        face.resize((big - 2 * inner, big - 2 * inner)),
        (inner, inner),
        mask.resize((big - 2 * inner, big - 2 * inner)),
    )
    round_face = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    round_face.paste(ring, (0, 0), mask)
    return round_face.resize((size, size), Image.LANCZOS)


def favourite_ribbon(
    source: bytes,
    text: str,
    position: str = "bottom",
    *,
    avatars=(),
    new: bool = False,
    household: bool = False,
    names=(),
    large: bool = False,
    colour: str = "gold",
    leaving: str | None = None,
) -> bytes:
    """A gold band marking someone's favourite, or a red one for a household
    favourite, with a red NEW for a recent one.

    On a poster as it shows in a list: a star and the faces of whoever
    favourited it, stacked (``avatars``: (picture or None, name)), or
    ``text`` when there are no faces. ``large`` (the poster on a title's own
    page, where there's room): a star and each full name, one under the
    other. Along the bottom by default, or along the top (below the
    poster's Leaving Soon tag, ``leaving``, when it has one), or at the top
    right, mirroring Leaving Soon's tag (top left) so both fit. Wide pictures
    (an episode's thumbnail) get a deeper band."""
    image = Image.open(BytesIO(source)).convert("RGB")
    image.thumbnail((1000, 1500))
    start = 0
    if position == "top" and leaving:
        width, height = image.size
        start = _poster_tag_bottom(ImageDraw.Draw(image), width, height, leaving)
        start += round(height * 0.01)
    if large and names:
        _ribbon_list(image, list(names), position, new, household, colour, start)
    else:
        _ribbon_band(
            image, text, position, list(avatars), new, household, colour, start
        )
    output = BytesIO()
    image.save(output, format="JPEG", quality=88)
    return output.getvalue()


def _ribbon_colours(household, colour="gold"):
    if household:
        return HOUSEHOLD_RED, FAVOURITE_GOLD
    return RIBBON_COLOURS.get(colour, RIBBON_COLOURS["gold"])


def _pill(drawing, x, middle, size):
    """A red NEW at x, centred on middle; returns its width."""
    font, box = _fit(drawing, "NEW", round(size * 0.42), size * 2)
    width = box[2] - box[0] + 2 * round(size * 0.22)
    height = box[3] - box[1] + 2 * round(size * 0.14)
    drawing.rounded_rectangle(
        (x, middle - height / 2, x + width, middle + height / 2),
        radius=round(height / 2),
        fill=NEW_RED,
    )
    drawing.text(
        (x + round(size * 0.22) - box[0], middle - (box[3] - box[1]) / 2 - box[1]),
        "NEW",
        fill="#FFFFFF",
        font=font,
    )
    return width


def _pill_width(drawing, size):
    _font, box = _fit(drawing, "NEW", round(size * 0.42), size * 2)
    return box[2] - box[0] + 2 * round(size * 0.22)


def _edge(drawing, width, height, y):
    """The thin dark line between a full-width band and the picture."""
    thick = max(2, round(height * 0.004))
    drawing.rectangle((0, y - thick, width, y + thick), fill="#111111")


def _ribbon_band(image, text, position, faces, new, household, colour, start=0):
    """One line: the star (or heart) and the faces stacked, or the text."""
    width, height = image.size
    drawing = ImageDraw.Draw(image)
    fill, ink = _ribbon_colours(household, colour)
    wide = width > height
    across = position in {"bottom", "top"}
    if across:
        band = max(24, round(height * (0.15 if wide else 0.085)))
        top = height - band if position == "bottom" else start
        if start:
            _edge(drawing, width, height, top)
        _edge(drawing, width, height, top if position == "bottom" else top + band)
        drawing.rectangle((0, top, width, top + band), fill=fill)
    else:
        band = max(24, round(width * (0.06 if wide else 0.1)))
        top = round(height * 0.05)
    pad = round(band * 0.18)
    face = round(band * 0.8)
    step = round(face * 0.84)  # each face tucked a little under the next
    faces = faces[:5]
    faces_width = (face + (len(faces) - 1) * step) if faces else 0
    pill_width = _pill_width(drawing, band) if new else 0
    if faces:
        text = "♥" if household else "★"
    room = width * (0.92 if across else 0.44) - faces_width - pill_width - 4 * pad
    font, box = _fit(
        drawing, text, round(band * (0.62 if faces else 0.6)), max(room, band)
    )
    text_width = box[2] - box[0]
    content = (
        text_width
        + (pad + faces_width if faces else 0)
        + (pad + pill_width if new else 0)
    )
    if across:
        x = (width - content) / 2
    else:
        left = width - content - 2 * pad
        drawing.rectangle((left, top, width, top + band), fill=fill)
        x = left + pad
    middle = top + band / 2
    drawing.text(
        (x - box[0], middle - (box[3] - box[1]) / 2 - box[1]), text, fill=ink, font=font
    )
    x += text_width + pad
    for index, (picture, name) in enumerate(faces):
        disc = _avatar(picture, name, face)
        image.paste(disc, (round(x + index * step), round(middle - face / 2)), disc)
    if faces:
        x += faces_width + pad
    if new:
        _pill(ImageDraw.Draw(image), x, middle, band)


RIBBON_LINES = 5  # names listed on a large ribbon; the rest are "+ N more"


def _ribbon_list(image, names, position, new, household, colour, start=0):
    """A star and each full name, one under the other (a household favourite
    says so first)."""
    width, height = image.size
    drawing = ImageDraw.Draw(image)
    fill, ink = _ribbon_colours(household, colour)
    wide = width > height
    lines = [f"★ {name}" for name in names[:RIBBON_LINES]]
    if len(names) > RIBBON_LINES:
        lines[-1] = f"+ {len(names) - RIBBON_LINES + 1} more"
    if household:
        lines.insert(0, "♥ Household favourite")
    size = max(12, round(height * (0.06 if wide else 0.042)))
    gap = round(size * 0.35)
    pad = round(size * 0.6)
    pill_width = _pill_width(drawing, size * 1.4) if new else 0
    across = position in {"bottom", "top"}
    room = width * (0.9 if across else 0.75) - 2 * pad
    fitted = []
    for index, line in enumerate(lines):
        extra = pill_width + pad if new and index == 0 else 0
        fitted.append(_fit(drawing, line, size, room - extra))
    size = min(font.size for font, _box in fitted)  # every line the same size
    fitted = [_fit(drawing, line, size, width) for line in lines]
    line_height = max(box[3] - box[1] for _f, box in fitted)
    block_height = len(lines) * line_height + (len(lines) - 1) * gap
    block_width = max(
        box[2] - box[0] + (pill_width + pad if new and index == 0 else 0)
        for index, (_f, box) in enumerate(fitted)
    )
    band = block_height + 2 * pad
    if across:
        top = height - band if position == "bottom" else start
        if start:
            _edge(drawing, width, height, top)
        _edge(drawing, width, height, top if position == "bottom" else top + band)
        drawing.rectangle((0, top, width, top + band), fill=fill)
        x = (width - block_width) / 2
    else:
        top = round(height * 0.05)
        left = width - block_width - 2 * pad
        drawing.rectangle((left, top, width, top + band), fill=fill)
        x = left + pad
    y = top + pad
    for index, (line, (font, box)) in enumerate(zip(lines, fitted, strict=True)):
        middle = y + line_height / 2
        drawing.text((x, middle), line, fill=ink, font=font, anchor="lm")
        if new and index == 0:
            _pill(drawing, x + box[2] + pad, middle, size * 1.4)
        y += line_height + gap


def _recorded(label: str) -> str:
    return f"{BADGE_STYLE}:{label}"


def _jpeg(source: bytes) -> bytes:
    image = Image.open(BytesIO(source)).convert("RGB")
    output = BytesIO()
    image.save(output, format="JPEG", quality=95)
    return output.getvalue()


WIDE_SIZE = (1280, 720)


def widen(source: bytes, size=WIDE_SIZE) -> bytes:
    """A poster made landscape, for the wide tiles apps show in some rows
    (Jellyfin's Thumb): the poster at full height, centred on a blurred,
    darkened copy of itself."""
    poster = Image.open(BytesIO(source)).convert("RGB")
    width, height = size
    cover = max(width / poster.width, height / poster.height)
    backdrop = poster.resize(
        (round(poster.width * cover), round(poster.height * cover)), Image.LANCZOS
    )
    left = (backdrop.width - width) // 2
    top = (backdrop.height - height) // 2
    backdrop = backdrop.crop((left, top, left + width, top + height))
    backdrop = backdrop.filter(ImageFilter.GaussianBlur(height / 24))
    canvas = ImageEnhance.Brightness(backdrop).enhance(0.45)
    fit = min(height / poster.height, width / poster.width)
    front = poster.resize(
        (round(poster.width * fit), round(poster.height * fit)), Image.LANCZOS
    )
    canvas.paste(front, ((width - front.width) // 2, (height - front.height) // 2))
    output = BytesIO()
    canvas.save(output, format="JPEG", quality=90)
    return output.getvalue()


def _store_dir() -> Path:
    path = Path(current_app.instance_path) / "posters"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _tag(item, kind="Primary"):
    if kind == "Backdrop":
        return next(iter(item.get("BackdropImageTags") or []), None)
    return (item.get("ImageTags") or {}).get(kind)


def badge_kinds(item) -> list[str]:
    """Where a title's artwork shows: its poster, its wide card (the Thumb
    image) and its backdrop (behind its page, and the wide card when there's
    no Thumb)."""
    return [kind for kind in ("Primary", "Thumb", "Backdrop") if _tag(item, kind)]


# Telling a backdrop's copies apart: small grey versions compared outside
# the tag (where a tagged copy and its original are the same picture).
SMALL = (64, 36)
FINE = (160, 90)
TAG_AREA = (0.40, 0.18, 0.92, 0.50)  # left, top, right, bottom, as fractions
SAME_PICTURE = 2.0


def _grey(data: bytes, size) -> list[int]:
    image = Image.open(BytesIO(data)).convert("L")
    if image.width > 320:  # as Jellyfin serves them to compare (maxWidth=320)
        image = image.resize((320, round(image.height * 320 / image.width)))
    return list(image.resize(size).tobytes())


def _difference(a, b, size, *, area="all") -> float:
    """Mean grey difference over the whole picture, or only "inside" or
    "outside" the tag."""
    width, height = size
    left, top, right, bottom = TAG_AREA
    total = count = 0
    for index, (x, y) in enumerate(zip(a, b, strict=True)):
        if area != "all":
            col, row = index % width, index // width
            inside = left * width <= col <= right * width and (
                top * height <= row <= bottom * height
            )
            if inside != (area == "inside"):
                continue
        total += abs(x - y)
        count += 1
    return total / max(count, 1)


def _backdrops(jellyfin, item_id) -> list[dict]:
    listing = getattr(jellyfin, "image_list", None)
    if listing is None:
        return []
    return sorted(
        (i for i in listing(item_id) or [] if i.get("ImageType") == "Backdrop"),
        key=lambda i: i.get("ImageIndex") or 0,
    )


def _put(jellyfin, item_id, data, kind, reference):
    """Upload a picture. Jellyfin adds an uploaded backdrop to the title's
    backdrops instead of replacing the first, so each redraw of the tag left
    the old one behind (a title had 421), apps that rotate backdrops showed
    old day counts, and restoring left tagged copies. So afterwards every
    other copy of the picture (``reference``, the original), tagged or not,
    is deleted and the new one made the first."""
    if kind != "Backdrop":
        return jellyfin.put_image(item_id, data, kind)
    before = {i.get("ImageTag") for i in _backdrops(jellyfin, item_id)}
    jellyfin.put_image(item_id, data, kind)
    return _tidy_backdrops(jellyfin, item_id, data, reference, before)


def _tidy_backdrops(jellyfin, item_id, uploaded, reference, before) -> int:
    """Delete the copies of ``reference`` other than the just-uploaded one,
    and make that one the first backdrop; returns how many were deleted."""
    images = _backdrops(jellyfin, item_id)
    new = [i["ImageIndex"] for i in images if i.get("ImageTag") not in before]
    if len(images) < 2 or not new:
        return 0
    small = {}
    for image in images:
        index = image["ImageIndex"]
        small[index] = jellyfin.image_at(item_id, "Backdrop", index, max_width=320)
    if len(new) > 1:
        # An earlier upload's tag can settle meanwhile: the new one is the
        # one that looks most like what was uploaded, tag text included.
        mine = _grey(uploaded, FINE)
        new.sort(key=lambda i: _difference(_grey(small[i], FINE), mine, FINE))
    keep = new[0]
    original = _grey(reference, SMALL)
    copies = [
        index
        for index, data in small.items()
        if index != keep
        and _difference(_grey(data, SMALL), original, SMALL, area="outside")
        < SAME_PICTURE
    ]
    for index in sorted(copies, reverse=True):  # later indexes shift down
        jellyfin.delete_image(item_id, "Backdrop", index)
    keep -= sum(index < keep for index in copies)
    if keep:
        jellyfin.move_image(item_id, "Backdrop", keep, 0)
    return len(copies)


def _record_applied(snapshot, jellyfin):
    current_tag = _tag(jellyfin.item(snapshot.jellyfin_item_id), snapshot.image_type)
    if current_tag and current_tag != snapshot.original_image_tag:
        snapshot.badged_image_tag = current_tag
    audit(
        "leaving_soon_applied",
        snapshot.media_identity_id,
        "Leaving Soon poster applied.",
        f"artwork-apply:{snapshot.candidate_id}",
        candidate_id=snapshot.candidate_id,
    )
    db.session.commit()


def _record_restored(snapshot, reason):
    snapshot.active = False
    snapshot.restored_at = utcnow()
    audit(
        "leaving_soon_removed",
        snapshot.media_identity_id,
        reason,
        f"artwork-restore:{snapshot.id}",
        candidate_id=snapshot.candidate_id,
    )
    db.session.commit()


def apply_badge(candidate):
    """Badge one candidate's poster, keeping the original to restore.

    The web app (after a decision) and the worker both reconcile artwork; a
    lease per candidate stops them both making a snapshot at once, which could
    save the other's badged poster as the "original"."""
    from . import leases

    scope = f"artwork:{candidate.id}"
    try:
        owner = leases.acquire(scope, 120)
    except OperationalError:
        db.session.rollback()
        return False
    if not owner:
        return False  # being applied elsewhere right now
    try:
        return _apply_badge(candidate)
    finally:
        try:
            leases.release(scope, owner)
        except OperationalError:
            db.session.rollback()  # it expires on its own


def _apply_badge(candidate):
    media = db.session.get(MediaIdentity, candidate.media_identity_id)
    if not media or not media.jellyfin_id:
        return False
    try:
        kinds = badge_kinds(client("jellyfin").item(media.jellyfin_id))
    except (IntegrationError, ValueError):
        return False
    results = [_apply_one(candidate, media, kind) for kind in kinds]
    return bool(results) and all(results)


def _apply_one(candidate, media, kind):
    snapshots = (
        db.session.query(PosterSnapshot)
        .filter_by(candidate_id=candidate.id, active=True, image_type=kind)
        .order_by(PosterSnapshot.badged_image_tag.is_(None), PosterSnapshot.created_at)
        .all()
    )
    existing = snapshots[0] if snapshots else None
    for extra in snapshots[1:]:
        # A second snapshot of the same picture (two processes once badged it at
        # the same moment): the first one restores it, so retire the others.
        extra.active = False
        extra.restored_at = utcnow()
    if snapshots[1:]:
        db.session.commit()
    if existing:
        if existing.badged_image_tag:
            return _keep_badged(existing, badge_label(candidate))
        try:
            jellyfin = client("jellyfin")
            current = jellyfin.image(existing.jellyfin_item_id, kind)
            badged = Path(existing.badged_image_bytes_path).read_bytes()
            if current == badged:
                _record_applied(existing, jellyfin)
                return True
            original = Path(existing.original_image_bytes_path).read_bytes()
            if _jpeg(current) != original:
                return False
            _put(jellyfin, existing.jellyfin_item_id, badged, kind, original)
            _record_applied(existing, jellyfin)
            return True
        except (IntegrationError, OSError, ValueError):
            db.session.rollback()
            return False
    try:
        jellyfin = client("jellyfin")
        original_tag = _tag(jellyfin.item(media.jellyfin_id), kind)
        original = _jpeg(jellyfin.image(media.jellyfin_id, kind))
        label = badge_label(candidate)
        badged = badge_poster(original, label, kind)
        directory = _store_dir()
        snapshot = PosterSnapshot(
            media_identity_id=media.id,
            jellyfin_item_id=media.jellyfin_id,
            candidate_id=candidate.id,
            original_image_tag=original_tag,
            badge_label=_recorded(label),
            image_type=kind,
        )
        db.session.add(snapshot)
        db.session.flush()
        source_path = directory / f"{snapshot.id}-source.jpg"
        badge_path = directory / f"{snapshot.id}-badge.jpg"
        source_path.write_bytes(original)
        badge_path.write_bytes(badged)
        snapshot.original_image_bytes_path = str(source_path)
        snapshot.badged_image_bytes_path = str(badge_path)
        snapshot.active = True
        # The original must be durable before an external image update can happen.
        db.session.commit()
        _put(jellyfin, media.jellyfin_id, badged, kind, original)
        _record_applied(snapshot, jellyfin)
        return True
    except (IntegrationError, OSError, ValueError) as exc:
        current_app.logger.warning(
            "%s artwork application failed for candidate %s: %s",
            kind,
            candidate.id,
            type(exc).__name__,
        )
        db.session.rollback()
        return False


def _keep_badged(snapshot, label):
    """Keep the banner right: redraw it when its words change (days left),
    and badge the poster again if Jellyfin's artwork was replaced since (a
    metadata refresh that replaces images downloads a fresh one). A replaced
    poster becomes the one to restore: someone chose it, or Jellyfin did."""
    try:
        jellyfin = client("jellyfin")
        kind = snapshot.image_type
        current_tag = _tag(jellyfin.item(snapshot.jellyfin_item_id), kind)
        if not current_tag:
            return True
        replaced = current_tag != snapshot.badged_image_tag
        if replaced:
            current = jellyfin.image(snapshot.jellyfin_item_id, kind)
            ours = Path(snapshot.badged_image_bytes_path).read_bytes()
            if current == ours:
                # Our own upload: Jellyfin's tag for it only settles a while
                # after the upload. Not replaced; note the tag it has now.
                snapshot.badged_image_tag = current_tag
                db.session.commit()
                replaced = False
        if not replaced and snapshot.badge_label == _recorded(label):
            return True
        if replaced:
            original = _jpeg(current)
            Path(snapshot.original_image_bytes_path).write_bytes(original)
            snapshot.original_image_tag = current_tag
            snapshot.badged_image_tag = None
            # Artwork Jellyfin downloaded afresh: a clean original again.
            snapshot.original_suspect = False
        else:
            original = Path(snapshot.original_image_bytes_path).read_bytes()
        Path(snapshot.badged_image_bytes_path).write_bytes(
            badge_poster(original, label, kind)
        )
        snapshot.badge_label = _recorded(label)
        # What restoring needs must be durable before the upload replaces it.
        db.session.commit()
        _put(
            jellyfin,
            snapshot.jellyfin_item_id,
            Path(snapshot.badged_image_bytes_path).read_bytes(),
            kind,
            original,
        )
        _record_applied(snapshot, jellyfin)
        if replaced:
            audit(
                "leaving_soon_reapplied",
                snapshot.media_identity_id,
                "Leaving Soon poster applied again: Jellyfin's artwork had been "
                "replaced.",
                f"artwork-reapply:{snapshot.id}:{current_tag}",
                candidate_id=snapshot.candidate_id,
            )
            db.session.commit()
        return True
    except (IntegrationError, OSError, ValueError) as exc:
        current_app.logger.warning(
            "Poster re-application failed for snapshot %s: %s",
            snapshot.id,
            type(exc).__name__,
        )
        db.session.rollback()
        return False


def restore_badge(snapshot):
    if snapshot.original_suspect:
        return _restore_from_providers(snapshot)
    try:
        jellyfin = client("jellyfin")
        kind = snapshot.image_type
        expected = Path(snapshot.badged_image_bytes_path).read_bytes()
        current = jellyfin.image(snapshot.jellyfin_item_id, kind)
        original = Path(snapshot.original_image_bytes_path).read_bytes()
        if _jpeg(current) == original:
            _record_restored(snapshot, "Original poster is already present.")
            return True
        same_bytes = (
            hashlib.sha256(current).digest() == hashlib.sha256(expected).digest()
        )
        if not same_bytes:
            current_tag = _tag(jellyfin.item(snapshot.jellyfin_item_id), kind)
            if (
                not snapshot.badged_image_tag
                or current_tag != snapshot.badged_image_tag
            ):
                # External artwork changed; avoid overwriting someone else's edit.
                return False
        _put(jellyfin, snapshot.jellyfin_item_id, original, kind, original)
        _record_restored(
            snapshot,
            "Original poster restored after cleanup was cancelled.",
        )
        return True
    except (IntegrationError, OSError, ValueError) as exc:
        current_app.logger.warning(
            "Poster restoration failed for snapshot %s: %s",
            snapshot.id,
            type(exc).__name__,
        )
        db.session.rollback()
        return False


def _restore_from_providers(snapshot):
    """The saved original may be badged: have Jellyfin download the artwork
    afresh instead (once per title; the others share the refresh)."""
    try:
        others = db.session.query(PosterSnapshot).filter(
            PosterSnapshot.jellyfin_item_id == snapshot.jellyfin_item_id,
            PosterSnapshot.active.is_(True),
            PosterSnapshot.original_suspect.is_(True),
        )
        client("jellyfin").refresh_images(snapshot.jellyfin_item_id)
        for other in others:
            _record_restored(
                other, "Artwork downloaded afresh after cleanup was cancelled."
            )
        return True
    except IntegrationError as exc:
        current_app.logger.warning(
            "Artwork refresh failed for snapshot %s: %s",
            snapshot.id,
            type(exc).__name__,
        )
        db.session.rollback()
        return False


REPAIR_RETRY = 6  # hours before asking Jellyfin again for the same title


def _repair_suspect_originals():
    """Ask Jellyfin to download fresh artwork for titles whose saved original
    may be badged; the next pass sees genuinely new pictures, saves them as
    the originals and badges them."""
    now = utcnow()
    rows = (
        db.session.query(PosterSnapshot)
        .filter(
            PosterSnapshot.active.is_(True),
            PosterSnapshot.original_suspect.is_(True),
        )
        .all()
    )
    asked = set()
    for snapshot in rows:
        last = snapshot.repair_requested_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if last and (now - last).total_seconds() < REPAIR_RETRY * 3600:
            continue
        if snapshot.jellyfin_item_id not in asked:
            try:
                client("jellyfin").refresh_images(snapshot.jellyfin_item_id)
            except IntegrationError:
                continue
            asked.add(snapshot.jellyfin_item_id)
        snapshot.repair_requested_at = now
    db.session.commit()


def _delete_tagged(jellyfin, item_id, original) -> int:
    """Delete backdrops that are ``original`` with a tag on it."""
    picture = _grey(original, SMALL)
    tagged = []
    for image in _backdrops(jellyfin, item_id):
        index = image["ImageIndex"]
        data = _grey(jellyfin.image_at(item_id, "Backdrop", index, 320), SMALL)
        same = _difference(data, picture, SMALL, area="outside") < SAME_PICTURE
        if same and _difference(data, picture, SMALL, area="inside") >= SAME_PICTURE:
            tagged.append(index)
    for index in sorted(tagged, reverse=True):
        jellyfin.delete_image(item_id, "Backdrop", index)
    return len(tagged)


TIDIED = "backdrops_tidied"


def _tidy_existing_backdrops():
    """Once: delete the tagged backdrop copies uploads left behind (see
    _put), on titles still Leaving Soon and on titles restored since."""
    from .services import set_setting, setting

    if setting(TIDIED):
        return
    rows = (
        db.session.query(PosterSnapshot)
        .filter(PosterSnapshot.image_type == "Backdrop")
        .order_by(PosterSnapshot.active.desc(), PosterSnapshot.created_at.desc())
        .all()
    )
    done, failed = set(), 0
    for snapshot in rows:
        item = snapshot.jellyfin_item_id
        if item in done or snapshot.original_suspect:
            continue
        done.add(item)
        try:
            jellyfin = client("jellyfin")
            if len(_backdrops(jellyfin, item)) < 2:
                continue
            original = Path(snapshot.original_image_bytes_path).read_bytes()
            if snapshot.active:
                badged = Path(snapshot.badged_image_bytes_path).read_bytes()
                removed = _put(jellyfin, item, badged, "Backdrop", original)
                _record_applied(snapshot, jellyfin)
            else:
                # Restored: only the tagged copies go; whatever else the title
                # has now (perhaps a backdrop chosen since) stays.
                removed = _delete_tagged(jellyfin, item, original)
            current_app.logger.info(
                "Deleted %s leftover backdrop copies of %s", removed, item
            )
        except IntegrationError as exc:
            if exc.status_code == 404:
                continue  # no longer in Jellyfin: nothing left to tidy
            failed += 1
            current_app.logger.warning(
                "Tidying the backdrops of %s failed: %s", item, exc
            )
            db.session.rollback()
        except (OSError, ValueError) as exc:
            failed += 1
            current_app.logger.warning(
                "Tidying the backdrops of %s failed: %s", item, type(exc).__name__
            )
            db.session.rollback()
    if not failed:
        set_setting(TIDIED, utcnow().isoformat())
        db.session.commit()


def reconcile_artwork():
    _repair_suspect_originals()
    _tidy_existing_backdrops()
    active = (
        db.session.query(PurgeCandidate)
        .filter(PurgeCandidate.state.in_(["REVIEW", "LEAVING_SOON"]))
        .all()
    )
    for candidate in active:
        if candidate.target_part_ids:
            continue  # a few episodes are leaving, not the show
        apply_badge(candidate)
    snapshots = db.session.query(PosterSnapshot).filter_by(active=True).all()
    for snapshot in snapshots:
        candidate = db.session.get(PurgeCandidate, snapshot.candidate_id)
        if not candidate or candidate.state not in {"REVIEW", "LEAVING_SOON"}:
            restore_badge(snapshot)
