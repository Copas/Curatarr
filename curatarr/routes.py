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
from sqlalchemy import func

from . import db
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
    UnsupportedWebhookEvent,
    action_context,
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


@bp.before_app_request
def guard_request():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    if request.path == "/api/v1/webhook/jellyfin":
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
    if current_app.config["CSRF_ENABLED"]:
        supplied = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            abort(403)


PUBLIC_ENDPOINTS = {
    "main.sign_in_page",
    "main.health",
    "main.api_status",
    "main.webhook",
    "static",
}


@bp.before_app_request
def require_sign_in():
    if current_app.config["ALLOW_UNAUTHENTICATED"]:
        return None
    if request.endpoint in PUBLIC_ENDPOINTS or current_user():
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Sign-in required"}), 401
    target = request.full_path.rstrip("?") if request.method == "GET" else None
    return redirect(url_for("main.sign_in_page", next=target))


HOUSEHOLD_PAGES = {
    "main.overview",
    "main.review",
    "main.shows",
    "main.titles",
    "main.title",
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
    "main.shows": "library",
    "main.reset_unwatched": "library",
    "main.history": "activity",
    "main.history_detail": "activity",
    "main.settings": "manage",
    "main.setup": "manage",
    "main.library_policy": "manage",
    "main.cleanup_preview": "manage",
    "main.operations": "manage",
}


def _nav_section():
    """Which top-level navigation entry the current page belongs to."""
    if request.endpoint == "main.rules":
        return "manage"
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
        "theme": request.cookies.get("curatarr_theme")
        if request.cookies.get("curatarr_theme") in {"light", "dark"}
        else "dark",
    }


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
    limit = min(max(request.args.get("limit", 50, type=int), 1), 200)
    rows = (
        db.session.query(LifecycleAction)
        .order_by(LifecycleAction.created_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify(
        [
            {
                "id": row.id,
                "type": row.action_type,
                "state": row.state,
                "reason": row.reason_text,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    )


@bp.get("/")
def overview():
    setup_incomplete = not current_app.config["DEMO_MODE"] and any(
        not step["done"] and not step.get("optional") for step in setup_progress()
    )
    return render_template(
        "overview.html",
        status=_status(),
        metrics=metrics_summary(),
        reconciliation=reconciliation_state(),
        setup_incomplete=setup_incomplete,
        demo_mode=current_app.config["DEMO_MODE"],
        **overview_data(),
    )


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
                own = (request.form.get("webhook_token") or "").strip()
                token = rotate_webhook_token(own or None)
                if own:
                    flash("Webhook token saved.", "success")
                else:
                    # Shown once on the next page; redirecting means a reload
                    # can never resubmit the form and create another token.
                    session["new_webhook_token"] = token
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
        household_users=household_users,
        household_allowed=household_user_ids(),
        household_error=household_error,
        **settings_view(),
    )


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
    except UnsupportedWebhookEvent as exc:
        # Acknowledge so the plugin does not report errors for extra types.
        record_webhook("ignored", reason=str(exc))
        return jsonify({"accepted": False, "ignored": str(exc)}), 202
    except (ValueError, TypeError) as exc:
        record_webhook("rejected", reason=str(exc))
        abort(400)
    record_webhook("accepted", event=event)
    return jsonify({"event_id": event.id, "accepted": created}), 202


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
    return render_template(
        "shows.html",
        rows=rows,
        libraries=libraries,
        library_id=library_id,
        total_on_disk=sum(r["on_disk"] for r in rows),
        total_frees=sum(r["frees"] for r in rows),
    )


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
    return redirect(url_for("main.review"))


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
    return render_template("title.html", media=media, **title_view(media))


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
    return render_template(
        "titles.html",
        rows=rows,
        query=query,
        media_type=media_type,
        page=page,
        has_next=page * 48 < total,
        total=total,
    )
