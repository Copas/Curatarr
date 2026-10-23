"""What the household's choices change about cleanup (curation.py)."""

from datetime import UTC, datetime, timedelta

from curatarr import curation, db
from curatarr.lifecycle import _candidate_input, update_forecast
from curatarr.models import (
    Library,
    LibraryPolicy,
    LifecycleEvent,
    MediaIdentity,
    MediaPart,
    UserMediaState,
    utcnow,
)
from curatarr.policy import DEFAULTS, CandidateInput, eligible, rank
from curatarr.services import set_setting

OCTOBER = datetime(2026, 10, 10, tzinfo=UTC)
JULY = datetime(2026, 7, 1, tzinfo=UTC)


def _film(title="Film", *, size=60, keywords=None, tmdb=1, added_days=400):
    library = db.session.query(Library).first()
    if not library:
        library = Library(jellyfin_library_id="m", name="Movies", media_type="movies")
        db.session.add(library)
        db.session.flush()
    media = MediaIdentity(
        library_id=library.id,
        media_type="movie",
        title=title,
        jellyfin_id=f"{title}-id",
        radarr_id=tmdb,
        tmdb_id=tmdb,
        keywords=keywords,
        added_at=utcnow() - timedelta(days=added_days),
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
    curation.forget()
    return media


def _played(media, user="u1", when=None, count=1):
    db.session.add(
        UserMediaState(
            media_identity_id=media.id,
            jellyfin_user_id=user,
            last_played_at=when or utcnow(),
            play_count=count,
        )
    )
    db.session.commit()
    curation.forget()


def _requested(media, user_id="u1", days_ago=3):
    at = (utcnow() - timedelta(days=days_ago)).isoformat()
    set_setting(
        curation.REQUESTS_KEY,
        {media.id: [{"user_id": user_id, "user": "Sam", "at": at}]},
    )
    db.session.commit()
    curation.forget()


# --- Requests ------------------------------------------------------------------


def test_a_request_holds_a_title_until_the_requester_watches_it(app):
    media = _film()
    _requested(media)
    held = _candidate_input(media)
    assert held.held_until is not None
    assert not eligible(held, DEFAULTS, utcnow())
    _played(media, user="someone-else")
    assert _candidate_input(media).held_until is not None  # not who asked
    _played(media, user="u1")
    assert _candidate_input(media).held_until is None


def test_an_old_unwatched_request_stops_holding(app):
    media = _film()
    _requested(media, days_ago=200)
    assert _candidate_input(media).held_until is None


def test_requests_are_matched_from_seerr(app, monkeypatch):
    film = _film("Asked For", tmdb=55)

    class Seerr:
        def requests(self):
            return [
                {
                    "type": "movie",
                    "status": 2,
                    "createdAt": "2026-10-17T14:44:24.000Z",
                    "media": {"tmdbId": 55},
                    "requestedBy": {
                        "displayName": "Riley",
                        "jellyfinUserId": "ABC-123",
                    },
                },
                {"type": "movie", "status": 3, "media": {"tmdbId": 55}},  # declined
                {"type": "movie", "status": 2, "media": {"tmdbId": 999}},  # not here
            ]

    monkeypatch.setattr("curatarr.services.configured", lambda kind: True)
    monkeypatch.setattr("curatarr.services.client", lambda kind: Seerr())
    assert curation.sync_requests() == 1
    (entry,) = curation.requests_for(film.id)
    assert entry["user"] == "Riley" and entry["user_id"] == "abc123"


# --- Rewatched -----------------------------------------------------------------


def test_a_film_watched_twice_goes_to_the_back_like_a_favourite(app):
    media = _film()
    _played(media, count=2)
    assert _candidate_input(media).rewatched
    now = utcnow()
    old = now - timedelta(days=300)
    once = CandidateInput("once", "movie", old, old, 10)
    twice = CandidateInput(
        "twice", "movie", old, old - timedelta(days=50), 10, rewatched=True
    )
    order = rank([twice, once], "oldest_watched_nonfavorite_first", now)
    assert [item.media_id for item in order] == ["once", "twice"]


def test_a_show_two_people_follow_counts_as_rewatched(app):
    media = MediaIdentity(media_type="series", title="Show")
    states = [UserMediaState(play_count=0), UserMediaState(play_count=1)]
    assert not curation.rewatched(media, states, {"a": 9})
    assert curation.rewatched(media, states, {"a": 9, "b": 5})
    assert curation.rewatched(media, [UserMediaState(play_count=3)], {})


# --- Import lists --------------------------------------------------------------


def test_list_arrivals_get_a_shorter_grace_and_go_first(app):
    media = _film(added_days=10)
    set_setting(curation.LISTS_KEY, [media.id])
    db.session.commit()
    curation.forget()
    item = _candidate_input(media)
    assert item.from_list
    assert eligible(item, DEFAULTS, utcnow())  # past 7 days, within 30
    now = utcnow()
    added = now - timedelta(days=100)
    person = CandidateInput("person", "movie", added - timedelta(days=200), None, 10)
    listed = CandidateInput("listed", "movie", added, None, 10, from_list=True)
    order = rank([person, listed], "oldest_watched_nonfavorite_first", now)
    assert order[0].media_id == "listed"
    _played(media)
    assert not _candidate_input(media).from_list  # someone chose to watch it


def test_list_arrivals_come_from_list_tags_and_radarrs_lists(app):
    show = MediaIdentity(id="show", media_type="series")
    film = MediaIdentity(id="film", media_type="movie")
    other = MediaIdentity(id="other", media_type="movie")

    class Sonarr:
        def import_lists(self):
            return [{"enabled": True, "tags": [7]}, {"enabled": False, "tags": [8]}]

    class Radarr:
        def import_lists(self):
            return []

        def list_movies(self):
            return [{"tmdbId": 5, "lists": [1]}, {"tmdbId": 6, "lists": []}]

    found = curation.ListArrivals(Sonarr(), Radarr())
    found.observe(show, {"tags": [7]})
    found.observe(film, {"tmdbId": 5, "tags": []})
    found.observe(other, {"tmdbId": 6, "tags": [8]})
    assert found.found == {"show", "film"}


# --- Seasons -------------------------------------------------------------------


def test_halloween_films_are_held_through_october_and_the_weeks_before(app):
    media = _film("Pumpkin", keywords=["halloween", "slasher"])
    opts = curation.options()
    until, season = curation.seasonal_hold(media, OCTOBER, opts)
    assert season["key"] == "halloween" and until.date().isoformat() == "2026-11-01"
    assert curation.seasonal_hold(media, datetime(2026, 9, 20, tzinfo=UTC), opts)
    assert curation.seasonal_hold(media, JULY, opts) is None
    assert curation.seasonal_hold(media, OCTOBER, opts | {"seasonal": False}) is None


def test_a_film_watched_last_december_is_a_christmas_film(app):
    media = _film("Snow Day")
    db.session.add(
        LifecycleEvent(
            source="jellyfin",
            event_type="item_played",
            occurred_at=datetime(utcnow().year - 1, 12, 20, tzinfo=UTC),
            media_identity_id=media.id,
            payload_hash="x",
            normalized_json={},
        )
    )
    db.session.commit()
    curation.forget()
    assert [s["key"] for s in curation.seasons_for(media)] == ["christmas"]


def test_removed_seasonal_films_come_back_before_their_season(app, monkeypatch):
    media = _film("Pumpkin", keywords=["halloween"], tmdb=77)
    curation.remember_for_return(media, {"qualityProfileId": 4, "rootFolderPath": "/m"})
    added = []

    class Radarr:
        def lookup_tmdb(self, tmdb_id):
            return {"tmdbId": tmdb_id, "title": "Pumpkin"}

        def add_movie(self, movie):
            added.append(movie)

    monkeypatch.setattr("curatarr.services.configured", lambda kind: True)
    monkeypatch.setattr("curatarr.services.client", lambda kind: Radarr())
    set_setting(curation.SETTINGS_KEY, {"seasonal_return": True})
    db.session.commit()
    assert curation.return_seasonal(JULY) == 0
    assert curation.return_seasonal(datetime(2026, 9, 25, tzinfo=UTC)) == 1
    assert (
        added[0]["qualityProfileId"] == 4 and added[0]["addOptions"]["searchForMovie"]
    )
    assert curation.return_seasonal(OCTOBER) == 0  # once only


def test_household_choices_page_saves(app, client):
    page = client.get("/rules/household").text
    assert "Seasonal shelves" in page and "Connect Seerr" in page
    client.post(
        "/rules/household",
        data={
            "request_hold_days": "30",
            "list_grace_days": "3",
            "season_lead_days": "7",
        },
    )
    assert curation.options() | {} == curation.DEFAULTS | {
        "request_hold_days": 30,
        "list_grace_days": 3,
        "season_lead_days": 7,
        "seasonal": False,
        "seasonal_return": False,
        "leaving_collection": False,  # an unticked box
        "poster_language": False,
    }


def test_the_title_page_says_why_a_title_is_kept(app, client):
    media = _film()
    _requested(media)
    page = client.get(f"/titles/{media.id}").text
    assert "Requested by Sam" in page and "kept until they watch it" in page


# --- Disk forecast -------------------------------------------------------------


class Disk:
    def __init__(self, free):
        self.free = free

    def queue(self):
        return {"records": []}

    def diskspace(self):
        return [{"path": "/media", "totalSpace": 10_000, "freeSpace": self.free}]


def test_the_forecast_says_when_the_disk_runs_low_and_what_goes_first(app, monkeypatch):
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _k: Disk(3_000))
    old = _film("Old", size=500)
    _film("Older", size=400, tmdb=2)
    db.session.add(
        LibraryPolicy(
            library_id=old.library_id,
            policy_json={"disk_path": "/media/movies", "low_free_percent": 10},
        )
    )
    new = _film("New arrival", size=1_400, tmdb=3, added_days=1)
    new.parts[0].acquired_at = utcnow() - timedelta(days=1)
    db.session.commit()
    (disk,) = update_forecast()
    # 1,400 bytes in a week is 200 a day; 2,000 above the 1,000 mark: 10 days.
    assert disk["per_day"] == 200 and disk["days"] == 10.0
    assert [p["title"] for p in disk["picks"]] == ["Old", "Older"]
    assert "Disk forecast" in app.test_client().get("/").text


