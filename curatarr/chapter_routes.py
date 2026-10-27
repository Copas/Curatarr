"""Chapter generation pages: settings, library maintenance, and one file."""

from flask import abort, flash, redirect, render_template, request, url_for

from . import db
from .chapters import (
    DEFAULTS,
    GENERATION_VERSION,
    MAX_CONCURRENCY,
    WHISPER_MODELS,
    ChapterConfigError,
    effective_config,
    timestamp,
)
from .enrichment import (
    ACTION_LABELS,
    describe,
    dismiss_identity,
    environment_status,
    failed_groups,
    file_label,
    file_locations_known,
    fix_default_audio,
    flatten,
    form_values,
    global_layer,
    identity_check_enabled,
    identity_issues,
    job_activity,
    library_layers,
    maintenance_summary,
    marker_review,
    needs_chapters_query,
    options,
    queue_library,
    queue_scan,
    queue_selected,
    queue_smart_upgrade,
    queue_subtitles,
    request_scan_after_discovery,
    request_skip_marker_check,
    save_settings,
    search_transcripts,
    smart_chapters_blocked,
    state_for,
    transcripts_searchable,
    undo_marker,
)
from .integrations import IntegrationError
from .mediafile import MediaToolError
from .models import ChapterState, Library, MediaIdentity, MediaPart
from .routes import NAV_SECTIONS, bp
from .services import request_reconciliation, setting

NAV_SECTIONS.update(
    {
        "main.chapter_settings": "settings",
        "main.chapter_maintenance": "enhance",
        "main.chapter_files": "enhance",
        "main.chapter_confirm_library": "enhance",
        "main.chapter_part": "enhance",
        "main.chapter_markers": "enhance",
        "main.chapter_dubs": "enhance",
        "main.chapter_problems": "enhance",
        "main.chapter_identity": "enhance",
        "main.transcript_search": "library",
    }
)


def _gpu_hint():
    """For Enhance: the extra to install, and a graphics card going unused
    (an install with faster-whisper but without the CUDA libraries)."""
    from .transcribe import available, cuda_libraries, nvidia_gpu, recommended_extras

    card = nvidia_gpu()
    unused = card if card and available() and not cuda_libraries() else None
    return {"recommended_extras": recommended_extras(), "gpu_unused": unused}


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
            flash("Enhance settings saved", "success")
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
        max_concurrency=MAX_CONCURRENCY,
        environment=environment_status(),
    )


def _enabled_libraries():
    return {
        row.id
        for row in _managed_libraries()
        if effective_config(global_layer(), library_layers().get(row.id))["enabled"]
    }


def _backlog(library_id):
    """What "Process all…" would do: files without chapters it would add to the
    queue, and those already queued or running (new files queue on their own
    when a library processes new media or its whole library)."""
    if library_id in (None, "all"):
        library, targets = None, sorted(_enabled_libraries())
    else:
        library = db.session.get(Library, library_id)
        if not library or not library.managed:
            abort(404)
        targets = [library.id]
    addable = queued = 0
    for target in targets:
        query = needs_chapters_query(target)
        addable += query.filter(ChapterState.job_state == "idle").count()
        queued += query.filter(ChapterState.job_state != "idle").count()
    return {
        "library": library,
        "targets": targets,
        "addable": addable,
        "queued": queued,
        "name": library.name if library else "every library with chapters enabled",
    }


def _library_arg():
    library_id = request.args.get("library") or None
    if library_id and not db.session.get(Library, library_id):
        abort(404)
    return library_id


@bp.get("/chapters")
def chapter_maintenance():
    """Enhance → Progress: what's been done, what's left, what's running."""
    library_id = _library_arg()
    return render_template(
        "chapters.html",
        libraries=_managed_libraries(),
        library_id=library_id,
        failed_groups=failed_groups(library_id),
        backlog=_backlog(library_id),
        summary=maintenance_summary(library_id),
        compat=_compat_progress(),
        seek=_seek_progress(),
        thumbs=_thumbnail_status(),
        smart_blocked=smart_chapters_blocked(),
        **_gpu_hint(),
        activity=job_activity(),
        action_labels=ACTION_LABELS,
        file_label=file_label,
        enabled_libraries=_enabled_libraries(),
        # Asked for, not yet picked up by the worker: show that, not the button.
        scan_requested=bool(setting("chapter_scan_requested")),
        markers_requested=bool(setting("skip_markers_requested")),
    )


