from datetime import timedelta

import pytest

from curatarr import db
from curatarr.demo import seed_demo
from curatarr.lifecycle import evaluate_retention, expire_notices, resume_approved
from curatarr.models import (
    Library,
    LibraryPolicy,
    LifecycleAction,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
    utcnow,
)
from curatarr.services import ingest_event


@pytest.fixture(autouse=True)
def _inactivity_cleanup_enabled(app):
    """These tests exercise inactivity cleanup, which libraries must opt into."""
    from curatarr.services import set_setting

    with app.app_context():
        set_setting("global_policy", {"inactivity_cleanup": True})


def test_automatic_notice_expires_into_dry_run(app):
    app.config["DEMO_MODE"] = True
    with app.app_context():
        library = Library(
            jellyfin_library_id="auto-demo", name="Demo Movies", media_type="movies"
        )
        db.session.add(library)
        db.session.flush()
        db.session.add(
            LibraryPolicy(
                library_id=library.id,
                policy_json={
                    "review_mode": "automatic",
                    "notice_days": 0,
                },
            )
        )
        media = MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title="Sample Film",
            jellyfin_id="auto-movie",
            radarr_id=1,
            tmdb_id=20001,
            added_at=utcnow() - timedelta(days=200),
        )
        db.session.add(media)
        db.session.flush()
        db.session.add(
            MediaPart(
                media_identity_id=media.id,
                kind="movie_file",
                has_file=True,
                size_bytes=100,
            )
        )
        db.session.commit()
        assert evaluate_retention() == 1
        candidate = db.session.query(PurgeCandidate).one()
        assert candidate.state == "LEAVING_SOON"
        assert expire_notices() == 1
        assert candidate.state == "COMPLETED"
        assert (
            db.session.query(LifecycleAction)
            .filter_by(action_type="would_delete")
            .count()
            == 1
        )
        assert (
            db.session.query(LifecycleAction)
            .filter_by(action_type="delete_media")
            .count()
            == 0
        )


def test_pending_playback_rescues_notice_before_expiration(app):
    app.config["DEMO_MODE"] = True
    with app.app_context():
        seed_demo()
        candidate = (
            db.session.query(PurgeCandidate).filter_by(state="LEAVING_SOON").one()
        )
        candidate.scheduled_delete_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        ingest_event(
            {
                "event_id": "notice-expiry-playback",
                "event_type": "playback_started",
                "item_external_id": candidate.media.jellyfin_id,
                "user_external_id": "viewer",
            }
        )
        assert expire_notices() == 0
        assert candidate.state == "RESCUED"
        assert candidate.scheduled_delete_at is None
        assert (
            db.session.query(LifecycleAction)
            .filter(LifecycleAction.action_type.in_(["delete_media", "would_delete"]))
            .count()
            == 0
        )


def _expired_demo_notice():
    seed_demo()
    candidate = db.session.query(PurgeCandidate).filter_by(state="LEAVING_SOON").one()
    candidate.scheduled_delete_at = utcnow() - timedelta(seconds=1)
    db.session.commit()
    return candidate


def test_deferred_expiry_resumes_and_rescues_on_late_playback(app, monkeypatch):
    app.config["DEMO_MODE"] = True
    with app.app_context():
        candidate = _expired_demo_notice()
        ingest_event(
            {
                "event_id": "unprocessed-playback",
                "event_type": "playback_started",
                "item_external_id": candidate.media.jellyfin_id,
                "user_external_id": "viewer",
            }
        )
        with monkeypatch.context() as patch:
            patch.setattr("curatarr.services.process_pending_events", lambda: 0)
            assert expire_notices() == 1
        assert candidate.state == "APPROVED"
        assert (
            db.session.query(LifecycleAction)
            .filter_by(action_type="delete_deferred", candidate_id=candidate.id)
            .count()
            == 1
        )
        assert resume_approved() == 1
        assert candidate.state == "RESCUED"
        assert (
            db.session.query(LifecycleAction)
            .filter(LifecycleAction.action_type.in_(["delete_media", "would_delete"]))
            .count()
            == 0
        )


def test_deferred_expiry_resumes_into_dry_run(app, monkeypatch):
    app.config["DEMO_MODE"] = True
    with app.app_context():
        candidate = _expired_demo_notice()
        ingest_event(
            {
                "event_id": "unrelated-playback",
                "event_type": "playback_started",
                "item_external_id": "unknown-item",
                "user_external_id": "viewer",
            }
        )
        with monkeypatch.context() as patch:
            patch.setattr("curatarr.services.process_pending_events", lambda: 0)
            assert expire_notices() == 1
            assert resume_approved() == 0
        assert candidate.state == "APPROVED"
        assert resume_approved() == 1
        assert resume_approved() == 0
        assert candidate.state == "COMPLETED"
        assert (
            db.session.query(LifecycleAction)
            .filter_by(action_type="would_delete", candidate_id=candidate.id)
            .count()
            == 1
        )
