from datetime import timedelta

from curatarr import db
from curatarr.lifecycle import (
    _validate_deletion,
    evaluate_retention,
    reconcile_candidates,
)
from curatarr.models import (
    Library,
    LibraryPolicy,
    LifecycleAction,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
    UserMediaState,
    utcnow,
)


def test_favorite_change_reorders_quota_before_deletion(app, monkeypatch):
    class FakeArr:
        def queue(self):
            return {"records": []}

        def request(self, _method, path):
            movie_id = int(path.split("/")[-1])
            return {"tmdbId": 100 + movie_id}

    class FakeJellyfin:
        def item(self, item_id):
            movie_id = int(item_id.split("-")[-1])
            return {"ProviderIds": {"Tmdb": str(100 + movie_id)}}

    monkeypatch.setattr(
        "curatarr.lifecycle.client",
        lambda kind: FakeJellyfin() if kind == "jellyfin" else FakeArr(),
    )
    with app.app_context():
        library = Library(
            jellyfin_library_id="quota", name="Movies", media_type="movies"
        )
        db.session.add(library)
        db.session.flush()
        db.session.add(
            LibraryPolicy(
                library_id=library.id,
                policy_json={
                    "quota_enabled": True,
                    "high_water_bytes": 150,
                    "low_water_bytes": 100,
                },
            )
        )
        media_rows = []
        states = []
        for index, days in [(1, 100), (2, 50)]:
            media = MediaIdentity(
                library_id=library.id,
                media_type="movie",
                title=f"Movie {index}",
                jellyfin_id=f"movie-{index}",
                radarr_id=index,
                tmdb_id=100 + index,
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
            state = UserMediaState(
                media_identity_id=media.id,
                jellyfin_user_id="viewer",
                last_played_at=utcnow() - timedelta(days=days),
            )
            db.session.add(state)
            media_rows.append(media)
            states.append(state)
        candidate = PurgeCandidate(
            media_identity_id=media_rows[0].id,
            state="APPROVED",
            reason_code="quota",
            reason_text="Over quota",
            reclaimable_bytes=100,
            last_activity_snapshot=states[0].last_played_at,
        )
        db.session.add(candidate)
        db.session.commit()
        assert _validate_deletion(candidate) is None
        states[0].favorite = True
        db.session.commit()
        assert (
            _validate_deletion(candidate)
            == "Capacity ranking no longer selects this title"
        )


def test_quota_candidate_blocks_after_library_drops_below_trigger(app, monkeypatch):
    class FakeRadarr:
        def queue(self):
            return {"records": []}

        def request(self, _method, _path):
            return {"tmdbId": 101}

    class FakeJellyfin:
        def item(self, _item_id):
            return {"ProviderIds": {"Tmdb": "101"}}

    monkeypatch.setattr(
        "curatarr.lifecycle.client",
        lambda kind: FakeJellyfin() if kind == "jellyfin" else FakeRadarr(),
    )
    with app.app_context():
        library = Library(
            jellyfin_library_id="quota-drop", name="Movies", media_type="movies"
        )
        db.session.add(library)
        db.session.flush()
        db.session.add(
            LibraryPolicy(
                library_id=library.id,
                policy_json={
                    "quota_enabled": True,
                    "high_water_bytes": 150,
                    "low_water_bytes": 100,
                },
            )
        )
        media = MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title="Candidate",
            jellyfin_id="movie-1",
            radarr_id=1,
            tmdb_id=101,
            added_at=utcnow() - timedelta(days=40),
        )
        db.session.add(media)
        db.session.flush()
        part = MediaPart(
            media_identity_id=media.id,
            kind="movie_file",
            has_file=True,
            size_bytes=200,
        )
        db.session.add(part)
        db.session.commit()
        assert evaluate_retention() == 1
        candidate = db.session.query(PurgeCandidate).one()
        candidate.state = "APPROVED"
        db.session.commit()
        assert _validate_deletion(candidate) is None
        part.size_bytes = 100
        db.session.commit()
        assert (
            _validate_deletion(candidate)
            == "Library is no longer above high-water mark"
        )


def _two_movie_quota_library(monkeypatch):
    class FakeRadarr:
        def queue(self):
            return {"records": []}

        def request(self, _method, path):
            return {"tmdbId": 100 + int(path.split("/")[-1])}

    class FakeJellyfin:
        def item(self, item_id):
            return {"ProviderIds": {"Tmdb": str(100 + int(item_id.split("-")[-1]))}}

    monkeypatch.setattr(
        "curatarr.lifecycle.client",
        lambda kind: FakeJellyfin() if kind == "jellyfin" else FakeRadarr(),
    )
    library = Library(
        jellyfin_library_id="quota-run", name="Movies", media_type="movies"
    )
    db.session.add(library)
    db.session.flush()
    db.session.add(
        LibraryPolicy(
            library_id=library.id,
            policy_json={
                "quota_enabled": True,
                "high_water_bytes": 150,
                "low_water_bytes": 50,
                "dry_run": False,
            },
        )
    )
    parts = []
    for index in (1, 2):
        media = MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title=f"Run {index}",
            jellyfin_id=f"movie-{index}",
            radarr_id=index,
            tmdb_id=100 + index,
            added_at=utcnow() - timedelta(days=40 + index),
        )
        db.session.add(media)
        db.session.flush()
        part = MediaPart(
            media_identity_id=media.id,
            kind="movie_file",
            has_file=True,
            size_bytes=100,
        )
        db.session.add(part)
        parts.append(part)
    db.session.commit()
    assert evaluate_retention() == 2
    candidates = db.session.query(PurgeCandidate).order_by(PurgeCandidate.score).all()
    for candidate in candidates:
        candidate.state = "APPROVED"
    db.session.commit()
    return candidates, parts


def test_quota_run_continues_between_marks_after_own_delete(app, monkeypatch):
    with app.app_context():
        candidates, _parts = _two_movie_quota_library(monkeypatch)
        first = next(c for c in candidates if c.media.title == "Run 2")
        second = next(c for c in candidates if c.media.title == "Run 1")
        # Record a completed Curatarr delete that took the library to 100 bytes.
        for part in first.media.parts:
            part.has_file = False
        first.state = "COMPLETED"
        db.session.add(
            LifecycleAction(
                idempotency_key="quota-run-delete",
                reason_text="Quota cleanup.",
                action_type="delete_media",
                state="SUCCEEDED",
                media_identity_id=first.media_identity_id,
                candidate_id=first.id,
                completed_at=utcnow(),
            )
        )
        db.session.commit()
        assert _validate_deletion(second) is None
        assert reconcile_candidates() == 0
        assert second.state == "APPROVED"


def test_quota_external_drop_between_marks_blocks(app, monkeypatch):
    with app.app_context():
        candidates, parts = _two_movie_quota_library(monkeypatch)
        parts[0].size_bytes = 20
        db.session.commit()
        assert all(
            _validate_deletion(c) == "Library is no longer above high-water mark"
            for c in candidates
        )
        assert reconcile_candidates() == 2
