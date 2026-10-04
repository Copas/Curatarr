"""Server-rendered UI and narrow operational API."""

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
from .auth import SignInError, current_user, needs_server_url, sign_in, sign_out
from .integrations import CLIENTS, IntegrationError
from .lifecycle import evaluate_retention, execute_approved, review_candidate
from .models import (
    Integration,
    Library,
    LifecycleAction,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
    TitleOverride,
    utcnow,
)
from .policy import effective_policy, validate_policy
from .services import (
    UnsupportedWebhookEvent,
    action_context,
    dry_run_by_library,
    history_actions,
    history_filter_options,
    ingest_event,
    integrations_ready,
    lifecycle_outlook,
    media_titles_for,
    metrics_summary,
    process_pending_events,
    reclaimed_bytes_total,
    reconciliation_state,
    record_webhook,
    request_reconciliation,
    request_title_now,
    resolved_policy,
    review_rows,
    rotate_webhook_token,
    save_integration,
    save_policy_layer,
    save_reconcile_interval,
    setting,
    settings_view,
    setup_progress,
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
    "main.titles": "overrides",
    "main.title": "overrides",
    "main.history": "history",
    "main.history_detail": "history",
    "main.settings": "settings",
    "main.setup": "settings",
    "main.library_policy": "retention",
}


def _nav_section():
    """Which top-level navigation entry the current page belongs to."""
    if request.endpoint == "main.rules":
        return (request.view_args or {}).get("section")
    return NAV_SECTIONS.get(request.endpoint)


@bp.app_context_processor
def template_values():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return {
        "csrf_token": session["csrf_token"],
        "nav_section": _nav_section(),
        "dry_run_split": dry_run_by_library(),
        "signed_in_user": session.get("user"),
    }


def _dry_run_status():
    """True only when no library can delete (every library in dry run)."""
    return not dry_run_by_library()["deleting"]


