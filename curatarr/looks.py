"""Looks: the default look, and saved looks by name.

A look is a set of artwork settings: the look itself (the words that steer
the pictures), the reference photos and who's in them, the engine, where to
start from, and the two reference options. The default look is what
Generate and Batches start from each time; it's edited on the Looks page,
and each one before it is kept there to put back. Restore the default (on
Generate and Batches) puts their fields back to it; on the Looks page,
Restore the built-in look puts the default's words back to Curatarr's own.

Saved looks are named alternatives: Try it opens Generate with one chosen,
and Make it the default makes it the default look. Something wanted for one
run only ("going for funny" for a comedy) goes in the run's extra note, which
is added after the look for that run and never kept.
"""

import uuid

from .models import utcnow
from .services import set_setting, setting

KEY = "artwork_looks"
HISTORY = 30
FIELDS = ("style", "photos", "cast", "engine", "originals", "external_ok", "approach")


def _stored() -> dict:
    return dict(setting(KEY) or {})


def _write(stored) -> None:
    from . import db

    set_setting(KEY, stored)
    db.session.commit()


def built_in() -> dict:
    """Curatarr's own look: the built-in words and options, with the reference
    photos from Write prompts' settings."""
    from . import custom_posters

    options = custom_posters.PROMPT_DEFAULTS | (
        custom_posters.settings().get("prompts") or {}
    )
    return clean(
        {
            "style": custom_posters.DEFAULT_STYLE,
            "photos": options.get("photos") or "",
            "originals": True,
            "approach": "original",
        }
    ) | {"name": "Built-in"}


def default() -> dict:
    """The default look (with ``saved_at`` and ``by`` once one is saved)."""
    look = _stored().get("default")
    return dict(look) if look else built_in()


def history() -> list[dict]:
    """Earlier default looks, newest first."""
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


def save_default(values, who=None, name=None) -> dict:
    """Make these settings the default look; the one before is kept."""
    stored = _stored()
    look = clean(values) | {
        "name": name or "",
        "saved_at": utcnow().isoformat(),
        "by": who or "",
    }
    before = stored.get("default")
    past = list(stored.get("history") or [])
    if before:
        past.insert(0, before)
    _write(stored | {"default": look, "history": past[:HISTORY]})
    return look


def restore_built_in(who=None) -> dict:
    """Put the built-in words and options back in the default look, keeping
    its reference photos, cast and engine."""
    own = built_in()
    now = default()
    return save_default(
        now | {k: own[k] for k in ("style", "approach", "originals")},
        who,
        name="Built-in",
    )


def restore(index: int, who=None) -> dict:
    """Put an earlier default look back."""
    past = history()
    if not 0 <= index < len(past):
        raise ValueError("That earlier version isn't there any more.")
    return save_default(past[index], who, name=past[index].get("name"))


def save_look(values, name, who=None, look_id=None) -> dict:
    """Save a named look (a new one, or ``look_id`` changed)."""
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


def make_default(look_id, who=None) -> dict:
    """Make a saved look the default look (a copy: changing the saved look
    later doesn't change the default)."""
    look = get(look_id)
    if look is None:
        raise ValueError("That look isn't there any more.")
    return save_default(look, who, name=look["name"])


def chosen(key) -> dict:
    """The look a form or link names: ``saved:<id>``, else the default."""
    if key and key.startswith("saved:"):
        look = get(key[6:])
        if look:
            return look
    return default()
