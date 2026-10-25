"""Server-rendered UI and narrow operational API."""

import hashlib
import hmac
import secrets
from io import BytesIO

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from markupsafe import Markup
from sqlalchemy import func

from . import better_copy, db, icons, jellyfin_links
from .auth import (
    SignInError,
    current_user,
    household_user_ids,
    needs_server_url,
    sign_in,
    sign_out,
)
from .integrations import CLIENTS, IntegrationError
from .lifecycle import evaluate_retention, review_candidate
from .models import (
    Library,
    LifecycleAction,
    MediaIdentity,
    PurgeCandidate,
    utcnow,
)
from .policy import effective_policy
from .services import (
    RoutineWebhookEvent,
    UnsupportedWebhookEvent,
    _as_datetime,
    action_context,
    api_key_valid,
    dry_run_by_library,
    history_actions,
    history_filter_options,
    ingest_event,
    integrations_ready,
    media_titles_for,
    metrics_summary,
    operational_status,
    overview_data,
    process_pending_events,
    reconciliation_state,
    record_webhook,
    request_reconciliation,
    request_title_now,
    review_rows,
    revoke_api_key,
    rotate_api_key,
    rotate_webhook_token,
    save_integration,
    save_policy_layer,
    save_reconcile_interval,
    save_title_override,
    set_setting,
    setting,
    settings_view,
    setup_progress,
    title_view,
)

bp = Blueprint("main", __name__)


