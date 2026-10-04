"""Section 98 acceptance edge cases not covered by feature-specific tests."""

from datetime import timedelta
from hashlib import sha256
from io import BytesIO

from PIL import Image

from curatarr import db
from curatarr.artwork import _jpeg, apply_badge, reconcile_artwork
from curatarr.integrations import IntegrationError
from curatarr.lifecycle import _candidate_input, evaluate_retention, rescue_candidate
from curatarr.models import (
    Library,
    LifecycleAction,
    MediaIdentity,
    MediaPart,
    PosterSnapshot,
    PurgeCandidate,
    UserMediaState,
    utcnow,
)
from curatarr.policy import rank
from curatarr.services import ingest_event, process_pending_events


def _library(media_type="tv"):
    library = Library(
        jellyfin_library_id=f"edge-{media_type}", name="Edge", media_type=media_type
    )
    db.session.add(library)
    db.session.flush()
    return library


def _series(library, episodes, *, title="Edge Series", sonarr_id=7, days_old=200):
    media = MediaIdentity(
        library_id=library.id,
        media_type="series",
        title=title,
        jellyfin_id=f"{title}-id",
        tvdb_id=7000 + sonarr_id,
        sonarr_id=sonarr_id,
        added_at=utcnow() - timedelta(days=days_old),
    )
    db.session.add(media)
    db.session.flush()
    for season, episode, stored in episodes:
        db.session.add(
            MediaPart(
                media_identity_id=media.id,
                kind="episode",
                season_number=season,
                episode_number=episode,
                jellyfin_id=f"{title}-{season}-{episode}" if stored else None,
                sonarr_episode_id=season * 100 + episode,
                arr_file_id=season * 100 + episode if stored else None,
                has_file=stored,
                size_bytes=100 if stored else 0,
                air_date=utcnow() - timedelta(days=400),
            )
        )
    db.session.commit()
    return media


def _movie(library, index, *, days_old=200):
    media = MediaIdentity(
        library_id=library.id,
        media_type="movie",
        title=f"Edge Movie {index}",
        jellyfin_id=f"edge-movie-{index}",
        radarr_id=index,
        tmdb_id=20000 + index,
        added_at=utcnow() - timedelta(days=days_old),
    )
    db.session.add(media)
    db.session.flush()
    db.session.add(
        MediaPart(
            media_identity_id=media.id, kind="movie_file", has_file=True, size_bytes=100
        )
    )
    db.session.commit()
    return media


def _complete(media, season, episode, user, event_id):
    ingest_event(
        {
            "event_id": event_id,
            "event_type": "item_played",
            "item_external_id": f"{media.title}-{season}-{episode}",
            "series_external_id": media.jellyfin_id,
            "season_number": season,
            "episode_number": episode,
            "user_external_id": user,
            "played": True,
        }
    )
    process_pending_events()


def _searched_seasons():
    return sorted(
        row.payload_json["season"]
        for row in db.session.query(LifecycleAction).filter_by(
            action_type="sonarr_season_search"
        )
    )


def test_two_users_on_different_seasons_union_demand(app):
    """Edge case 2: each viewer advances only their own path; nothing skips ahead."""
    with app.app_context():
        media = _series(
            _library(),
            [
                (1, 1, True),
                (1, 2, True),
                (2, 1, True),
                (2, 2, False),
                (3, 1, False),
                (4, 1, False),
            ],
        )
        _complete(media, 1, 1, "viewer-a", "a-s1")
        assert _searched_seasons() == [2]
        _complete(media, 2, 1, "viewer-b", "b-s2")
        assert _searched_seasons() == [2, 3]
        assert evaluate_retention() == 0
        assert db.session.query(PurgeCandidate).count() == 0


