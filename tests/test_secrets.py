import pytest

from curatarr import db, routes, services
from curatarr.integrations import IntegrationError
from curatarr.models import AppSetting, Integration
from curatarr.secrets import (
    PREFIX,
    decrypt_secret,
    encrypt_legacy_secrets,
    encrypt_secret,
)
from curatarr.services import client as integration_client
from curatarr.services import set_setting, setting


def test_webhook_token_is_encrypted_and_key_mismatch_fails(app):
    with app.app_context():
        set_setting("webhook_token", "webhook-private-token")
        row = db.session.query(AppSetting).filter_by(key="webhook_token").one()
        assert row.is_secret
        assert row.value_json.startswith(PREFIX)
        assert "webhook-private-token" not in row.value_json
        assert setting("webhook_token") == "webhook-private-token"
        app.config["SECRET_KEY"] = "different-key"
        with pytest.raises(ValueError, match="cannot be decrypted"):
            setting("webhook_token")


def test_saved_integration_key_is_encrypted_and_not_rendered(app, client, monkeypatch):
    class FakeClient:
        def __init__(self, _url, key):
            assert key == "private-api-key"

        def version(self):
            return "test-version"

    monkeypatch.setitem(routes.CLIENTS, "radarr", FakeClient)
    response = client.post(
        "/settings",
        data={
            "kind": "radarr",
            "base_url": "http://radarr.example.test",
            "api_key": "private-api-key",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        row = db.session.query(Integration).filter_by(kind="radarr").one()
        assert row.secret_ref.startswith(PREFIX)
        assert "private-api-key" not in row.secret_ref
        assert decrypt_secret(row.secret_ref) == "private-api-key"
    assert b"private-api-key" not in client.get("/settings").data
    assert b"private-api-key" not in client.get("/api/v1/status").data


def test_plaintext_upgrade_is_idempotent_and_client_requires_encryption(
    app, monkeypatch
):
    class FakeClient:
        def __init__(self, _url, key):
            assert key == "old-api-key"

    monkeypatch.setitem(services.CLIENTS, "sonarr", FakeClient)
    with app.app_context():
        db.session.add(
            Integration(
                kind="sonarr",
                base_url="http://sonarr.example.test",
                secret_ref="old-api-key",
            )
        )
        db.session.add(AppSetting(key="webhook_token", value_json="old-webhook-token"))
        db.session.commit()
        with pytest.raises(IntegrationError, match="credential is unavailable"):
            integration_client("sonarr")
        assert encrypt_legacy_secrets() == 2
        assert encrypt_legacy_secrets() == 0
        assert isinstance(integration_client("sonarr"), FakeClient)
        assert setting("webhook_token") == "old-webhook-token"
        assert db.session.query(Integration).one().secret_ref.startswith(PREFIX)


def test_secret_cipher_round_trip(app):
    with app.app_context():
        encrypted = encrypt_secret("sample")
        assert encrypted != "sample"
        assert decrypt_secret(encrypted) == "sample"