@bp.after_app_request
def security_headers(response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' https://cdn.jsdelivr.net; "
        "img-src 'self' data:; object-src 'none'; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# Calls from Jellyfin (the Webhook plugin, the Curatarr plugin) carry the
# webhook token instead of a sign-in.
JELLYFIN_PATHS = {"/api/v1/webhook/jellyfin", "/api/v1/jellyfin/delete"}


@bp.before_app_request
def guard_request():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    if request.path in JELLYFIN_PATHS:
        token = setting("webhook_token")
        if not token or not hmac.compare_digest(
            request.headers.get("X-Curatarr-Token", ""), token
        ):
            record_webhook(
                "rejected",
                reason="No webhook token has been created yet"
                if not token
                else "The X-Curatarr-Token header was missing or wrong",
            )
            abort(403)
        return
    if request.content_type and request.content_type.startswith("application/json"):
        abort(415)
    if request.endpoint in UPLOAD_ENDPOINTS:
        request.max_content_length = UPLOAD_LIMIT  # pictures; 1 MB elsewhere
    if current_app.config["CSRF_ENABLED"]:
        supplied = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            abort(403)


UPLOAD_ENDPOINTS: set[str] = set()  # pages that take a file (poster_routes)
UPLOAD_LIMIT = 25 * 1024 * 1024

PUBLIC_ENDPOINTS = {
    "main.sign_in_page",
    "main.health",
    "main.api_status",
    "main.webhook",
    "main.jellyfin_delete",
    "static",
}


@bp.before_app_request
def require_sign_in():
    if current_app.config["ALLOW_UNAUTHENTICATED"]:
        return None
    if request.endpoint in PUBLIC_ENDPOINTS or current_user():
        return None
    # Other tools (a dashboard, Homepage) read status and history with the
    # API key from Settings. It only ever grants these read-only GETs.
    if (
        request.method == "GET"
        and request.path.startswith("/api/v1/")
        and api_key_valid(request.headers.get("X-Api-Key"))
    ):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Sign-in required"}), 401
    target = request.full_path.rstrip("?") if request.method == "GET" else None
    return redirect(url_for("main.sign_in_page", next=target))


HOUSEHOLD_PAGES = {
    "main.overview",
    "main.search",
    "main.more",
    "main.review",
    "main.shows",
    "main.titles",
    "main.title",
    "main.transcript_search",
    "main.poster",
    "main.review_action",
    "main.theme_preference",
    "main.sign_out_page",
    "main.sign_in_page",
}


@bp.before_app_request
def authorize_household():
    if current_app.config["ALLOW_UNAUTHENTICATED"]:
        return
    user = current_user()
    if not user or user.get("role") != "household":
        return
    if request.endpoint in PUBLIC_ENDPOINTS:
        return
    if request.endpoint not in HOUSEHOLD_PAGES:
        abort(403)
    if request.endpoint == "main.title" and request.method != "GET":
        abort(403)
    if request.endpoint == "main.review_action" and (
        request.view_args.get("choice")
        not in {"keep", "snooze_30", "snooze_90", "never_purge"}
    ):
        abort(403)


def _safe_next(value):
    if (
        value
        and value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
    ):
        return value
    return url_for("main.overview")


@bp.route("/login", methods=["GET", "POST"])
def sign_in_page():
    if request.method == "POST":
        try:
            sign_in(
                request.form.get("username", "").strip(),
                request.form.get("password", ""),
                request.remote_addr or "unknown",
                request.form.get("server_url"),
            )
        except SignInError as exc:
            flash(str(exc), "danger")
            return redirect(
                url_for("main.sign_in_page", next=request.args.get("next")), 303
            )
        return redirect(_safe_next(request.args.get("next")), 303)
    return render_template(
        "login.html",
        needs_server_url=needs_server_url(),
        demo_mode=current_app.config["DEMO_MODE"],
    )


@bp.post("/logout")
def sign_out_page():
    sign_out()
    return redirect(url_for("main.sign_in_page"), 303)


@bp.post("/preferences/theme")
def theme_preference():
    theme = request.form.get("theme")
    if theme not in {"light", "dark"}:
        abort(400)
    response = redirect(_safe_next(request.form.get("back")), 303)
    response.set_cookie("curatarr_theme", theme, max_age=31536000, samesite="Lax")
    return response


CHOICE_LABELS = {
    "first_n_episodes": "First N episodes of Season 1",
    "season_1": "All of Season 1",
    "entire_series": "Entire series (never trimmed)",
    "recommend": "Recommend only",
    "require_review": "Require review",
    "automatic": "Automatic",
    "manual_forever": "Wait for a decision",
    "auto_delete_after_notice": "Delete after the notice period",
    "oldest_unwatched_first": "Oldest unwatched first",
    "oldest_watched_nonfavorite_first": "Oldest watched non-favorites first",
    "weighted": "Weighted score",
    "true": "Yes",
    "false": "No",
}


WHEN_FORMATS = {
    "date": "%Y-%m-%d",
    "time": "%H:%M",
    "seconds": "%H:%M:%S",
    "datetime": "%Y-%m-%d %H:%M",
}


@bp.app_template_filter("when")
def when(value, style="datetime"):
    """A stored UTC time, shown in the viewer's time zone by local-time.js.

    The UTC text inside the element is what shows if scripts do not run.
    """
    moment = _as_datetime(value)
    if moment is None:
        return ""
    text = moment.strftime(WHEN_FORMATS[style]) + ("" if style == "date" else " UTC")
    return Markup(
        f'<time datetime="{moment.isoformat()}" data-style="{style}">{text}</time>'
    )


@bp.app_template_filter("commas")
def commas(value):
    """An integer count with thousands separators (e.g. 2857 -> "2,857")."""
    if value is None:
        return value
    return f"{value:,}"


@bp.app_template_filter("size")
def size_label(value):
    """Bytes as MB, GB, or TB (binary, matching Sonarr/Radarr/Jellyfin)."""
    from .policy import SIZE_UNITS, best_size_unit

    if value is None:
        return "not set"
    unit = best_size_unit(value)
    amount = value / SIZE_UNITS[unit]
    return f"{amount:.{2 if unit == 'TB' else 1 if unit == 'GB' else 0}f} {unit}"


@bp.app_template_filter("size_amount")
def size_amount(value):
    """A byte count in its best unit, trimmed for an input box (e.g. 2.5)."""
    from .policy import SIZE_UNITS, best_size_unit

    if value is None:
        return ""
    return f"{value / SIZE_UNITS[best_size_unit(value)]:.3f}".rstrip("0").rstrip(".")


@bp.app_template_filter("size_unit")
def size_unit(value):
    from .policy import best_size_unit

    return best_size_unit(value)


@bp.app_template_filter("choice_label")
def choice_label(value):
    """Readable text for stored policy values (enums and booleans)."""
    if value is None:
        return "not set"
    key = str(value).lower()
    return CHOICE_LABELS.get(key, str(value).replace("_", " "))


NAV_SECTIONS = {
    "main.overview": "overview",
    "main.review": "review",
    "main.review_delete_confirm": "review",
    "main.titles": "library",
    "main.title": "library",
    "main.title_reset": "library",
    "main.title_remove": "library",
    "main.free_space": "library",
    "main.shows": "library",
    "main.reset_unwatched": "library",
    "main.search": "library",
    "main.history": "activity",
    "main.history_detail": "activity",
    "main.settings": "settings",
    "main.setup": "settings",
    "main.library_policy": "settings",
    "main.cleanup_preview": "settings",
    "main.operations": "settings",
    "main.more": "more",
}

# Pages where the full dry-run explanation belongs; elsewhere the header pill says it.
DRY_RUN_BANNER_PAGES = {
    "main.review",
    "main.review_delete_confirm",
    "main.cleanup_preview",
    "main.library_policy",
}


def _work_paused():
    if request.endpoint in (None, "static") or request.path.startswith("/api/"):
        return None
    from . import pause

    return pause.state()


@bp.post("/pause")
def pause_work():
    """Pause all of Curatarr's work for a while, to free the machine."""
    from . import pause

    hours = request.form.get("hours", "")
    if hours not in {"1", "4", "0"}:
        abort(400)
    user = (session.get("user") or {}).get("name")
    pause.pause(int(hours) or None, by=user)
    flash(
        "Paused. Running jobs stop and go back in the queue within 15 seconds, "
        "and the language model is unloaded from the graphics card.",
        "success",
    )
    return redirect(_safe_next(request.form.get("next")))


@bp.post("/resume")
def resume_work():
    from . import pause

    pause.resume()
    flash("Resumed. Queued work starts again within 15 seconds.", "success")
    return redirect(_safe_next(request.form.get("next")))


@bp.post("/jellyfin-plugin/update")
def jellyfin_plugin_update():
    """The plugin update banner's buttons."""
    from . import jellyfin_plugin

    action = request.form.get("action")
    try:
        if action == "update":
            jellyfin_plugin.update()
            flash(
                "Jellyfin downloaded the plugin update. It loads when Jellyfin "
                "restarts.",
                "success",
            )
        elif action in {"restart", "restart_idle"}:
            jellyfin_plugin.restart(when_idle=action == "restart_idle")
            flash(
                f"Jellyfin restarts once nobody's watching (after "
                f"{jellyfin_plugin.IDLE_MINUTES} quiet minutes) and loads the update."
                if action == "restart_idle"
                else "Jellyfin is restarting to load the update; it's back in "
                "a minute or so.",
                "success",
            )
        elif action == "cancel_idle":
            jellyfin_plugin.cancel_idle_restart()
            flash("Jellyfin won't be restarted by Curatarr.", "success")
        else:
            abort(400)
    except IntegrationError as exc:
        flash(f"Jellyfin couldn't do that: {exc}", "danger")
    return redirect(_safe_next(request.form.get("next")))


def action_name(action):
    """What a history entry did, in words. Cleanup of a series never deletes
    the show: it removes the later episodes and keeps the first ones."""
    if action.action_type in {"delete_media", "would_delete"}:
        media = (
            db.session.get(MediaIdentity, action.media_identity_id)
            if action.media_identity_id
            else None
        )
        series = media is not None and media.media_type == "series"
        if action.action_type == "would_delete":
            return "Dry run: would reset" if series else "Dry run: would delete"
        return "Reset to first episodes" if series else "Deleted"
    return (
        ACTION_WORDS.get(action.action_type)
        or action.action_type.replace("_", " ").title()
    )


ACTION_WORDS = {
    "auto_fix_started": "Searched automatically",
    "auto_fix_gave_up": "Couldn't be fixed; stopped trying",
    "shrunk": "Shrunk to a smaller copy",
    "seasonal_returned": "Brought back for its season",
    "compat_audio_added": "Added TV-friendly audio",
    "finished_at_credits": "Marked watched at the credits",
    "custom_poster_applied": "Custom poster put on",
    "custom_poster_restored": "Original poster restored",
}


def _nav_section():
    """Which top-level navigation entry the current page belongs to."""
    if request.endpoint == "main.rules":
        return "settings"
    return NAV_SECTIONS.get(request.endpoint)


@bp.app_context_processor
def template_values():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    signed_in_user = session.get("user")
    is_admin = current_app.config["ALLOW_UNAUTHENTICATED"] or bool(
        signed_in_user and signed_in_user.get("role", "admin") == "admin"
    )
    return {
        "csrf_token": session["csrf_token"],
        "nav_section": _nav_section(),
        "dry_run_split": dry_run_by_library(),
        "signed_in_user": signed_in_user,
        "is_admin": is_admin,
        "icon": icons.icon,
        "jellyfin_link": jellyfin_links.link,
        "jellyfin_link_for": jellyfin_links.link_for,
        "action_name": action_name,
        "better_copy_status": better_copy.status,
        "better_copy_checked": better_copy.checked_at,
        "jellyfin_part_link": jellyfin_links.part_link,
        "jellyfin_url": jellyfin_links.item_url,
        "work_paused": _work_paused() if is_admin else None,
        "plugin_notice": _plugin_notice() if is_admin else None,
        "dry_run_banner": request.endpoint in DRY_RUN_BANNER_PAGES
        or (
            request.endpoint == "main.rules"
            and (request.view_args or {}).get("section") == "retention"
        ),
        "nav_counts": _nav_counts(signed_in_user, is_admin),
        "theme": request.cookies.get("curatarr_theme")
        if request.cookies.get("curatarr_theme") in {"light", "dark"}
        else "dark",
    }


def _plugin_notice():
    if request.endpoint in (None, "static") or request.path.startswith("/api/"):
        return None
    from sqlalchemy.exc import OperationalError

    from . import jellyfin_plugin

    try:
        return jellyfin_plugin.notice()
    except OperationalError:
        return None


def _nav_counts(signed_in_user, is_admin) -> dict:
    """Small attention badges for the navigation (one cheap query each)."""
    if not (signed_in_user or current_app.config["ALLOW_UNAUTHENTICATED"]):
        return {}
    if request.endpoint in (None, "static") or request.path.startswith("/api/"):
        return {}
    counts = {
        "review": db.session.query(PurgeCandidate)
        .filter(PurgeCandidate.state == "REVIEW")
        .count()
    }
    if is_admin and _nav_section() == "enhance":
        from . import dub_jobs
        from .enrichment import maintenance_failed_count

        counts["dubs"] = dub_jobs.counts()["dubs_to_act"]
        from . import filecheck_jobs, misfiled, missing_keep, wrong_version

        counts["problems"] = (
            filecheck_jobs.counts()["problems_to_act"]
            + missing_keep.count()
            + wrong_version.count()
            + misfiled.count()
        )
        counts["failed"] = maintenance_failed_count()
        from .enrichment import identity_issues

        counts["identity"] = len(identity_issues())
    return counts


def _dry_run_status():
    """True only when no library can delete (every library in dry run)."""
    return not dry_run_by_library()["deleting"]


def _status():
    return operational_status(demo_mode=current_app.config["DEMO_MODE"])


@bp.get("/health")
def health():
    try:
        db.session.execute(db.select(func.count()).select_from(Library)).scalar()
        database = "healthy"
    except Exception:
        current_app.logger.exception("Database health check failed")
        database = "unhealthy"
    if database != "healthy":
        return jsonify(
            {
                "application": "healthy",
                "database": database,
                "integrations": "unknown",
                "scheduler": "unknown",
            }
        ), 503
    from .services import _as_datetime

    last_heartbeat = _as_datetime(setting("worker_heartbeat_at"))
    worker = (
        "healthy"
        if last_heartbeat and (utcnow() - last_heartbeat).total_seconds() < 60
        else "offline"
    )
    if worker == "offline" and reconciliation_state()["state"] == "running":
        # The heartbeat is written between cycles; a long reconciliation is busy.
        worker = "busy"
    result = {
        "application": "healthy",
        "database": database,
        "integrations": _status()["integrations"],
        "scheduler": worker,
    }
    return jsonify(result), 200 if database == "healthy" else 503


@bp.get("/api/v1/transcripts/search")
def api_transcript_search():
    """Episodes by what's said in them (read-only): ?q=words&limit=10."""
    from .enrichment import search_transcripts

    query = (request.args.get("q") or "").strip()[:200]
    try:
        limit = max(1, min(int(request.args.get("limit", 10)), 50))
    except ValueError:
        limit = 10
    results = search_transcripts(query, limit) if query else []
    for row in results:
        row.pop("marked", None)  # the page's highlighting; plain lines suffice here
    return jsonify({"query": query, "results": results})


@bp.get("/api/v1/identity-flags")
def api_identity_flags():
    """Episodes whose transcript fits another episode (read-only)."""
    from .enrichment import identity_flags_api

    return jsonify({"flags": identity_flags_api()})


@bp.get("/api/v1/status")
def api_status():
    return jsonify(_status())


@bp.get("/api/v1/metrics")
def api_metrics():
    return jsonify(metrics_summary())


@bp.get("/api/v1/libraries")
def api_libraries():
    return jsonify(
        [
            {
                "id": row.id,
                "name": row.name,
                "media_type": row.media_type,
                "size_bytes": row.last_size_bytes,
                "enabled": row.enabled,
            }
            for row in db.session.query(Library).all()
        ]
    )


@bp.get("/api/v1/review/summary")
def api_review_summary():
    return jsonify(
        {
            "review": _status()["review_count"],
            "leaving_soon": _status()["leaving_soon_count"],
        }
    )


@bp.get("/api/v1/history")
def api_history():
    """Newest first. ``type`` and ``exclude`` (repeatable) filter by action type."""
    limit = min(max(request.args.get("limit", 50, type=int), 1), 200)
    query = db.session.query(LifecycleAction)
    if request.args.getlist("type"):
        query = query.filter(
            LifecycleAction.action_type.in_(request.args.getlist("type"))
        )
    if request.args.getlist("exclude"):
        query = query.filter(
            LifecycleAction.action_type.notin_(request.args.getlist("exclude"))
        )
    rows = query.order_by(LifecycleAction.created_at.desc()).limit(limit).all()
    ids = {row.media_identity_id for row in rows if row.media_identity_id}
    media = {
        item.id: item
        for item in db.session.query(MediaIdentity).filter(MediaIdentity.id.in_(ids))
    }
    libraries = {row.id: row.name for row in db.session.query(Library)}

    def describe(row):
        item = media.get(row.media_identity_id)
        return {
            "id": row.id,
            "type": row.action_type,
            "label": row.action_type.replace("_", " ").capitalize(),
            "state": row.state,
            "reason": row.reason_text,
            "created_at": _as_datetime(row.created_at).isoformat(),
            "completed_at": (
                _as_datetime(row.completed_at).isoformat() if row.completed_at else None
            ),
            "title": item.title if item else None,
            "media_type": item.media_type if item else None,
            "library": libraries.get(item.library_id) if item else None,
            "jellyfin_item_id": item.jellyfin_id if item else None,
            # Paths on this Curatarr server; join them to its address.
            "path": url_for("main.history_detail", action_id=row.id),
            "title_path": url_for("main.title", media_id=item.id) if item else None,
        }

    return jsonify([describe(row) for row in rows])


@bp.get("/")
def overview():
    setup_incomplete = not current_app.config["DEMO_MODE"] and any(
        not step["done"] and not step.get("optional") for step in setup_progress()
    )
    status = _status()
    data = overview_data()
    admin = current_app.config["ALLOW_UNAUTHENTICATED"] or (
        (session.get("user") or {}).get("role", "admin") == "admin"
    )
    return render_template(
        "overview.html",
        status=status,
        metrics=metrics_summary(),
        reconciliation=reconciliation_state(),
        setup_incomplete=setup_incomplete,
        demo_mode=current_app.config["DEMO_MODE"],
        needs=_needs_you(status, data["scheduled"], setup_incomplete, admin),
        care=_library_care() if admin else None,
        handled=_handled_summary() if admin else None,
        leaving=_leaving_summary(data["scheduled"]),
        **data,
    )


def _leaving_summary(_scheduled=None):
    """How many titles are Leaving Soon, how much they free, and the first date.
    All of them: Overview's upcoming list stops at five."""
    leaving = (
        db.session.query(PurgeCandidate)
        .filter(PurgeCandidate.state == "LEAVING_SOON")
        .all()
    )
    if not leaving:
        return None
    dates = [c.scheduled_delete_at for c in leaving if c.scheduled_delete_at]
    return {
        "count": len(leaving),
        "bytes": sum(c.reclaimable_bytes or 0 for c in leaving),
        "first": min(dates) if dates else None,
    }


def _handled_summary():
    from . import autofix

    return autofix.summary()


NEEDS_SHOWN = 4  # of each kind of problem listed individually on Overview


def _needs_you(status, scheduled, setup_incomplete, admin) -> list[dict]:
    """Everything waiting on someone, newest decisions first, for Overview."""
    # Leaving Soon titles are decided (they go unless someone watches): they
    # get a line of their own under the list (_leaving_summary), not a row here.
    needs = []
    if status["review_count"]:
        needs.append(
            {
                "icon": "review",
                "title": f"{status['review_count']} title"
                + ("s" if status["review_count"] != 1 else "")
                + " waiting for a decision",
                "detail": "Keep, snooze or approve cleanup",
                "href": url_for("main.review"),
            }
        )
    if not admin:
        return needs
    if setup_incomplete:
        needs.insert(
            0,
            {
                "icon": "tools",
                "tone": "accent",
                "title": "Continue setup",
                "detail": "Connect Jellyfin, Sonarr and Radarr, then discover",
                "href": url_for("main.setup"),
            },
        )
    # Not set up is the setup checklist's business (and Sonarr or Radarr may be
    # left out on purpose); this is for ones that stopped answering.
    unhealthy = [
        k
        for k, v in status["integrations"].items()
        if v not in ("healthy", "unconfigured")
    ]
    if unhealthy:
        needs.insert(
            0,
            {
                "icon": "settings",
                "tone": "danger",
                "title": ", ".join(k.title() for k in unhealthy)
                + (" isn't" if len(unhealthy) == 1 else " aren't")
                + " responding",
                "detail": " · ".join(
                    f"{k.title()}: {status['integrations'][k]}" for k in unhealthy
                )
                + ". Check the connection in Settings → Integrations",
                "href": url_for("main.settings"),
            },
        )
    from . import autofix, better_copy, dub_jobs
    from .enrichment import file_label, maintenance_failed_count

    auto = autofix.enabled()

    def handled(status, kind):
        """Being worked on, or about to be: not something that needs you."""
        return status == "pending" or (status is None and auto[kind])

    for state in [
        s
        for s in dub_jobs.review()["to_act"]
        if not handled(better_copy.status(s, "dub"), "dubs")
    ][:NEEDS_SHOWN]:
        needs.append(
            {
                "icon": "audio",
                "tone": "warning",
                "title": file_label(state),
                "detail": "No English audio: replace it with another release?",
                "dub": state.id,
                "jellyfin_media": state.media.id,
                "replace_status": better_copy.status(state, "dub"),
                "checked_at": better_copy.checked_at(state, "dub"),
                "href": url_for("main.chapter_dubs"),
            }
        )
    from . import filecheck_jobs

    for state in [
        s
        for s in filecheck_jobs.review()["to_act"]
        if not handled(better_copy.status(s, "file"), "files")
    ][:NEEDS_SHOWN]:
        needs.append(
            {
                "icon": "subtitles"
                if (state.file_check.get("subtitles") or {}).get("verdict") == "burned"
                else "tools",
                "tone": "warning",
                "title": file_label(state),
                "detail": filecheck_jobs.describe(state.file_check)[0] + ".",
                "problem": state.id,
                "jellyfin_media": state.media.id,
                "replace_status": better_copy.status(state, "file"),
                "checked_at": better_copy.checked_at(state, "file"),
                "href": url_for("main.chapter_problems"),
            }
        )
    from . import missing_keep, wrong_version

    for row in [
        r for r in wrong_version.rows() if not handled(r["status"], "wrong_show")
    ][:NEEDS_SHOWN]:
        needs.append(
            {
                "icon": "tools",
                "tone": "danger",
                "title": row["media"].title,
                "detail": "Looks like another show with the same name: "
                + "; ".join(row["evidence"])
                + ".",
                "wrong_version": row["media"].id,
                "replace_status": row["status"],
                "checked_at": row["checked_at"],
                "href": url_for("main.chapter_problems"),
            }
        )
    for row in [
        r
        for r in missing_keep.rows()
        if r["state"] != "waiting" and not (r["state"] == "new" and auto["missing"])
    ][:NEEDS_SHOWN]:
        needs.append(
            {
                "icon": "tools",
                "tone": "warning",
                "title": row["media"].title,
                "detail": f"Missing {row['codes']}, which it always keeps"
                + (
                    "; Sonarr has the show unmonitored, so nothing will fetch them."
                    if row["unmonitored"]
                    else "."
                ),
                "missing_show": row["media"].id,
                "search_state": row["state"],
                "searched": row["searched"],
                "href": url_for("main.chapter_problems"),
            }
        )
    from . import misfiled

    shows = {}
    for row in misfiled.rows():
        # Fixed by itself (now or once its episode is freed): nothing to do.
        if row["plan"] == "wait" or (row["plan"] and auto["misfiled"]):
            continue
        shows.setdefault(row["series_title"], []).append(row)
    for title, rows_ in list(shows.items())[:NEEDS_SHOWN]:
        codes = ", ".join(r["from_code"] for r in rows_)
        needs.append(
            {
                "icon": "tools",
                "tone": "warning",
                "title": title,
                "detail": f"{codes} look{'s' if len(rows_) == 1 else ''} filed under "
                "the wrong episode in Sonarr.",
                "jellyfin_media": rows_[0]["media"].id if rows_[0]["media"] else None,
                "href": url_for("main.chapter_problems") + "#misfiled",
            }
        )
    from .enrichment import identity_issues

    for state in identity_issues()[:NEEDS_SHOWN]:
        issue = state.identity_issue
        needs.append(
            {
                "icon": "audio" if issue.get("kind") == "audio" else "tools",
                "tone": "warning",
                "title": file_label(state),
                "detail": f"Plays in {issue.get('language')} by default, not English."
                if issue.get("kind") == "audio"
                else f"Filed as E{issue.get('filed_as'):02}, but sounds like "
                f"E{issue.get('fits'):02}. Check it in Sonarr.",
                "identity": state.id,
                "fix_audio": issue.get("kind") == "audio"
                and bool(issue.get("english_track")),
                "jellyfin_media": state.media.id,
                "href": url_for("main.chapter_identity"),
            }
        )
    failed = maintenance_failed_count()
    if failed:
        needs.append(
            {
                "icon": "chapters",
                "tone": "danger",
                "title": f"{failed} chapter job"
                + ("s" if failed != 1 else "")
                + " failed",
                "detail": "See why on Enhance → Progress",
                "href": url_for("main.chapter_maintenance") + "#failed",
            }
        )
    return needs


def _library_care() -> dict | None:
    """Coverage and what's running, for Overview's Library care card."""
    from .enrichment import job_activity, maintenance_summary

    summary = maintenance_summary()
    if not summary["scanned"]:
        return None
    from . import compat_audio, seek_repair

    activity = job_activity()
    return {
        "summary": summary,
        "activity": activity,
        "compat": compat_audio.progress(),
        "seek": seek_repair.progress(),
    }


@bp.get("/operations")
def operations():
    return render_template(
        "operations.html",
        metrics=metrics_summary(),
        reconciliation=reconciliation_state(),
        status=_status(),
        **overview_data(),
    )


@bp.get("/setup")
def setup():
    return render_template(
        "setup.html",
        steps=setup_progress(),
        destructive=dry_run_by_library()["deleting"],
    )


@bp.post("/demo/simulate")
def demo_simulate():
    if not current_app.config["DEMO_MODE"]:
        abort(404)
    from .demo import simulate

    choice = request.form.get("choice", "")
    try:
        simulate(choice)
    except ValueError:
        abort(400)
    flash(f"Simulated: {choice.replace('_', ' ')}", "success")
    return redirect(url_for("main.overview"))


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        kind = request.form.get("kind")
        try:
            if kind in CLIENTS:
                save_integration(
                    kind, request.form.get("base_url"), request.form.get("api_key")
                )
                flash(f"{kind.title()} connected", "success")
            elif kind == "webhook":
                from . import jellyfin_plugin

                own = (request.form.get("webhook_token") or "").strip()
                token = rotate_webhook_token(own or None)
                jellyfin_plugin.configure()  # the plugin sends the token too
                if own:
                    flash("Webhook token saved.", "success")
                else:
                    # Shown once on the next page; redirecting means a reload
                    # can never resubmit the form and create another token.
                    session["new_webhook_token"] = token
            elif kind == "plugin_setup":
                from . import jellyfin_plugin

                outcome = jellyfin_plugin.set_up(
                    _plugin_addresses(), restart=request.form.get("restart") == "1"
                )
                flash(PLUGIN_OUTCOMES[outcome], "success")
            elif kind == "plugin_address":
                from . import jellyfin_plugin

                jellyfin_plugin.set_address(request.form.get("curatarr_url"))
                flash("The plugin reaches Curatarr at that address now.", "success")
            elif kind == "api_key":
                session["new_api_key"] = rotate_api_key()
            elif kind == "api_key_revoke":
                revoke_api_key()
                flash("API key revoked; tools using it can no longer read.", "success")
            elif kind == "scheduler":
                try:
                    minutes = int(request.form.get("interval_minutes", ""))
                except ValueError:
                    minutes = None
                save_reconcile_interval(minutes)
                flash("Reconciliation interval saved", "success")
            elif kind == "household":
                from .services import client as integration_client

                users = integration_client("jellyfin").users()
                if not isinstance(users, list):
                    raise TypeError("Jellyfin did not return a user list.")
                eligible = {
                    str(user["Id"])
                    for user in users
                    if isinstance(user, dict)
                    and user.get("Id")
                    and isinstance(user.get("Policy"), dict)
                    and not user["Policy"].get("IsAdministrator")
                    and not user["Policy"].get("IsDisabled")
                }
                selected = set(request.form.getlist("user_id"))
                if not selected <= eligible:
                    raise ValueError("Select only enabled Jellyfin household users.")
                set_setting("household_user_ids", sorted(selected))
                flash("Household access saved", "success")
        except (ValueError, TypeError, IntegrationError) as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("main.settings"))
    new_token = session.pop("new_webhook_token", None)
    if new_token is None and setting("webhook_token") is None:
        new_token = rotate_webhook_token()
    from .services import client as integration_client

    try:
        users = integration_client("jellyfin").users()
        if not isinstance(users, list):
            raise TypeError("Jellyfin did not return a user list.")
        household_users = [
            user
            for user in users
            if isinstance(user, dict)
            and isinstance(user.get("Policy"), dict)
            and not user["Policy"].get("IsAdministrator")
            and not user["Policy"].get("IsDisabled")
        ]
        household_error = None
    except (IntegrationError, TypeError) as exc:
        household_users, household_error = [], str(exc)
    return render_template(
        "settings.html",
        new_token=new_token,
        new_api_key=session.pop("new_api_key", None),
        api_key_present=bool(setting("api_key_sha256")),
        household_users=household_users,
        household_allowed=household_user_ids(),
        household_error=household_error,
        plugin=_plugin_view(),
        **settings_view(),
    )


