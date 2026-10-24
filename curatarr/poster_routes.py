"""Library → Posters, and a title's custom poster (custom_posters.py)."""

from pathlib import Path

from flask import abort, flash, redirect, render_template, request, send_file, url_for

from . import custom_posters, db
from .integrations import IntegrationError
from .models import CustomPoster, MediaIdentity
from .routes import NAV_SECTIONS, UPLOAD_ENDPOINTS, bp

NAV_SECTIONS["main.custom_posters_page"] = "library"
UPLOAD_ENDPOINTS.add("main.title_poster_upload")


@bp.get("/library/posters")
def custom_posters_page():
    from .services import setting

    state = custom_posters.import_state()
    folder = custom_posters.settings().get("folder")
    options = custom_posters.prompt_options()
    written, count = [], None
    if request.args.get("prompts"):
        titles = custom_posters.prompt_titles(options["titles"], options["skip_done"])
        count = len(titles)
        written = custom_posters.prompts(
            custom_posters.describe(titles),
            batch=options["batch"],
            photos=options["photos"],
            people=options["people"],
            output=folder or "the folder Curatarr imports posters from",
        )
    return render_template(
        "custom_posters.html",
        posters=custom_posters.gallery(),
        folder=folder,
        unmatched=sorted(state["unmatched"].items()),
        checked=state.get("at"),
        options=options,
        accounts=sorted(
            (setting("jellyfin_user_names", {}) or {}).items(), key=lambda a: a[1]
        ),
        written=written,
        count=count,
    )


@bp.post("/library/posters/prompts")
def custom_posters_prompts():
    custom_posters.save_prompt_options(request.form)
    return redirect(url_for("main.custom_posters_page", prompts=1) + "#prompts")


@bp.post("/library/posters/settings")
def custom_posters_settings():
    try:
        custom_posters.save_settings(request.form.get("folder"))
    except ValueError as exc:
        flash(str(exc), "danger")
    else:
        flash(
            "Import folder saved; new pictures there are put on within minutes.",
            "success",
        )
    return redirect(url_for("main.custom_posters_page"))


@bp.get("/library/posters/<poster_id>.jpg")
def custom_poster_image(poster_id):
    poster = db.session.get(CustomPoster, poster_id)
    if not poster or not Path(poster.image_path).exists():
        abort(404)
    return send_file(poster.image_path, mimetype="image/jpeg", max_age=3600)


def _back(media):
    if request.form.get("back") == "gallery":
        return redirect(url_for("main.custom_posters_page"))
    return redirect(url_for("main.title", media_id=media.id) + "#poster")


@bp.post("/titles/<media_id>/poster")
def title_poster_upload(media_id):
    media = db.session.get(MediaIdentity, media_id)
    if not media:
        abort(404)
    upload = request.files.get("poster")
    if not upload or not upload.filename:
        flash("Choose a picture to upload first.", "warning")
        return _back(media)
    try:
        custom_posters.apply(media, upload.read(), "Uploaded")
    except (ValueError, IntegrationError) as exc:
        db.session.rollback()
        flash(f"Couldn't put that poster on: {exc}", "danger")
    else:
        flash(
            f"{media.title} has its new poster. Jellyfin apps may show their "
            "cached copy for a little while.",
            "success",
        )
    return _back(media)


@bp.post("/titles/<media_id>/poster/restore")
def title_poster_restore(media_id):
    media = db.session.get(MediaIdentity, media_id)
    poster = custom_posters.active(media_id) if media else None
    if not poster:
        abort(404)
    try:
        flash(custom_posters.restore(poster), "success")
    except (ValueError, IntegrationError) as exc:
        db.session.rollback()
        flash(f"Couldn't restore the poster: {exc}", "danger")
    return _back(media)
