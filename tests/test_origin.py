"""A title's page says where it came from and why its current copy is here."""

from datetime import timedelta

import pytest

from curatarr import db, origin
from curatarr.curation import REQUESTS_KEY, forget
from curatarr.integrations import IntegrationError
from curatarr.models import (
    ChapterState,
    Library,
    LifecycleAction,
    MediaIdentity,
    MediaPart,
    utcnow,
)
from curatarr.services import set_setting


class FakeRadarr:
    def __init__(self, movie, lists=(), listed=(), grab=None, fail=False):
        self.movie, self.lists, self.listed, self.grab, self.fail = (
            movie,
            list(lists),
            list(listed),
            grab,
            fail,
        )

    def request(self, method, path, **kwargs):
        if self.fail:
            raise IntegrationError("Radarr is down")
        return self.movie

    def import_lists(self):
        return self.lists

    def list_movies(self):
        return self.listed

    def current_grab(self, movie):
        return self.grab


@pytest.fixture
def radarr(monkeypatch):
    def install(fake):
        monkeypatch.setattr(origin, "client", lambda kind: fake)
        monkeypatch.setattr(
            origin, "configured", lambda kind: kind in ("radarr", "seerr")
        )
        return fake

    return install


def _film(arrived, seen=None, title="Heat"):
    library = Library(jellyfin_library_id="m", name="Movies", media_type="movies")
    db.session.add(library)
    db.session.flush()
    media = MediaIdentity(
        library_id=library.id, media_type="movie", title=title, radarr_id=7, tmdb_id=949
    )
    db.session.add(media)
    db.session.flush()
    part = MediaPart(
        media_identity_id=media.id,
        kind="movie_file",
        has_file=True,
        acquired_at=arrived,
    )
    db.session.add(part)
    db.session.flush()
    if seen:
        db.session.add(
            ChapterState(
                media_part_id=part.id, media_identity_id=media.id, created_at=seen
            )
        )
    db.session.commit()
    return media


def _scanned_long_ago(media):
    """Curatarr has been looking at files for months (another title)."""
    other = MediaIdentity(library_id=media.library_id, media_type="movie", title="Old")
    db.session.add(other)
    db.session.flush()
    part = MediaPart(media_identity_id=other.id, kind="movie_file", has_file=True)
    db.session.add(part)
    db.session.flush()
    db.session.add(
        ChapterState(
            media_part_id=part.id,
            media_identity_id=other.id,
            created_at=utcnow() - timedelta(days=90),
        )
    )
    db.session.commit()


def test_a_copy_from_before_curatarr_was_looking_isnt_called_first(app, radarr):
    media = _film(utcnow() - timedelta(days=10), seen=utcnow() - timedelta(days=9))
    radarr(FakeRadarr({"id": 7, "tmdbId": 949}))
    assert origin.describe(media)["this_copy"]["why"] == ""


def _action(media, kind, reason, at):
    db.session.add(
        LifecycleAction(
            media_identity_id=media.id,
            action_type=kind,
            state="SUCCEEDED",
            idempotency_key=f"{kind}:{reason}",
            reason_text=reason,
            created_at=at,
        )
    )
    db.session.commit()


def test_a_film_from_an_import_list_found_by_radarrs_search(app, radarr):
    now = utcnow()
    media = _film(now - timedelta(days=1), seen=now - timedelta(hours=23))
    _scanned_long_ago(media)
    radarr(
        FakeRadarr(
            {"id": 7, "tmdbId": 949, "added": "2026-09-05T17:21:13Z"},
            lists=[{"id": 1, "name": "Crime Classics"}, {"id": 2, "name": "Other"}],
            listed=[{"tmdbId": 949, "lists": [1]}, {"tmdbId": 5, "lists": [2]}],
            grab={
                "sourceTitle": "Heat.1995.1080p.BluRay-GRP",
                "data": {"releaseSource": "Search", "indexer": "Indexer"},
            },
        )
    )
    found = origin.describe(media)
    assert found["added"].month == 9
    assert [r["text"] for r in found["reasons"]] == [
        "On Radarr's import list: Crime Classics"
    ]
    assert found["guess"] is None
    assert found["this_copy"]["why"] == "The first copy in the library."
    assert found["this_copy"]["source"] == (
        "Radarr's automatic search found it on Indexer."
    )
    assert found["this_copy"]["release"] == "Heat.1995.1080p.BluRay-GRP"