PLUGIN_OUTCOMES = {
    "connected": "Done: deletes in Jellyfin go through Curatarr now.",
    "unconnected": "The plugin is installed, but Jellyfin couldn't reach Curatarr "
    "at any address tried. Enter the address below.",
    "restarting": "Jellyfin is installing the plugin and restarting. Curatarr "
    "sets it up as soon as Jellyfin is back, in a minute or so.",
    "installed": "Jellyfin installed the plugin. Restart Jellyfin when it suits "
    "you; Curatarr sets the plugin up as soon as it's back.",
}


def _plugin_addresses():
    """Where Jellyfin might reach Curatarr: as this browser does, and locally
    (each is tried through the plugin, from Jellyfin's side)."""
    import socket
    from urllib.parse import urlsplit

    here = request.host_url.rstrip("/")
    port = urlsplit(here).port or 8787
    return [
        here,
        f"http://127.0.0.1:{port}",
        f"http://{socket.gethostname()}:{port}",
    ]


def _plugin_view():
    from . import jellyfin_plugin

    view = jellyfin_plugin.status()
    view["suggested_url"] = view.get("curatarr_url") or request.host_url.rstrip("/")
    if view["state"] in {"not_installed", "restart"}:
        view["watching"] = jellyfin_plugin.watching()
    return view


