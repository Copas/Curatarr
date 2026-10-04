"""Owner's rule (2026-10-04): cleanup happens only when free space runs low.

Unwatched titles are chosen first, only enough is selected to get back to the
low free-space threshold, low space gives a Leaving Soon notice (14 days by
default), and critical space removes without a notice. Dry run still applies.
"""

from datetime import timedelta

from test_disk_pressure import PressureServices

from curatarr import db
from curatarr.lifecycle import evaluate_retention, expire_notices
from curatarr.models import (
    Library,
    LibraryPolicy,
    LifecycleAction,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
    utcnow,
)
from curatarr.policy import _aware


def _library_with_defaults():
    """Only what a person must set for free-space cleanup: a path and on."""
    library = Library(jellyfin_library_id="space", name="Movies", media_type="movies")
    db.session.add(library)
    db.session.flush()
    db.session.add(
        LibraryPolicy(
            library_id=library.id,
            policy_json={
                "free_space_enabled": True,
                "disk_path": "/media/movies",
                "low_free_percent": 20,
                "critical_free_percent": 10,
            },
        )
    )
    for index in (1, 2, 3):
        media = MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title=f"Unwatched {index}",
            jellyfin_id=f"movie-{index}",
            radarr_id=index,
            tmdb_id=100 + index,
            added_at=utcnow() - timedelta(days=400 - index),  # long unwatched
        )
        db.session.add(media)
        db.session.flush()
        db.session.add(
            MediaPart(
                media_identity_id=media.id,
                kind="movie_file",
                has_file=True,
                size_bytes=60,
            )
        )
    db.session.commit()


def _candidates():
    return db.session.query(PurgeCandidate).order_by(PurgeCandidate.score).all()


def test_unwatched_titles_are_left_alone_while_space_is_fine(app, monkeypatch):
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _k: PressureServices(250))
    _library_with_defaults()
    assert evaluate_retention() == 0  # 25% free, above the 20% threshold


def test_low_space_selects_only_enough_with_a_leaving_soon_notice(app, monkeypatch):
    # 15% free; 50 bytes needed to reach 20%, so one 60-byte title is enough.
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _k: PressureServices(150))
    _library_with_defaults()
    assert evaluate_retention() == 1
    (candidate,) = _candidates()
    assert candidate.reason_code == "disk_pressure"
    assert candidate.state == "LEAVING_SOON"
    days = (_aware(candidate.scheduled_delete_at) - utcnow()).total_seconds() / 86400
    assert 13.9 < days <= 14
    assert evaluate_retention() == 0  # hourly re-runs do not select more


def test_critical_space_removes_without_notice(app, monkeypatch):
    # 5% free; 150 bytes needed to reach 20%: all three, removed right away.
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _k: PressureServices(50))
    _library_with_defaults()
    assert evaluate_retention() == 3
    assert all(_aware(c.scheduled_delete_at) <= utcnow() for c in _candidates())
    assert expire_notices() == 3
    # Dry run is still on by default, so this only records what would happen.
    assert (
        db.session.query(LifecycleAction).filter_by(action_type="would_delete").count()
        == 3
    )
    assert (
        db.session.query(LifecycleAction).filter_by(action_type="delete_media").count()
        == 0
    )


def test_existing_inactivity_candidates_are_rescued_when_turned_off(app, monkeypatch):
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _k: PressureServices(250))
    _library_with_defaults()
    media = db.session.query(MediaIdentity).first()
    db.session.add(
        PurgeCandidate(
            media_identity_id=media.id,
            state="REVIEW",
            reason_code="inactivity",
            reason_text="No playback within 90 days.",
            reclaimable_bytes=60,
        )
    )
    db.session.commit()
    evaluate_retention()
    candidate = db.session.query(PurgeCandidate).one()
    assert candidate.state == "RESCUED"


def test_libraries_can_still_opt_into_inactivity_cleanup(app, monkeypatch):
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _k: PressureServices(250))
    _library_with_defaults()
    policy = db.session.query(LibraryPolicy).one()
    policy.policy_json = policy.policy_json | {"inactivity_cleanup": True}
    db.session.commit()
    assert evaluate_retention() == 3
    assert {c.reason_code for c in _candidates()} == {"inactivity"}


def test_retention_page_offers_the_inactivity_switch_off_by_default(app, client):
    page = client.get("/rules/retention").text
    assert "Clean up unwatched titles even when space is fine" in page
    assert '<option value="false" selected>No</option>' in page


def test_libraries_on_one_disk_share_a_single_selection(app, monkeypatch, tmp_path):
    """Four libraries on one NAS must not each select the whole shortfall."""
    import os

    class NasNotReported:
        def diskspace(self):
            return [{"path": "/", "totalSpace": 10_000, "freeSpace": 9_000}]

        def queue(self):
            return {"records": []}

    class Stats:  # the NAS: 1000 bytes, 100 free = 10%, below 15% low
        f_blocks, f_frsize, f_bavail = 1000, 1, 100

    real_statvfs = os.statvfs
    monkeypatch.setattr(
        "os.statvfs",
        lambda path: (
            Stats() if str(path).startswith(str(tmp_path)) else real_statvfs(path)
        ),
    )
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _k: NasNotReported())
    for name in ("Movies", "Classic Movies", "Shows Movies", "More Movies"):
        folder = tmp_path / name.replace(" ", "_")
        folder.mkdir()
        library = Library(jellyfin_library_id=name, name=name, media_type="movies")
        db.session.add(library)
        db.session.flush()
        db.session.add(
            LibraryPolicy(
                library_id=library.id,
                policy_json={
                    "free_space_enabled": True,
                    "disk_path": str(folder),
                    "low_free_percent": 15,
                    "critical_free_percent": 5,
                },
            )
        )
        for index in (1, 2):
            media = MediaIdentity(
                library_id=library.id,
                media_type="movie",
                title=f"{name} {index}",
                jellyfin_id=f"{name}-{index}",
                radarr_id=hash((name, index)) % 100000,
                tmdb_id=index,
                added_at=utcnow() - timedelta(days=300),
            )
            db.session.add(media)
            db.session.flush()
            db.session.add(
                MediaPart(
                    media_identity_id=media.id,
                    kind="movie_file",
                    has_file=True,
                    size_bytes=30,
                )
            )
    db.session.commit()
    # 50 bytes are needed for the whole disk: two 30-byte titles, not eight.
    assert evaluate_retention() == 2
    assert {c.reason_code for c in _candidates()} == {"disk_pressure"}
