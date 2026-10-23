"""Install and set up the Curatarr plugin for Jellyfin from Settings.

One button does it: Curatarr adds the plugin's repository to Jellyfin (a
manifest published from this repository's jellyfin-plugin/ releases, which
also brings later versions as Jellyfin plugin updates), installs the plugin
and restarts Jellyfin to load it. While that's pending the worker looks every
cycle; once the plugin is loaded, Curatarr finds an address Jellyfin can
reach it at (the plugin's own Test, tried with the address the button was
pressed from and the usual local ones) and gives the plugin that address and
the webhook token. Nothing is typed or copied. They're given again whenever
they differ (a new token), at each reconciliation.
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
# {"curatarr_url": the address that worked, "candidates": addresses to try,
#  "pending": set up was asked for and isn't finished, "active": last seen}
KEY = "jellyfin_plugin"


def _norm(value) -> str:
    return str(value or "").replace("-", "").lower()


def _stored() -> dict:
    from .services import setting

    return dict(setting(KEY, {}) or {})


def _store(**changes) -> None:
    from .services import set_setting

    stored = _stored()
    if any(stored.get(key) != value for key, value in changes.items()):
        set_setting(KEY, stored | changes)
        db.session.commit()


def _plugin(jellyfin) -> dict | None:
    for plugin in jellyfin.plugins():
        if _norm(plugin.get("Id")) == _norm(PLUGIN_ID):
            return plugin
    return None


def _clean(url) -> str | None:
    url = str(url or "").strip().rstrip("/")
    return url if url.startswith(("http://", "https://")) else None


def active() -> bool:
    """For the setup checklist, which shouldn't ask Jellyfin on every load."""
    return bool(_stored().get("active"))


def status() -> dict:
    """For Settings. state: not_installed, restart (installed, not loaded
    yet), active, disabled or unreachable; with pending, configured and the
    address in use."""
    from .services import client

    stored = _stored()
    base = {
        "pending": bool(stored.get("pending")),
        "curatarr_url": stored.get("curatarr_url"),
        "problem": stored.get("problem"),
    }
    try:
        jellyfin = client("jellyfin")
        plugin = _plugin(jellyfin)
    except IntegrationError:
        return base | {"state": "unreachable"}
    if plugin is None:
        _store(active=False)
        return base | {"state": "not_installed"}
    state = {"Active": "active", "Restart": "restart"}.get(
        plugin.get("Status"), "disabled"
    )
    _store(active=state == "active")
    result = base | {"state": state, "version": plugin.get("Version")}
    if state == "active":
        result["configured"] = configure(jellyfin)
        result |= {
            k: v for k, v in _stored().items() if k in ("curatarr_url", "problem")
        }
        result["pending"] = bool(_stored().get("pending"))
    return result


def set_up(candidates, *, restart: bool = True) -> str:
    """The button: whatever's missing, in order. Returns what happened, for
    the page; raises IntegrationError or ValueError."""
    from .services import client

    jellyfin = client("jellyfin")
    stored = _stored()
    tried = [_clean(url) for url in [stored.get("curatarr_url"), *candidates]]
    _store(candidates=list(dict.fromkeys(url for url in tried if url)), problem=None)
    repositories = jellyfin.repositories()
    if not any(r.get("Url") == REPOSITORY["Url"] for r in repositories):
        jellyfin.set_repositories([*repositories, REPOSITORY])
    plugin = _plugin(jellyfin)
    if plugin and plugin.get("Status") == "Active":
        if configure(jellyfin):
            return "connected"
        return "unconnected"
    if plugin is None:
        try:
            jellyfin.install_package(NAME, PLUGIN_ID, REPOSITORY["Url"])
        except IntegrationError as exc:
            if exc.status_code == 404:
                raise ValueError(
                    "Jellyfin couldn't find the plugin in its repository yet. "
                    "Try again in a few minutes."
                ) from exc
            raise
    _store(pending=True)
    if restart:
        jellyfin.restart()
        return "restarting"
    return "installed"


def set_address(url) -> None:
    """An address typed by hand: checked through the plugin when it's loaded."""
    clean = _clean(url)
    if clean is None:
        raise ValueError("Enter Curatarr's address, starting with http:// or https://.")
    _store(candidates=[clean], curatarr_url=None, problem=None)
    if not configure():
        raise ValueError(
            _stored().get("problem") or "Jellyfin couldn't reach Curatarr there."
        )


def _find_address(jellyfin, token) -> str | None:
    """The first address the plugin can reach Curatarr at, through its own
    Test (so it's checked from Jellyfin's side)."""
    problem = None
    for url in _stored().get("candidates") or []:
        try:
            answer = jellyfin.test_plugin(url, token)
        except IntegrationError:
            continue
        if answer.get("Ok"):
            _store(curatarr_url=url, problem=None)
            return url
        problem = problem or f"{url}: {answer.get('Message')}"
    _store(problem=problem or "No address to try: press Connect on this page.")
    return None


def configure(jellyfin=None) -> bool:
    """Give the loaded plugin Curatarr's address and the webhook token when
    it lacks them; returns whether it has them now. Never raises."""
    from .services import client, setting

    token = setting("webhook_token")
    if not token:
        return False
    try:
        jellyfin = jellyfin or client("jellyfin")
        url = _stored().get("curatarr_url") or _find_address(jellyfin, token)
        if url is None:
            return False
        current = jellyfin.plugin_configuration(PLUGIN_ID)
        wanted = {"CuratarrUrl": url, "Token": token}
        if any(current.get(key) != value for key, value in wanted.items()):
            jellyfin.set_plugin_configuration(PLUGIN_ID, current | wanted)
        _store(pending=False)
        return True
    except IntegrationError:
        return False


def tick() -> None:
    """Every worker cycle, while a set up is pending: once Jellyfin is back
    with the plugin loaded, set it up."""
    from .services import client

    if not _stored().get("pending"):
        return
    try:
        plugin = _plugin(client("jellyfin"))
    except IntegrationError:
        return  # still restarting
    if plugin and plugin.get("Status") == "Active":
        _store(active=True)
        configure()


def sync() -> None:
    """At each reconciliation: note whether the plugin is loaded, keep its
    repository listed (for updates), and give it the settings again if they
    changed."""
    from .services import client

    try:
        jellyfin = client("jellyfin")
        plugin = _plugin(jellyfin)
        loaded = bool(plugin and plugin.get("Status") == "Active")
        _store(active=loaded)
        if not loaded:
            return
        repositories = jellyfin.repositories()
        if not any(r.get("Url") == REPOSITORY["Url"] for r in repositories):
            jellyfin.set_repositories([*repositories, REPOSITORY])
    except IntegrationError:
        return
    if _stored().get("curatarr_url") or _stored().get("candidates"):
        configure(jellyfin)


def watching() -> int:
    """How many people are playing something (a restart stops them)."""
    from .services import client

    try:
        sessions = client("jellyfin").sessions() or []
    except IntegrationError:
        return 0
    return sum(1 for s in sessions if s.get("NowPlayingItem"))