def _queue_reconciliation():
    if not integrations_ready():
        flash(
            "Add the Jellyfin, Sonarr, and Radarr API keys in Settings first.",
            "warning",
        )
    else:
        request_reconciliation()
        flash(
            "Discovery and reconciliation queued. The background worker starts within "
            "15 seconds and a large library can take a few minutes; the status above "
            "updates automatically.",
            "success",
        )
    return redirect(url_for("main.overview"))


@bp.post("/discover")
def discover_route():
    if not current_app.config["DEMO_MODE"]:
        return _queue_reconciliation()
    from .demo import seed_demo

    count = 3 if seed_demo() else 0
    flash(f"Discovered {count} Jellyfin libraries", "success")
    return redirect(url_for("main.overview"))


@bp.post("/reconcile")
def reconcile_route():
    if not current_app.config["DEMO_MODE"]:
        return _queue_reconciliation()
    process_pending_events()
    from .artwork import reconcile_artwork
    from .lifecycle import expire_snoozes

    expire_snoozes()
    count = evaluate_retention()
    reconcile_artwork()
    flash(f"Reconciliation created {count} candidates", "success")
    return redirect(url_for("main.overview"))


@bp.route("/api/v1/webhook/jellyfin", methods=["POST"])
def webhook():
    # The Jellyfin Webhook plugin does not always label its body as JSON, so parse
    # it regardless; the shared token was already checked in guard_request.
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        record_webhook("rejected", reason="The body was not a JSON object")
        abort(400)
    try:
        event, created = ingest_event(payload)
    except RoutineWebhookEvent as exc:
        # Expected every few seconds during playback; not worth a warning.
        return jsonify({"accepted": False, "ignored": str(exc)}), 202
    except UnsupportedWebhookEvent as exc:
        # Acknowledge so the plugin does not report errors for extra types.
        record_webhook("ignored", reason=str(exc))
        return jsonify({"accepted": False, "ignored": str(exc)}), 202
    except (ValueError, TypeError) as exc:
        record_webhook("rejected", reason=str(exc))
        abort(400)
    record_webhook("accepted", event=event)
    return jsonify({"event_id": event.id, "accepted": created}), 202


