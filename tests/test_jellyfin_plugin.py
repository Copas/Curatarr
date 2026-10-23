"""Installing and setting up the Curatarr plugin for Jellyfin from Settings."""

import pytest

from curatarr import db, jellyfin_plugin
from curatarr.integrations import IntegrationError
from curatarr.services import set_setting, setting

PLUGIN = {"Id": jellyfin_plugin.PLUGIN_ID.replace("-", ""), "Version": "0.1.0.0"}


class Jellyfin:
    def __init__(self, status=None):
        self.repos = [
            {"Name": "Jellyfin Stable", "Url": "https://repo", "Enabled": True}
        ]
        self.installed = []
        self.status = status  # the plugin's Status once installed, or None
        self.config = {"CuratarrUrl": "", "Token": "", "InterceptDeletes": True}
        self.saves = 0
        self.restarts = 0
        self.missing = False

    def plugins(self):
        others = [{"Id": "webhook", "Status": "Active"}]
        return others + ([PLUGIN | {"Status": self.status}] if self.status else [])

    def repositories(self):
        return list(self.repos)

    def set_repositories(self, repos):
        self.repos = repos

    def install_package(self, name, guid, repo):
        if self.missing:
            raise IntegrationError("not found", 404)
        self.installed.append((name, guid, repo))
        self.status = "Restart"

    def plugin_configuration(self, _id):
        return dict(self.config)

    def set_plugin_configuration(self, _id, config):
        self.saves += 1
        self.config = config

    def sessions(self):
        return [{"NowPlayingItem": {"Name": "A film"}}, {}]

    def restart(self):
        self.restarts += 1

    def users(self):
        return []


@pytest.fixture
def jellyfin(app, monkeypatch):
    fake = Jellyfin()
    monkeypatch.setattr("curatarr.services.client", lambda _kind: fake)
    with app.app_context():
        set_setting("webhook_token", "the-token")
        db.session.commit()
    return fake


def test_install_adds_the_repository_once_and_installs(app, jellyfin):
    with app.app_context():
        jellyfin_plugin.install("http://curatarr.local:8787/")
        jellyfin.status = None
        jellyfin_plugin.install("http://curatarr.local:8787")
        assert [r["Url"] for r in jellyfin.repos].count(
            jellyfin_plugin.REPOSITORY["Url"]
        ) == 1
        assert jellyfin.repos[0]["Url"] == "https://repo"  # the others are kept
        assert jellyfin.installed[0][:2] == ("Curatarr", jellyfin_plugin.PLUGIN_ID)
        assert (
            setting(jellyfin_plugin.KEY)["curatarr_url"] == "http://curatarr.local:8787"
        )
        with pytest.raises(ValueError):
            jellyfin_plugin.install("curatarr.local")


def test_a_release_jellyfin_cant_find_yet_says_so(app, jellyfin):
    jellyfin.missing = True
    with app.app_context(), pytest.raises(ValueError, match="couldn't find"):
        jellyfin_plugin.install("http://curatarr.local:8787")


def test_once_loaded_the_plugin_is_given_the_address_and_token(app, jellyfin):
    with app.app_context():
        jellyfin_plugin.install("http://curatarr.local:8787")
        assert jellyfin_plugin.status()["state"] == "restart"
        assert jellyfin.saves == 0  # not loaded yet
        jellyfin.status = "Active"
        view = jellyfin_plugin.status()
        assert view["state"] == "active" and view["configured"]
        assert jellyfin.config == {
            "CuratarrUrl": "http://curatarr.local:8787",
            "Token": "the-token",
            "InterceptDeletes": True,  # the plugin's own settings are kept
        }
        jellyfin_plugin.sync()
        assert jellyfin.saves == 1  # unchanged: not saved again
        set_setting("webhook_token", "a-new-token")
        db.session.commit()
        jellyfin_plugin.sync()
        assert jellyfin.config["Token"] == "a-new-token"


def test_settings_page_walks_through_install_and_restart(app, client, jellyfin):
    page = client.get("/settings").text
    assert "Install in Jellyfin" in page and 'value="http://localhost"' in page
    client.post(
        "/settings", data={"kind": "plugin_install", "curatarr_url": "http://c:8787"}
    )
    page = client.get("/settings").text
    assert "Restart Jellyfin" in page and "1 person is watching" in page
    client.post("/settings", data={"kind": "plugin_restart"})
    assert jellyfin.restarts == 1
    jellyfin.status = "Active"
    page = client.get("/settings").text
    assert (
        "deletes in Jellyfin go through Curatarr at <code>http://c:8787</code>" in page
    )
    client.post(
        "/settings",
        data={"kind": "plugin_address", "curatarr_url": "http://other:8787"},
    )
    assert jellyfin.config["CuratarrUrl"] == "http://other:8787"


def test_unreachable_jellyfin_shows_a_note(app, client, monkeypatch):
    def down(_kind):
        raise IntegrationError("down")

    monkeypatch.setattr("curatarr.services.client", down)
    with app.app_context():
        assert jellyfin_plugin.status() == {"state": "unreachable"}
