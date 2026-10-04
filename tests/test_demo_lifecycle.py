"""End-to-end automatic lifecycle through the demo adapters and CLI."""

from curatarr import db
from curatarr.demo import seed_demo
from curatarr.models import (
    AppSetting,
    Library,
    LibraryPolicy,
    LifecycleAction,
    MediaPart,
    PurgeCandidate,
)


def _enable_automatic_movie_cleanup():
    seed_demo()
    library = db.session.query(Library).filter_by(name="Demo Movies").one()
    policy = db.session.query(LibraryPolicy).filter_by(library_id=library.id).one()
    policy.policy_json = policy.policy_json | {
        "quota_review_mode": "automatic",
        "movie_inactivity_days": 3650,
        "notice_days": 0,
        "dry_run": False,
    }
    db.session.commit()
    return library


def _deletes(state=None):
    query = db.session.query(LifecycleAction).filter_by(action_type="delete_media")
    if state:
        query = query.filter_by(state=state)
    return query.count()


def _stored_movie_bytes(library):
    return sum(
        part.size_bytes
        for part in db.session.query(MediaPart)
        .join(MediaPart.media)
        .filter_by(library_id=library.id)
        if part.has_file
    )


def test_demo_quota_cleanup_runs_end_to_end(app):
    app.config["DEMO_MODE"] = True
    runner = app.test_cli_runner()
    with app.app_context():
        library = _enable_automatic_movie_cleanup()
        assert _stored_movie_bytes(library) > 80_000_000_000
    result = runner.invoke(args=["reconcile"])
    assert result.exit_code == 0, result.output
    with app.app_context():
        deleted = _deletes("SUCCEEDED")
        assert deleted > 0
        assert _deletes() == deleted
        assert _stored_movie_bytes(library) <= 70_000_000_000
        completed = (
            db.session.query(PurgeCandidate)
            .filter_by(state="COMPLETED", reason_code="quota")
            .count()
        )
        assert completed == deleted
        # The pre-seeded TV notice is not due and other libraries keep review.
        assert db.session.query(PurgeCandidate).filter_by(state="LEAVING_SOON").count()
    status = app.test_client().get("/api/v1/status").get_json()
    assert status["reclaimed_bytes"] == deleted * 2_000_000_000
    # A second cycle is below high water and must not delete anything else.
    assert runner.invoke(args=["reconcile"]).exit_code == 0
    with app.app_context():
        assert _deletes() == deleted


def test_demo_radarr_outage_blocks_automatic_deletion(app):
    app.config["DEMO_MODE"] = True
    runner = app.test_cli_runner()
    with app.app_context():
        library = _enable_automatic_movie_cleanup()
        before = _stored_movie_bytes(library)
    with app.app_context():
        from curatarr.lifecycle import evaluate_retention

        assert evaluate_retention() > 0
        db.session.add(AppSetting(key="demo_outage", value_json="radarr"))
        db.session.commit()
    # The worker keeps running during the outage but must not delete anything.
    assert runner.invoke(args=["worker", "--once"]).exit_code == 0
    with app.app_context():
        assert _deletes() == 0
        assert _stored_movie_bytes(library) == before
        blocked = (
            db.session.query(LifecycleAction)
            .filter_by(action_type="delete_blocked")
            .all()
        )
        assert blocked
        assert all(
            row.reason_text == "External integration cannot be verified"
            for row in blocked
        )
        assert db.session.query(PurgeCandidate).filter_by(
            state="BLOCKED", reason_code="quota"
        ).count() == len(blocked)
    status = app.test_client().get("/api/v1/status").get_json()
    assert status["reclaimed_bytes"] == 0
