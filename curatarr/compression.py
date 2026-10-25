"""Compression first: hold low-space cleanup while an external transcoder
(Tdarr, Unmanic, FileFlows) still has work queued, so space is won back by
recompressing before anything is deleted.

The transcoder (or a small script beside it) says how much it has waiting,
either by calling ``POST /api/v1/compression`` with the webhook token, or by
keeping a file in place while it has work (its contents, if a number, being
how many files). A signal older than ``stale_hours`` counts as nothing
waiting, so a transcoder that stops reporting doesn't hold cleanup forever.
Below the critical threshold, cleanup goes ahead regardless.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import db
from .models import utcnow

KEY = "compression_first"  # {"enabled", "file", "stale_hours"}
SIGNAL_KEY = "compression_signal"  # {"pending", "source", "at"}
DEFAULTS = {"enabled": False, "file": "", "stale_hours": 6}


def settings() -> dict:
    from .services import setting

    return DEFAULTS | (setting(KEY) or {})


def save_settings(enabled: bool, file: str, stale_hours) -> dict:
    from .services import set_setting

    file = (file or "").strip()
    if file and not Path(file).is_absolute():
        raise ValueError("The signal file needs a full path, starting with /.")
    try:
        hours = max(1, min(int(stale_hours), 168))
    except (TypeError, ValueError):
        hours = DEFAULTS["stale_hours"]
    options = {"enabled": bool(enabled), "file": file, "stale_hours": hours}
    set_setting(KEY, options)
    db.session.commit()
    return options


def report(pending, source=None) -> dict:
    """What the transcoder has waiting: a number of files, or true/false."""
    from .services import set_setting

    if isinstance(pending, bool):
        count = int(pending)
    elif isinstance(pending, int) and pending >= 0:
        count = pending
    else:
        raise ValueError("pending must be a number of files, or true/false")
    state = {
        "pending": count,
        "source": str(source or "a transcoder")[:60],
        "at": utcnow().isoformat(),
    }
    set_setting(SIGNAL_KEY, state)
    db.session.commit()
    return state


def _aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def signal() -> dict | None:
    """The latest fresh signal ({pending, source, at}), or None."""
    from .services import _as_datetime, setting

    options = settings()
    stale = timedelta(hours=options["stale_hours"])
    now = utcnow()
    found = []
    reported = setting(SIGNAL_KEY) or {}
    at = _aware(_as_datetime(reported.get("at")))
    if at and now - at <= stale:
        found.append(reported | {"at": at})
    if options["file"]:
        path = Path(options["file"])
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            modified = None
        if modified and now - modified <= stale:
            count = int(text) if text.isdigit() else 1
            found.append({"pending": count, "source": path.name, "at": modified})
    return max(found, key=lambda s: s["at"], default=None)


def holding() -> dict | None:
    """The signal holding low-space cleanup, when it's on and work waits."""
    if not settings()["enabled"]:
        return None
    current = signal()
    return current if current and current["pending"] else None
