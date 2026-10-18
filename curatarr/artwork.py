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


def badge_poster(source: bytes, label: str = "LEAVING SOON") -> bytes:
    image = Image.open(BytesIO(source)).convert("RGB")
    width, height = image.size
    ribbon_height = max(34, round(height * 0.12))
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((0, height - ribbon_height, width, height), fill="#172431")
    size = max(15, round(width * 0.075))
    font = ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    box = drawing.textbbox((0, 0), label, font=font)
    while box[2] - box[0] > width * 0.92 and size > 10:  # longer labels fit too
        size -= 1
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", size)
        box = drawing.textbbox((0, 0), label, font=font)
    drawing.text(
        (
            (width - (box[2] - box[0])) / 2,
            height - ribbon_height + (ribbon_height - (box[3] - box[1])) / 2 - box[1],
        ),
        label,
        fill="#FFFFFF",
        font=font,
    )
    output = BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()


def _jpeg(source: bytes) -> bytes:
    image = Image.open(BytesIO(source)).convert("RGB")
    output = BytesIO()
    image.save(output, format="JPEG", quality=95)
    return output.getvalue()


def _store_dir() -> Path:
    path = Path(current_app.instance_path) / "posters"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _primary_tag(item):
    return (item.get("ImageTags") or {}).get("Primary")


def _record_applied(snapshot, jellyfin):
    current_tag = _primary_tag(jellyfin.item(snapshot.jellyfin_item_id))
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
    existing = (
        db.session.query(PosterSnapshot)
        .filter_by(candidate_id=candidate.id, active=True)
        .first()
    )
    if existing:
        if existing.badged_image_tag:
            return _keep_badged(existing, badge_label(candidate))
        try:
            jellyfin = client("jellyfin")
            current = jellyfin.image(existing.jellyfin_item_id)
            badged = Path(existing.badged_image_bytes_path).read_bytes()
            if current == badged:
                _record_applied(existing, jellyfin)
                return True
            original = Path(existing.original_image_bytes_path).read_bytes()
            if _jpeg(current) != original:
                return False
            jellyfin.put_image(existing.jellyfin_item_id, badged)
            _record_applied(existing, jellyfin)
            return True
        except (IntegrationError, OSError, ValueError):
            db.session.rollback()
            return False
    try:
        jellyfin = client("jellyfin")
        original_tag = _primary_tag(jellyfin.item(media.jellyfin_id))
        original = _jpeg(jellyfin.image(media.jellyfin_id))
        label = badge_label(candidate)
        badged = badge_poster(original, label)
        directory = _store_dir()
        snapshot = PosterSnapshot(
            media_identity_id=media.id,
            jellyfin_item_id=media.jellyfin_id,
            candidate_id=candidate.id,
            original_image_tag=original_tag,
            badge_label=label,
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
        jellyfin.put_image(media.jellyfin_id, badged)
        _record_applied(snapshot, jellyfin)
        return True
    except (IntegrationError, OSError, ValueError) as exc:
        current_app.logger.warning(
            "Poster application failed for candidate %s: %s",
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
        current_tag = _primary_tag(jellyfin.item(snapshot.jellyfin_item_id))
        if not current_tag:
            return True
        replaced = current_tag != snapshot.badged_image_tag
        if not replaced and snapshot.badge_label == label:
            return True
        if replaced:
            original = _jpeg(jellyfin.image(snapshot.jellyfin_item_id))
            Path(snapshot.original_image_bytes_path).write_bytes(original)
            snapshot.original_image_tag = current_tag
            snapshot.badged_image_tag = None
        else:
            original = Path(snapshot.original_image_bytes_path).read_bytes()
        Path(snapshot.badged_image_bytes_path).write_bytes(
            badge_poster(original, label)
        )
        snapshot.badge_label = label
        # What restoring needs must be durable before the upload replaces it.
        db.session.commit()
        jellyfin.put_image(
            snapshot.jellyfin_item_id,
            Path(snapshot.badged_image_bytes_path).read_bytes(),
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
    try:
        jellyfin = client("jellyfin")
        expected = Path(snapshot.badged_image_bytes_path).read_bytes()
        current = jellyfin.image(snapshot.jellyfin_item_id)
        original = Path(snapshot.original_image_bytes_path).read_bytes()
        if _jpeg(current) == original:
            _record_restored(snapshot, "Original poster is already present.")
            return True
        same_bytes = (
            hashlib.sha256(current).digest() == hashlib.sha256(expected).digest()
        )
        if not same_bytes:
            current_tag = _primary_tag(jellyfin.item(snapshot.jellyfin_item_id))
            if (
                not snapshot.badged_image_tag
                or current_tag != snapshot.badged_image_tag
            ):
                # External artwork changed; avoid overwriting someone else's edit.
                return False
        jellyfin.put_image(snapshot.jellyfin_item_id, original)
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


def reconcile_artwork():
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
