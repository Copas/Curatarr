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


def test_only_once(app, radarr):
    with app.app_context():
        media = _film()
        request_removal(media)
        with pytest.raises(ValueError, match="already"):
            request_removal(media)
        media.media_type = "other"
        with pytest.raises(ValueError, match="isn't a film or a TV show"):
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


def _old_unwatched_film(title="Never Watched", days=900, size=500):
    library = db.session.query(Library).filter_by(name="Movies").one()
    media = MediaIdentity(
        library_id=library.id,
        media_type="movie",
        title=title,
        jellyfin_id=f"jf-{title}",
        radarr_id=hash(title) % 10000,
        added_at=utcnow() - timedelta(days=days),
    )
    db.session.add(media)
    db.session.flush()
    db.session.add(
        MediaPart(
            media_identity_id=media.id,
            kind="movie_file",
            has_file=True,
            size_bytes=size,
        )
    )
    db.session.commit()
    return media


def test_free_space_filters_and_sorts(app):
    from curatarr.services import free_space_rows

    with app.app_context():
        watched = _film()
        unwatched = _old_unwatched_film()

        def titles(**kw):
            return [r["media"].title for r in free_space_rows(**kw)]

        assert titles(sort="size") == [unwatched.title, watched.title]
        assert titles(watched="watched") == [watched.title]
        assert titles(watched="unwatched") == [unwatched.title]
        assert titles(sort="recent") == [watched.title, unwatched.title]
        assert titles(sort="added") == [unwatched.title, watched.title]
        assert titles(kind="series") == []


def test_a_show_marked_leaving_soon_is_trimmed_when_its_notice_ends(app, monkeypatch):
    from test_reset import FakeJellyfin as ShowJellyfin
    from test_reset import FakeSonarr, _show

    from curatarr.lifecycle import expire_notices

    sonarr = FakeSonarr()
    monkeypatch.setattr(
        "curatarr.lifecycle.client",
        lambda kind: ShowJellyfin() if kind == "jellyfin" else sonarr,
    )
    with app.app_context():
        media, _parts = _show()
        candidate = request_removal(media, where="the Free up space page")
        assert candidate.state == "LEAVING_SOON"
        assert candidate.reclaimable_bytes == 300  # S1E4 and Season 2
        assert expire_notices() == 0  # still in its notice period
        candidate.scheduled_delete_at = utcnow() - timedelta(minutes=1)
        db.session.commit()
        assert expire_notices() == 1
        assert sorted(c[1] for c in sonarr.calls if c[0] == "delete") == [
            1004,
            2001,
            2002,
        ]
        assert ("unmonitor_seasons", 77) in sonarr.calls
        assert candidate.state == "COMPLETED"


def test_the_page_marks_ticked_titles_and_keep_comes_back(
    app, client, radarr, monkeypatch
):
    monkeypatch.setattr("curatarr.artwork.reconcile_artwork", lambda: None)
    with app.app_context():
        first, second = _film().id, _old_unwatched_film().id
    page = client.get("/free-space?watched=unwatched").text
    assert "Never Watched" in page and "Not Very Good" not in page
    response = client.post(
        "/free-space/leaving-soon",
        data={"media_id": [first, second], "back": "/free-space?sort=added"},
        follow_redirects=True,
    )
    assert "Marked 2 titles Leaving Soon" in response.text
    assert response.text.count("Leaving ") >= 2
    with app.app_context():
        candidate = (
            db.session.query(PurgeCandidate).filter_by(media_identity_id=first).one()
        )
        candidate_id = candidate.id
    response = client.post(
        f"/review/{candidate_id}/keep",
        data={"back": "/free-space?sort=added"},
    )
    assert response.headers["Location"].endswith("/free-space?sort=added")
    with app.app_context():
        assert db.session.get(PurgeCandidate, candidate_id).state == "RESCUED"
