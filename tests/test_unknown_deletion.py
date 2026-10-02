from datetime import timedelta

from curatarr import db
from curatarr.integrations import IntegrationError
from curatarr.lifecycle import reconcile_unknown_actions, recover_stale_actions
from curatarr.models import (
    Library,
    LifecycleAction,
    MediaIdentity,
    PurgeCandidate,
    utcnow,
)


def test_ambiguous_movie_result_is_reconciled_without_retry(app, monkeypatch):
    class GoneRadarr:
        def movie(self, _movie_id):
            raise IntegrationError("not found", status_code=404)

    monkeypatch.setattr("curatarr.lifecycle.client", lambda _kind: GoneRadarr())
    with app.app_context():
        library = Library(
            jellyfin_library_id="uncertain", name="Demo Movies", media_type="movies"
        )
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title="Sample Film",
            radarr_id=12,
            tmdb_id=123,
        )
        db.session.add(media)
        db.session.flush()
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="EXECUTING",
            reason_code="inactivity",
            reason_text="Old",
            reclaimable_bytes=100,
        )
        db.session.add(candidate)
        db.session.flush()
        action = LifecycleAction(
            idempotency_key="uncertain-delete",
            action_type="delete_media",
            state="RUNNING",
            media_identity_id=media.id,
            candidate_id=candidate.id,
            reason_text="Old",
            payload_json={},
            started_at=utcnow() - timedelta(minutes=20),
        )
        db.session.add(action)
        db.session.commit()
        assert recover_stale_actions() == 1
        assert action.state == "UNKNOWN_RECONCILE"
        assert reconcile_unknown_actions() == 1
        assert action.state == "SUCCEEDED"
        assert candidate.state == "COMPLETED"
