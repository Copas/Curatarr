from datetime import timedelta

from curatarr import db
from curatarr.integrations import IntegrationError
from curatarr.lifecycle import execute_approved, reconcile_unknown_actions
from curatarr.models import (
    Library,
    LibraryPolicy,
    LifecycleAction,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
    utcnow,
)


def test_validated_movie_delete_calls_radarr_once(app, monkeypatch):
    calls = []

    class FakeRadarr:
        def queue(self):
            return {"records": []}

        def request(self, _method, _path):
            return {"tmdbId": 321}

        def delete_movie(self, movie_id):
            calls.append(movie_id)

    class FakeJellyfin:
        def item(self, _item_id):
            return {"ProviderIds": {"Tmdb": "321"}}

    monkeypatch.setattr(
        "curatarr.lifecycle.client",
        lambda kind: FakeJellyfin() if kind == "jellyfin" else FakeRadarr(),
    )
    with app.app_context():
        library = Library(
            jellyfin_library_id="delete-demo", name="Demo Movies", media_type="movies"
        )
        db.session.add(library)
        db.session.flush()
        db.session.add(
            LibraryPolicy(library_id=library.id, policy_json={"dry_run": False})
        )
        media = MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title="Sample Film",
            jellyfin_id="film-1",
            radarr_id=42,
            tmdb_id=321,
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
        assert execute_approved(candidate.id) == "succeeded"
        assert calls == [42]
        assert candidate.state == "COMPLETED"
        assert (
            db.session.query(LifecycleAction)
            .filter_by(action_type="delete_media", state="SUCCEEDED")
            .count()
            == 1
        )


def _series_candidate():
    library = Library(jellyfin_library_id="tv-delete", name="TV", media_type="tv")
    db.session.add(library)
    db.session.flush()
    db.session.add(
        LibraryPolicy(
            library_id=library.id,
            policy_json={"dry_run": False, "minimum_episodes": 1},
        )
    )
    media = MediaIdentity(
        library_id=library.id,
        media_type="series",
        title="Sample Series",
        jellyfin_id="series-1",
        sonarr_id=42,
        tvdb_id=321,
        added_at=utcnow() - timedelta(days=200),
    )
    db.session.add(media)
    db.session.flush()
    parts = []
    for episode_number, file_id in enumerate((100, 200, 300), start=1):
        part = MediaPart(
            media_identity_id=media.id,
            kind="episode",
            season_number=1,
            episode_number=episode_number,
            sonarr_episode_id=episode_number,
            arr_file_id=file_id,
            has_file=True,
            size_bytes=100,
        )
        db.session.add(part)
        parts.append(part)
    candidate = PurgeCandidate(
        media_identity_id=media.id,
        state="APPROVED",
        reason_code="inactivity",
        reason_text="Old",
        reclaimable_bytes=200,
    )
    db.session.add(candidate)
    db.session.commit()
    return candidate, parts


def _sonarr(
    monkeypatch,
    *,
    file_ids=(100, 200, 300),
    episode_files=(100, 200, 300),
    fail_on=None,
):
    calls = []

    class FakeSonarr:
        def queue(self):
            return {"records": []}

        def request(self, _method, _path):
            return {"tvdbId": 321}

        def episodes(self, _series_id):
            return [
                {"id": number, "hasFile": True, "episodeFileId": file_id}
                for number, file_id in enumerate(episode_files, start=1)
            ]

        def episode_files(self, _series_id):
            return [{"id": file_id} for file_id in file_ids]

        def delete_episode_file(self, file_id):
            calls.append(file_id)
            if file_id == fail_on:
                raise IntegrationError("uncertain")

    class FakeJellyfin:
        def item(self, _item_id):
            return {"ProviderIds": {"Tvdb": "321"}}

    sonarr = FakeSonarr()
    monkeypatch.setattr(
        "curatarr.lifecycle.client",
        lambda kind: FakeJellyfin() if kind == "jellyfin" else sonarr,
    )
    return sonarr, calls


def test_tv_delete_blocks_stale_shared_file_mapping(app, monkeypatch):
    with app.app_context():
        candidate, parts = _series_candidate()
        _, calls = _sonarr(monkeypatch, episode_files=(200, 200, 300))
        assert execute_approved(candidate.id) == "blocked"
        assert candidate.state == "BLOCKED"
        assert calls == []
        assert all(part.has_file for part in parts)


def test_tv_delete_shared_unretained_file_once(app, monkeypatch):
    with app.app_context():
        candidate, parts = _series_candidate()
        parts[2].arr_file_id = 200
        db.session.commit()
        _, calls = _sonarr(
            monkeypatch, file_ids=(100, 200), episode_files=(100, 200, 200)
        )
        assert execute_approved(candidate.id) == "succeeded"
        assert calls == [200]
        assert parts[0].has_file
        assert not parts[1].has_file
        assert not parts[2].has_file


def test_partial_tv_delete_is_reconciled_without_retry(app, monkeypatch):
    with app.app_context():
        candidate, parts = _series_candidate()
        sonarr, calls = _sonarr(monkeypatch, fail_on=300)
        assert execute_approved(candidate.id) == "unknown"
        assert set(calls) == {200, 300}
        action = (
            db.session.query(LifecycleAction)
            .filter_by(action_type="delete_media")
            .one()
        )
        assert action.state == "UNKNOWN_RECONCILE"
        sonarr.episode_files = lambda _series_id: [{"id": 100}, {"id": 300}]
        assert reconcile_unknown_actions() == 1
        assert action.state == "FAILED_FINAL"
        assert candidate.state == "BLOCKED"
        assert not parts[1].has_file
        assert parts[2].has_file
        assert set(calls) == {200, 300}
