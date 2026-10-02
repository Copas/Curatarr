from curatarr import db
from curatarr.lifecycle import run_actions
from curatarr.models import Library, MediaIdentity, MediaPart, WatchStateSnapshot
from curatarr.services import queue_watch_restoration


def test_returned_episode_queues_played_state_restoration(app):
    app.config["DEMO_MODE"] = True
    with app.app_context():
        library = Library(
            jellyfin_library_id="demo-return", name="Demo TV", media_type="tv"
        )
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id,
            media_type="series",
            title="Sample Series",
            jellyfin_id="return-series",
            tvdb_id=123,
            sonarr_id=12,
        )
        db.session.add(media)
        db.session.flush()
        db.session.add(
            MediaPart(
                media_identity_id=media.id,
                kind="episode",
                season_number=2,
                episode_number=1,
                jellyfin_id="returned-episode",
                has_file=True,
            )
        )
        snapshot = WatchStateSnapshot(
            media_identity_id=media.id,
            jellyfin_user_id="demo-viewer",
            provider_key="tvdb:123",
            season_number=2,
            episode_number=1,
            played=True,
        )
        db.session.add(snapshot)
        db.session.commit()
        assert queue_watch_restoration() == 1
        assert run_actions() == 1
        assert snapshot.restored_at is not None
