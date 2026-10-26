"""The nudity index: films read a frame a second, detections turned into
moments, films rated for all ages skipped, and the rankings for admins only."""

import pytest
from test_enrichment import _movie, media_env  # noqa: F401

from curatarr import db, enrichment, nudity
from curatarr.models import ChapterState
from curatarr.services import set_setting


def _hit(at, kind="breasts", score=0.55, who=None, chest=0.0):
    """A detection the large model scored ``score`` (the small one 0.5)."""
    return [at, kind, 0.5, who, score, chest]


def test_detections_become_moments():
    result = {
        "hits": [
            _hit(60, score=0.3),  # the large model disagrees: not counted
            _hit(300),
            _hit(301),  # two seconds running: a moment
            _hit(305),  # same moment
            _hit(900, score=0.7),  # confident on its own
            _hit(1000, score=0.7, chest=0.65),  # a man's bare chest
            _hit(1100, score=0.7, who="m"),  # only men in frame
            _hit(1800, "vulva", 0.6),  # genitals need more certainty
            _hit(1801, "vulva", 0.62),
            _hit(2000, "vulva", 0.75),
            _hit(2001, "vulva", 0.72),
            _hit(2400, "bottoms", 0.8, "f"),
            _hit(2500, "penis", 0.9),  # noted, not shown
            [3000, "breasts", 0.9, None],  # read before the large model checked
        ]
    }
    found = nudity.summary(result)
    assert found["breasts"] == {"first": 300, "scenes": 2, "seconds": 4}
    assert found["vulva"]["first"] == 2000 and found["vulva"]["scenes"] == 1
    assert found["bottoms"]["scenes"] == 1 and found["bottoms_f"]["scenes"] == 1
    assert "penis" in found
    assert [m["kind"] for m in nudity.timeline(result)] == [
        "breasts",
        "breasts",
        "vulva",
        "bottoms",
    ]
    assert nudity.summary({"hits": [_hit(10, score=0.45)]}) == {}
    assert nudity.clock(3725) == "1:02:05" and nudity.clock(65) == "1:05"


class Jellyfin:
    def __init__(self, ratings):
        self.ratings = ratings

    def item(self, item_id):
        return {"Id": item_id, "OfficialRating": self.ratings.get(item_id)}


@pytest.fixture
def indexing(app, media_env, monkeypatch):  # noqa: F811
    set_setting("chapter_options", {**enrichment.options(), "nudity_index": True})
    db.session.commit()
    monkeypatch.setattr(nudity, "available", lambda: True)
    monkeypatch.setattr(nudity, "_transcoding", lambda: False)
    monkeypatch.setattr(enrichment, "file_exists", lambda _path: True)
    read = []

    def scan(path):
        read.append(path)
        return {
            "frames": 5400,
            "seconds": 5400,
            "hits": [_hit(120, score=0.7), _hit(2000, "vulva", 0.8)],
            "checked": 2,
        }

    monkeypatch.setattr(nudity, "scan", scan)
    jellyfin = Jellyfin({})
    monkeypatch.setattr("curatarr.services.client", lambda _kind: jellyfin)
    return read, jellyfin


def test_films_are_indexed_once_and_again_when_replaced(indexing):
    read, _jellyfin = indexing
    _library, media, part = _movie()
    part.size_bytes = 1000
    db.session.commit()
    assert nudity.enabled() and nudity.sweep() == 1
    state = db.session.query(ChapterState).filter_by(media_part_id=part.id).one()
    assert state.nudity_size == 1000 and len(state.nudity["hits"]) == 2
    assert nudity.sweep() == 0  # done
    part.size_bytes = 2000  # a new copy
    db.session.commit()
    assert nudity.sweep() == 1 and len(read) == 2
    shown = nudity.for_title(media.id)
    assert shown["found"]["breasts"]["first"] == 120 and not shown["skipped"]


def test_animation_and_films_for_all_ages_are_skipped(indexing):
    read, jellyfin = indexing
    _library, media, part = _movie()
    jellyfin.ratings["jf-film"] = "PG"
    nudity.sweep()
    assert read == [] and nudity.for_title(media.id)["skipped"] == "rated PG"
    media.genres = ["Animation", "Family"]
    part.size_bytes = 5
    db.session.commit()
    jellyfin.ratings["jf-film"] = "R"
    nudity.sweep()
    assert read == [] and nudity.for_title(media.id)["skipped"] == "animation"


def test_off_by_default(app, monkeypatch):
    monkeypatch.setattr(nudity, "available", lambda: True)
    assert not nudity.enabled()


def test_rankings_on_fun_facts_and_the_films_page(indexing, client):
    _read, _jellyfin = indexing
    _library, media, _part = _movie()
    nudity.sweep()
    nudity._cache.clear()
    facts = nudity.facts()
    assert facts["films"] == 1 and facts["clean"] == 0
    titles = {c["title"]: c for c in facts["categories"]}
    assert titles["Soonest breasts"]["films"][0]["value"] == "2:00"
    assert titles["Full frontal: women"]["films"][0]["value"] == 1
    page = client.get("/fun").text
    assert 'id="nudity"' in page and "Soonest breasts" in page
    title = client.get(f"/titles/{media.id}").text
    assert "Breasts: first at 2:00" in title
    assert 'class="nudity-bar"' in title and "33:20 · Full frontal: women" in title
    assert [m["kind"] for m in nudity.for_title(media.id)["timeline"]] == [
        "breasts",
        "vulva",
    ]


def test_the_rankings_are_for_administrators_only(app, client, monkeypatch):
    monkeypatch.setattr(nudity, "facts", lambda: pytest.fail("worked out for a viewer"))
    app.config["ALLOW_UNAUTHENTICATED"] = False
    app.config["DEMO_MODE"] = True
    set_setting("household_user_ids", ["user-demo-viewer"])
    db.session.commit()
    client.post("/login", data={"username": "demo-viewer", "password": "demo"})
    page = client.get("/fun")
    assert page.status_code == 200 and "<h1>Fun facts</h1>" in page.text
    assert 'id="nudity"' not in page.text


def test_a_model_that_wont_download_leaves_films_for_later(indexing, monkeypatch):
    _library, media, _part = _movie()

    def unavailable(_path):
        raise nudity.ModelUnavailable("offline")

    monkeypatch.setattr(nudity, "scan", unavailable)
    assert nudity.sweep() == 0
    assert nudity.for_title(media.id) is None  # not failed: tried again later
    assert nudity._due().count() == 1


def test_the_setting_shows_on_enhance(app, client, monkeypatch):
    monkeypatch.setattr(nudity, "available", lambda: True)
    page = client.get("/chapters/settings")
    assert page.status_code == 200 and "Index nudity in films" in page.text
