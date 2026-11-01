"""The house look: the artwork settings that are known to work, locked in.

Generate and Batches start from it, show it read-only and, when it's chosen,
use it whatever else the form sends: a link or a half-filled form can't blank
or change it. It changes only on its own page, with a box ticked to confirm,
and every earlier version is kept there to put back. Something wanted for one
run only ("going for funny" for a comedy) goes in the run's own extra note,
which is added to the look for those titles and never kept.
"""

from .models import utcnow
from .services import set_setting, setting

KEY = "artwork_house_look"
HISTORY = 30
FIELDS = ("style", "photos", "cast", "engine", "originals", "external_ok", "approach")


def current() -> dict | None:
    """The house look's settings (with ``saved_at`` and ``by``), or None."""
    stored = setting(KEY) or {}
    look = stored.get("look")
    return dict(look) if look else None


def history() -> list[dict]:
    """Earlier versions, newest first."""
    return list((setting(KEY) or {}).get("history") or [])


def clean(values) -> dict:
    """The settings from a form (or a dict), tidied as Generate tidies them."""
    get = values.get
    cast = get("cast") or ""
    if isinstance(cast, str):
        cast = [n.strip() for n in cast.split(",") if n.strip()]
    return {
        "style": " ".join(str(get("style") or "").split())[:600],
        "photos": str(get("photos") or "").strip(),
        "cast": list(cast)[:3],
        "engine": str(get("engine") or "auto"),
        "originals": get("originals") in {True, "on", "1", "true"},
        "external_ok": get("external_ok") in {True, "on", "1", "true"},
        "approach": "story" if get("approach") == "story" else "original",
    }


def save(values, who=None) -> dict:
    """Make these the house look; the one before goes to the history."""
    from . import db

    stored = setting(KEY) or {}
    look = clean(values) | {"saved_at": utcnow().isoformat(), "by": who or ""}
    before = stored.get("look")
    past = list(stored.get("history") or [])
    if before:
        past.insert(0, before)
    set_setting(KEY, {"look": look, "history": past[:HISTORY]})
    db.session.commit()
    return look


def restore(index: int, who=None) -> dict:
    """Put an earlier version back (it becomes the house look again)."""
    past = history()
    if not 0 <= index < len(past):
        raise ValueError("That earlier version isn't there any more.")
    return save(past[index], who)


def job_options(extra="") -> tuple[dict, str]:
    """(options, engine) for a job made with the house look, plus ``extra``
    (this run's own note) after the look. ValueError without a house look."""
    look = current()
    if look is None:
        raise ValueError("There's no house look yet: save one under House look.")
    style = look["style"]
    extra = " ".join(str(extra or "").split())[:300]
    if extra:
        style = f"{style} {extra}".strip()
    options = {
        "photos": look["photos"],
        "cast": list(look["cast"]),
        "style": style,
        "originals": look["originals"],
        "external_ok": look["external_ok"],
        "approach": look["approach"],
        "look": "house",
        "look_saved_at": look.get("saved_at"),
    }
    if extra:
        options["extra"] = extra
    return options, look["engine"]
