from datetime import timedelta

from curatarr import db
from curatarr.integrations import IntegrationError
from curatarr.lifecycle import (
    execute_approved,
    reconcile_unknown_actions,
    recover_stale_actions,
)
from curatarr.models import (
    Library,
    LibraryPolicy,
    LifecycleAction,
    MediaIdentity,
    MediaPart,
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


def test_radarr_timeout_after_delete_is_reconciled_without_second_delete(
    app, monkeypatch
):
    deleted = []

    class FakeRadarr:
        def queue(self):
            return {"records": []}

        def request(self, _method, _path):
            return {"tmdbId": 123}

        def delete_movie(self, movie_id):
            deleted.append(movie_id)
            raise IntegrationError("Ambiguous timeout")

        def movie(self, _movie_id):
            raise IntegrationError("Gone", status_code=404)

    class FakeJellyfin:
        def item(self, _item_id):
            return {"ProviderIds": {"Tmdb": "123"}}

    monkeypatch.setattr(
        "curatarr.lifecycle.client",
        lambda kind: FakeJellyfin() if kind == "jellyfin" else FakeRadarr(),
    )
    with app.app_context():
        library = Library(
            jellyfin_library_id="timeout-movie", name="Movies", media_type="movies"
        )
        db.session.add(library)
        db.session.flush()
        db.session.add(
            LibraryPolicy(library_id=library.id, policy_json={"dry_run": False})
        )
        media = MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title="Timeout Film",
            jellyfin_id="timeout-film",
            radarr_id=12,
            tmdb_id=123,
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
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="APPROVED",
            reason_code="inactivity",
            reason_text="Old",
            reclaimable_bytes=100,
        )
        db.session.add(candidate)
        db.session.commit()
        assert execute_approved(candidate.id) == "unknown"
        action = (
            db.session.query(LifecycleAction)
            .filter_by(action_type="delete_media")
            .one()
        )
        assert action.state == "UNKNOWN_RECONCILE"
        assert reconcile_unknown_actions() == 1
        assert action.state == "SUCCEEDED"
        assert candidate.state == "COMPLETED"
        assert deleted == [12]
