"""Library → Posters, a title's custom poster (custom_posters.py), and the
list the Curatarr plugin for Jellyfin reads to show an account its own."""

import hmac
from io import BytesIO
from pathlib import Path

from flask import (
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import custom_posters, db
from .integrations import IntegrationError
from .models import CustomPoster, MediaIdentity
from .routes import NAV_SECTIONS, PUBLIC_ENDPOINTS, UPLOAD_ENDPOINTS, bp

NAV_SECTIONS["main.custom_posters_page"] = "library"
UPLOAD_ENDPOINTS.update({"main.title_poster_upload", "main.custom_posters_avatar"})
# The plugin signs these with the webhook token instead of a sign-in.
PUBLIC_ENDPOINTS.update({"main.jellyfin_posters", "main.jellyfin_poster_image"})


def _accounts():
    from .services import setting

    names = setting("jellyfin_user_names", {}) or {}
    return sorted(names.items(), key=lambda account: account[1])


@bp.get("/library/posters")
def custom_posters_page():
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
    accounts = _accounts()
    ribbon = custom_posters.ribbon_settings()
    viewers_of = {
        who: [v for v, seen in (ribbon["sees"] or {}).items() if who in seen]
        for who, _name in accounts
    }
    return render_template(
        "custom_posters.html",
        groups=custom_posters.gallery_groups(),
        folder=folder,
        import_accounts=custom_posters.settings().get("accounts") or [],
        unmatched=sorted(state["unmatched"].items()),
        checked=state.get("at"),
        options=options,
        accounts=accounts,
        ribbon=ribbon,
        ribbon_viewers_of=viewers_of,
        avatar_uploaded={
            who: custom_posters.has_uploaded_avatar(who) for who, _name in accounts
        },
        ribbon_mixed=any(
            0 < len(viewers_of[who]) < len(accounts) - 1 for who, _name in accounts
        ),
        written=written,
        count=count,
    )


@bp.post("/library/posters/ribbons")
def custom_posters_ribbons():
    options = custom_posters.save_ribbon_settings(request.form, _accounts())
    flash(
        "Favourite ribbons saved; Jellyfin apps show them within a couple of minutes."
        if options["enabled"]
        else "Favourite ribbons are off.",
        "success",
    )
    return redirect(url_for("main.custom_posters_page") + "#ribbons")


@bp.post("/library/posters/avatar")
def custom_posters_avatar():
    """An account's picture on ribbons: uploaded, or removed (back to its
    Jellyfin profile picture, or its initials)."""
    who = request.form.get("account") or ""
    if who not in dict(_accounts()):
        abort(400)
    upload = request.files.get("picture")
    try:
        if request.form.get("remove"):
            custom_posters.save_avatar(who, None)
            flash("Picture removed.", "success")
        elif upload and upload.filename:
            custom_posters.save_avatar(who, upload.read())
            flash(
                "Picture saved; ribbons show it within a couple of minutes.", "success"
            )
        else:
            flash("Choose a picture first.", "warning")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("main.custom_posters_page") + "#ribbons")


@bp.get("/library/posters/avatar/<account>.jpg")
def custom_posters_avatar_image(account):
    from .artwork import _avatar

    if account not in dict(_accounts()):
        abort(404)
    picture = custom_posters.avatar_picture(account)
    disc = _avatar(picture, custom_posters.account_name(account), 96)
    output = BytesIO()
    disc.save(output, format="PNG")
    return send_file(BytesIO(output.getvalue()), mimetype="image/png", max_age=0)


@bp.get("/library/posters/ribbon-preview.jpg")
def custom_posters_ribbon_preview():
    """The ribbon as it would look, on a real poster from the library (a
    plain stand-in when Jellyfin can't be reached)."""
    from PIL import Image

    options = custom_posters.ribbon_settings()
    poster, leaving = custom_posters.preview_poster()
    if poster is None:
        blank = BytesIO()
        Image.new("RGB", (500, 750), "#2b3a4a").save(blank, format="JPEG")
        poster = blank.getvalue()
    people = _accounts()[:2] or [("sam", "Sam")]
    household = request.args.get("household") == "1"
    picture = custom_posters.draw_ribbon(
        poster,
        [i for i, _name in people],
        [name for _id, name in people],
        options,
        new=bool(options["new_days"]) and not household,
        household=household,
        large=request.args.get("size") == "large",
        leaving=leaving,
    )
    return send_file(BytesIO(picture), mimetype="image/jpeg", max_age=0)


@bp.post("/library/posters/prompts")
def custom_posters_prompts():
    custom_posters.save_prompt_options(request.form)
    return redirect(url_for("main.custom_posters_page", prompts=1) + "#prompts")


@bp.post("/library/posters/settings")
def custom_posters_settings():
    try:
        known = dict(_accounts())
        custom_posters.save_settings(
            request.form.get("folder"),
            [a for a in request.form.getlist("accounts") if a in known],
        )
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
    user_id = request.form.get("for") or None
    if user_id and user_id not in dict(_accounts()):
        abort(400)
    try:
        custom_posters.apply(media, upload.read(), "Uploaded", user_id=user_id)
    except (ValueError, IntegrationError) as exc:
        db.session.rollback()
        flash(f"Couldn't put that poster on: {exc}", "danger")
    else:
        who = custom_posters.account_name(user_id) if user_id else "everyone"
        flash(
            f"{media.title} has its new poster for {who}. Jellyfin apps may show "
            "their cached copy for a little while"
            + (
                "; other accounts keep the usual one (needs the Curatarr plugin)."
                if user_id
                else "."
            ),
            "success",
        )
    return _back(media)


@bp.post("/titles/<media_id>/poster/restore")
def title_poster_restore(media_id):
    media = db.session.get(MediaIdentity, media_id)
    chosen = request.form.getlist("poster")
    posters = (
        [db.session.get(CustomPoster, p) for p in chosen]
        if chosen
        else [custom_posters.active(media_id)]
    )
    if (
        not media
        or not posters
        or any(
            p is None or p.media_identity_id != media.id or not p.active
            for p in posters
        )
    ):
        abort(404)
    said = []
    for poster in posters:
        try:
            said.append(custom_posters.restore(poster))
        except (ValueError, IntegrationError) as exc:
            db.session.rollback()
            flash(f"Couldn't restore the poster: {exc}", "danger")
    if said:
        flash(" ".join(said), "success")
    return _back(media)


def _plugin_allowed() -> bool:
    from .services import setting

    token = setting("webhook_token")
    supplied = request.headers.get("X-Curatarr-Token", "")
    return bool(token) and hmac.compare_digest(supplied, token)


def _bare(item_id) -> str:
    return str(item_id or "").replace("-", "").lower()


@bp.get("/api/v1/jellyfin/posters")
def jellyfin_posters():
    """Each account's own posters: which item, whose, and the tag to use."""
    if not _plugin_allowed():
        abort(403)
    posters = custom_posters.plugin_list()
    return jsonify({"posters": posters})


@bp.get("/api/v1/jellyfin/posters/<path:poster_id>.jpg")
def jellyfin_poster_image(poster_id):
    if not _plugin_allowed():
        abort(403)
    try:
        picture = custom_posters.plugin_picture(
            poster_id, large=request.args.get("size") == "large"
        )
    except (IntegrationError, ValueError, OSError):
        abort(503)  # Jellyfin's poster couldn't be read: the plugin tries again
    if picture is None:
        abort(404)
    return send_file(BytesIO(picture), mimetype="image/jpeg", max_age=300)
