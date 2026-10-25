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
#  "pending": set up was asked for and isn't finished, "active": last seen,
#  "version": the version loaded, "waiting": a newer one installed that loads
#  at Jellyfin's next restart, "latest": the newest in Jellyfin's catalogue,
#  "restart_when_idle": restart Jellyfin once nobody's watching,
#  "idle_since": when playback was last seen to stop}
KEY = "jellyfin_plugin"
# The plugin version this Curatarr's features are made for.
WANTED = "0.7.0.0"
# A restart when nobody's watching waits this long after the last playback,
# so someone between episodes isn't cut off.
IDLE_MINUTES = 10
# Jellyfin's states for an installed version: Active is loaded; Superseded is
# loaded with a newer version waiting (Restart) for Jellyfin's next restart.
LOADED = {"Active", "Superseded"}


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


def _versions(jellyfin) -> list[dict]:
    return [p for p in jellyfin.plugins() if _norm(p.get("Id")) == _norm(PLUGIN_ID)]


def _plugin(jellyfin) -> dict | None:
    """The version Jellyfin has loaded, else the one it will load."""
    order = {"Active": 0, "Superseded": 1, "Restart": 2}
    versions = sorted(_versions(jellyfin), key=lambda p: order.get(p.get("Status"), 3))
    return versions[0] if versions else None


def _loaded(plugin) -> bool:
    return bool(plugin) and plugin.get("Status") in LOADED


def version_key(version) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(version or "").split("."))
    except ValueError:
        return ()


def _check_versions(jellyfin) -> None:
    """Note the version loaded, one waiting for a restart, and the newest in
    the catalogue (for the update banner). Never raises."""
    try:
        versions = _versions(jellyfin)
    except IntegrationError:
        return
    loaded = next((p for p in versions if p.get("Status") in LOADED), None)
    waiting = [p.get("Version") for p in versions if p.get("Status") == "Restart"]
    latest = None
    try:
        for package in jellyfin.packages():
            if _norm(package.get("guid")) == _norm(PLUGIN_ID):
                found = [v.get("version") for v in package.get("versions") or []]
                latest = max(found, key=version_key, default=None)
    except IntegrationError:
        latest = _stored().get("latest")
    _store(
        version=loaded.get("Version") if loaded else None,
        waiting=max(waiting, key=version_key) if waiting else None,
        latest=latest,
    )


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
    state = (
        "active"
        if _loaded(plugin)
        else "restart"
        if plugin.get("Status") == "Restart"
        else "disabled"
    )
    _store(active=state == "active")
    _check_versions(jellyfin)
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
    if _loaded(plugin):
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
    """Every worker cycle: once Jellyfin is back with the plugin loaded after
    a set up, set it up; and restart Jellyfin for a waiting update once
    nobody's watching, when asked to."""
    from .services import client

    if _stored().get("restart_when_idle"):
        _restart_if_idle()
    if not _stored().get("pending"):
        return
    try:
        plugin = _plugin(client("jellyfin"))
    except IntegrationError:
        return  # still restarting
    if _loaded(plugin):
        _store(active=True)
        configure()


def _restart_if_idle() -> None:
    from datetime import timedelta

    from .models import utcnow
    from .services import _as_datetime, client

    try:
        jellyfin = client("jellyfin")
        if not any(p.get("Status") == "Restart" for p in _versions(jellyfin)):
            _store(restart_when_idle=False, idle_since=None)  # restarted already
            _check_versions(jellyfin)
            return
        if any(s.get("NowPlayingItem") for s in jellyfin.sessions() or []):
            _store(idle_since=None)
            return
        since = _as_datetime(_stored().get("idle_since"))
        if since is None:
            _store(idle_since=utcnow().isoformat())
            return
        if utcnow() - since < timedelta(minutes=IDLE_MINUTES):
            return
        jellyfin.restart()
    except IntegrationError:
        return  # restarting, or away
    _store(restart_when_idle=False, idle_since=None, pending=True)


def update() -> None:
    """Install the newest version from the catalogue (it loads when Jellyfin
    next restarts). Raises IntegrationError."""
    from .services import client

    jellyfin = client("jellyfin")
    jellyfin.install_package(NAME, PLUGIN_ID, REPOSITORY["Url"])
    _check_versions(jellyfin)


def restart(when_idle: bool) -> None:
    """Restart Jellyfin to load a waiting update: now, or once nobody's
    watching (the worker looks every cycle)."""
    from .services import client

    if when_idle:
        _store(restart_when_idle=True, idle_since=None)
        return
    client("jellyfin").restart()
    _store(restart_when_idle=False, pending=True)


def cancel_idle_restart() -> None:
    _store(restart_when_idle=False, idle_since=None)


def notice() -> dict | None:
    """For the banner: a newer version to install, or one installed and
    waiting for Jellyfin to restart. From what was last noted (no call to
    Jellyfin)."""
    stored = _stored()
    version = stored.get("version")
    if not version:
        return None
    waiting, latest = stored.get("waiting"), stored.get("latest")
    have = max(version, waiting or version, key=version_key)
    base = {
        "version": version,
        # Some of this Curatarr's features need it.
        "wanted": version_key(version) < version_key(WANTED),
    }
    if latest and version_key(latest) > version_key(have):
        return base | {"kind": "update", "new": latest}
    if waiting and version_key(waiting) > version_key(version):
        return base | {
            "kind": "restart",
            "new": waiting,
            "when_idle": bool(stored.get("restart_when_idle")),
        }
    return None


def sync() -> None:
    """At each reconciliation: note whether the plugin is loaded, keep its
    repository listed (for updates), and give it the settings again if they
    changed."""
    from .services import client

    try:
        jellyfin = client("jellyfin")
        plugin = _plugin(jellyfin)
        loaded = _loaded(plugin)
        _store(active=loaded)
        _check_versions(jellyfin)
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
