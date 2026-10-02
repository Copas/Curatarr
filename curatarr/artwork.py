"""Reversible Leaving Soon posters, stored only under application data."""

import hashlib
from io import BytesIO
from pathlib import Path

from flask import current_app
from PIL import Image, ImageDraw, ImageFont

from . import db
from .integrations import IntegrationError
from .models import MediaIdentity, PosterSnapshot, PurgeCandidate, utcnow
from .services import audit, client


def badge_poster(source: bytes) -> bytes:
    image = Image.open(BytesIO(source)).convert("RGB")
    width, height = image.size
    ribbon_height = max(34, round(height * 0.12))
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((0, height - ribbon_height, width, height), fill="#172431")
    font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(15, round(width * 0.075)))
    label = "LEAVING SOON"
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


def apply_badge(candidate):
    media = db.session.get(MediaIdentity, candidate.media_identity_id)
    if not media or not media.jellyfin_id:
        return False
    existing = (
        db.session.query(PosterSnapshot)
        .filter_by(candidate_id=candidate.id, active=True)
        .first()
    )
    if existing:
        return True
    try:
        jellyfin = client("jellyfin")
        original = _jpeg(jellyfin.image(media.jellyfin_id))
        badged = badge_poster(original)
        directory = _store_dir()
        snapshot = PosterSnapshot(
            media_identity_id=media.id,
            jellyfin_item_id=media.jellyfin_id,
            candidate_id=candidate.id,
        )
        db.session.add(snapshot)
        db.session.flush()
        source_path = directory / f"{snapshot.id}-source.jpg"
        badge_path = directory / f"{snapshot.id}-badge.jpg"
        source_path.write_bytes(original)
        badge_path.write_bytes(badged)
        snapshot.original_image_bytes_path = str(source_path)
        snapshot.badged_image_bytes_path = str(badge_path)
        jellyfin.put_image(media.jellyfin_id, badged)
        snapshot.active = True
        audit(
            "leaving_soon_applied",
            media.id,
            "Leaving Soon poster applied.",
            f"artwork-apply:{candidate.id}",
            candidate_id=candidate.id,
        )
        db.session.commit()
        return True
    except (IntegrationError, OSError, ValueError) as exc:
        current_app.logger.warning(
            "Poster application failed for candidate %s: %s",
            candidate.id,
            type(exc).__name__,
        )
        db.session.rollback()
        return False


def restore_badge(snapshot):
    try:
        jellyfin = client("jellyfin")
        expected = Path(snapshot.badged_image_bytes_path).read_bytes()
        current = jellyfin.image(snapshot.jellyfin_item_id)
        if hashlib.sha256(current).digest() != hashlib.sha256(expected).digest():
            # External artwork changed; avoid overwriting someone else's edit.
            return False
        original = Path(snapshot.original_image_bytes_path).read_bytes()
        jellyfin.put_image(snapshot.jellyfin_item_id, original)
        snapshot.active = False
        snapshot.restored_at = utcnow()
        audit(
            "leaving_soon_removed",
            snapshot.media_identity_id,
            "Original poster restored after cleanup was cancelled.",
            f"artwork-restore:{snapshot.id}",
            candidate_id=snapshot.candidate_id,
        )
        db.session.commit()
        return True
    except (IntegrationError, OSError) as exc:
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
