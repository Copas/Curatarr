"""Titles show their year in lists and pickers: "Carrie (1976)", since a
remake shares its name."""

from curatarr import db
from curatarr.models import Library, MediaIdentity
from curatarr.services import _match_media, _year


def _library():
    library = Library(jellyfin_library_id="films", name="Movies", media_type="movies")
    db.session.add(library)
    db.session.flush()
    return library


def test_the_label_adds_the_year_once():
    assert MediaIdentity(title="Carrie", year=1976).label == "Carrie (1976)"
    assert MediaIdentity(title="Carrie").label == "Carrie"  # no year known
    # Some names already carry it.
    assert (
        MediaIdentity(title="The Bunny Game (2010)", year=2010).label
        == "The Bunny Game (2010)"
    )


def test_only_a_plausible_year_is_kept():
    assert _year(1976) == 1976 and _year("2013") == 2013
    for nothing in (0, None, "", "soon", 99999):  # Radarr says 0 when unsure
        assert _year(nothing) is None


def test_discovery_reads_jellyfins_year_and_keeps_it_when_absent(app):
    library = _library()
    media = _match_media(
        library, "movie", {"Id": "a1", "Name": "Carrie", "ProductionYear": 1976}
    )
    assert media.year == 1976
    again = _match_media(library, "movie", {"Id": "a1", "Name": "Carrie"})
    assert again.id == media.id and again.year == 1976


def test_generates_title_picker_tells_remakes_apart(app, client):
    library = _library()
    for year, item in ((1976, "c1"), (2013, "c2")):
        db.session.add(
            MediaIdentity(
                library_id=library.id,
                media_type="movie",
                title="Carrie",
                year=year,
                jellyfin_id=item,
            )
        )
    db.session.commit()
    found = client.get("/library/posters/generate/find?q=carr").get_json()["titles"]
    assert sorted(t["title"] for t in found) == ["Carrie (1976)", "Carrie (2013)"]
    assert "Carrie (1976)" in client.get("/titles?q=carrie").text


def test_the_filter_falls_back_to_the_name_for_a_stand_in(app):
    """Some pages cache plain copies of a title (Fun facts): those show their
    name, never a blank."""
    from types import SimpleNamespace

    from curatarr.routes import labelled

    assert labelled(MediaIdentity(title="Carrie", year=2013)) == "Carrie (2013)"
    assert labelled(SimpleNamespace(title="Heat")) == "Heat"
    assert labelled({"label": "not a title"}) == ""
