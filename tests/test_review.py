from datetime import timedelta

from curatarr import db
from curatarr.lifecycle import expire_snoozes, review_candidate
from curatarr.models import (
    Library,
    MediaIdentity,
    PurgeCandidate,
    TitleOverride,
    utcnow,
)


def test_snooze_and_never_purge(app):
    with app.app_context():
        library = Library(
            jellyfin_library_id="review-demo", name="Demo", media_type="movies"
        )
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title="Sample Film",
            jellyfin_id="sample",
        )
        db.session.add(media)
        db.session.flush()
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="REVIEW",
            reason_code="inactivity",
            reason_text="Old",
            reclaimable_bytes=100,
        )
        db.session.add(candidate)
        db.session.commit()
        review_candidate(candidate.id, "snooze_30")
        assert candidate.state == "SNOOZED"
        candidate.snooze_until = utcnow() - timedelta(seconds=1)
        db.session.commit()
        assert expire_snoozes() == 1
        assert candidate.state == "RESCUED"
        next_candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="REVIEW",
            reason_code="inactivity",
            reason_text="Old",
            reclaimable_bytes=100,
        )
        db.session.add(next_candidate)
        db.session.commit()
        review_candidate(next_candidate.id, "never_purge")
        assert (
            db.session.query(TitleOverride).one().override_json["never_purge"] is True
        )
        assert next_candidate.state == "RESCUED"


def test_predelete_blocks_integration_outage(app):
    from curatarr.demo import seed_demo
    from curatarr.lifecycle import _validate_deletion
    from curatarr.models import AppSetting

    app.config["DEMO_MODE"] = True
    with app.app_context():
        seed_demo()
        media = (
            db.session.query(MediaIdentity).filter_by(jellyfin_id="demo-movie-1").one()
        )
        media.added_at = utcnow() - timedelta(days=200)
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="APPROVED",
            reason_code="inactivity",
            reason_text="Old",
            reclaimable_bytes=2_000_000_000,
        )
        db.session.add(candidate)
        db.session.commit()
        assert _validate_deletion(candidate) is None
        db.session.add(AppSetting(key="demo_outage", value_json="radarr"))
        db.session.commit()
        assert (
            _validate_deletion(candidate) == "External integration cannot be verified"
        )


def test_title_never_purge_cancels_pending_review(app, client):
    with app.app_context():
        library = Library(
            jellyfin_library_id="override-demo", name="Demo Movies", media_type="movies"
        )
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title="Protected Film",
            jellyfin_id="protected-film",
        )
        db.session.add(media)
        db.session.flush()
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="REVIEW",
            reason_code="inactivity",
            reason_text="Old",
            reclaimable_bytes=100,
        )
        db.session.add(candidate)
        db.session.commit()
        media_id, candidate_id = media.id, candidate.id
    assert (
        client.post(f"/titles/{media_id}", data={"never_purge": "true"}).status_code
        == 302
    )
    with app.app_context():
        assert db.session.get(PurgeCandidate, candidate_id).state == "RESCUED"
