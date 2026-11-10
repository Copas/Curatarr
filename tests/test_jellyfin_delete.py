"""Delete in Jellyfin, through the Curatarr plugin: Leaving Soon instead."""

from test_request_removal import _film

from curatarr import db
from curatarr.models import LibraryPolicy, PurgeCandidate
from curatarr.services import set_setting

DASHED = "01234567-89ab-cdef-0123-456789abcdef"
ITEM = DASHED.replace("-", "")  # Jellyfin's usual form


def _post(client, body, token="plugin-token"):
    return client.post(
        "/api/v1/jellyfin/delete", json=body, headers={"X-Curatarr-Token": token}
    )


def _setup(app, monkeypatch):
    monkeypatch.setattr("curatarr.artwork.reconcile_artwork", lambda: None)
    with app.app_context():
        set_setting("webhook_token", "plugin-token")
        media = _film()
        media.jellyfin_id = ITEM
        db.session.commit()
        return media.id


def test_a_deleted_film_is_marked_leaving_soon_instead(app, client, monkeypatch):
    media_id = _setup(app, monkeypatch)
    response = _post(client, {"itemId": DASHED, "userName": "Sam"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["handled"] and body["result"] == "leaving_soon" and body["deleteAt"]
    with app.app_context():
        candidate = (
            db.session.query(PurgeCandidate).filter_by(media_identity_id=media_id).one()
        )
        assert candidate.state == "LEAVING_SOON"
        assert "a delete in Jellyfin by Sam" in candidate.reason_text
    # Deleting it again deletes it now, through Radarr (the worker's next pass).
    again = _post(client, {"itemId": ITEM, "userName": "Sam"}).get_json()
    assert again["handled"] and again["result"] == "deleting"
    with app.app_context():
        assert db.session.get(PurgeCandidate, candidate.id).state == "APPROVED"
    third = _post(client, {"itemId": ITEM, "userName": "Sam"}).get_json()
    assert third["result"] == "already"


def test_the_web_apps_delete_deletes_now(app, client, monkeypatch):
    media_id = _setup(app, monkeypatch)
    body = _post(client, {"itemId": ITEM, "userName": "Sam", "mode": "now"}).get_json()
    assert body["handled"] and body["result"] == "deleting"
    with app.app_context():
        candidate = (
            db.session.query(PurgeCandidate).filter_by(media_identity_id=media_id).one()
        )
        assert candidate.state == "APPROVED"


def test_the_web_apps_leaving_soon_never_deletes(app, client, monkeypatch):
    _setup(app, monkeypatch)
    status = _post(client, {"itemId": ITEM, "mode": "status"}).get_json()
    assert status["handled"] and not status["leavingSoon"]
    first = _post(
        client, {"itemId": ITEM, "userName": "Sam", "mode": "leaving_soon"}
    ).get_json()
    assert first["result"] == "leaving_soon"
    with app.app_context():
        candidate = db.session.query(PurgeCandidate).one()
        assert "Leaving Soon in Jellyfin by Sam" in candidate.reason_text
    again = _post(client, {"itemId": ITEM, "mode": "leaving_soon"}).get_json()
    assert again["result"] == "already"
    status = _post(client, {"itemId": ITEM, "mode": "status"}).get_json()
    assert status["leavingSoon"] and not status["deleting"]
    assert _post(client, {"itemId": ITEM, "mode": "other"}).status_code == 400


def test_unknown_items_are_left_to_jellyfin(app, client, monkeypatch):
    _setup(app, monkeypatch)
    body = _post(client, {"itemId": "f" * 32}).get_json()
    assert body == {"handled": False}
    assert _post(client, {"itemId": "not-an-id"}).status_code == 400


def test_a_title_curatarr_must_keep_refuses_the_delete(app, client, monkeypatch):
    media_id = _setup(app, monkeypatch)
    with app.app_context():
        policy = db.session.query(LibraryPolicy).one()
        policy.policy_json = {"dry_run": False, "never_purge": True}
        db.session.commit()
    body = _post(client, {"itemId": ITEM, "userName": "Sam"}).get_json()
    assert body["handled"] and "Never Purge" in body["refused"]
    with app.app_context():
        assert (
            not db.session.query(PurgeCandidate)
            .filter_by(media_identity_id=media_id)
            .count()
        )


def test_needs_the_webhook_token(app, client, monkeypatch):
    _setup(app, monkeypatch)
    assert _post(client, {"itemId": ITEM}, token="wrong").status_code == 403
    assert _post(client, {"check": True}).get_json() == {"ok": True}


class _Jellyfin:
    """Jellyfin, which sees the library under its own mount."""

    def __init__(self, told):
        self.told = told

    def item(self, _item_id):
        return {"ProviderIds": {"Tmdb": "321"}}

    def paths(self, ids):
        return {i: f"/Movies/Not Very Good (2020)/{i}.mkv" for i in ids}

    def files_deleted(self, paths):
        self.told.extend(paths)


def test_deleting_an_already_deleted_film_again_isnt_an_error(app, client, monkeypatch):
    # Radarr deleted the file, but Jellyfin still lists the film until it
    # notices; deleting it there again used to fail ("Radarr doesn't have a
    # file") and Jellyfin showed an error.
    media_id = _setup(app, monkeypatch)
    told = []
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _kind: _Jellyfin(told))
    with app.app_context():
        from curatarr.models import MediaIdentity

        for part in db.session.get(MediaIdentity, media_id).parts:
            part.has_file = False
        db.session.commit()
    body = _post(client, {"itemId": ITEM, "userName": "Sam", "mode": "now"}).get_json()
    assert body["handled"] and body["result"] == "already"
    assert "refused" not in body
    assert told == ["/Movies/Not Very Good (2020)"]


def test_jellyfin_is_told_when_curatarr_deletes_a_film(app, monkeypatch):
    from curatarr.lifecycle import execute_approved, review_candidate
    from curatarr.lifecycle import request_removal as remove

    told = []

    class Radarr:
        def queue(self):
            return {"records": []}

        def request(self, _method, _path):
            return {"tmdbId": 321, "sizeOnDisk": 100}

        def list_movies(self):
            return []

        def delete_movie(self, _movie_id, exclude=False):
            pass

    monkeypatch.setattr(
        "curatarr.lifecycle.client",
        lambda kind: _Jellyfin(told) if kind == "jellyfin" else Radarr(),
    )
    with app.app_context():
        media = _film()
        candidate = remove(media, where="a test")
        review_candidate(candidate.id, "delete")
        assert execute_approved(candidate.id) == "succeeded"
    # The film's folder, as Jellyfin sees it: Radarr deleted the whole folder.
    assert told == ["/Movies/Not Very Good (2020)"]


def test_a_deletion_is_carried_out_once_however_many_try(app):
    from curatarr import leases
    from curatarr.lifecycle import execute_approved

    owner = leases.acquire("execute:some-candidate", 60)
    assert owner
    assert execute_approved("some-candidate") == "busy"
    leases.release("execute:some-candidate", owner)
