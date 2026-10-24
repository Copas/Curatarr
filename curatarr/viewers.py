"""Who watches in a web browser.

Each time Curatarr looks at what's playing (every few seconds while there's
work), it notes, per person, whether they're watching in Jellyfin's web app or
another app. Over the last 30 days, someone who mostly watches in a browser
counts as a browser viewer: their next episodes get a stereo AAC track
(compat_audio), which browsers play as it is. Having signed in on a browser
once doesn't count: Jellyfin's device list would make almost everyone one.
"""

from datetime import timedelta

from .models import utcnow

KEY = "viewing_apps"
DAYS = 30
MIN_SAMPLES = 20  # a few minutes of watching in a browser


def _norm(user_id) -> str:
    return str(user_id or "").replace("-", "").lower()


def note(sessions, now=None) -> None:
    """Count who's watching in which kind of app, per day."""
    from . import db
    from .services import set_setting, setting

    playing = [s for s in sessions or [] if s.get("NowPlayingItem") and s.get("UserId")]
    if not playing:
        return
    now = now or utcnow()
    today = now.date().isoformat()
    oldest = (now - timedelta(days=DAYS)).date().isoformat()
    stored = dict(setting(KEY) or {})
    for session in playing:
        user = _norm(session["UserId"])
        days = {d: c for d, c in (stored.get(user) or {}).items() if d >= oldest}
        web, other = days.get(today, [0, 0])
        if "web" in str(session.get("Client") or "").lower():
            web += 1
        else:
            other += 1
        days[today] = [web, other]
        stored[user] = days
    set_setting(KEY, stored)
    db.session.commit()


def browser_viewers(now=None) -> set[str]:
    """People who've mostly watched in a browser over the last 30 days."""
    from .services import setting

    now = now or utcnow()
    oldest = (now - timedelta(days=DAYS)).date().isoformat()
    viewers = set()
    for user, days in (setting(KEY) or {}).items():
        web = sum(c[0] for d, c in days.items() if d >= oldest)
        other = sum(c[1] for d, c in days.items() if d >= oldest)
        if web >= MIN_SAMPLES and web >= other:
            viewers.add(user)
    return viewers
