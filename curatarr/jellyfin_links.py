"""Links from Curatarr pages to the same item in Jellyfin's web app."""

from urllib.parse import quote, urlsplit, urlunsplit

from flask import g, request
from markupsafe import Markup, escape

from . import db
from .models import Integration, MediaIdentity
from .services import setting

LOOPBACK = {"127.0.0.1", "localhost", "::1"}

# A play-in-a-rounded-square mark; Jellyfin's own logo is not ours to reuse.
ICON = (
    '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linejoin="round" '
    'aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="4"/>'
    '<path d="M10 8.5v7l5.5-3.5z" fill="currentColor"/></svg>'
)


def web_base() -> str | None:
    """Jellyfin's address as this browser can reach it.

    Curatarr usually talks to Jellyfin over loopback on the same host. A
    browser elsewhere cannot use 127.0.0.1, so the host part is replaced by the
    one this page was requested on, keeping Jellyfin's port and scheme.
    """
    if "jellyfin_web_base" in g:
        return g.jellyfin_web_base
    base = None
    row = db.session.query(Integration).filter_by(kind="jellyfin").first()
    if row and row.base_url:
        parts = urlsplit(row.base_url)
        host = parts.hostname or ""
        if host in LOOPBACK:
            visible = urlsplit(f"//{request.host}").hostname or host
            if ":" in visible:
                visible = f"[{visible}]"
            netloc = visible + (f":{parts.port}" if parts.port else "")
            parts = parts._replace(netloc=netloc)
        base = urlunsplit(parts._replace(query="", fragment="")).rstrip("/")
    g.jellyfin_web_base = base
    return base


def item_url(item_id) -> str | None:
    base = web_base()
    if not base or not item_id:
        return None
    url = f"{base}/web/#/details?id={quote(str(item_id))}"
    server = setting("jellyfin_server_id")
    if server:
        url += f"&serverId={quote(str(server))}"
    return url


def link(item_id, label="") -> Markup:
    """A small icon link to the item in Jellyfin (empty when unknown)."""
    url = item_url(item_id)
    if not url:
        return Markup("")
    name = f"Open {label} in Jellyfin" if label else "Open in Jellyfin"
    return Markup(
        f'<a class="jf-link" href="{escape(url)}" target="_blank" rel="noopener" '
        f'title="{escape(name)}" aria-label="{escape(name)}">{ICON}</a>'
    )


def link_for(media, label=None) -> Markup:
    """Link for a MediaIdentity or its id."""
    if media is None:
        return Markup("")
    if isinstance(media, str):
        media = db.session.get(MediaIdentity, media)
        if media is None:
            return Markup("")
    return link(media.jellyfin_id, label or media.title)


def part_link(part, media=None) -> Markup:
    """An episode links to the episode; a movie file to the movie."""
    media = media or part.media
    if part.kind == "episode" and part.jellyfin_id:
        label = (
            f"{media.title} S{part.season_number or 0:02}E{part.episode_number or 0:02}"
        )
        return link(part.jellyfin_id, label)
    return link_for(media)
