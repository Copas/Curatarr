"""Reversible Leaving Soon posters, stored only under application data."""

import hashlib
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from flask import current_app
from PIL import Image, ImageDraw, ImageFont
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


def badge_poster(
    source: bytes, label: str = "LEAVING SOON", kind: str = "Primary"
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
            fill=BADGE_RED,
        )
        drawing.text(
            (left + pad_x - box[0], top + pad_y - box[1]),
            label,
            fill="#FFFFFF",
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


def _recorded(label: str) -> str:
    return f"{BADGE_STYLE}:{label}"


def _jpeg(source: bytes) -> bytes:
    image = Image.open(BytesIO(source)).convert("RGB")
    output = BytesIO()
    image.save(output, format="JPEG", quality=95)
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
        except (IntegrationError, OSError, ValueError) as exc:
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
        apply_badge(candidate)
    snapshots = db.session.query(PosterSnapshot).filter_by(active=True).all()
    for snapshot in snapshots:
        candidate = db.session.get(PurgeCandidate, snapshot.candidate_id)
        if not candidate or candidate.state not in {"REVIEW", "LEAVING_SOON"}:
            restore_badge(snapshot)
