"""Title page "Remove this film": Leaving Soon on request, then the usual path."""

from datetime import timedelta

import pytest

from curatarr import db
from curatarr.lifecycle import (
    execute_approved,
    reconcile_candidates,
    request_removal,
    review_candidate,
)
from curatarr.models import (
    Library,
    LibraryPolicy,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
    UserMediaState,
    utcnow,
)
from curatarr.policy import _aware


def _film(*, added_days_ago=3, watched_hours_ago=2):
    library = Library(jellyfin_library_id="films", name="Movies", media_type="movies")
    db.session.add(library)
    db.session.flush()
    db.session.add(LibraryPolicy(library_id=library.id, policy_json={"dry_run": False}))
    media = MediaIdentity(
        library_id=library.id,
        media_type="movie",
        title="Not Very Good",
        jellyfin_id="film-1",
        radarr_id=42,
        tmdb_id=321,
        added_at=utcnow() - timedelta(days=added_days_ago),
    )
    db.session.add(media)
    db.session.flush()
    db.session.add(
        MediaPart(
            media_identity_id=media.id,
            kind="movie_file",
            has_file=True,
            size_bytes=100,
            arr_file_id=7,
        )
    )
    db.session.add(
        UserMediaState(
            media_identity_id=media.id,
            jellyfin_user_id="viewer",
            last_played_at=utcnow() - timedelta(hours=watched_hours_ago),
            play_count=1,
        )
    )
    db.session.commit()
    return media


class FakeRadarr:
    def __init__(self):
        self.deleted = []

    def queue(self):
        return {"records": []}

    def request(self, _method, _path):
        return {"tmdbId": 321}

    def delete_movie(self, movie_id):
        self.deleted.append(movie_id)


class FakeJellyfin:
    def item(self, _item_id):
        return {"ProviderIds": {"Tmdb": "321"}}


@pytest.fixture
def radarr(monkeypatch):
    fake = FakeRadarr()
    monkeypatch.setattr(
        "curatarr.lifecycle.client",
        lambda kind: FakeJellyfin() if kind == "jellyfin" else fake,
    )
    return fake


def test_a_just_watched_new_film_goes_leaving_soon_and_can_be_deleted(app, radarr):
    with app.app_context():
        media = _film()
        candidate = request_removal(media)
        assert candidate.state == "LEAVING_SOON"
        assert _aware(candidate.scheduled_delete_at) > utcnow() + timedelta(days=13)
        assert reconcile_candidates() == 0  # not a rule's choice: nothing undoes it
        review_candidate(candidate.id, "delete")
        assert execute_approved(candidate.id) == "succeeded"
        assert radarr.deleted == [42]
        assert candidate.state == "COMPLETED"


def test_playing_it_again_cancels_the_removal(app, radarr):
    with app.app_context():
        media = _film()
        candidate = request_removal(media)
        db.session.query(UserMediaState).one().last_played_at = utcnow()
        db.session.commit()
        assert reconcile_candidates() == 1
        assert candidate.state == "RESCUED"


def test_only_films_and_only_once(app, radarr):
    with app.app_context():
        media = _film()
        request_removal(media)
        with pytest.raises(ValueError, match="already"):
            request_removal(media)
        media.media_type = "series"
        with pytest.raises(ValueError, match="Only films"):
            request_removal(media)


def test_the_title_page_offers_it(app, client, radarr, monkeypatch):
    monkeypatch.setattr("curatarr.artwork.reconcile_artwork", lambda: None)
    with app.app_context():
        media = _film()
        media_id = media.id
    page = client.get(f"/titles/{media_id}").text
    assert "Remove this film" in page
    response = client.post(f"/titles/{media_id}/remove", follow_redirects=True)
    assert "is Leaving Soon" in response.text
    assert "Remove this film" not in response.text
    with app.app_context():
        assert db.session.query(PurgeCandidate).one().state == "LEAVING_SOON"
