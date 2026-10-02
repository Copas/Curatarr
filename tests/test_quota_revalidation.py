from datetime import timedelta

from curatarr import db
from curatarr.lifecycle import _validate_deletion
from curatarr.models import (
    Library,
    LibraryPolicy,
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
