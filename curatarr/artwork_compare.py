"""Posters → Compare: a title's picture as it is against one a worker made
for the same title and type, side by side in random order, to find which
you prefer (made with an outside AI before, say, against a local model).

Choices are kept (ArtworkPreference) and tallied by picture type and engine;
a generated picture you preferred can then be put on from its job's page,
as any other.
"""

import random
from pathlib import Path

from . import custom_posters, db
from .models import (
    ArtworkCandidate,
    ArtworkImage,
    ArtworkJob,
    ArtworkPreference,
    CustomPoster,
)


def current_file(poster: CustomPoster) -> Path | None:
    """The picture's file: Curatarr's copy, or else the import folder's file
    it came from (where Curatarr's copy can't be read)."""
    places = [Path(poster.image_path)]
    folder = custom_posters.settings().get("folder")
    if folder and poster.source and poster.source != "Uploaded":
        places.append(Path(folder) / Path(poster.source).name)
    for place in places:
        try:
            with open(place, "rb"):
                return place
        except OSError:
            continue
    return None


def current_for(candidate: ArtworkCandidate) -> CustomPoster | None:
    """The picture the candidate's title has now, for its accounts (the
    first one's) or everyone; None when it has none of that type."""
    job = candidate.image.job
    if job.media_identity_id is None:
        return None
    for user_id in job.accounts or [None]:
        poster = custom_posters.active(
            job.media_identity_id, user_id, candidate.image.kind
        )
        if poster and current_file(poster) is not None:
            return poster
    return None


def _waiting():
    """Generated pictures of library titles not compared yet, oldest first,
    so a title's set comes together."""
    judged = db.session.query(ArtworkPreference.candidate_id)
    return (
        db.session.query(ArtworkCandidate)
        .join(ArtworkImage, ArtworkCandidate.image_id == ArtworkImage.id)
        .join(ArtworkJob, ArtworkImage.job_id == ArtworkJob.id)
        .filter(
            ArtworkJob.purpose == "library",
            ArtworkCandidate.path.isnot(None),
            ArtworkCandidate.decision.is_(None)
            | (ArtworkCandidate.decision == "approved"),
            ArtworkCandidate.id.not_in(judged),
        )
        .order_by(ArtworkCandidate.created_at)
    )


def next_matchup(rng=random):
    """(candidate, current picture, whether the new one's on the left), or
    None when there's nothing left to compare."""
    for candidate in _waiting().limit(200):
        current = current_for(candidate)
        if current is not None:
            return candidate, current, rng.random() < 0.5
    return None


def remaining() -> int:
    return sum(1 for c in _waiting() if current_for(c) is not None)


def record(candidate_id, poster_id, winner, notes=None, by=None) -> ArtworkPreference:
    if winner not in ("new", "current", "tie"):
        raise ValueError("Choose one of them, or about the same.")
    candidate = db.session.get(ArtworkCandidate, candidate_id)
    current = db.session.get(CustomPoster, poster_id)
    if candidate is None or current is None:
        raise ValueError("Those pictures are gone.")
    choice = ArtworkPreference(
        candidate_id=candidate.id,
        custom_poster_id=current.id,
        winner=winner,
        notes=(notes or "").strip()[:1000] or None,
        created_by=by,
    )
    db.session.add(choice)
    db.session.commit()
    return choice


def results() -> dict:
    """Tallies: overall, by picture type and by engine, each {"new",
    "current", "tie"}; and the generated pictures preferred but not put on
    yet, newest choice first."""

    def tally():
        return {"new": 0, "current": 0, "tie": 0}

    overall, by_kind, by_engine = tally(), {}, {}
    preferred = []
    choices = (
        db.session.query(ArtworkPreference)
        .order_by(ArtworkPreference.created_at.desc())
        .all()
    )
    for choice in choices:
        candidate = choice.candidate
        kind = candidate.image.kind
        overall[choice.winner] += 1
        by_kind.setdefault(kind, tally())[choice.winner] += 1
        by_engine.setdefault(candidate.engine or "?", tally())[choice.winner] += 1
        if choice.winner == "new" and candidate.decision != "approved":
            preferred.append(choice)
    return {
        "overall": overall,
        "kinds": by_kind,
        "engines": by_engine,
        "preferred": preferred,
        "count": len(choices),
    }


def undo_last(by=None) -> bool:
    """Takes back the latest choice (a slip of the finger)."""
    last = (
        db.session.query(ArtworkPreference)
        .order_by(ArtworkPreference.created_at.desc())
        .first()
    )
    if last is None:
        return False
    db.session.delete(last)
    db.session.commit()
    return True
