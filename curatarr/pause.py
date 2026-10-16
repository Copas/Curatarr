"""Pausing all of Curatarr's work on request, to free the machine for something else.

Unlike the automatic pause for playback, which freezes running tools where they
are, this one is for when the graphics card or processor is needed elsewhere:
running jobs are stopped and put back in the queue (they start over when work
resumes), nothing new starts, and the language model is unloaded so its
graphics memory is free. It lasts for a set time or until resumed.
"""

from datetime import timedelta

import requests

from . import db, throttle
from .models import utcnow
from .services import _as_datetime, set_setting, setting

KEY = "work_paused"
_unloaded_for = None  # the pause whose language model has been unloaded


def state() -> dict | None:
    """The pause in effect ({since, until, by}), or None. Ends an expired one."""
    paused = setting(KEY)
    if not paused:
        return None
    until = _as_datetime(paused.get("until"))
    if until and until <= utcnow():
        set_setting(KEY, None)
        db.session.commit()
        return None
    return paused


def pause(hours: float | None, by: str | None = None) -> dict:
    now = utcnow()
    value = {
        "since": now.isoformat(),
        "until": (now + timedelta(hours=hours)).isoformat() if hours else None,
        "by": by,
    }
    set_setting(KEY, value)
    db.session.commit()
    return value


def resume() -> None:
    set_setting(KEY, None)
    db.session.commit()


def enforce() -> bool:
    """Worker cycle: apply the pause (or lift it). True while paused."""
    global _unloaded_for
    paused = state()
    if not paused:
        throttle.allow()
        _unloaded_for = None
        return False
    throttle.stop_all()
    if _unloaded_for != paused["since"]:
        _unload_language_models()
        _unloaded_for = paused["since"]
    return True


def _unload_language_models() -> None:
    """Ask Ollama to drop every loaded model from graphics memory now."""
    from .enrichment import options

    try:
        base = options()["ollama_url"].rstrip("/")
        loaded = requests.get(f"{base}/api/ps", timeout=10).json().get("models") or []
        for model in loaded:
            requests.post(
                f"{base}/api/generate",
                json={"model": model.get("name"), "keep_alive": 0},
                timeout=30,
            )
    except (requests.RequestException, ValueError, KeyError):
        pass  # Ollama isn't running: its memory is free anyway