@bp.get("/chapters/files")
def chapter_files():
    """Enhance → Files: files without chapters, to preview or process."""
    library_id = _library_arg()
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
        "chapter_files.html",
        libraries=_managed_libraries(),
        library_id=library_id,
        backlog=_backlog(library_id),
        summary=maintenance_summary(library_id),
        file_label=file_label,
        candidates=candidates,
        enabled_libraries=_enabled_libraries(),
    )


@bp.get("/chapters/activity")
def chapter_activity_fragment():
    library_id = request.args.get("library") or None
    if library_id and not db.session.get(Library, library_id):
        abort(404)
    return render_template(
        "_chapter_activity.html",
        library_id=library_id,
        activity=job_activity(),
        action_labels=ACTION_LABELS,
        file_label=file_label,
        thumbs=_thumbnail_status(),
    )


@bp.post("/chapters/scan")
def chapter_scan():
    library_id = request.form.get("library") or None
    count = queue_scan(library_id, everything=request.form.get("all") == "1")
    if count:
        flash(
            f"Queued {count} file{'' if count == 1 else 's'} for a read-only scan. "
            "The worker reads them in the background; reload to see the counts.",
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
        flash("Every file is already scanned or has a job queued.", "info")
    return redirect(url_for("main.chapter_maintenance", library=library_id))


@bp.post("/chapters/subtitles")
def chapter_subtitles():
    library_id = request.form.get("library") or None
    count = queue_subtitles(library_id)
    flash(
        f"Queued {count} files. Each is checked for English subtitles; only "
        "those without get generated ones (transcribing first if needed).",
        "success",
    )
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
    return redirect(url_for("main.chapter_files", library=library_id))


@bp.post("/chapters/recaps")
def chapter_recaps():
    """Ask the worker to check skip markers on episodes not yet checked."""
    library_id = request.form.get("library") or None
    request_skip_marker_check(library_id)
    flash(
        'Skip-marker checks will be queued within a minute: a "previously on" '
        'recap, a "next time on" preview, Intro Skipper\'s own markers, and '
        "whether each episode is the one it's filed as. Episodes without a saved "
        "transcript get only the parts that matter transcribed, once the chapter "
        "backlog is done.",
        "success",
    )
    return redirect(url_for("main.chapter_maintenance", library=library_id))


@bp.post("/chapters/upgrade")
def chapter_upgrade():
    """Redo Navigation chapters with Smart Chapters where Smart is the setting."""
    library_id = request.form.get("library") or None
    count = queue_smart_upgrade(library_id)
    flash(
        f"Queued {count} file{'s' if count != 1 else ''} to redo with Smart "
        "Chapters. Each file's own chapters stay until the new ones are written.",
        "success",
    )
    return redirect(url_for("main.chapter_maintenance", library=library_id))


@bp.route("/chapters/libraries/<library_id>/process", methods=["GET", "POST"])
def chapter_confirm_library(library_id):
    """Queue one library's backlog, or (library_id "all") every enabled one's."""
    backlog = _backlog(library_id)
    library, count, name = backlog["library"], backlog["addable"], backlog["name"]
    if request.method == "POST":
        if request.form.get("confirm") != str(count):
            flash(
                "The number of files changed; check the count and confirm again.",
                "warning",
            )
            return redirect(
                url_for("main.chapter_confirm_library", library_id=library_id)
            )
        queued = sum(queue_library(target) for target in backlog["targets"])
        flash(f"Queued {queued} files in {name} for chapters.", "success")
        return redirect(
            url_for("main.chapter_maintenance", library=library.id if library else None)
        )
    return render_template(
        "chapter_confirm.html",
        library=library,
        name=name,
        count=count,
        already_queued=backlog["queued"],
        summary=maintenance_summary(library.id if library else None),
        config=effective_config(
            global_layer(), library_layers().get(library.id) if library else None
        ),
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
        identity_on=identity_check_enabled(),
        generation_version=GENERATION_VERSION,
    )


@bp.post("/chapters/parts/<part_id>/<action>")
def chapter_part_action(part_id, action):
    from .enrichment import request as request_action

    part = db.session.get(MediaPart, part_id)
    if not part or action not in {
        "preview",
        "generate",
        "regenerate",
        "remove",
        "subtitles",
        "remove_subtitles",
        "recap",
    }:
        abort(404)
    state = state_for(part)
    if action == "remove_subtitles" and not state.subtitle_fingerprint:
        flash("Curatarr can only remove subtitles it generated itself.", "danger")
        return redirect(url_for("main.chapter_part", part_id=part.id))
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
                "subtitles": "Subtitle generation queued.",
                "remove_subtitles": "Subtitle removal queued.",
                "recap": "Skip-marker check queued; it starts within 15 seconds.",
            }[action],
            "success",
        )
    return redirect(url_for("main.chapter_part", part_id=part.id))


