"""Operational commands for a single-node worker."""

import time
from datetime import timedelta

import click
from flask import current_app

from .artwork import reconcile_artwork
from .integrations import IntegrationError
from .lifecycle import (
    evaluate_retention,
    expire_notices,
    expire_snoozes,
    reconcile_unknown_actions,
    recover_stale_actions,
    run_actions,
)
from .models import utcnow
from .services import (
    check_integration,
    discover,
    process_pending_events,
    queue_watch_restoration,
    reconcile_user_state,
    set_setting,
    setting,
)


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
                expire_notices()
                reconcile_artwork()
                set_setting("last_reconcile_at", utcnow().isoformat())
                click.echo(f"Reconciliation complete: {created} new candidates")
            except IntegrationError as exc:
                current_app.logger.error("Reconciliation stopped: %s", exc)
                raise click.ClickException(str(exc)) from exc

    @app.cli.command("worker")
    @click.option("--once", is_flag=True, help="Run one worker cycle and exit")
    def worker_command(once):
        """Process durable events/actions; reconcile hourly by default."""
        while True:
            with current_app.app_context():
                try:
                    process_pending_events()
                    recover_stale_actions()
                    reconcile_unknown_actions()
                    expire_snoozes()
                    run_actions()
                    expire_notices()
                    reconcile_artwork()
                    last = setting("last_reconcile_at")
                    from .services import _as_datetime

                    interval = setting("reconciliation_interval_seconds", 3600)
                    due = not last or utcnow() - _as_datetime(last) >= timedelta(
                        seconds=interval
                    )
                    if due:
                        for kind in ("jellyfin", "sonarr", "radarr"):
                            check_integration(kind)
                        if current_app.config["DEMO_MODE"]:
                            from .demo import seed_demo

                            seed_demo()
                        else:
                            discover()
                            reconcile_user_state()
                            queue_watch_restoration()
                        evaluate_retention()
                        set_setting("last_reconcile_at", utcnow().isoformat())
                    set_setting("worker_heartbeat_at", utcnow().isoformat())
                except IntegrationError as exc:
                    current_app.logger.warning("Worker cycle incomplete: %s", exc)
            if once:
                break
            time.sleep(15)
