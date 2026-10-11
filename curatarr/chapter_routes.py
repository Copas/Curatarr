"""Chapter generation pages: settings, library maintenance, and one file."""

from flask import abort, flash, redirect, render_template, request, url_for

from . import db
from .chapters import (
    DEFAULTS,
    GENERATION_VERSION,
    WHISPER_MODELS,
    ChapterConfigError,
    effective_config,
    timestamp,
)
from .enrichment import (
    describe,
    environment_status,
    file_locations_known,
    flatten,
    form_values,
    global_layer,
    library_layers,
    maintenance_summary,
    needs_chapters_query,
    options,
    queue_library,
    queue_scan,
    queue_selected,
    request_scan_after_discovery,
    save_settings,
    state_for,
)
from .models import ChapterState, Library, MediaIdentity, MediaPart
from .routes import NAV_SECTIONS, bp
from .services import request_reconciliation

NAV_SECTIONS.update(
    {
        "main.chapter_settings": "manage",
        "main.chapter_maintenance": "manage",
        "main.chapter_confirm_library": "manage",
        "main.chapter_part": "library",
    }
)


@bp.app_template_filter("chapter_time")
def chapter_time(seconds):
    return timestamp(seconds or 0)[:8]


def _managed_libraries():
    return [
        row for row in db.session.query(Library).order_by(Library.name) if row.managed
    ]


@bp.route("/chapters/settings", methods=["GET", "POST"])
def chapter_settings():
    scope = request.args.get("scope", "global")
    library = None
    if scope != "global":
        library = db.session.get(Library, scope)
        if not library or not library.managed:
            abort(404)
    if request.method == "POST":
        try:
            save_settings(library, request.form)
        except ChapterConfigError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        else:
            flash("Chapter settings saved", "success")
        return redirect(request.full_path.rstrip("?"))
    inherited = effective_config(global_layer()) if library else DEFAULTS
    effective = (
        effective_config(global_layer(), library_layers().get(library.id))
        if library
        else effective_config(global_layer())
    )
    try:
        opts = options()
    except ChapterConfigError:
        opts = None
    return render_template(
        "chapter_settings.html",
        library=library,
        libraries=_managed_libraries(),
        values=form_values(library),
        inherited=inherited,
        inherited_values=flatten(inherited),
        effective=effective,
        opts=opts,
        whisper_models=sorted(WHISPER_MODELS),
        environment=environment_status(),
    )


@bp.get("/chapters")
def chapter_maintenance():
    library_id = request.args.get("library") or None
    if library_id and not db.session.get(Library, library_id):
        abort(404)
    candidates = (
        needs_chapters_query(library_id)
        .join(Library, MediaIdentity.library_id == Library.id)
        .order_by(
            MediaIdentity.title, MediaPart.season_number, MediaPart.episode_number
        )
        .limit(300)
        .all()
    )
    return render_template(
        "chapters.html",
        libraries=_managed_libraries(),
        library_id=library_id,
        summary=maintenance_summary(library_id),
        candidates=candidates,
        enabled_libraries={
            row.id
            for row in _managed_libraries()
            if effective_config(global_layer(), library_layers().get(row.id))["enabled"]
        },
    )


@bp.post("/chapters/scan")
def chapter_scan():
    library_id = request.form.get("library") or None
    count = queue_scan(library_id)
    if count:
        flash(
            f"Queued {count} files for a read-only chapter scan. The worker "
            "inspects them in the background; reload to see the counts.",
            "success",
        )
    elif not file_locations_known():
        # Discovery records each file's location; it has not run since the
        # upgrade that added them. Run it now, then scan.
        request_scan_after_discovery(library_id)
        request_reconciliation()
        flash(
            "Curatarr has not recorded where your files are yet. It is "
            "discovering them now (a minute or two), and the scan starts "
            "automatically afterwards. Reload this page in a few minutes.",
            "info",
        )
    else:
        flash("Every file is already scanned or has a chapter job queued.", "info")
    return redirect(url_for("main.chapter_maintenance", library=library_id))


@bp.post("/chapters/queue")
def chapter_queue():
    action = request.form.get("action")
    ids = request.form.getlist("state_id")
    library_id = request.form.get("library") or None
    if action not in {"preview", "generate"}:
        abort(400)
    if not ids:
        flash("Select at least one file.", "warning")
    else:
        count = queue_selected(ids, action)
        flash(
            f"Queued {count} file{'s' if count != 1 else ''} for "
            + (
                "a dry run (nothing is modified)."
                if action == "preview"
                else "chapters."
            ),
            "success",
        )
    return redirect(url_for("main.chapter_maintenance", library=library_id))


@bp.route("/chapters/libraries/<library_id>/process", methods=["GET", "POST"])
def chapter_confirm_library(library_id):
    library = db.session.get(Library, library_id)
    if not library or not library.managed:
        abort(404)
    count = needs_chapters_query(library_id).count()
    if request.method == "POST":
        if request.form.get("confirm") != str(count):
            flash(
                "The number of files changed; check the count and confirm again.",
                "warning",
            )
            return redirect(
                url_for("main.chapter_confirm_library", library_id=library_id)
            )
        queued = queue_library(library_id)
        flash(f"Queued {queued} files in {library.name} for chapters.", "success")
        return redirect(url_for("main.chapter_maintenance", library=library_id))
    return render_template(
        "chapter_confirm.html",
        library=library,
        count=count,
        summary=maintenance_summary(library_id),
        config=effective_config(global_layer(), library_layers().get(library_id)),
    )


@bp.get("/chapters/parts/<part_id>")
def chapter_part(part_id):
    part = db.session.get(MediaPart, part_id)
    if not part:
        abort(404)
    state = db.session.query(ChapterState).filter_by(media_part_id=part.id).first()
    return render_template(
        "chapter_part.html",
        part=part,
        media=part.media,
        state=state,
        info=describe(state),
        preview=state.last_preview_json if state else None,
        generation_version=GENERATION_VERSION,
    )


@bp.post("/chapters/parts/<part_id>/<action>")
def chapter_part_action(part_id, action):
    from .enrichment import request as request_action

    part = db.session.get(MediaPart, part_id)
    if not part or action not in {"preview", "generate", "regenerate", "remove"}:
        abort(404)
    state = state_for(part)
    if action in {"regenerate", "remove"} and not state.written_fingerprint:
        flash(
            "Curatarr can only regenerate or remove chapters it wrote itself.",
            "danger",
        )
        return redirect(url_for("main.chapter_part", part_id=part.id))
    try:
        request_action(part, action)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    else:
        flash(
            {
                "preview": "Preview queued. Nothing is modified; reload in a few "
                "minutes (Smart Chapters transcribes the audio first).",
                "generate": "Chapter generation queued.",
                "regenerate": "Regeneration queued.",
                "remove": "Removal queued.",
            }[action],
            "success",
        )
    return redirect(url_for("main.chapter_part", part_id=part.id))
