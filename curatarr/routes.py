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
from .integrations import CLIENTS, IntegrationError, normalized_url
from .lifecycle import evaluate_retention, execute_approved, review_candidate
from .models import (
    AcquisitionState,
    Integration,
    Library,
    LifecycleAction,
    LifecycleEvent,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
    TitleOverride,
    UserMediaState,
    utcnow,
)
from .policy import effective_policy, validate_policy
from .services import (
    discover,
    ingest_event,
    process_pending_events,
    resolved_policy,
    set_setting,
    setting,
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
            abort(403)
        return
    if request.content_type and request.content_type.startswith("application/json"):
        abort(415)
    if current_app.config["CSRF_ENABLED"]:
        supplied = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            abort(403)


@bp.app_context_processor
def template_values():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return {"csrf_token": session["csrf_token"], "dry_run": _dry_run_status()}


def _dry_run_status():
    return not any(
        row.policy and row.policy.policy_json.get("dry_run") is False
        for row in db.session.query(Library).all()
    )


def _status():
    integrations = {kind: "unconfigured" for kind in CLIENTS}
    for row in db.session.query(Integration).all():
        integrations[row.kind] = row.health_state
    if current_app.config["DEMO_MODE"]:
        outage = setting("demo_outage")
        if outage in integrations:
            integrations[outage] = "unhealthy (simulated)"
    reclaimed = (
        db.session.query(func.sum(PurgeCandidate.reclaimable_bytes))
        .join(LifecycleAction, LifecycleAction.candidate_id == PurgeCandidate.id)
        .filter(
            LifecycleAction.action_type == "delete_media",
            LifecycleAction.state == "SUCCEEDED",
        )
        .scalar()
        or 0
    )
    return {
        "app": "curatarr",
        "version": "0.1.0",
        "dry_run": _dry_run_status(),
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
    libraries = db.session.query(Library).all()
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
        db.session.query(AcquisitionState)
        .filter(AcquisitionState.state != "DORMANT")
        .order_by(AcquisitionState.updated_at.desc())
        .limit(5)
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
    return render_template(
        "overview.html",
        status=_status(),
        library_reports=library_reports,
        actions=actions,
        errors=errors,
        acquisitions=acquisitions,
        media_titles=media_titles,
        demo_mode=current_app.config["DEMO_MODE"],
    )


@bp.post("/demo/simulate")
def demo_simulate():
    if not current_app.config["DEMO_MODE"]:
        abort(404)
    from .demo import seed_demo

    seed_demo()
    choice = request.form.get("choice")
    if choice in {"complete_episode", "start_playback", "favorite", "unfavorite"}:
        event_type = {
            "complete_episode": "item_played",
            "start_playback": "playback_started",
            "favorite": "favorite_changed",
            "unfavorite": "favorite_changed",
        }[choice]
        is_episode = choice in {"complete_episode", "start_playback"}
        ingest_event(
            {
                "event_id": secrets.token_hex(16),
                "event_type": event_type,
                "item_external_id": "demo-episode-1-1-1"
                if is_episode
                else "demo-series-3",
                "series_external_id": "demo-series-1"
                if is_episode
                else "demo-series-3",
                "season_number": 1,
                "episode_number": 1,
                "user_external_id": "demo-viewer-a",
                "played": choice == "complete_episode",
                "favorite": choice == "favorite" if not is_episode else None,
            }
        )
        process_pending_events()
    elif choice == "advance_30_days":
        from datetime import timedelta

        from .models import UserMediaState

        for media in db.session.query(MediaIdentity).all():
            if media.added_at:
                media.added_at -= timedelta(days=30)
        for state in db.session.query(UserMediaState).all():
            if state.last_played_at:
                state.last_played_at -= timedelta(days=30)
        db.session.commit()
    elif choice == "increase_movie_size":
        from .models import MediaPart

        media = (
            db.session.query(MediaIdentity)
            .filter_by(jellyfin_id="demo-movie-1")
            .first()
        )
        if media:
            part = (
                db.session.query(MediaPart)
                .filter_by(media_identity_id=media.id)
                .first()
            )
            part.size_bytes += 1_000_000_000
            db.session.commit()
    elif choice == "add_movie":
        from .models import MediaPart

        library = (
            db.session.query(Library)
            .filter_by(jellyfin_library_id="demo-movies")
            .first()
        )
        next_id = (
            db.session.query(func.max(MediaIdentity.radarr_id)).scalar() or 50
        ) + 1
        media = MediaIdentity(
            library_id=library.id,
            media_type="movie",
            title=f"Demo Movie {next_id:02d}",
            jellyfin_id=f"demo-movie-{next_id}",
            radarr_id=next_id,
            tmdb_id=20000 + next_id,
            added_at=utcnow(),
        )
        db.session.add(media)
        db.session.flush()
        db.session.add(
            MediaPart(
                media_identity_id=media.id,
                kind="movie_file",
                has_file=True,
                size_bytes=2_000_000_000,
                acquired_at=utcnow(),
            )
        )
        library.last_size_bytes = (library.last_size_bytes or 0) + 2_000_000_000
        db.session.commit()
    elif choice in {
        "outage_sonarr",
        "outage_radarr",
        "outage_jellyfin",
        "restore_integrations",
    }:
        set_setting(
            "demo_outage",
            None if choice == "restore_integrations" else choice.split("_")[1],
        )
    else:
        abort(400)
    flash(f"Simulated: {choice.replace('_', ' ')}", "success")
    return redirect(url_for("main.overview"))


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        kind = request.form.get("kind")
        if kind in CLIENTS:
            try:
                url = normalized_url(request.form.get("base_url", ""))
            except ValueError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("main.settings"))
            key = request.form.get("api_key", "")
            row = db.session.query(Integration).filter_by(kind=kind).first()
            if not key and row:
                from .secrets import decrypt_secret

                try:
                    key = decrypt_secret(row.secret_ref)
                except ValueError:
                    flash(
                        "Saved key cannot be decrypted; provide a new API key", "danger"
                    )
                    return redirect(url_for("main.settings"))
            if not key:
                flash("API key is required", "danger")
                return redirect(url_for("main.settings"))
            try:
                version = CLIENTS[kind](url, key).version()
            except IntegrationError:
                flash(
                    f"{kind.title()} could not be reached; settings were not saved",
                    "danger",
                )
                return redirect(url_for("main.settings"))
            if not row:
                row = Integration(kind=kind, base_url=url)
                db.session.add(row)
            row.base_url = url
            from .secrets import encrypt_secret

            row.secret_ref = encrypt_secret(key)
            row.detected_version = version
            row.health_state = "healthy"
            row.last_health_at = utcnow()
            db.session.commit()
            flash(f"{kind.title()} connected", "success")
        elif kind == "webhook":
            token = request.form.get("webhook_token") or secrets.token_urlsafe(32)
            set_setting("webhook_token", token)
            rows = {row.kind: row for row in db.session.query(Integration).all()}
            return render_template(
                "settings.html",
                integrations=rows,
                webhook_present=True,
                new_token=token,
                interval_minutes=setting("reconciliation_interval_seconds", 3600) // 60,
            )
        elif kind == "scheduler":
            try:
                minutes = int(request.form.get("interval_minutes", "60"))
                if not 5 <= minutes <= 1440:
                    raise ValueError("Reconciliation interval must be 5–1440 minutes")
            except ValueError as exc:
                flash(str(exc), "danger")
            else:
                set_setting("reconciliation_interval_seconds", minutes * 60)
                flash("Reconciliation interval saved", "success")
        return redirect(url_for("main.settings"))
    new_token = None
    if not setting("webhook_token"):
        new_token = secrets.token_urlsafe(32)
        set_setting("webhook_token", new_token)
    rows = {row.kind: row for row in db.session.query(Integration).all()}
    return render_template(
        "settings.html",
        integrations=rows,
        webhook_present=bool(setting("webhook_token")),
        new_token=new_token,
        interval_minutes=setting("reconciliation_interval_seconds", 3600) // 60,
    )


@bp.post("/discover")
def discover_route():
    try:
        if current_app.config["DEMO_MODE"]:
            from .demo import seed_demo

            count = 3 if seed_demo() else 0
        else:
            count = discover()
        flash(f"Discovered {count} Jellyfin libraries", "success")
    except IntegrationError:
        current_app.logger.warning("Discovery failed: integration unavailable")
        flash("Discovery failed; check integration status", "danger")
    return redirect(url_for("main.overview"))


@bp.post("/reconcile")
def reconcile_route():
    try:
        process_pending_events()
        from .lifecycle import expire_snoozes

        expire_snoozes()
        count = evaluate_retention()
        from .artwork import reconcile_artwork

        reconcile_artwork()
        flash(f"Reconciliation created {count} candidates", "success")
    except IntegrationError:
        flash("Reconciliation paused because an integration is unavailable", "danger")
    return redirect(url_for("main.overview"))


@bp.route("/api/v1/webhook/jellyfin", methods=["POST"])
def webhook():
    if not request.is_json:
        abort(415)
    try:
        event, created = ingest_event(request.get_json())
    except (ValueError, TypeError):
        abort(400)
    return jsonify({"event_id": event.id, "accepted": created}), 202


@bp.get("/review")
def review():
    rows = (
        db.session.query(PurgeCandidate)
        .filter(
            PurgeCandidate.state.in_(
                ["ELIGIBLE", "REVIEW", "LEAVING_SOON", "SNOOZED", "APPROVED"]
            )
        )
        .order_by(PurgeCandidate.eligible_at)
        .all()
    )
    activity = {}
    for candidate in rows:
        states = (
            db.session.query(UserMediaState)
            .filter_by(media_identity_id=candidate.media_identity_id)
            .all()
        )
        activity[candidate.id] = {
            "last_played": max(
                (state.last_played_at for state in states if state.last_played_at),
                default=None,
            ),
            "favorite": any(state.favorite for state in states),
        }
    return render_template("review.html", candidates=rows, activity=activity)


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
    return redirect(url_for("main.review"))


@bp.get("/history")
def history():
    query = db.session.query(LifecycleAction)
    action_type = request.args.get("type", "").strip()
    state = request.args.get("state", "").strip()
    title_query = request.args.get("q", "").strip()
    if action_type:
        query = query.filter(LifecycleAction.action_type == action_type)
    if state:
        query = query.filter(LifecycleAction.state == state)
    if title_query:
        query = query.join(
            MediaIdentity, LifecycleAction.media_identity_id == MediaIdentity.id
        )
        query = query.filter(MediaIdentity.title.ilike(f"%{title_query}%"))
    rows = query.order_by(LifecycleAction.created_at.desc()).limit(200).all()
    return render_template(
        "history.html",
        actions=rows,
        action_type=action_type,
        state=state,
        title_query=title_query,
    )


@bp.get("/history/<action_id>")
def history_detail(action_id):
    action = db.session.get(LifecycleAction, action_id)
    if not action:
        abort(404)
    event = db.session.get(LifecycleEvent, action.event_id) if action.event_id else None
    media = (
        db.session.get(MediaIdentity, action.media_identity_id)
        if action.media_identity_id
        else None
    )
    return render_template(
        "history_detail.html", action=action, event=event, media=media
    )


@bp.route("/libraries/<library_id>/policy", methods=["GET", "POST"])
def library_policy(library_id):
    library = db.session.get(Library, library_id)
    if not library:
        abort(404)
    from .models import LibraryPolicy

    row = library.policy or LibraryPolicy(library_id=library.id)
    if request.method == "POST":
        values = {}
        try:
            for key, value in request.form.items():
                if key == "csrf_token" or value == "":
                    continue
                if key in {
                    "quota_enabled",
                    "free_space_enabled",
                    "dry_run",
                    "never_purge",
                }:
                    values[key] = value == "true"
                elif key.endswith(
                    ("_days", "_threshold", "_episodes", "_bytes", "_percent")
                ):
                    values[key] = int(value)
                else:
                    values[key] = value
            validated = validate_policy(values, complete=False)
            effective_policy(
                "series" if library.media_type == "tv" else "movie",
                setting("global_policy", {}),
                validated,
            )
            row.policy_json = validated
        except (ValueError, TypeError) as exc:
            flash(str(exc), "danger")
        else:
            db.session.add(row)
            db.session.commit()
            flash("Policy saved", "success")
        return redirect(url_for("main.library_policy", library_id=library.id))
    effective, sources = effective_policy(
        "series" if library.media_type == "tv" else "movie",
        setting("global_policy", {}),
        row.policy_json or {},
    )
    return render_template(
        "policy.html",
        library=library,
        policy=row.policy_json or {},
        effective=effective,
        sources=sources,
    )


@bp.route("/titles/<media_id>", methods=["GET", "POST"])
def title(media_id):
    media = db.session.get(MediaIdentity, media_id)
    if not media:
        abort(404)
    if request.method == "POST":
        values = {}
        for key in (
            "never_purge",
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
            if key == "never_purge":
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