def test_play_counts_are_stored_for_each_person(app):
    from curatarr.services import _save_play_counts

    media = _film()
    _played(media, user="u1", count=0)
    _save_play_counts({(media.id, "u1"): 3, (media.id, "u2"): 1, (media.id, "u3"): 0})
    counts = {
        s.jellyfin_user_id: s.play_count
        for s in db.session.query(UserMediaState).filter_by(media_identity_id=media.id)
    }
    assert counts == {"u1": 3, "u2": 1}


def test_the_review_queue_offers_shrink_and_shows_notes(app, client):
    from curatarr.models import PurgeCandidate

    media = _film("Epic", size=20 * 1024**3)
    _played(media, count=2)
    db.session.add(
        PurgeCandidate(
            media_identity_id=media.id,
            state="REVIEW",
            reason_code="disk_pressure",
            reason_text="Disk pressure is low.",
            reclaimable_bytes=1,
        )
    )
    db.session.commit()
    page = client.get("/review").text
    assert "Shrink…" in page and "Rewatched: last in line" in page


def test_a_film_watched_all_year_is_not_seasonal(app):
    media = _film("Comfort")
    year = utcnow().year - 1
    for month in (3, 6, 12):
        db.session.add(
            LifecycleEvent(
                source="jellyfin",
                event_type="item_played",
                occurred_at=datetime(year, month, 20, tzinfo=UTC),
                media_identity_id=media.id,
                payload_hash="x",
                normalized_json={},
            )
        )
    db.session.commit()
    curation.forget()
    assert curation.seasons_for(media) == []
