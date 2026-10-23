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
    # Deleting it again changes nothing.
    again = _post(client, {"itemId": ITEM, "userName": "Sam"}).get_json()
    assert again["handled"] and again["result"] == "already"


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
