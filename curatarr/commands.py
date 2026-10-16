"""Operational commands for a single-node worker."""

import time

import click
from flask import current_app
from sqlalchemy.exc import OperationalError

from . import db
from .artwork import reconcile_artwork
from .enrichment import dispatch, queue_new_media, run_requested_scan
from .integrations import IntegrationError
from .lifecycle import (
    evaluate_retention,
    expire_notices,
    expire_snoozes,
    reconcile_unknown_actions,
    recover_stale_actions,
    resume_approved,
    run_actions,
)
from .models import utcnow
from .observability import log_operation
from .services import (
    _as_datetime,
    check_integration,
    discover,
    integrations_ready,
    process_pending_events,
    queue_watch_restoration,
    reconcile_acquisition,
    reconcile_user_state,
    set_setting,
    setting,
)


def _record_reconcile_duration(started):
    duration_ms = round((time.monotonic() - started) * 1000)
    set_setting("last_reconcile_duration_ms", duration_ms)
    log_operation("reconcile", "ok", duration_ms=duration_ms)


RECONCILE_RETRY_SECONDS = 300


def _reconcile_due(now):
    interval = setting("reconciliation_interval_seconds", 3600)
    last = _as_datetime(setting("last_reconcile_at"))
    attempt = _as_datetime(setting("last_reconcile_attempt_at"))
    requested = _as_datetime(setting("reconcile_requested_at"))
    if requested and (not attempt or requested > attempt):
        return True  # Discover/Reconcile was pressed; skip schedule and backoff.
    if attempt and (not last or attempt > last):
        # The previous attempt failed; back off instead of retrying every cycle.
        return (now - attempt).total_seconds() >= min(interval, RECONCILE_RETRY_SECONDS)
    return not last or (now - last).total_seconds() >= interval


def worker_cycle():
    """One worker pass. Returns idle, waiting_for_setup, or reconciled.

    The heartbeat is written first so health reports the worker as running even
    while reconciliation is waiting for setup or failing on an outage.
    """
    set_setting("worker_heartbeat_at", utcnow().isoformat())
    process_pending_events()
    recover_stale_actions()
    reconcile_unknown_actions()
    expire_snoozes()
    run_actions()
    resume_approved()
    expire_notices()
    reconcile_artwork()
    _dispatch_chapters()
    now = utcnow()
    if not _reconcile_due(now):
        return "idle"
    demo = current_app.config["DEMO_MODE"]
    if not demo and not integrations_ready():
        return "waiting_for_setup"
    set_setting("last_reconcile_attempt_at", now.isoformat())
    started = time.monotonic()
    for kind in ("jellyfin", "sonarr", "radarr"):
        check_integration(kind)
    if demo:
        from .demo import seed_demo

        seed_demo()
    else:
        discover()
        reconcile_user_state()
        queue_watch_restoration()
        reconcile_acquisition()
    evaluate_retention()
    set_setting("last_reconcile_at", utcnow().isoformat())
    _record_reconcile_duration(started)
    if not demo:
        # Discovery just recorded new imports; queue their chapter jobs.
        queue_new_media()
        run_requested_scan()
        _dispatch_chapters()
    return "reconciled"


def _dispatch_chapters():
    """Start chapter jobs in background threads; never blocks the cycle."""
    try:
        from .enrichment import run_requested_skip_marker_check

        run_requested_skip_marker_check()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Queueing skip-marker checks failed")
    try:
        dispatch()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Chapter job dispatch failed")
    try:
        from .enrichment import check_movie_markers

        check_movie_markers()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Movie skip-marker check failed")
    try:
        from . import dub_jobs

        dub_jobs.start()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Starting the dub check failed")
    try:
        from . import filecheck_jobs

        filecheck_jobs.start()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Starting the file check failed")
    try:
        from .enrichment import resolve_missing_failures

        resolve_missing_failures()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Checking missing files failed")
    try:
        from . import trivia

        trivia.sweep()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Counting transcripts failed")
    try:
        from .enrichment import link_transcripts

        link_transcripts()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Linking transcripts failed")


def register_commands(app):
    from .chapter_commands import chapters_group

    app.cli.add_command(chapters_group)

    @app.cli.command("encrypt-secrets")
    def encrypt_secrets_command():
        """Encrypt credentials saved by older Curatarr versions."""
        from .secrets import encrypt_legacy_secrets

        count = encrypt_legacy_secrets()
        click.echo(f"Encrypted {count} legacy database secrets")

    @app.cli.command("demo-seed")
    def demo_seed_command():
        """Populate deterministic synthetic libraries when demo mode is enabled."""
        if not current_app.config["DEMO_MODE"]:
            raise click.ClickException("Set CURATARR_DEMO_MODE=true first")
        from .demo import seed_demo

        click.echo("Demo data created" if seed_demo() else "Demo data already present")

    @app.cli.command("reconcile")
    def reconcile_command():
        """Refresh external state and evaluate policies once."""
        with current_app.app_context():
            started = time.monotonic()
            try:
                for kind in ("jellyfin", "sonarr", "radarr"):
                    check_integration(kind)
                if current_app.config["DEMO_MODE"]:
                    from .demo import seed_demo

                    seed_demo()
                else:
                    discover()
                    reconcile_user_state()
                    queue_watch_restoration()
                    reconcile_acquisition()
                process_pending_events()
                recover_stale_actions()
                reconcile_unknown_actions()
                expire_snoozes()
                created = evaluate_retention()
                run_actions()
                resume_approved()
                expire_notices()
                reconcile_artwork()
                set_setting("last_reconcile_at", utcnow().isoformat())
                _record_reconcile_duration(started)
                click.echo(f"Reconciliation complete: {created} new candidates")
            except IntegrationError as exc:
                current_app.logger.error("Reconciliation stopped: %s", exc)
                raise click.ClickException(str(exc)) from exc

    @app.cli.command("worker")
    @click.option("--once", is_flag=True, help="Run one worker cycle and exit")
    def worker_command(once):
        """Process durable events/actions; reconcile hourly by default."""
        from .enrichment import release_abandoned_jobs

        with current_app.app_context():
            released = release_abandoned_jobs()
            if released:
                current_app.logger.info(
                    "Requeued %s chapter jobs left running by the previous worker",
                    released,
                )
        announced = None
        while True:
            with current_app.app_context():
                try:
                    result = worker_cycle()
                    if result == "waiting_for_setup" and announced != result:
                        current_app.logger.info(
                            "Reconciliation waits until Jellyfin, Sonarr, and Radarr "
                            "API keys are saved in Settings"
                        )
                    announced = result
                except IntegrationError as exc:
                    db.session.rollback()
                    set_setting(
                        "last_reconcile_error",
                        {"at": utcnow().isoformat(), "message": str(exc)},
                    )
                    current_app.logger.warning(
                        "Reconciliation incomplete; retrying in %s seconds: %s",
                        RECONCILE_RETRY_SECONDS,
                        exc,
                    )
                except OperationalError as exc:
                    # SQLite has one writer; a long discovery transaction can
                    # outlast the 30-second wait. Restarting the worker would
                    # also abandon running chapter jobs, so try again shortly.
                    db.session.rollback()
                    if once or "locked" not in str(exc):
                        raise
                    current_app.logger.warning(
                        "Database busy; retrying the worker cycle in 15 seconds"
                    )
            if once:
                break
            time.sleep(15)
