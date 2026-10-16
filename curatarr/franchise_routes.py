"""Library → Collections: gaps in film series, and suggestions (franchises.py)."""

import re

from flask import flash, redirect, render_template, request, url_for

from . import franchises
from .integrations import IntegrationError
from .routes import NAV_SECTIONS, bp

NAV_SECTIONS["main.collections"] = "library"
SHOWN = 30  # series listed before "Show all"


def _back():
    anchor = request.form.get("anchor", "")
    url = url_for("main.collections", all=request.form.get("all") or None)
    return redirect(url + (anchor if re.fullmatch(r"#[a-z]+-\d+", anchor) else ""))


@bp.get("/collections")
def collections():
    error = None
    found, picks = [], {"films": [], "genres": []}
    try:
        found = franchises.gaps()
        picks = franchises.suggestions()
    except (IntegrationError, ValueError) as exc:
        error = str(exc)
    return render_template(
        "collections.html",
        gaps=found if request.args.get("all") else found[:SHOWN],
        gaps_total=len(found),
        show_all=bool(request.args.get("all")),
        picks=picks,
        missing_total=sum(len(c["missing"]) for c in found),
        error=error,
    )


@bp.post("/collections/add")
def collections_add():
    ids = [int(x) for x in request.form.getlist("tmdb_id") if x.isdigit()]
    collection = request.form.get("collection")
    if not ids:
        flash("Tick at least one film.", "warning")
    else:
        try:
            added = franchises.add(
                ids, int(collection) if collection and collection.isdigit() else None
            )
        except (IntegrationError, ValueError) as exc:
            flash(f"Couldn't add to Radarr: {exc}", "danger")
        else:
            flash(
                f"Added {', '.join(added)} to Radarr; it's searching now."
                if added
                else "Those are already in Radarr.",
                "success" if added else "info",
            )
    return _back()


@bp.post("/collections/<int:collection_id>/follow")
def collections_follow(collection_id):
    try:
        title = franchises.follow(collection_id)
    except (IntegrationError, ValueError) as exc:
        flash(f"Couldn't follow it in Radarr: {exc}", "danger")
    else:
        flash(
            f"Following {title}. Radarr adds its missing films and any new ones "
            "as they're released.",
            "success",
        )
    return redirect(url_for("main.collections"))


@bp.post("/collections/dismiss")
def collections_dismiss():
    kind, tmdb_id = request.form.get("kind"), request.form.get("tmdb_id", "")
    if kind in {"collections", "films"} and tmdb_id.isdigit():
        franchises.dismiss(kind, int(tmdb_id))
        flash("Hidden. It won't be suggested again.", "success")
    return _back()