def test_a_request_and_a_find_favorites_add_are_named(app, radarr):
    now = utcnow()
    media = _film(now - timedelta(days=1))
    set_setting(REQUESTS_KEY, {media.id: [{"user": "Sam", "at": now.isoformat()}]})
    forget()
    _action(media, "film_added", "Added Heat because Alex favourited Heat 2.", now)
    radarr(FakeRadarr({"id": 7, "tmdbId": 949}))
    texts = [r["text"] for r in origin.describe(media)["reasons"]]
    assert texts == [
        "Requested in Seerr by Sam",
        "Added Heat because Alex favourited Heat 2.",
    ]


def test_with_no_request_list_or_curatarr_add_it_was_most_likely_added_by_hand(
    app, radarr
):
    media = _film(utcnow() - timedelta(days=1))
    radarr(FakeRadarr({"id": 7, "tmdbId": 949}))
    found = origin.describe(media)
    assert found["reasons"] == []
    assert "most likely added in Radarr by hand" in found["guess"]


def test_a_copy_curatarr_replaced_says_so(app, radarr):
    now = utcnow()
    media = _film(now - timedelta(hours=2), seen=now - timedelta(days=30))
    _action(
        media,
        "file_replaced",
        "Replaced Heat: it was cut short.",
        now - timedelta(hours=3),
    )
    radarr(FakeRadarr({"id": 7, "tmdbId": 949}, grab=None))
    copy = origin.describe(media)["this_copy"]
    assert (
        copy["why"]
        == "Curatarr replaced the copy before it. Replaced Heat: it was cut short."
    )
    assert "imported by hand" in copy["source"]


def test_an_upgrade_is_a_later_copy_and_radarr_down_leaves_its_parts_out(app, radarr):
    now = utcnow()
    media = _film(now - timedelta(hours=2), seen=now - timedelta(days=30))
    radarr(FakeRadarr(None, fail=True))
    found = origin.describe(media)
    assert found["added"] is None
    assert found["this_copy"]["why"].startswith("It replaced an earlier copy")
    assert found["this_copy"]["source"] is None