@bp.route("/api/v1/jellyfin/delete", methods=["POST"])
def jellyfin_delete():
    """The Curatarr plugin for Jellyfin asks before Jellyfin deletes a title
    (token checked in guard_request)."""
    from . import jellyfin_delete as deletes

    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        abort(400)
    if payload.get("check"):
        return jsonify({"ok": True})  # the plugin's Test button: address and token work
    status, body = deletes.handle(
        payload.get("itemId"), payload.get("userName"), payload.get("mode")
    )
    if body.get("result") in {"leaving_soon", "deleting"}:
        from .artwork import reconcile_artwork

        reconcile_artwork()  # badge the poster now, not on the next worker pass
    return jsonify(body), status


@bp.get("/review")
def review():
    return render_template("review.html", rows=review_rows())


@bp.get("/review/<candidate_id>/delete")
def review_delete_confirm(candidate_id):
    candidate = db.session.get(PurgeCandidate, candidate_id)
    if not candidate or candidate.state not in {"REVIEW", "LEAVING_SOON", "SNOOZED"}:
        abort(404)
    return render_template("review_delete_confirm.html", candidate=candidate)


@bp.get("/shows")
def shows():
    from .services import shows_by_size

    library_id = request.args.get("library") or None
    rows = shows_by_size(library_id)
    libraries = (
        db.session.query(Library)
        .filter(Library.media_type == "tv")
        .order_by(Library.name)
        .all()
    )
    from .services import favorited_by

    return render_template(
        "shows.html",
        rows=rows,
        favorites=favorited_by(r["media"].id for r in rows),
        libraries=libraries,
        library_id=library_id,
        total_on_disk=sum(r["on_disk"] for r in rows),
        total_frees=sum(r["frees"] for r in rows),
    )


