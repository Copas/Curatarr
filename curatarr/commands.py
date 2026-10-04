"""Operational commands for a single-node worker."""

import time

import click
from flask import current_app

from . import db
from .artwork import reconcile_artwork
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
from .models import Integration, utcnow
from .observability import log_operation
from .services import (
    _as_datetime,
    check_integration,
    discover,
    process_pending_events,
    queue_watch_restoration,
    reconcile_user_state,
    set_setting,
    setting,
)


def _record_reconcile_duration(started):
    duration_ms = round((time.monotonic() - started) * 1000)
    set_setting("last_reconcile_duration_ms", duration_ms)
    log_operation("reconcile", "ok", duration_ms=duration_ms)


RECONCILE_RETRY_SECONDS = 300


def _integrations_ready():
    rows = {row.kind: row for row in db.session.query(Integration)}
    return all(
        rows.get(kind) is not None and rows[kind].secret_ref
        for kind in ("jellyfin", "sonarr", "radarr")
    )


def _reconcile_due(now):
    interval = setting("reconciliation_interval_seconds", 3600)
    last = _as_datetime(setting("last_reconcile_at"))
    attempt = _as_datetime(setting("last_reconcile_attempt_at"))
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
    now = utcnow()
    if not _reconcile_due(now):
        return "idle"
    demo = current_app.config["DEMO_MODE"]
    if not demo and not _integrations_ready():
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
    evaluate_retention()
    set_setting("last_reconcile_at", utcnow().isoformat())
    _record_reconcile_duration(started)
    return "reconciled"


def register_commands(app):
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
                    current_app.logger.warning(
                        "Reconciliation incomplete; retrying in %s seconds: %s",
                        RECONCILE_RETRY_SECONDS,
                        exc,
                    )
            if once:
                break
            time.sleep(15)