def test_the_title_page_shows_it(app, client, radarr):
    media = _film(utcnow() - timedelta(days=1))
    added = (utcnow() - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    radarr(FakeRadarr({"id": 7, "tmdbId": 949, "added": added}))
    page = client.get(f"/titles/{media.id}").text
    assert "Where it came from</h2>" in page and "Added to Radarr" in page
    assert "most likely added in Radarr by hand" in page
    assert "<strong>This copy</strong> arrived <local-time>" in page


def test_where_a_title_came_from_is_recorded_when_it_arrives(app, client, radarr):
    """Worked out each time the page opened, the answer changed with the
    lists and never said when; now it's noted once, soon after arrival, and
    shows on the page (and in Overview's Recently added)."""
    now = utcnow()
    media = _film(now - timedelta(hours=2), seen=now - timedelta(hours=1))
    added = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    radarr(
        FakeRadarr(
            {"id": 7, "tmdbId": 949, "added": added},
            lists=[{"id": 1, "name": "Horror - TMDb Popular"}],
            listed=[{"tmdbId": 949, "lists": [1]}],
        )
    )
    assert origin.sweep(now=now) == 1
    db.session.expire_all()
    kept = db.session.get(MediaIdentity, media.id).origin
    assert kept["short"] == "Horror - TMDb Popular" and kept["on_arrival"] is True
    assert origin.sweep(now=now) == 0  # once
    page = client.get(f"/titles/{media.id}").text
    assert "<strong>Horror - TMDb Popular</strong>" in page
    assert "noted when it arrived" in page
    from curatarr import recent

    films = recent.added()["films"]
    assert films and films[0]["origin"] == "Horror - TMDb Popular"
    assert 'class="reel-meta reel-origin"' in client.get("/").text


def test_with_radarr_down_nothing_is_recorded_and_older_titles_say_afterwards(
    app, radarr
):
    now = utcnow()
    media = _film(now - timedelta(days=200))
    radarr(FakeRadarr(None, fail=True))
    assert origin.sweep(now=now) == 0
    assert db.session.get(MediaIdentity, media.id).origin is None
    radarr(FakeRadarr({"id": 7, "tmdbId": 949, "added": "2026-01-05T17:21:13Z"}))
    assert origin.sweep(now=now) == 1
    kept = db.session.get(MediaIdentity, media.id).origin
    # Looked at long after it was added: a list may have dropped it since.
    assert kept["short"] == origin.LATE and kept["on_arrival"] is False
    assert "lists change" in kept["guess"]


def test_added_long_before_curatarr_looked_isnt_called_by_hand(app, radarr):
    """An import list that brought it may have dropped it since; but a "by
    hand" made on arrival, while the lists still showed, stands."""
    now = utcnow()
    media = _film(now - timedelta(days=1))
    radarr(FakeRadarr({"id": 7, "tmdbId": 949, "added": "2026-01-05T17:21:13Z"}))
    info = origin.describe(media)
    assert info.get("late") and "by hand" not in origin.short(info)
    media.origin = {"short": origin.HAND, "on_arrival": True}
    info = origin.describe(media)
    assert origin.short(info) == origin.HAND


def test_without_seerr_it_doesnt_claim_nobody_requested_it(app, radarr, monkeypatch):
    now = utcnow()
    media = _film(now - timedelta(days=200))
    radarr(FakeRadarr({"id": 7, "tmdbId": 949, "added": "2026-01-05T17:21:13Z"}))
    monkeypatch.setattr(origin, "configured", lambda kind: kind == "radarr")
    info = origin.describe(media)
    assert "Not requested in Seerr" not in info["guess"]
    assert "Seerr isn't connected" in info["guess"]
    assert origin.short(info) == origin.UNKNOWN


def test_import_lists_are_noted_when_a_film_is_added_before_its_file_arrives(
    app, radarr
):
    """A film waited a month for a file; by then the trending list that
    brought it had dropped it, and it was called "Can't tell"."""
    now = utcnow()
    added = now - timedelta(hours=2)
    movie = {
        "id": 7,
        "tmdbId": 949,
        "added": added.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    old = {"id": 8, "tmdbId": 5, "added": "2026-01-05T17:21:13Z"}
    fake = radarr(
        FakeRadarr(
            movie,
            lists=[{"id": 1, "name": "Horror - TMDb Popular"}],
            listed=[{"tmdbId": 949, "lists": [1]}, {"tmdbId": 5, "lists": [1]}],
        )
    )
    assert origin.note_new([], [movie, old], None, fake, now=now) == 1
    assert origin.note_new([], [movie, old], None, fake, now=now) == 0  # once
    # A month on, the file arrives and the list has moved on.
    later = now + timedelta(days=30)
    app.extensions.pop("curatarr_origin_lists", None)
    fake.listed = []
    media = _film(later - timedelta(hours=1))
    origin.record(media, now=later)
    kept = media.origin
    assert kept["short"] == "Horror - TMDb Popular" and kept["on_arrival"] is True
    assert kept["reasons"][0]["at"] is not None


def test_a_film_noted_on_no_list_when_added_is_by_hand_not_cant_tell(app, radarr):
    now = utcnow()
    movie = {"id": 7, "tmdbId": 949, "added": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    fake = radarr(FakeRadarr(movie))
    origin.note_new([], [movie], None, fake, now=now)
    later = now + timedelta(days=30)
    media = _film(later - timedelta(hours=1))
    origin.record(media, now=later)
    assert media.origin["short"] == origin.HAND
    assert media.origin["on_arrival"] is True


def test_notes_are_dropped_after_a_year_and_unreadable_lists_wait(app, radarr):
    from curatarr.services import setting

    now = utcnow()
    movie = {"id": 7, "tmdbId": 949, "added": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    fake = radarr(FakeRadarr(movie, fail=True))
    fake.import_lists = lambda: (_ for _ in ()).throw(IntegrationError("down"))
    assert origin.note_new([], [movie], None, fake, now=now) == 0
    fake = radarr(FakeRadarr(movie))
    assert origin.note_new([], [movie], None, fake, now=now) == 1
    origin.note_new([], [], None, fake, now=now + timedelta(days=400))
    assert setting(origin.EARLY_KEY) == {}