FREE_SPACE_PAGE = 50


@bp.get("/free-space")
def free_space():
    """Mark films and shows Leaving Soon by hand, sorted and filtered."""
    from . import curation
    from .services import (
        FREE_SPACE_SORTS,
        active_viewers,
        free_space_rows,
        free_space_view,
    )

    args = request.args
    view = free_space_view((current_user() or {}).get("id"), args)
    viewers = active_viewers()
    kind = view.get("type") if view.get("type") in {"movie", "series"} else "all"
    watched = view.get("watched") or "any"
    viewer = watched.removeprefix("user:") if watched.startswith("user:") else None
    if viewer and viewer not in {user_id for user_id, _name in viewers}:
        viewer = None  # no longer active: fall back to everyone
    if not viewer and watched not in {"watched", "unwatched"}:
        watched = "any"
    sort = view.get("sort") if view.get("sort") in FREE_SPACE_SORTS else "size"
    library_id = view.get("library") or None
    rows = free_space_rows(
        kind=kind,
        watched="any" if viewer else watched,
        sort=sort,
        library_id=library_id,
        viewer=viewer,
    )
    pages = max((len(rows) + FREE_SPACE_PAGE - 1) // FREE_SPACE_PAGE, 1)
    page = min(max(args.get("page", 1, type=int), 1), pages)
    shown = rows[(page - 1) * FREE_SPACE_PAGE : page * FREE_SPACE_PAGE]
    for row in shown:
        found = curation.signals(row["media"])
        row["held"] = _held_note(found) if found["request"] or found["season"] else None
    leaving = (
        db.session.query(PurgeCandidate)
        .filter(PurgeCandidate.state == "LEAVING_SOON")
        .all()
    )
    return render_template(
        "free_space.html",
        rows=shown,
        count=len(rows),
        total_frees=sum(r["frees"] for r in rows),
        page=page,
        pages=pages,
        kind=kind,
        watched=f"user:{viewer}" if viewer else watched,
        viewers=viewers,
        sort=sort,
        sorts=FREE_SPACE_SORTS,
        library_id=library_id,
        libraries=db.session.query(Library)
        .filter(Library.media_type.in_(("tv", "movies")))
        .order_by(Library.name)
        .all(),
        leaving_count=len(leaving),
        leaving_bytes=sum(c.reclaimable_bytes or 0 for c in leaving),
    )


def _held_note(found):
    if found["request"]:
        who = found["request"].get("user") or "Someone"
        return f"{who} requested it and hasn't watched it yet"
    return f"Held for {found['season']} season"


@bp.post("/free-space/leaving-soon")
def free_space_mark():
    from .artwork import reconcile_artwork
    from .lifecycle import request_removals

    ids = request.form.getlist("media_id")
    media_rows = [m for m in (db.session.get(MediaIdentity, i) for i in ids) if m]
    back = request.form.get("back") or url_for("main.free_space")
    if not media_rows:
        flash("Tick the titles to mark Leaving Soon first.", "warning")
        return redirect(_safe_next(back))
    marked, failed = request_removals(media_rows, where="the Free up space page")
    if marked:
        reconcile_artwork()
        freed = sum(c.reclaimable_bytes or 0 for c in marked)
        first = min(c.scheduled_delete_at for c in marked)
        flash(
            f"Marked {len(marked)} title{'' if len(marked) == 1 else 's'} Leaving Soon "
            f"({size_label(freed)}). Their posters say so in Jellyfin; each goes on "
            f"{first:%B %-d} unless someone plays any of it or you choose Keep.",
            "info",
        )
    if failed:
        flash(
            "Not marked: " + "; ".join(why for _media, why in failed),
            "warning",
        )
    return redirect(_safe_next(back))


def _reset_plan_signature(plan, skip_days):
    """Fingerprint of the exact shows on the confirmation page, so a list that changed
    between viewing and confirming is refused instead of resetting something unseen."""
    ids = ",".join(sorted(str(r["media"].id) for r in plan))
    return hashlib.sha256(f"{skip_days}|{ids}".encode()).hexdigest()[:16]


@bp.route("/shows/reset-unwatched", methods=["GET", "POST"])
def reset_unwatched():
    from .lifecycle import reset_many
    from .services import unwatched_reset_plan

    try:
        skip_days = min(max(int(request.values.get("skip_days", 30)), 0), 3650)
    except ValueError:
        skip_days = 30
    library_id = request.values.get("library") or None
    plan, skipped = unwatched_reset_plan(skip_days, library_id)
    signature = _reset_plan_signature(plan, skip_days)
    phrase = f"reset {len(plan)} show{'' if len(plan) == 1 else 's'}"
    if request.method == "POST":
        back = url_for(
            "main.reset_unwatched", skip_days=skip_days, library=library_id or None
        )
        if request.form.get("signature") != signature:
            flash(
                "The list of shows changed since you opened this page. Review it again before confirming.",
                "danger",
            )
            return redirect(back)
        if request.form.get("confirm", "").strip().lower() != phrase:
            flash(f"Nothing was reset. To confirm, type “{phrase}” exactly.", "danger")
            return redirect(back)
        queued, failed = reset_many([r["media"] for r in plan])
        flash(
            f"Started {queued} reset{'' if queued == 1 else 's'}. Curatarr's worker does them one "
            "at a time in the background; History shows each outcome."
            + (
                f" {len(failed)} could not start: "
                + "; ".join(f"{m.title} ({why})" for m, why in failed)
                if failed
                else ""
            ),
            "info" if not failed else "warning",
        )
        return redirect(url_for("main.shows"))
    live = sorted({r["library"] for r in plan if not r["dry_run"]})
    return render_template(
        "reset_unwatched.html",
        plan=plan,
        skipped=skipped,
        skip_days=skip_days,
        library_id=library_id,
        signature=signature,
        phrase=phrase,
        total_frees=sum(r["frees"] for r in plan),
        total_files=sum(r["reset_files"] for r in plan),
        live_libraries=live,
    )


@bp.get("/posters/<media_id>")
def poster(media_id):
    media = db.session.get(MediaIdentity, media_id)
    if not media or not media.jellyfin_id:
        abort(404)
    if not current_app.config["ALLOW_UNAUTHENTICATED"] and (
        (current_user() or {}).get("role") == "household" and not media.library.managed
    ):
        abort(404)
    try:
        from .artwork import _jpeg
        from .services import client as integration_client

        data = _jpeg(integration_client("jellyfin").image(media.jellyfin_id))
    except (IntegrationError, OSError, ValueError):
        abort(404)
    return send_file(BytesIO(data), mimetype="image/jpeg", max_age=300)


@bp.post("/review/<candidate_id>/<choice>")
def review_action(candidate_id, choice):
    if choice == "delete" and request.form.get("confirm") != "delete":
        abort(400)
    try:
        candidate = review_candidate(candidate_id, choice)
        if choice == "delete":
            # Approved; the worker deletes it within a minute (resume_approved),
            # so a large show is not cut off by the web request timeout.
            flash(
                "Deletion approved. Curatarr's worker carries it out in the "
                "background, usually within a minute; History shows the outcome.",
                "info",
            )
        else:
            flash("Review decision saved", "success")
        from .artwork import reconcile_artwork

        reconcile_artwork()
    except ValueError as exc:
        flash(str(exc), "danger")
    if request.form.get("back") == "title":
        candidate = db.session.get(PurgeCandidate, candidate_id)
        if candidate:
            return redirect(url_for("main.title", media_id=candidate.media_identity_id))
    if request.form.get("back") == "overview":
        return redirect(url_for("main.overview"))
    if (request.form.get("back") or "").startswith("/free-space"):
        return redirect(_safe_next(request.form["back"]))
    return redirect(url_for("main.review"))


@bp.post("/titles/<media_id>/shrink/<action>")
def title_shrink(media_id, action):
    """Shrink instead of delete (shrink.py): look, cancel, or OK a result."""
    from . import shrink

    media = db.session.get(MediaIdentity, media_id)
    if not media or action not in {"shrink", "cancel", "dismiss"}:
        abort(404)
    try:
        if action == "shrink":
            flash(shrink.request(media), "info")
        elif action == "cancel":
            shrink.cancel(media)
        else:
            shrink.dismiss(media)
    except ValueError as exc:
        flash(str(exc), "danger")
    if request.form.get("back") == "review":
        return redirect(url_for("main.review"))
    return redirect(url_for("main.title", media_id=media.id))


@bp.post("/titles/<media_id>/remove")
def title_remove(media_id):
    """Send a film to Leaving Soon (lifecycle.request_removal)."""
    from .artwork import reconcile_artwork
    from .lifecycle import request_removal

    media = db.session.get(MediaIdentity, media_id)
    if not media:
        abort(404)
    try:
        candidate = request_removal(media)
    except ValueError as exc:
        flash(str(exc), "danger")
    else:
        reconcile_artwork()
        flash(
            f"{media.title} is Leaving Soon: its poster says so in Jellyfin, and "
            f"it's deleted on {candidate.scheduled_delete_at:%B %-d} unless someone "
            "plays it or you choose Keep. Approve deletion to remove it sooner.",
            "info",
        )
    return redirect(url_for("main.title", media_id=media_id))


@bp.route("/titles/<media_id>/reset", methods=["GET", "POST"])
def title_reset(media_id):
    from .lifecycle import reset_series

    media = db.session.get(MediaIdentity, media_id)
    if not media or media.media_type != "series":
        abort(404)
    if request.method == "GET":
        return render_template("reset_confirm.html", media=media, **title_view(media))
    try:
        candidate, result = reset_series(media, run=False)
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("main.title", media_id=media.id))
    if result == "queued":
        flash(
            f"Reset of {media.title} started. Curatarr's worker does it in the "
            "background, usually within a minute; a long show can take a few "
            "minutes. The outcome appears under Lifecycle decisions and Recent "
            "history on this page.",
            "info",
        )
    elif result == "succeeded":
        flash(
            f"{media.title} was reset to its always-keep episodes. Curatarr fetches "
            "more once someone watches it again.",
            "success",
        )
    elif result == "dry_run":
        flash(
            f"Dry run: {media.title} would be reset to its always-keep episodes "
            f"(freeing {size_label(candidate.reclaimable_bytes)}). Nothing was deleted.",
            "info",
        )
    elif result == "deferred":
        flash(
            "New playback is being processed first; the reset runs right after.", "info"
        )
    else:
        blocked = (
            db.session.query(LifecycleAction)
            .filter_by(candidate_id=candidate.id, action_type="delete_blocked")
            .order_by(LifecycleAction.created_at.desc())
            .first()
        )
        reason = blocked.reason_text if blocked else "see History"
        flash(f"The reset was not done: {reason}.", "danger")
    return redirect(url_for("main.title", media_id=media.id))


