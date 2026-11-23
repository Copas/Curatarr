"""Libraries Curatarr ignores entirely (ignored.py, Setup)."""

from datetime import timedelta

from curatarr import db, ignored, playback_trouble
from curatarr.models import Library, PlaybackTry, utcnow


class _Jellyfin:
    def __init__(self):
        self.asked = 0

    def items(self, parent_id, start=0, limit=100):
        self.asked += 1
        return {"Items": [{"Id": "AAAA-1"}, {"Id": "aaaa2"}], "TotalRecordCount": 2}


def _library(name="Private", media_type="other", enabled=True):
    library = Library(
        jellyfin_library_id=f"jf-{name}",
        name=name,
        media_type=media_type,
        enabled=enabled,
    )
    db.session.add(library)
    db.session.commit()
    return library


def test_items_in_an_ignored_library_are_dropped(app, monkeypatch):
    monkeypatch.setattr(
        ignored, "_cache", {"at": None, "key": None, "ids": frozenset()}
    )
    jellyfin = _Jellyfin()
    _library(enabled=False)
    items = [{"Id": "aaaa1"}, {"Id": "bbbb"}]
    assert ignored.drop(items, jellyfin) == [{"Id": "bbbb"}]
    ignored.drop(items, jellyfin)
    assert jellyfin.asked == 1  # remembered


def test_nothing_ignored_asks_nothing(app):
    _library()
    assert ignored.drop([{"Id": "x"}], object()) == [{"Id": "x"}]


def test_setup_switches_an_unmanaged_library_and_not_a_managed_one(app, client):
    private, movies = _library(), _library("Films", "movies")
    page = client.get("/setup").text
    assert 'id="libraries"' in page and "Private" in page
    client.post(f"/libraries/{private.id}/ignore", data={"ignore": "1"})
    assert db.session.get(Library, private.id).enabled is False
    assert "Ignored" in client.get("/setup").text
    client.post(f"/libraries/{private.id}/ignore", data={"ignore": "0"})
    assert db.session.get(Library, private.id).enabled is True
    assert (
        client.post(f"/libraries/{movies.id}/ignore", data={"ignore": "1"}).status_code
        == 400
    )


def test_plays_in_an_ignored_library_arent_recorded(app, monkeypatch):
    monkeypatch.setattr(ignored, "item_ids", lambda jellyfin=None: frozenset({"aaaa1"}))
    session = {
        "Id": "s1",
        "DeviceName": "TV",
        "PlayState": {"PositionTicks": 0},
        "NowPlayingItem": {"Id": "AAAA-1", "Type": "Movie"},
    }
    playback_trouble.record([session], utcnow())
    assert db.session.query(PlaybackTry).count() == 0
    session["NowPlayingItem"]["Id"] = "cccc"
    playback_trouble.record([session], utcnow() + timedelta(seconds=1))
    assert db.session.query(PlaybackTry).count() == 1
