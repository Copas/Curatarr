"""Install and set up the Curatarr plugin for Jellyfin from Settings.

Jellyfin installs plugins from repositories: Curatarr adds the plugin's (a
manifest published from this repository's jellyfin-plugin/ releases),
installs the plugin from it, and once Jellyfin has restarted and loaded it,
gives it Curatarr's address and the webhook token, so nothing is copied by
hand. The settings are given again whenever they differ (a new token, a new
address), on the settings page and at each reconciliation.
"""

from . import db
from .integrations import IntegrationError

NAME = "Curatarr"
PLUGIN_ID = "42d6dc0d-a758-4b43-ab9c-303c6727aada"
REPOSITORY = {
    "Name": "Curatarr",
    "Url": "https://raw.githubusercontent.com/Copas/Curatarr/main/jellyfin-plugin/manifest.json",
    "Enabled": True,
}
KEY = "jellyfin_plugin"  # {"curatarr_url": what the plugin is given}


def _norm(value) -> str:
    return str(value or "").replace("-", "").lower()


def _plugin(jellyfin) -> dict | None:
    for plugin in jellyfin.plugins():
        if _norm(plugin.get("Id")) == _norm(PLUGIN_ID):
            return plugin
    return None


def _wanted() -> dict | None:
    from .services import setting

    url = (setting(KEY, {}) or {}).get("curatarr_url")
    token = setting("webhook_token")
    if not url or not token:
        return None
    return {"CuratarrUrl": url, "Token": token}


def status() -> dict:
    """For Settings: state is not_installed, restart (installed, Jellyfin
    must restart to load it), active, disabled or unreachable."""
    from .services import client, setting

    try:
        jellyfin = client("jellyfin")
        plugin = _plugin(jellyfin)
    except IntegrationError:
        return {"state": "unreachable"}
    stored = setting(KEY, {}) or {}
    if plugin is None:
        return {"state": "not_installed", "curatarr_url": stored.get("curatarr_url")}
    state = {"Active": "active", "Restart": "restart"}.get(
        plugin.get("Status"), "disabled"
    )
    result = {
        "state": state,
        "version": plugin.get("Version"),
        "curatarr_url": stored.get("curatarr_url"),
    }
    if state == "active":
        result["configured"] = configure(jellyfin)
    return result


def install(curatarr_url: str) -> None:
    """Add the repository (once) and install the plugin; raises
    IntegrationError or ValueError with a message for the page."""
    from .services import client

    set_address(curatarr_url)
    jellyfin = client("jellyfin")
    repositories = jellyfin.repositories()
    if not any(r.get("Url") == REPOSITORY["Url"] for r in repositories):
        jellyfin.set_repositories([*repositories, REPOSITORY])
    try:
        jellyfin.install_package(NAME, PLUGIN_ID, REPOSITORY["Url"])
    except IntegrationError as exc:
        if exc.status_code == 404:
            raise ValueError(
                "Jellyfin couldn't find the plugin in its repository yet. "
                "Try again in a few minutes."
            ) from exc
        raise


def set_address(curatarr_url: str) -> None:
    from .services import set_setting

    url = (curatarr_url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError("Enter Curatarr's address, starting with http:// or https://.")
    set_setting(KEY, {"curatarr_url": url})
    db.session.commit()


def configure(jellyfin=None) -> bool:
    """Give the loaded plugin Curatarr's address and token when it lacks
    them; returns whether it has them now. Never raises."""
    from .services import client

    wanted = _wanted()
    if wanted is None:
        return False
    try:
        jellyfin = jellyfin or client("jellyfin")
        current = jellyfin.plugin_configuration(PLUGIN_ID)
        if all(current.get(key) == value for key, value in wanted.items()):
            return True
        jellyfin.set_plugin_configuration(PLUGIN_ID, current | wanted)
        return True
    except IntegrationError:
        return False


def sync() -> None:
    """At each reconciliation: an installed, loaded plugin gets the settings
    again if they changed."""
    from .services import client

    if _wanted() is None:
        return
    try:
        jellyfin = client("jellyfin")
        plugin = _plugin(jellyfin)
    except IntegrationError:
        return
    if plugin and plugin.get("Status") == "Active":
        configure(jellyfin)


def watching() -> int:
    """How many people are playing something (a restart stops them)."""
    from .services import client

    try:
        sessions = client("jellyfin").sessions() or []
    except IntegrationError:
        return 0
    return sum(1 for s in sessions if s.get("NowPlayingItem"))


def restart() -> None:
    from .services import client

    client("jellyfin").restart()
