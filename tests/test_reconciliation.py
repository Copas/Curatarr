from datetime import UTC, datetime

from curatarr import db
from curatarr.models import (
    EpisodeUserState,
    Library,
    MediaIdentity,
    MediaPart,
    UserMediaState,
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
        db.session.commit()
        assert reconcile_user_state() == 2
        state = db.session.query(UserMediaState).one()
        assert state.favorite is True
        assert state.completed_episode_count == 1
        assert db.session.query(EpisodeUserState).one().played is True
        assert reconcile_user_state() == 0
