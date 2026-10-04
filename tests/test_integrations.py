import pytest
import requests

from curatarr.integrations import IntegrationError, RadarrClient, normalized_url


class FakeResponse:
    content = b"{}"

    def __init__(self):
        self.headers = {"Content-Type": "application/json"}

    def raise_for_status(self):
        return None

    def json(self):
        return {}


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeResponse()


def test_radarr_deletion_has_no_import_exclusion():
    session = FakeSession()
    RadarrClient("http://radarr.example.local:7878/", "dummy", session).delete_movie(42)
    args, kwargs = session.calls[0]
    assert args == ("DELETE", "http://radarr.example.local:7878/api/v3/movie/42")
    assert kwargs["params"] == {"deleteFiles": "true", "addImportExclusion": "false"}
    assert kwargs["timeout"] == (5, 60)


def test_client_errors_are_sanitized():
    class FailingSession:
        calls = 0

        def request(self, *_args, **_kwargs):
            self.calls += 1
            raise requests.Timeout("secret-token-in-url")

    session = FailingSession()
    client = RadarrClient("http://radarr.example.local", "dummy", session)
    with pytest.raises(IntegrationError) as exc:
        client.health()
    assert "secret-token" not in str(exc.value)
    assert session.calls == 3
    with pytest.raises(IntegrationError):
        client.delete_movie(42)
    assert session.calls == 4
    with pytest.raises(ValueError):
        normalized_url("http://user:pass@radarr.example.local")


def test_jellyfin_api_key_uses_mediabrowser_authorization():
    from curatarr.integrations import JellyfinClient

    session = FakeSession()
    JellyfinClient("http://jellyfin.example.local:8096", "dummy", session).users()
    headers = session.calls[0][1]["headers"]
    assert 'Token="dummy"' in headers["Authorization"]
    assert headers["Authorization"].startswith("MediaBrowser ")
    assert "X-Emby-Token" not in headers
    session = FakeSession()
    JellyfinClient("http://jellyfin.example.local:8096", "", session).request(
        "GET", "/System/Info/Public"
    )
    assert "Authorization" not in session.calls[0][1]["headers"]
