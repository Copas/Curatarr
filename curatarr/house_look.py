"""Saved looks, and the house look: artwork settings that are known to work.

A saved look is a named set of artwork settings (the look, reference photos,
who's in them, engine, where to start from, the reference options). One of
them is the house look: Generate and Batches start from it, show it
read-only and, when it's chosen, use it whatever else the form sends, so a
link or a half-filled form can't blank or change it. Any saved look can be
tried on a few titles (Try it opens Generate with it chosen; the pictures wait
in Review as usual) and then made the house look, with a box ticked to
confirm. Each house look before it is kept to put back. Something wanted for
one run only ("going for funny" for a comedy) goes in the run's own extra
note, which is added to the look for those titles and never kept.
"""

import uuid

from .models import utcnow
from .services import set_setting, setting

KEY = "artwork_house_look"
HISTORY = 30
FIELDS = ("style", "photos", "cast", "engine", "originals", "external_ok", "approach")


def _stored() -> dict:
    return dict(setting(KEY) or {})


def _write(stored) -> None:
    from . import db

    set_setting(KEY, stored)
    db.session.commit()


def current() -> dict | None:
    """The house look's settings (with ``name``, ``saved_at``, ``by``), or None."""
    look = _stored().get("look")
    return dict(look) if look else None


def history() -> list[dict]:
    """Earlier house looks, newest first."""
    return list(_stored().get("history") or [])


def looks() -> list[dict]:
    """The saved looks, in the order they were first saved."""
    return [dict(look) for look in _stored().get("looks") or []]


def get(look_id) -> dict | None:
    return next((look for look in looks() if look["id"] == look_id), None)


def clean(values) -> dict:
    """The settings from a form (or a dict), tidied as Generate tidies them."""
    get_value = values.get
    cast = get_value("cast") or ""
    if isinstance(cast, str):
        cast = [n.strip() for n in cast.split(",") if n.strip()]
    return {
        "style": " ".join(str(get_value("style") or "").split())[:600],
        "photos": str(get_value("photos") or "").strip(),
        "cast": list(cast)[:3],
        "engine": str(get_value("engine") or "auto"),
        "originals": get_value("originals") in {True, "on", "1", "true"},
        "external_ok": get_value("external_ok") in {True, "on", "1", "true"},
        "approach": "story" if get_value("approach") == "story" else "original",
    }


def save_look(values, name, who=None, look_id=None) -> dict:
    """Save a named look (a new one, or ``look_id`` changed). The house look
    isn't touched: it changes only through make_house."""
    name = " ".join(str(name or "").split())[:80]
    if not name:
        raise ValueError("Give the look a name.")
    stored = _stored()
    saved = list(stored.get("looks") or [])
    if any(
        look["name"].lower() == name.lower() and look["id"] != look_id for look in saved
    ):
        raise ValueError(f"There's already a look called {name}.")
    look = clean(values) | {
        "id": look_id or uuid.uuid4().hex[:12],
        "name": name,
        "saved_at": utcnow().isoformat(),
        "by": who or "",
    }
    if look_id:
        if not any(old["id"] == look_id for old in saved):
            raise ValueError("That look isn't there any more.")
        saved = [look if old["id"] == look_id else old for old in saved]
    else:
        saved.append(look)
    _write(stored | {"looks": saved})
    return look


def delete_look(look_id) -> dict:
    stored = _stored()
    saved = list(stored.get("looks") or [])
    look = next((old for old in saved if old["id"] == look_id), None)
    if look is None:
        raise ValueError("That look isn't there any more.")
    _write(stored | {"looks": [old for old in saved if old["id"] != look_id]})
    return look


def save(values, who=None, name=None) -> dict:
    """Make these settings the house look; the one before goes to the history."""
    stored = _stored()
    look = clean(values) | {
        "name": name or values.get("name") or "House look",
        "saved_at": utcnow().isoformat(),
        "by": who or "",
    }
    if values.get("id"):
        look["id"] = values["id"]
    before = stored.get("look")
    past = list(stored.get("history") or [])
    if before:
        past.insert(0, before)
    _write(stored | {"look": look, "history": past[:HISTORY]})
    return look


def make_house(look_id, who=None) -> dict:
    """Make a saved look the house look (a copy: editing the saved look later
    doesn't change the house look)."""
    look = get(look_id)
    if look is None:
        raise ValueError("That look isn't there any more.")
    return save(look, who, name=look["name"])


def restore(index: int, who=None) -> dict:
    """Put an earlier house look back."""
    past = history()
    if not 0 <= index < len(past):
        raise ValueError("That earlier version isn't there any more.")
    return save(past[index], who)


def job_options(extra="", look_id=None) -> tuple[dict, str]:
    """(options, engine) for a job made with the house look, or with saved
    look ``look_id``, plus ``extra`` (this run's own note) after the look.
    ValueError when there's no such look."""
    look = get(look_id) if look_id else current()
    if look is None:
        raise ValueError(
            "That look isn't there any more."
            if look_id
            else "There's no house look yet: save one under Looks."
        )
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
        "look": f"saved:{look_id}" if look_id else "house",
        "look_name": look.get("name") or "House look",
        "look_saved_at": look.get("saved_at"),
    }
    if extra:
        options["extra"] = extra
    return options, look["engine"]
