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


def test_settings_explain_where_each_api_key_is(app, client):
    page = client.get("/settings").text
    assert "Sonarr Settings → General → Security → API Key" in page
    assert "Radarr Settings → General → Security → API Key" in page
    assert "Jellyfin Dashboard → API Keys" in page
    assert 'placeholder="http://host:8989"' in page
    db.session.add(
        Integration(kind="jellyfin", base_url="http://media.example.local:8096")
    )
    db.session.commit()
    page = client.get("/settings").text
    # Sonarr and Radarr suggestions reuse the Jellyfin host with default ports.
    assert 'placeholder="http://media.example.local:7878"' in page
    assert 'href="http://media.example.local:8989/settings/general"' in page
    assert 'href="http://media.example.local:8096/web/#/dashboard/keys"' in page


def test_settings_show_webhook_setup_with_template(app, client):
    import json

    from curatarr.services import JELLYFIN_WEBHOOK_TEMPLATE, normalize_event

    page = client.get("/settings").text
    assert "Add Generic Destination" in page
    assert "http://localhost/api/v1/webhook/jellyfin" in page
    assert "{{NotificationType}}" in page
    # The template renders valid JSON that Curatarr accepts, even for a movie.
    rendered = JELLYFIN_WEBHOOK_TEMPLATE
    for name, value in {
        "NotificationType": "PlaybackStop",
        "ItemId": "0" * 32,
        "PlayedToCompletion": "True",
    }.items():
        rendered = rendered.replace("{{" + name + "}}", value)
    rendered = __import__("re").sub(r"\{\{\w+\}\}", "", rendered)
    assert normalize_event(json.loads(rendered))["event_type"] == "item_played"