@bp.post("/titles/<media_id>/request")
def title_request_now(media_id):
    media = db.session.get(MediaIdentity, media_id)
    if not media:
        abort(404)
    from .lifecycle import execute_action

    actions, skipped = request_title_now(media)
    if skipped:
        flash(skipped, "warning")
    elif not actions:
        flash(
            "Nothing to request: the always-keep episodes and the seasons "
            "viewing calls for are already present or already requested.",
            "info",
        )
    for action in actions:
        result = execute_action(action.id)
        status = "Sent to Sonarr" if result == "succeeded" else f"Not sent ({result})"
        flash(
            f"{action.reason_text} {status}.",
            "success" if result == "succeeded" else "danger",
        )
    return redirect(url_for("main.title", media_id=media.id))


HISTORY_FILTERS = ("type", "state", "q", "library", "user", "from", "to")


@bp.get("/history")
def history():
    filters = {key: request.args.get(key, "").strip() for key in HISTORY_FILTERS}
    try:
        rows = history_actions(filters)
    except ValueError:
        abort(400)
    return render_template(
        "history.html",
        actions=rows,
        filters=filters,
        media_titles=media_titles_for(rows),
        **history_filter_options(),
    )


@bp.get("/history/<action_id>")
def history_detail(action_id):
    context = action_context(action_id)
    if not context:
        abort(404)
    return render_template("history_detail.html", **context)


MODES = ["recommend", "require_review", "automatic"]
POLICY_FIELDS = {
    "minimum_mode": (
        "Always keep for each show",
        ["first_n_episodes", "season_1", "entire_series"],
    ),
    "minimum_episodes": ('N for "First N episodes"', "number"),
    "acquisition_threshold": ("Completed episodes before acquiring", "number"),
    "keep_one_season_ahead": ("Keep one season ahead", ["true", "false"]),
    "fill_minimum_footprint": (
        "Download always-keep episodes that are missing",
        ["true", "false"],
    ),
    "manage_specials": ("Manage Specials / Season 0", ["true", "false"]),
    "grace_days": ("New-media grace days", "number"),
    "inactivity_cleanup": (
        "Clean up unwatched titles even when space is fine",
        ["true", "false"],
    ),
    "tv_inactivity_days": ("TV counts as unwatched after (days)", "number"),
    "movie_inactivity_days": ("Movies count as unwatched after (days)", "number"),
    "meaningful_threshold": ("Meaningful TV watch (episodes)", "number"),
    "purge_strategy": (
        "Purge strategy",
        ["oldest_unwatched_first", "oldest_watched_nonfavorite_first", "weighted"],
    ),
    "review_mode": ("Review mode", MODES),
    "inactivity_review_mode": ("Inactivity cleanup review mode", MODES),
    "quota_review_mode": ("Quota cleanup review mode", MODES),
    "review_expiry": (
        "Review expiration",
        ["manual_forever", "auto_delete_after_notice"],
    ),
    "notice_days": ("Leaving Soon notice before removal (days)", "number"),
    "quota_enabled": ("Library-size limit", ["true", "false"]),
    "high_water_bytes": ("Size limit: start cleanup above", "size"),
    "low_water_bytes": ("Size limit: clean down to", "size"),
    "free_space_enabled": ("Free-space enforcement", ["true", "false"]),
    "disk_path": ("Arr-side disk path", "text"),
    "low_free_percent": (
        "Low free-space threshold % (clean up back to this)",
        "number",
    ),
    "critical_free_percent": ("Critical free-space threshold %", "number"),
    "low_pressure_review_mode": ("Below low free space", MODES),
    "critical_pressure_review_mode": ("Below critical free space (no notice)", MODES),
    "dry_run": ("Dry run", ["true", "false"]),
}
RULE_SECTIONS = {
    "acquisition": (
        "Acquisition rules",
        [
            "minimum_mode",
            "minimum_episodes",
            "acquisition_threshold",
            "keep_one_season_ahead",
            "fill_minimum_footprint",
            "manage_specials",
            "grace_days",
        ],
    ),
    "retention": (
        "Retention rules",
        [
            key
            for key in POLICY_FIELDS
            if key
            not in {
                "minimum_mode",
                "minimum_episodes",
                "acquisition_threshold",
                "keep_one_season_ahead",
                "fill_minimum_footprint",
                "manage_specials",
                "grace_days",
            }
        ],
    ),
}