def test_favorite_after_review_keeps_candidate_but_ranks_last(app):
    """Edge case 5: a favorite is a strong signal, not Never Purge."""
    app.config["DEMO_MODE"] = True
    with app.app_context():
        library = _library("movies")
        favorite = _movie(library, 1)
        _movie(library, 3)  # The demo Radarr queue reports movie 2 as downloading.
        assert evaluate_retention() == 2
        ingest_event(
            {
                "event_id": "fav-after-review",
                "event_type": "favorite_changed",
                "item_external_id": favorite.jellyfin_id,
                "user_external_id": "viewer-a",
                "favorite": True,
            }
        )
        process_pending_events()
        evaluate_retention()
        candidate = (
            db.session.query(PurgeCandidate)
            .filter_by(media_identity_id=favorite.id)
            .one()
        )
        assert candidate.state == "REVIEW"
        ranked = rank(
            [_candidate_input(m) for m in db.session.query(MediaIdentity)],
            "oldest_watched_nonfavorite_first",
            utcnow(),
        )
        assert ranked[-1].media_id == favorite.id


def test_favorite_from_one_user_counts_for_all(app):
    """Edge case 17: one viewer's favorite protects ranking though others never watched."""
    with app.app_context():
        library = _library("movies")
        favorite = _movie(library, 1, days_old=300)
        plain = _movie(library, 2, days_old=100)
        db.session.add_all(
            [
                UserMediaState(
                    media_identity_id=favorite.id,
                    jellyfin_user_id="viewer-a",
                    favorite=True,
                ),
                UserMediaState(
                    media_identity_id=favorite.id, jellyfin_user_id="viewer-b"
                ),
            ]
        )
        db.session.commit()
        item = _candidate_input(favorite)
        assert item.favorite and item.last_played_at is None
        for strategy in ("oldest_watched_nonfavorite_first", "weighted"):
            ranked = rank([item, _candidate_input(plain)], strategy, utcnow())
            assert [row.media_id for row in ranked] == [plain.id, favorite.id]


def test_series_at_minimum_footprint_is_never_queued(app):
    """Edge case 18: nothing outside the first-N footprint means nothing to reclaim."""
    app.config["DEMO_MODE"] = True
    with app.app_context():
        _series(
            _library(),
            [(1, 1, True), (1, 2, True), (1, 3, True), (1, 4, False), (2, 1, False)],
        )
        assert evaluate_retention() == 0
        assert db.session.query(PurgeCandidate).count() == 0


def test_future_season_without_episodes_is_not_searched(app):
    """Edge case 21: a season known only from metadata yields no search."""
    with app.app_context():
        media = _series(_library(), [(1, 1, True), (1, 2, False)])
        _complete(media, 1, 1, "viewer-a", "only-s1")
        assert _searched_seasons() == [1]


def test_artwork_restore_retries_after_temporary_failure(app, monkeypatch):
    """Edge case 14: a failed restore stays pending and later reconciliation repairs it."""
    output = BytesIO()
    Image.new("RGB", (300, 450), "#223344").save(output, format="JPEG")
    source = output.getvalue()

    class FlakyJellyfin:
        current = source
        fail_restore = True

        def item(self, _item_id):
            return {"ImageTags": {"Primary": sha256(self.current).hexdigest()}}

        def image(self, _item_id):
            return self.current

        def put_image(self, _item_id, data):
            if data == _jpeg(source) and self.fail_restore:
                self.fail_restore = False
                raise IntegrationError("Jellyfin unavailable")
            self.current = data

    fake = FlakyJellyfin()
    monkeypatch.setattr("curatarr.artwork.client", lambda _kind: fake)
    with app.app_context():
        media = _series(_library(), [(1, 1, True)])
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="LEAVING_SOON",
            reason_code="inactivity",
            reason_text="Old",
            reclaimable_bytes=100,
        )
        db.session.add(candidate)
        db.session.commit()
        assert apply_badge(candidate)
        rescue_candidate(candidate, "Playback resumed.")
        reconcile_artwork()
        snapshot = db.session.query(PosterSnapshot).one()
        assert snapshot.active
        assert fake.current != _jpeg(source)
        reconcile_artwork()
        db.session.refresh(snapshot)
        assert not snapshot.active
        assert fake.current == _jpeg(source)