@bp.get("/chapters/markers")
def chapter_markers():
    """Recent skip-marker changes to spot-check, each with an undo."""
    kind = request.args.get("kind") or None
    if kind not in (None, "recap", "preview", "hidden"):
        abort(404)
    return render_template(
        "chapter_markers.html",
        rows=marker_review(kind),
        kind=kind,
        file_label=file_label,
    )


@bp.post("/chapters/markers/<state_id>/undo")
def chapter_marker_undo(state_id):
    state = db.session.get(ChapterState, state_id)
    if not state:
        abort(404)
    try:
        message = undo_marker(
            state, request.form.get("kind", ""), request.form.get("segment") or None
        )
    except ValueError as exc:
        flash(str(exc), "warning")
    except IntegrationError as exc:
        db.session.rollback()
        flash(f"Intro Skipper couldn't be reached: {exc}", "danger")
    else:
        flash(message, "success")
    return redirect(request.form.get("next") or url_for("main.chapter_markers"))


@bp.get("/chapters/identity")
def chapter_identity():
    """Episodes whose transcript fits another episode, or isn't in English."""
    return render_template(
        "chapter_identity.html",
        states=identity_issues(),
        file_label=file_label,
        enabled=identity_check_enabled(),
    )


@bp.post("/chapters/identity/<state_id>/dismiss")
def chapter_identity_dismiss(state_id):
    state = db.session.get(ChapterState, state_id)
    if not state or not state.identity_issue:
        abort(404)
    dismiss_identity(state)
    flash("Dismissed. This episode won't be flagged again.", "success")
    return _identity_back()


def _identity_back():
    if request.form.get("back") == "overview":
        return redirect(url_for("main.overview"))
    return redirect(url_for("main.chapter_identity"))


@bp.post("/chapters/identity/<state_id>/fix-audio")
def chapter_identity_fix_audio(state_id):
    state = db.session.get(ChapterState, state_id)
    if not state or (state.identity_issue or {}).get("kind") != "audio":
        abort(404)
    try:
        message = fix_default_audio(state)
    except (ValueError, MediaToolError, OSError) as exc:
        db.session.rollback()
        flash(f"Couldn't change the default audio: {exc}", "danger")
    else:
        flash(message, "success")
    return _identity_back()


@bp.get("/chapters/dubs")
def chapter_dubs():
    """Films made in another language: is the English audio really English?"""
    from . import dub_jobs

    return render_template(
        "chapter_dubs.html",
        found=dub_jobs.review(),
        enabled=dub_jobs.enabled(),
        file_label=file_label,
        language=_language_name,
    )


def _language_name(value):
    from .dubs import name

    return name(value)