def _policy_page(section, title, keys, library):
    if request.method == "POST":
        try:
            save_policy_layer(library, request.form, keys)
        except (ValueError, TypeError) as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        else:
            flash("Policy saved", "success")
        return redirect(request.full_path.rstrip("?"))
    global_values = setting("global_policy", {})
    stored = global_values
    media_type = "series"
    if library is not None:
        stored = library.policy.policy_json if library.policy else {}
        media_type = "series" if library.media_type == "tv" else "movie"
    effective, sources = effective_policy(
        media_type, global_values, stored if library is not None else {}
    )
    # What a blank field resolves to: built-in defaults for the global layer,
    # global-or-built-in for a library.
    fallback, fallback_sources = effective_policy(
        media_type, global_values if library is not None else {}, {}
    )
    return render_template(
        "rules.html",
        section=section,
        heading=title,
        library=library,
        libraries=[
            row
            for row in db.session.query(Library).order_by(Library.name)
            if row.managed
        ],
        fields=[(key, *POLICY_FIELDS[key]) for key in keys],
        stored=stored,
        effective=effective,
        sources=sources,
        fallback=fallback,
        fallback_sources=fallback_sources,
    )


@bp.get("/cleanup-preview")
def cleanup_preview():
    from .lifecycle import preview_cleanup
    from .policy import effective_policy as resolve

    defaults, _ = resolve("series", setting("global_policy", {}), {})
    disk_path = request.args.get("path", defaults["disk_path"] or "").strip()
    raw = request.args.get("free", "").strip()
    result, error, free = None, None, None
    if raw:
        try:
            free = float(raw)
            if not 0 <= free <= 100:
                raise ValueError
        except ValueError:
            error = "Enter a free-space percentage between 0 and 100."
        else:
            result = preview_cleanup(free, disk_path or None)
    return render_template(
        "cleanup_preview.html",
        free=free if free is not None else max(defaults["low_free_percent"] - 1, 0),
        disk_path=disk_path,
        low=defaults["low_free_percent"],
        critical=defaults["critical_free_percent"],
        result=result,
        error=error,
    )


@bp.route("/rules/<section>", methods=["GET", "POST"])
def rules(section):
    if section == "household":
        return _household_rules()
    if section == "automatic":
        return _automatic_rules()
    if section not in RULE_SECTIONS:
        abort(404)
    library = None
    scope = request.args.get("scope", "global")
    if scope != "global":
        library = db.session.get(Library, scope)
        if not library:
            abort(404)
    title, keys = RULE_SECTIONS[section]
    return _policy_page(section, title, keys, library)


def _automatic_rules():
    """Which fixes Curatarr starts by itself (autofix.py)."""
    from . import autofix

    if request.method == "POST":
        autofix.save(request.form)
        flash("Saved", "success")
        return redirect(url_for("main.rules", section="automatic"))
    return render_template(
        "automatic_rules.html", kinds=autofix.KINDS, enabled=autofix.enabled()
    )


def _household_rules():
    """Requests, rewatches, import lists and seasons (curation.py)."""
    from . import curation
    from .services import configured

    if request.method == "POST":
        try:
            curation.save_options(request.form)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        else:
            curation.forget()
            flash("Saved", "success")
        return redirect(url_for("main.rules", section="household"))
    return render_template(
        "household_rules.html",
        options=curation.options(),
        seasons=curation.SEASONS,
        seerr=configured("seerr"),
        requests=len(setting(curation.REQUESTS_KEY, {}) or {}),
        listed=len(setting(curation.LISTS_KEY, []) or []),
        returning=setting(curation.RETURNS_KEY, {}) or {},
    )


@bp.route("/libraries/<library_id>/policy", methods=["GET", "POST"])
def library_policy(library_id):
    library = db.session.get(Library, library_id)
    if not library:
        abort(404)
    return _policy_page(None, "All policy settings", list(POLICY_FIELDS), library)


@bp.route("/titles/<media_id>", methods=["GET", "POST"])
def title(media_id):
    media = db.session.get(MediaIdentity, media_id)
    if not media:
        abort(404)
    if not current_app.config["ALLOW_UNAUTHENTICATED"] and (
        (current_user() or {}).get("role") == "household" and not media.library.managed
    ):
        abort(404)
    if request.method == "POST":
        try:
            save_title_override(media, request.form)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        else:
            flash("Title override saved", "success")
        return redirect(url_for("main.title", media_id=media.id))
    from . import custom_posters

    return render_template(
        "title.html",
        media=media,
        custom_poster=custom_posters.active(media.id),
        title_posters=custom_posters.for_title(media.id),
        poster_accounts=sorted(
            (setting("jellyfin_user_names", {}) or {}).items(), key=lambda a: a[1]
        ),
        **title_view(media),
    )


@bp.get("/more")
def more():
    """Everything the phone's bottom bar doesn't fit."""
    return render_template("more.html")


@bp.get("/titles")
def titles():
    query = request.args.get("q", "").strip()
    media_type = request.args.get("type", "")
    if media_type not in {"", "series", "movie"}:
        abort(400)
    page = max(request.args.get("page", 1, type=int), 1)
    rows_query = (
        db.session.query(MediaIdentity)
        .join(Library)
        .filter(Library.media_type.in_(("tv", "movies")))
    )
    if query:
        rows_query = rows_query.filter(MediaIdentity.title.ilike(f"%{query}%"))
    if media_type:
        rows_query = rows_query.filter(MediaIdentity.media_type == media_type)
    total = rows_query.count()
    rows = (
        rows_query.order_by(MediaIdentity.title).offset((page - 1) * 48).limit(48).all()
    )
    from .services import favorited_by

    return render_template(
        "titles.html",
        rows=rows,
        favorites=favorited_by(m.id for m in rows),
        query=query,
        media_type=media_type,
        page=page,
        has_next=page * 48 < total,
        total=total,
    )
