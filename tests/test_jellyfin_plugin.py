"""Installing and setting up the Curatarr plugin for Jellyfin from Settings."""

import pytest

from curatarr import db, jellyfin_plugin
from curatarr.commands import worker_cycle
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
        self.reachable = {"http://127.0.0.1:8787"}  # where Jellyfin reaches Curatarr
        self.tested = []

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

    def test_plugin(self, url, token):
        self.tested.append(url)
        ok = url in self.reachable and token == setting("webhook_token")
        return {"Ok": ok, "Message": "ok" if ok else "couldn't be reached"}

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


ADDRESSES = ["http://curatarr.example", "http://127.0.0.1:8787"]


def test_one_click_installs_restarts_and_then_sets_it_up(app, jellyfin):
    with app.app_context():
        assert jellyfin_plugin.set_up(ADDRESSES) == "restarting"
        assert jellyfin.installed[0][:2] == ("Curatarr", jellyfin_plugin.PLUGIN_ID)
        assert jellyfin.restarts == 1
        assert [r["Url"] for r in jellyfin.repos] == [
            "https://repo",
            jellyfin_plugin.REPOSITORY["Url"],
        ]
        jellyfin_plugin.tick()  # Jellyfin hasn't loaded it yet
        assert jellyfin.saves == 0
        jellyfin.status = "Active"  # back from the restart
        jellyfin_plugin.tick()
        # The first address Jellyfin could reach is the one given.
        assert jellyfin.tested == ADDRESSES
        assert jellyfin.config == {
            "CuratarrUrl": "http://127.0.0.1:8787",
            "Token": "the-token",
            "InterceptDeletes": True,  # the plugin's own settings are kept
        }
        assert not setting(jellyfin_plugin.KEY)["pending"]
        assert jellyfin_plugin.active()


def test_a_hand_installed_plugin_is_connected_without_a_restart(app, jellyfin):
    jellyfin.status = "Active"
    with app.app_context():
        assert jellyfin_plugin.set_up(ADDRESSES) == "connected"
        assert jellyfin.restarts == 0 and not jellyfin.installed
        assert jellyfin.config["CuratarrUrl"] == "http://127.0.0.1:8787"
        # Its repository is added, so Jellyfin offers updates.
        assert jellyfin_plugin.REPOSITORY["Url"] in [r["Url"] for r in jellyfin.repos]


def test_when_no_address_works_it_says_so_and_takes_one_by_hand(app, jellyfin):
    jellyfin.status = "Active"
    jellyfin.reachable = set()
    with app.app_context():
        assert jellyfin_plugin.set_up(ADDRESSES) == "unconnected"
        assert "couldn't be reached" in jellyfin_plugin.status()["problem"]
        with pytest.raises(ValueError):
            jellyfin_plugin.set_address("http://nas:8787")
        jellyfin.reachable = {"http://nas:8787"}
        jellyfin_plugin.set_address("http://nas:8787/")
        assert jellyfin.config["CuratarrUrl"] == "http://nas:8787"
        with pytest.raises(ValueError):
            jellyfin_plugin.set_address("nas:8787")


def test_a_new_token_reaches_the_plugin(app, jellyfin):
    jellyfin.status = "Active"
    with app.app_context():
        jellyfin_plugin.set_up(ADDRESSES)
        saves = jellyfin.saves
        jellyfin_plugin.sync()
        assert jellyfin.saves == saves  # unchanged: not saved again
        set_setting("webhook_token", "a-new-token")
        db.session.commit()
        jellyfin_plugin.sync()
        assert jellyfin.config["Token"] == "a-new-token"


def test_a_release_jellyfin_cant_find_yet_says_so(app, jellyfin):
    jellyfin.missing = True
    with app.app_context(), pytest.raises(ValueError, match="couldn't find"):
        jellyfin_plugin.set_up(ADDRESSES)


def test_settings_page_walks_through_it(app, client, jellyfin):
    page = client.get("/settings").text
    assert "Install in Jellyfin" in page and "1 person is watching" in page
    client.post("/settings", data={"kind": "plugin_setup", "restart": "1"})
    assert jellyfin.restarts == 1
    assert "Setting up…" in client.get("/settings").text
    jellyfin.status = "Active"
    with app.app_context():
        worker_cycle()  # the worker finishes once Jellyfin is back
    page = client.get("/settings").text
    assert "deletes in Jellyfin go through Curatarr at <code>http://127.0.0.1:" in page


def test_the_setup_checklist_offers_the_plugin(app, client, jellyfin):
    page = client.get("/setup").text
    assert "Install the Curatarr plugin for Jellyfin" in page
    assert "/settings#jellyfin-plugin" in page
    jellyfin.status = "Active"
    with app.app_context():
        jellyfin_plugin.sync()
    page = client.get("/setup").text
    item = page[page.index("Install the Curatarr plugin") :]
    assert "Done" in item[: item.index("</li>")]


def test_unreachable_jellyfin_shows_a_note(app, monkeypatch):
    def down(_kind):
        raise IntegrationError("down")

    monkeypatch.setattr("curatarr.services.client", down)
    with app.app_context():
        assert jellyfin_plugin.status()["state"] == "unreachable"
