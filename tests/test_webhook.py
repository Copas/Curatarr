from datetime import UTC, datetime, timedelta

from curatarr import db
from curatarr.models import (
    Library,
    LifecycleAction,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
)
from curatarr.services import process_pending_events, set_setting


def test_duplicate_event_and_playback_rescue(app, client):
    with app.app_context():
        set_setting("webhook_token", "test-token")
        library = Library(
            jellyfin_library_id="demo-tv", name="Demo TV", media_type="tv"
        )
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id,
            media_type="series",
            title="Sample Series",
            jellyfin_id="series-1",
            tvdb_id=123,
        )
        db.session.add(media)
        db.session.flush()
        db.session.add(
            MediaPart(
                media_identity_id=media.id,
                kind="episode",
                season_number=1,
                episode_number=1,
                jellyfin_id="episode-1",
                has_file=True,
            )
        )
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="REVIEW",
            reason_code="inactivity",
            reason_text="Old",
            reclaimable_bytes=100,
        )
        db.session.add(candidate)
        db.session.commit()
        candidate_id = candidate.id
    payload = {
        "event_id": "event-1",
        "event_type": "playback_started",
        "item_external_id": "episode-1",
        "series_external_id": "series-1",
        "item_type": "episode",
        "user_external_id": "demo-user",
    }
    headers = {"X-Curatarr-Token": "test-token"}
    assert (
        client.post(
            "/api/v1/webhook/jellyfin", json=payload, headers=headers
        ).status_code
        == 202
    )
    assert (
        client.post("/api/v1/webhook/jellyfin", json=payload, headers=headers).json[
            "accepted"
        ]
        is False
    )
    with app.app_context():
        assert process_pending_events() == 1
        assert db.session.get(PurgeCandidate, candidate_id).state == "RESCUED"
        assert (
            db.session.query(LifecycleAction)
            .filter_by(action_type="candidate_rescued")
            .count()
            == 1
        )


def test_playback_cancels_queued_delete_before_external_call(app, monkeypatch):
    from curatarr.lifecycle import execute_action
    from curatarr.services import ingest_event

    with app.app_context():
        library = Library(
            jellyfin_library_id="race-movies", name="Movies", media_type="movies"
        )
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title="Rescued Movie",
            jellyfin_id="race-movie",
            radarr_id=51,
            tmdb_id=99,
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
            idempotency_key="delete-race-movie",
            action_type="delete_media",
            state="PENDING",
            media_identity_id=media.id,
            candidate_id=candidate.id,
            reason_text="Old",
            payload_json={},
        )
        db.session.add(action)
        db.session.commit()
        ingest_event(
            {
                "event_id": "race-playback",
                "event_type": "playback_started",
                "item_external_id": "race-movie",
                "user_external_id": "viewer-a",
            }
        )
        assert process_pending_events() == 1
        monkeypatch.setattr(
            "curatarr.lifecycle.client",
            lambda _kind: (_ for _ in ()).throw(AssertionError("unexpected API call")),
        )
        assert execute_action(action.id) == "skipped"
        assert action.state == "CANCELLED"
        assert candidate.state == "RESCUED"


def test_webhook_token_is_required_and_shown_once(app, client):
    payload = {"event_type": "playback_started", "item_external_id": "sample"}
    assert client.post("/api/v1/webhook/jellyfin", json=payload).status_code == 403
    with app.app_context():
        from curatarr.services import setting

        assert setting("webhook_token") is None
    first = client.get("/settings")
    assert first.status_code == 200
    with app.app_context():
        token = setting("webhook_token")
        assert token and token.encode() in first.data
    assert token.encode() not in client.get("/settings").data


def test_dry_run_requires_predelete_validation(app, monkeypatch):
    from curatarr import lifecycle

    with app.app_context():
        library = Library(
            jellyfin_library_id="demo-movies", name="Demo Movies", media_type="movies"
        )
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title="Sample Film",
            jellyfin_id="movie-1",
            tmdb_id=123,
            radarr_id=42,
            added_at=datetime.now(UTC) - timedelta(days=200),
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
        monkeypatch.setattr(lifecycle, "_validate_deletion", lambda candidate: None)
        assert lifecycle.execute_approved(candidate.id) == "dry_run"
        assert (
            db.session.query(LifecycleAction)
            .filter_by(action_type="would_delete")
            .count()
            == 1
        )
