import pytest

from curatarr import db, franchises
from curatarr.models import Library, LifecycleAction, MediaIdentity, UserMediaState


def film(tmdb_id, title, year, *, existing=False, status="released", genres=()):
    return {
        "tmdbId": tmdb_id,
        "title": title,
        "year": year,
        "status": status,
        "isExisting": existing,
        "isExcluded": False,
        "genres": list(genres),
        "ratings": {"tmdb": {"value": 7.0}},
    }


class FakeRadarr:
    def __init__(self):
        self.added, self.updated = [], []

    def collections(self):
        return [
            {
                "id": 1,
                "tmdbId": 100,
                "title": "Slasher Collection",
                "monitored": False,
                "movies": [
                    film(1, "Slasher", 1980, existing=True),
                    film(2, "Slasher II", 1982, existing=True),
                    film(3, "Slasher III", 1984),
                    film(4, "Slasher Returns", 2099, status="announced"),
                ],
            },
            {
                "id": 2,
                "tmdbId": 200,
                "title": "Complete Collection",
                "movies": [film(5, "Done", 2000, existing=True)],
            },
            {
                "id": 3,
                "tmdbId": 300,
                "title": "Unowned Collection",
                "movies": [film(6, "Never Had", 2000)],
            },
            {
                "id": 4,
                "tmdbId": 400,
                "title": "Half Collection",
                "movies": [film(7, "A", 1990, existing=True)]
                + [film(8 + n, f"B{n}", 1991 + n) for n in range(3)],
            },
        ]

    def collection(self, collection_id):
        return next(c for c in self.collections() if c["id"] == collection_id)

    def update_collection(self, collection):
        self.updated.append(collection)

    def movies(self):
        return [
            {
                "id": 11,
                "tmdbId": 1,
                "genres": ["Horror"],
                "qualityProfileId": 4,
                "rootFolderPath": "/films",
                "collection": {"tmdbId": 100},
            },
            {
                "id": 12,
                "tmdbId": 50,
                "genres": ["Comedy"],
                "qualityProfileId": 1,
                "rootFolderPath": "/other",
            },
            {
                "id": 13,
                "tmdbId": 51,
                "genres": ["Comedy"],
                "qualityProfileId": 1,
                "rootFolderPath": "/other",
            },
        ]

    def recommendations(self):
        return [
            film(20, "Scary Pick", 2001, genres=["Horror"]),
            film(21, "Funny Pick", 2002, genres=["Comedy"]),
            film(22, "Owned Pick", 2003, existing=True, genres=["Horror"]),
            film(23, "Future Pick", 2099, status="announced", genres=["Horror"]),
        ]

    def lookup_tmdb(self, tmdb_id):
        return {"tmdbId": tmdb_id, "title": f"Film {tmdb_id}", "year": 1984}

    def add_movie(self, movie):
        self.added.append(movie)
        return movie | {"id": 99}


@pytest.fixture
def radarr(app, monkeypatch):
    fake = FakeRadarr()
    monkeypatch.setattr(franchises, "client", lambda kind: fake)
    franchises.forget()
    yield fake
    franchises.forget()


def test_gaps_list_partly_owned_series_nearly_complete_first(radarr):
    found = franchises.gaps()
    assert [c["title"] for c in found] == ["Slasher Collection", "Half Collection"]
    slasher = found[0]
    assert (slasher["have"], slasher["total"]) == (2, 3)
    assert [f["title"] for f in slasher["missing"]] == ["Slasher III"]
    assert [f["title"] for f in slasher["coming"]] == ["Slasher Returns"]


def test_dismissed_series_and_films_stay_hidden(radarr):
    franchises.dismiss("collections", 400)
    franchises.dismiss("films", 3)
    assert franchises.gaps() == []


def test_suggestions_rank_by_the_households_favourite_genres(radarr):
    library = Library(jellyfin_library_id="lib", name="Films", media_type="movies")
    db.session.add(library)
    db.session.flush()
    scary = MediaIdentity(
        library_id=library.id, media_type="movie", title="Fav", radarr_id=11
    )
    db.session.add(scary)
    db.session.flush()
    db.session.add(
        UserMediaState(media_identity_id=scary.id, jellyfin_user_id="u", favorite=True)
    )
    db.session.commit()
    picks = franchises.suggestions()
    assert [f["title"] for f in picks["films"]] == ["Scary Pick", "Funny Pick"]
    assert picks["films"][0]["matches"] == ["Horror"] and picks["genres"] == ["Horror"]


def test_add_uses_the_series_profile_and_folder(radarr):
    added = franchises.add([3], collection_tmdb_id=100)
    assert added == ["Film 3 (1984)"]
    sent = radarr.added[0]
    assert (sent["qualityProfileId"], sent["rootFolderPath"]) == (4, "/films")
    assert sent["monitored"] and sent["addOptions"] == {"searchForMovie": True}
    assert (
        db.session.query(LifecycleAction).filter_by(action_type="film_added").count()
        == 1
    )
    # Without a series, the library's most common profile and folder.
    franchises.add([20])
    assert (radarr.added[1]["qualityProfileId"], radarr.added[1]["rootFolderPath"]) == (
        1,
        "/other",
    )


def test_follow_turns_on_radarr_collection_monitoring(radarr):
    assert franchises.follow(1) == "Slasher Collection"
    sent = radarr.updated[0]
    assert sent["monitored"] and sent["searchOnAdd"] and sent["qualityProfileId"] == 4


def test_collections_page_and_actions(client, radarr):
    page = client.get("/collections").text
    assert "Slasher" in page and "You have <strong>2</strong> of 3" in page
    assert "Coming: Slasher Returns (2099)" in page and "Scary Pick" in page
    response = client.post(
        "/collections/add",
        data={"tmdb_id": ["3"], "collection": "100", "anchor": "#c-1"},
    )
    assert response.status_code == 302 and response.location.endswith(
        "/collections#c-1"
    )
    assert radarr.added[0]["tmdbId"] == 3
    evil = client.post(
        "/collections/dismiss",
        data={"kind": "films", "tmdb_id": "21", "anchor": "//evil.example"},
    )
    assert evil.location.endswith("/collections")
    assert "Funny Pick" not in client.get("/collections").text
