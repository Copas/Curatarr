from datetime import timedelta

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
