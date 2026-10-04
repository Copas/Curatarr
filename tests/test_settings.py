from curatarr import db
from curatarr.integrations import IntegrationError
from curatarr.models import Integration
from curatarr.services import setting


def test_first_run_jellyfin_row_without_key_asks_for_one(app, client):
    # Jellyfin sign-in creates the integration before any API key is saved.
    db.session.add(Integration(kind="jellyfin", base_url="http://jellyfin.local"))
    db.session.commit()
    response = client.post(
        "/settings",
        data={"kind": "jellyfin", "base_url": "http://jellyfin.local", "api_key": ""},
        follow_redirects=True,
    )
    assert b"API key is required" in response.data
    assert b"cannot be decrypted" not in response.data


def test_unreachable_integration_is_not_saved(app, client, monkeypatch):
    class Down:
        def __init__(self, *_args):
            pass

        def version(self):
            raise IntegrationError("down")

    monkeypatch.setitem(
        __import__("curatarr.services").services.CLIENTS, "radarr", Down
    )
    response = client.post(
        "/settings",
        data={"kind": "radarr", "base_url": "http://radarr.local", "api_key": "k"},
        follow_redirects=True,
    )
    assert b"Radarr could not be reached; settings were not saved" in response.data
    assert db.session.query(Integration).count() == 0


def test_reconcile_interval_is_bounded(app, client):
    for value in ("4", "1441", "soon"):
        response = client.post(
            "/settings",
            data={"kind": "scheduler", "interval_minutes": value},
            follow_redirects=True,
        )
        assert "Reconciliation interval must be 5–1440 minutes" in response.text
    client.post("/settings", data={"kind": "scheduler", "interval_minutes": "30"})
    assert setting("reconciliation_interval_seconds") == 1800


def test_webhook_token_rotation_shows_new_token_once(app, client):
    first = client.get("/settings")
    assert b"Copy this webhook token now" in first.data
    assert b"Copy this webhook token now" not in client.get("/settings").data
    response = client.post("/settings", data={"kind": "webhook", "webhook_token": ""})
    assert response.status_code == 200
    assert setting("webhook_token").encode() in response.data