@bp.post("/chapters/dubs/<state_id>/<action>")
def chapter_dub_action(state_id, action):
    from . import better_copy, dub_jobs

    state = db.session.get(ChapterState, state_id)
    if (
        not state
        or not state.dub_result
        or action not in ("replace", "dismiss", "recheck", "cancel")
    ):
        abort(404)
    try:
        if action == "replace":
            flash(_request_replace(state, "dub"), "success")
        elif action == "cancel":
            better_copy.cancel(state, "dub")
            flash("Cancelled. Nothing was changed.", "success")
        elif action == "dismiss":
            dub_jobs.dismiss(state)
            flash("Dismissed. This film won't be listed again.", "success")
        else:
            dub_jobs.recheck(state)
            flash("It will be listened to again shortly.", "success")
    except (IntegrationError, ValueError) as exc:
        db.session.rollback()
        flash(f"Couldn't do that: {exc}", "danger")
    if request.form.get("back") == "overview":
        return redirect(url_for("main.overview"))
    return redirect(url_for("main.chapter_dubs"))


@bp.post("/chapters/failed/retry-all")
def chapter_failed_retry_all():
    from sqlalchemy.exc import OperationalError

    from .enrichment import retry_all_failed

    try:
        count = retry_all_failed()
    except OperationalError:
        db.session.rollback()
        flash(
            "Curatarr's database is busy right now; nothing was changed. "
            "Try again in a minute.",
            "warning",
        )
        return redirect(url_for("main.chapter_maintenance"))
    flash(
        f"Queued {count} failed job{'s' if count != 1 else ''} again. Files that "
        "still can't be reached wait and retry every ten minutes instead of failing.",
        "success",
    )
    return redirect(url_for("main.chapter_maintenance"))


@bp.post("/chapters/failed/retry-kind/<kind>")
def chapter_failed_retry_kind(kind):
    from sqlalchemy.exc import OperationalError

    from .enrichment import retry_failed_kind

    try:
        count = retry_failed_kind(kind, request.form.get("library") or None)
    except OperationalError:
        db.session.rollback()
        flash(
            "Curatarr's database is busy right now; nothing was changed. "
            "Try again in a minute.",
            "warning",
        )
        return redirect(url_for("main.chapter_maintenance"))
    flash(f"Queued {count} failed job{'s' if count != 1 else ''} again.", "success")
    return redirect(
        url_for("main.chapter_maintenance", library=request.form.get("library") or None)
    )


@bp.post("/chapters/failed/<state_id>/<action>")
def chapter_failed_action(state_id, action):
    """Controls for a failed chapter job: try again, find another copy, ignore."""
    from . import enrichment

    client = enrichment.client
    state = db.session.get(ChapterState, state_id)
    if (
        not state
        or state.chapter_status != "failed"
        or action
        not in {
            "retry",
            "search",
            "ignore",
        }
    ):
        abort(404)
    part, media = state.part, state.media
    try:
        if action == "retry":
            enrichment._retry_failed(state)
            db.session.commit()
            flash(
                f"Trying {file_label(state)} again. If the file has gone, Curatarr "
                "checks with Radarr or Sonarr and waits for the next copy.",
                "success",
            )
        elif action == "search":
            if part.kind == "movie_file" and media.radarr_id:
                client("radarr").search_movie(media.radarr_id)
                flash(f"Radarr is searching for {media.title}.", "success")
            elif part.kind == "episode" and part.sonarr_episode_id:
                client("sonarr").episode_search([part.sonarr_episode_id])
                flash(f"Sonarr is searching for {file_label(state)}.", "success")
            else:
                flash("Radarr and Sonarr don't know this file.", "warning")
        else:
            state.chapter_status, state.chapter_error = "ignored", None
            state.job_state, state.next_attempt_at = "idle", None
            db.session.commit()
            flash(
                f"Ignored. {file_label(state)} won't get chapters unless it's "
                "replaced or you ask from its page.",
                "success",
            )
    except (IntegrationError, ValueError) as exc:
        db.session.rollback()
        flash(f"Couldn't do that: {exc}", "danger")
    return redirect(url_for("main.chapter_maintenance") + "#failed")


