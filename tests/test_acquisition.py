from curatarr import db
from curatarr.models import (
    AcquisitionState,
    Library,
    LibraryPolicy,
    LifecycleAction,
    MediaIdentity,
    MediaPart,
)
from curatarr.services import ingest_event, process_pending_events


def test_completion_fills_current_and_next_only(app, monkeypatch):
    with app.app_context():
        library = Library(jellyfin_library_id="tv-a", name="Demo TV", media_type="tv")
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id,
            media_type="series",
            title="Sample Series",
            jellyfin_id="show-a",
            tvdb_id=123,
            sonarr_id=21,
        )
        db.session.add(media)
        db.session.flush()
        for season, episode, stored in [
            (1, 1, True),
            (1, 2, False),
            (2, 1, False),
            (3, 1, False),
        ]:
            db.session.add(
                MediaPart(
                    media_identity_id=media.id,
                    kind="episode",
                    season_number=season,
                    episode_number=episode,
                    jellyfin_id="ep-1" if (season, episode) == (1, 1) else None,
                    sonarr_episode_id=season * 10 + episode,
                    has_file=stored,
                )
            )
        db.session.commit()
        payload = {
            "event_id": "complete-1",
            "event_type": "item_played",
            "item_external_id": "ep-1",
            "series_external_id": "show-a",
            "season_number": 1,
            "episode_number": 1,
            "user_external_id": "viewer-a",
            "played": True,
        }
        ingest_event(payload)
        process_pending_events()
        seasons = [
            row.payload_json["season"]
            for row in db.session.query(LifecycleAction)
            .filter_by(action_type="sonarr_season_search")
            .all()
        ]
        assert seasons == [1, 2]
        ingest_event(payload)
        process_pending_events()
        assert (
            db.session.query(LifecycleAction)
            .filter_by(action_type="sonarr_season_search")
            .count()
            == 2
        )

        class FakeSonarr:
            def monitor_episode(self, _episode_id):
                return None

            def season_search(self, _series_id, _season):
                return None

            def queue(self):
                return {"records": []}

            def commands(self):
                return []

        monkeypatch.setattr("curatarr.lifecycle.client", lambda _kind: FakeSonarr())
        from curatarr.lifecycle import run_actions

        assert run_actions() == 2
        assert db.session.query(AcquisitionState).one().state == "ACTIVE"


def test_two_users_same_episode_count_once(app):
    with app.app_context():
        library = Library(jellyfin_library_id="tv-b", name="Demo TV B", media_type="tv")
        db.session.add(library)
        db.session.flush()
        db.session.add(
            LibraryPolicy(
                library_id=library.id, policy_json={"acquisition_threshold": 2}
            )
        )
        media = MediaIdentity(
            library_id=library.id,
            media_type="series",
            title="Sample Series B",
            jellyfin_id="show-b",
            tvdb_id=456,
            sonarr_id=45,
        )
        db.session.add(media)
        db.session.flush()
        for season, episode, stored in [(1, 1, True), (1, 2, True), (2, 1, False)]:
            db.session.add(
                MediaPart(
                    media_identity_id=media.id,
                    kind="episode",
                    season_number=season,
                    episode_number=episode,
                    jellyfin_id=f"b-{season}-{episode}" if stored else None,
                    sonarr_episode_id=season * 10 + episode,
                    has_file=stored,
                )
            )
        db.session.commit()
        for index, user in enumerate(("viewer-a", "viewer-b"), 1):
            ingest_event(
                {
                    "event_id": f"same-{index}",
                    "event_type": "item_played",
                    "item_external_id": "b-1-1",
                    "series_external_id": "show-b",
                    "season_number": 1,
                    "episode_number": 1,
                    "user_external_id": user,
                    "played": True,
                }
            )
        process_pending_events()
        assert (
            db.session.query(LifecycleAction)
            .filter_by(action_type="sonarr_season_search")
            .count()
            == 0
        )
        ingest_event(
            {
                "event_id": "second-episode",
                "event_type": "item_played",
                "item_external_id": "b-1-2",
                "series_external_id": "show-b",
                "season_number": 1,
                "episode_number": 2,
                "user_external_id": "viewer-a",
                "played": True,
            }
        )
        process_pending_events()
        assert (
            db.session.query(LifecycleAction)
            .filter_by(action_type="sonarr_season_search")
            .count()
            == 1
        )
