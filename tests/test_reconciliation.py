from datetime import UTC, datetime, timedelta

from curatarr import db
from curatarr.models import (
    EpisodeUserState,
    Library,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
    UserMediaState,
    utcnow,
)
from curatarr.services import reconcile_user_state


def test_missed_playback_and_favorite_recovered(app, monkeypatch):
    when = datetime(2026, 1, 1, tzinfo=UTC).isoformat()

    class FakeJellyfin:
        def users(self):
            return [{"Id": "viewer-a"}]

        def user_items(self, _user, _library, start, _limit):
            if start:
                return {"Items": [], "TotalRecordCount": 2}
            return {
                "Items": [
                    {
                        "Id": "series-r",
                        "Type": "Series",
                        "UserData": {"IsFavorite": True},
                    },
                    {
                        "Id": "episode-r",
                        "Type": "Episode",
                        "SeriesId": "series-r",
                        "ParentIndexNumber": 1,
                        "IndexNumber": 1,
                        "UserData": {"Played": True, "LastPlayedDate": when},
                    },
                ],
                "TotalRecordCount": 2,
            }

    monkeypatch.setattr("curatarr.services.client", lambda _kind: FakeJellyfin())
    with app.app_context():
        library = Library(
            jellyfin_library_id="reconcile-tv", name="Demo TV", media_type="tv"
        )
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id,
            media_type="series",
            title="Sample Series",
            jellyfin_id="series-r",
        )
        db.session.add(media)
        db.session.flush()
        db.session.add(
            MediaPart(
                media_identity_id=media.id,
                kind="episode",
                season_number=1,
                episode_number=1,
                jellyfin_id="episode-r",
                has_file=True,
            )
        )
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="LEAVING_SOON",
            reason_code="inactivity",
            reason_text="Old",
            reclaimable_bytes=100,
            scheduled_delete_at=utcnow() + timedelta(days=3),
        )
        db.session.add(candidate)
        db.session.commit()
        assert reconcile_user_state() == 2
        state = db.session.query(UserMediaState).one()
        assert state.favorite is True
        assert state.completed_episode_count == 1
        assert db.session.query(EpisodeUserState).one().played is True
        assert candidate.state == "RESCUED"
        assert candidate.scheduled_delete_at is None
        assert reconcile_user_state() == 0


def test_reconciliation_finds_completed_episode_after_item_id_change(app, monkeypatch):
    class FakeJellyfin:
        def users(self):
            return [{"Id": "viewer-b"}]

        def user_items(self, _user, _library, start, _limit):
            return {
                "Items": []
                if start
                else [
                    {
                        "Id": "new-episode-id",
                        "Type": "Episode",
                        "SeriesId": "series-stable",
                        "ParentIndexNumber": 2,
                        "IndexNumber": 3,
                        "UserData": {"Played": True},
                    }
                ],
                "TotalRecordCount": 1,
            }

    monkeypatch.setattr("curatarr.services.client", lambda _kind: FakeJellyfin())
    with app.app_context():
        library = Library(
            jellyfin_library_id="changed-id", name="Demo TV", media_type="tv"
        )
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id,
            media_type="series",
            title="Series",
            jellyfin_id="series-stable",
        )
        db.session.add(media)
        db.session.flush()
        part = MediaPart(
            media_identity_id=media.id,
            kind="episode",
            season_number=2,
            episode_number=3,
            jellyfin_id="old-episode-id",
            has_file=True,
        )
        db.session.add(part)
        db.session.commit()
        assert reconcile_user_state() == 1
        assert db.session.query(EpisodeUserState).one().media_part_id == part.id
        assert db.session.query(UserMediaState).one().completed_episode_count == 1
        assert reconcile_user_state() == 0


def test_reconciliation_pages_without_total_count_and_keeps_users_distinct(
    app, monkeypatch
):
    class FakeJellyfin:
        def users(self):
            return [{"Id": "viewer-a"}, {"Id": "viewer-b"}]

        def user_items(self, user_id, _library, start, _limit):
            if start == 0:
                return {
                    "Items": [
                        {"Id": f"other-{number}", "Type": "Movie"}
                        for number in range(100)
                    ]
                }
            if start == 100:
                season = 1 if user_id == "viewer-a" else 2
                return {
                    "Items": [
                        {
                            "Id": f"episode-{season}",
                            "Type": "Episode",
                            "SeriesId": "series-paged",
                            "ParentIndexNumber": season,
                            "IndexNumber": 1,
                            "UserData": {"Played": True},
                        }
                    ]
                }
            return {"Items": []}

    monkeypatch.setattr("curatarr.services.client", lambda _kind: FakeJellyfin())
    with app.app_context():
        library = Library(
            jellyfin_library_id="paged-tv", name="Demo TV", media_type="tv"
        )
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id,
            media_type="series",
            title="Series",
            jellyfin_id="series-paged",
        )
        db.session.add(media)
        db.session.flush()
        for season in (1, 2):
            db.session.add(
                MediaPart(
                    media_identity_id=media.id,
                    kind="episode",
                    season_number=season,
                    episode_number=1,
                    jellyfin_id=f"episode-{season}",
                    has_file=True,
                )
            )
        db.session.commit()
        assert reconcile_user_state() == 2
        states = db.session.query(EpisodeUserState).all()
        assert {state.jellyfin_user_id for state in states} == {
            "viewer-a",
            "viewer-b",
        }
        assert {state.media_part_id for state in states} == {
            part.id for part in db.session.query(MediaPart).all()
        }
