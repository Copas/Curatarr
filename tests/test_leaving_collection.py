"""A Jellyfin collection of the titles Leaving Soon, kept in step."""

from test_request_removal import _film, _old_unwatched_film

from curatarr import db, leaving_collection
from curatarr.lifecycle import request_removal, review_candidate

N = leaving_collection._norm


class Jellyfin:
    def __init__(self):
        self.collections = {}
        self.calls = 0

    def create_collection(self, name, ids):
        self.calls += 1
        self.collections["c1"] = {"name": name, "items": set(ids)}
        return "c1"

    def collection_exists(self, collection_id):
        return collection_id in self.collections

    def collection_items(self, collection_id):
        self.calls += 1
        return list(self.collections[collection_id]["items"])

    def add_to_collection(self, collection_id, ids):
        self.collections[collection_id]["items"] |= set(ids)

    def remove_from_collection(self, collection_id, ids):
        self.collections[collection_id]["items"] -= set(ids)

    posters = 0

    def image(self, _item_id, _kind="Primary"):
        from io import BytesIO

        from PIL import Image

        out = BytesIO()
        Image.new("RGB", (200, 300), "#335577").save(out, format="JPEG")
        return out.getvalue()

    def put_image(self, _item_id, data, _kind="Primary"):
        self.posters += 1
        self.poster = data


class Radarr:
    def queue(self):
        return {"records": []}


def _setup(monkeypatch):
    jellyfin = Jellyfin()
    monkeypatch.setattr(
        "curatarr.services.client",
        lambda kind: jellyfin if kind == "jellyfin" else Radarr(),
    )
    return jellyfin


def test_titles_join_and_leave_the_collection(app, monkeypatch):
    jellyfin = _setup(monkeypatch)
    with app.app_context():
        assert leaving_collection.sync() is None  # nothing leaving, nothing made
        film = _film()
        other = _old_unwatched_film()
        first = request_removal(film)
        request_removal(other)
        leaving_collection.sync()
        assert jellyfin.collections["c1"]["name"] == "Leaving Soon"
        assert jellyfin.posters == 1 and jellyfin.poster[:2] == b"\xff\xd8"  # a JPEG
        assert jellyfin.collections["c1"]["items"] == {
            N("film-1"),
            N("jf-Never Watched"),
        }
        calls = jellyfin.calls
        leaving_collection.sync()
        assert jellyfin.calls == calls  # unchanged: Jellyfin isn't asked
        review_candidate(first.id, "keep")
        leaving_collection.sync()
        assert jellyfin.collections["c1"]["items"] == {N("jf-Never Watched")}


def test_a_collection_deleted_in_jellyfin_is_made_again(app, monkeypatch):
    jellyfin = _setup(monkeypatch)
    with app.app_context():
        request_removal(_film())
        leaving_collection.sync()
        jellyfin.collections.clear()
        other = _old_unwatched_film()
        request_removal(other)
        leaving_collection.sync()
        assert jellyfin.collections["c1"]["items"] == {
            N("film-1"),
            N("jf-Never Watched"),
        }


def test_turned_off_empties_it(app, monkeypatch):
    from curatarr.curation import SETTINGS_KEY
    from curatarr.services import set_setting

    jellyfin = _setup(monkeypatch)
    with app.app_context():
        request_removal(_film())
        leaving_collection.sync()
        set_setting(SETTINGS_KEY, {"leaving_collection": False})
        db.session.commit()
        leaving_collection.sync()
        assert jellyfin.collections["c1"]["items"] == set()