def _status():
    integrations = {kind: "unconfigured" for kind in CLIENTS}
    for row in db.session.query(Integration).all():
        integrations[row.kind] = row.health_state
    if current_app.config["DEMO_MODE"]:
        outage = setting("demo_outage")
        if outage in integrations:
            integrations[outage] = "unhealthy (simulated)"
    reclaimed = reclaimed_bytes_total()
    return {
        "app": "curatarr",
        "version": "0.1.0",
        "dry_run": _dry_run_status(),
        "dry_run_libraries": dry_run_by_library()["dry_run"],
        "deleting_libraries": dry_run_by_library()["deleting"],
        "review_count": db.session.query(PurgeCandidate)
        .filter_by(state="REVIEW")
        .count(),
        "leaving_soon_count": db.session.query(PurgeCandidate)
        .filter_by(state="LEAVING_SOON")
        .count(),
        "reclaimed_bytes": reclaimed,
        "pending_actions": db.session.query(LifecycleAction)
        .filter(LifecycleAction.state.in_(["PENDING", "FAILED_RETRYABLE", "RUNNING"]))
        .count(),
        "integrations": integrations,
    }


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
    every_library = db.session.query(Library).order_by(Library.name).all()
    libraries = [library for library in every_library if library.managed]
    unmanaged = [library.name for library in every_library if not library.managed]
    library_reports = []
    for library in libraries:
        media_type = "series" if library.media_type == "tv" else "movie"
        policy, _ = effective_policy(
            media_type,
            setting("global_policy", {}),
            library.policy.policy_json if library.policy else {},
        )
        size = (
            db.session.query(func.sum(MediaPart.size_bytes))
            .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
            .filter(
                MediaIdentity.library_id == library.id, MediaPart.has_file.is_(True)
            )
            .scalar()
            or 0
        )
        high = policy["high_water_bytes"] if policy["quota_enabled"] else None
        low = policy["low_water_bytes"] if policy["quota_enabled"] else None
        library_reports.append(
            {
                "library": library,
                "size": size,
                "high": high,
                "low": low,
                "pressure": "above high water"
                if high is not None and size > high
                else "normal",
                "free_space_enabled": policy["free_space_enabled"],
            }
        )
    actions = (
        db.session.query(LifecycleAction)
        .order_by(LifecycleAction.created_at.desc())
        .limit(10)
        .all()
    )
    errors = (
        db.session.query(LifecycleAction)
        .filter(
            LifecycleAction.state.in_(
                ["FAILED_RETRYABLE", "FAILED_FINAL", "UNKNOWN_RECONCILE"]
            )
        )
        .order_by(LifecycleAction.created_at.desc())
        .limit(5)
        .all()
    )
    acquisitions = (
        db.session.query(LifecycleAction)
        .filter_by(action_type="sonarr_season_search")
        .order_by(LifecycleAction.created_at.desc())
        .limit(10)
        .all()
    )
    media_ids = {
        row.media_identity_id for row in actions + acquisitions if row.media_identity_id
    }
    media_titles = {
        row.id: row.title
        for row in db.session.query(MediaIdentity).filter(
            MediaIdentity.id.in_(media_ids)
        )
    }
    scheduled, cleanup = lifecycle_outlook()
    media_titles |= {
        row.media_identity_id: row.media.title
        for row in scheduled + [candidate for _action, candidate in cleanup]
    }
    setup_incomplete = not current_app.config["DEMO_MODE"] and any(
        not step["done"] and not step.get("optional") for step in setup_progress()
    )
    return render_template(
        "overview.html",
        status=_status(),
        metrics=metrics_summary(),
        reconciliation=reconciliation_state(),
        scheduled=scheduled,
        cleanup=cleanup,
        setup_incomplete=setup_incomplete,
        library_reports=library_reports,
        unmanaged_libraries=unmanaged,
        actions=actions,
        errors=errors,
        acquisitions=acquisitions,
        media_titles=media_titles,
        demo_mode=current_app.config["DEMO_MODE"],
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
    new_token = None
    if request.method == "POST":
        kind = request.form.get("kind")
        try:
            if kind in CLIENTS:
                save_integration(
                    kind, request.form.get("base_url"), request.form.get("api_key")
                )
                flash(f"{kind.title()} connected", "success")
            elif kind == "webhook":
                new_token = rotate_webhook_token(request.form.get("webhook_token"))
            elif kind == "scheduler":
                try:
                    minutes = int(request.form.get("interval_minutes", ""))
                except ValueError:
                    minutes = None
                save_reconcile_interval(minutes)
                flash("Reconciliation interval saved", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        if new_token is None:
            return redirect(url_for("main.settings"))
    elif setting("webhook_token") is None:
        new_token = rotate_webhook_token()
    return render_template("settings.html", new_token=new_token, **settings_view())


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


@bp.get("/posters/<media_id>")
def poster(media_id):
    media = db.session.get(MediaIdentity, media_id)
    if not media or not media.jellyfin_id:
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
    try:
        candidate = review_candidate(candidate_id, choice)
        if choice == "delete":
            result = execute_approved(candidate.id)
            flash(f"Deletion result: {result}", "info")
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
    if request.method == "POST":
        values = {}
        for key in (
            "never_purge",
            "fill_minimum_footprint",
            "tv_inactivity_days",
            "movie_inactivity_days",
            "minimum_mode",
            "minimum_episodes",
            "acquisition_threshold",
            "meaningful_threshold",
            "grace_days",
            "review_mode",
            "purge_strategy",
        ):
            selected = request.form.get(key, "")
            if selected in {"", "inherit"}:
                continue
            if key in {"never_purge", "fill_minimum_footprint"}:
                values[key] = selected == "true"
            elif key.endswith(("_days", "_episodes", "_threshold")):
                try:
                    values[key] = int(selected)
                except ValueError:
                    flash(f"Invalid number for {key}", "danger")
                    return redirect(url_for("main.title", media_id=media.id))
            else:
                values[key] = selected
        override = media.override or TitleOverride(media_identity_id=media.id)
        try:
            override.override_json = validate_policy(values, complete=False)
            db.session.add(override)
            db.session.commit()
            if values.get("never_purge"):
                from .lifecycle import rescue_candidate
                from .services import _active_candidate

                candidate = _active_candidate(media.id)
                if candidate:
                    rescue_candidate(
                        candidate, "Never Purge was enabled for this title."
                    )
                    from .artwork import reconcile_artwork

                    reconcile_artwork()
            flash("Title override saved", "success")
        except ValueError as exc:
            flash(str(exc), "danger")
        return redirect(url_for("main.title", media_id=media.id))
    values, sources = resolved_policy(media)
    candidates = (
        db.session.query(PurgeCandidate)
        .filter_by(media_identity_id=media.id)
        .order_by(PurgeCandidate.eligible_at.desc())
        .limit(5)
        .all()
    )
    actions = (
        db.session.query(LifecycleAction)
        .filter_by(media_identity_id=media.id)
        .order_by(LifecycleAction.created_at.desc())
        .limit(10)
        .all()
    )
    return render_template(
        "title.html",
        media=media,
        values=values,
        sources=sources,
        candidates=candidates,
        actions=actions,
    )


@bp.get("/titles")
def titles():
    query = request.args.get("q", "").strip()
    rows = []
    if query:
        rows = (
            db.session.query(MediaIdentity)
            .filter(MediaIdentity.title.ilike(f"%{query}%"))
            .limit(50)
            .all()
        )
    return render_template("titles.html", rows=rows, query=query)