@bp.get("/chapters/problems")
def chapter_problems():
    """Burned-in subtitles and broken files (filecheck.py)."""
    from . import (
        autofix,
        default_audio,
        filecheck_jobs,
        misfiled,
        missing_keep,
        seek_repair,
        wrong_version,
    )

    return render_template(
        "chapter_problems.html",
        audio_found=default_audio.found_rows(),
        seek_found=seek_repair.found_rows(),
        misfiled=[r for r in misfiled.rows() if r["plan"] != "wait"],
        misfiled_waiting=[r for r in misfiled.rows() if r["plan"] == "wait"],
        misfiled_auto=autofix.automatic("misfiled"),
        missing_shows=missing_keep.rows(),
        wrong_shows=wrong_version.rows(),
        found=filecheck_jobs.review(),
        enabled=filecheck_jobs.enabled(),
        describe=filecheck_jobs.describe,
        file_label=file_label,
    )


@bp.post("/chapters/problems/<kind>/<state_id>/<action>")
def found_file_action(kind, state_id, action):
    """Fix, or Leave it, a file found playing an extra first ("audio") or that
    Roku can't seek in ("seek"); state_id "all" with Fix does every one."""
    from . import default_audio, seek_repair

    module = {"audio": default_audio, "seek": seek_repair}.get(kind)
    if module is None or action not in ("fix", "dismiss"):
        abort(404)
    try:
        if state_id == "all" and action == "fix":
            count = module.approve_all()
            flash(
                f"{count} file{'' if count == 1 else 's'} will be fixed shortly.",
                "success",
            )
        elif action == "fix":
            module.approve(state_id)
            flash("It will be fixed shortly, in the background.", "success")
        else:
            module.dismiss(state_id)
            flash("Left as it is. It won't be listed again.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(f"Couldn't do that: {exc}", "danger")
    return redirect(url_for("main.chapter_problems") + f"#found-{kind}")


def _request_replace(target, kind) -> str:
    """Queue a Replace once: a second click while it's being looked into, or
    after no better copy was found, changes nothing."""
    from . import better_copy

    current = better_copy.status(target, kind)
    if current == "pending":
        return "Already looking for a better copy."
    if current == "no_better":
        return "No better copy was available when Curatarr last looked."
    return better_copy.request(target, kind)


@bp.post("/chapters/problems/misfiled/<int:file_id>/<action>")
def misfiled_action(file_id, action):
    """Move a misfiled episode in Sonarr, or say it's filed right."""
    from . import misfiled

    if action not in ("fix", "dismiss"):
        abort(404)
    try:
        if action == "fix":
            mode = request.form.get("mode") or None
            if mode not in (None, "move", "replace", "discard"):
                abort(400)
            flash(misfiled.fix(file_id, mode=mode), "success")
        else:
            misfiled.dismiss(file_id)
            flash("Kept where it is. It won't be listed again.", "success")
    except (IntegrationError, ValueError) as exc:
        db.session.rollback()
        flash(f"Couldn't do that: {exc}", "danger")
    if request.form.get("back") == "overview":
        return redirect(url_for("main.overview"))
    return redirect(url_for("main.chapter_problems") + "#misfiled")


@bp.post("/chapters/problems/version/<media_id>/<action>")
def wrong_version_action(media_id, action):
    """Replace a show's files that are another show, or say it's the right one."""
    from . import better_copy, wrong_version

    media = db.session.get(MediaIdentity, media_id)
    if not media or action not in ("replace", "dismiss", "cancel"):
        abort(404)
    try:
        if action == "replace":
            flash(_request_replace(media, "version"), "success")
        elif action == "cancel":
            better_copy.cancel(media, "version")
            flash("Cancelled. Nothing was changed.", "success")
        else:
            wrong_version.dismiss(media)
            flash(
                f"Kept. {media.title} won't be listed unless its files change.",
                "success",
            )
    except (IntegrationError, ValueError) as exc:
        db.session.rollback()
        flash(f"Couldn't do that: {exc}", "danger")
    if request.form.get("back") == "overview":
        return redirect(url_for("main.overview"))
    return redirect(url_for("main.chapter_problems"))


@bp.post("/chapters/problems/show/<media_id>/<action>")
def missing_keep_action(media_id, action):
    """Search once for a show's missing always-keep episodes, or stop listing it."""
    from . import missing_keep

    media = db.session.get(MediaIdentity, media_id)
    if not media or action not in ("search", "ignore"):
        abort(404)
    if action == "ignore":
        missing_keep.ignore(media)
        db.session.commit()
        flash(f"{media.title} won't be listed for these episodes again.", "success")
    else:
        try:
            result = missing_keep.search_once(media)
        except ValueError as exc:
            flash(str(exc), "info")
        else:
            if result == "succeeded":
                flash(
                    f"Asked Sonarr to search for {media.title}'s missing episodes. "
                    "If nothing arrives within a couple of days, it's listed here again.",
                    "success",
                )
            else:
                flash(
                    f"Sonarr couldn't be asked ({result}). Try again shortly.", "danger"
                )
    if request.form.get("back") == "overview":
        return redirect(url_for("main.overview"))
    return redirect(url_for("main.chapter_problems"))


@bp.post("/chapters/problems/<state_id>/<action>")
def chapter_problem_action(state_id, action):
    from . import better_copy, filecheck_jobs

    state = db.session.get(ChapterState, state_id)
    if (
        not state
        or not state.file_check
        or action not in ("replace", "dismiss", "recheck", "cancel")
    ):
        abort(404)
    try:
        if action == "replace":
            flash(_request_replace(state, "file"), "success")
        elif action == "cancel":
            better_copy.cancel(state, "file")
            flash("Cancelled. Nothing was changed.", "success")
        elif action == "dismiss":
            filecheck_jobs.dismiss(state)
            flash("Kept. This file won't be listed again.", "success")
        else:
            filecheck_jobs.recheck(state)
            flash("It will be checked again shortly.", "success")
    except (IntegrationError, ValueError) as exc:
        db.session.rollback()
        flash(f"Couldn't do that: {exc}", "danger")
    if request.form.get("back") == "overview":
        return redirect(url_for("main.overview"))
    return redirect(url_for("main.chapter_problems"))


@bp.get("/search")
def search():
    """One search box: titles by name, and episodes and films by what was said."""
    query = (request.args.get("q") or "").strip()[:200]
    titles = []
    if query:
        titles = (
            db.session.query(MediaIdentity)
            .join(Library)
            .filter(
                Library.media_type.in_(("tv", "movies")),
                MediaIdentity.title.ilike(f"%{query}%"),
            )
            .order_by(MediaIdentity.title)
            .limit(12)
            .all()
        )
    return render_template(
        "search.html",
        query=query,
        titles=titles,
        searchable=transcripts_searchable(),
    )


@bp.get("/search/said")
def search_said():
    """The search page's "What was said", fetched after the titles are shown:
    the transcript search is what kept the whole page waiting."""
    query = (request.args.get("q") or "").strip()[:200]
    return render_template(
        "search_said.html",
        results=search_transcripts(query, 15) if query else [],
        titles_found=request.args.get("titles", "0") not in ("", "0"),
    )


@bp.get("/transcripts")
def transcript_search():
    """Find an episode by something said in it."""
    query = (request.args.get("q") or "").strip()[:200]
    return render_template(
        "transcript_search.html",
        query=query,
        results=search_transcripts(query, 25) if query else [],
        searchable=transcripts_searchable(),
    )


def _thumbnail_status():
    from . import thumbnails

    return thumbnails.status()


def _seek_progress():
    from . import seek_repair

    return seek_repair.progress()


def _compat_progress():
    from . import compat_audio

    return compat_audio.progress()
