from datetime import timedelta

import pytest

from curatarr import db
from curatarr.commands import RECONCILE_RETRY_SECONDS, worker_cycle
from curatarr.integrations import IntegrationError
from curatarr.models import Integration, utcnow
from curatarr.services import check_integration, set_setting, setting


def test_first_run_worker_waits_for_keys_but_reports_healthy(app, client):
    # State right after the first Jellyfin sign-in: URL saved, no API keys yet.
    db.session.add(Integration(kind="jellyfin", base_url="http://jellyfin.local"))
    db.session.commit()
    assert worker_cycle() == "waiting_for_setup"
    assert setting("last_reconcile_attempt_at") is None
    health = client.get("/health").get_json()
    assert health["scheduler"] == "healthy"
    assert check_integration("jellyfin") == "unconfigured"
    assert (
        client.get("/health").get_json()["integrations"]["jellyfin"] == "unconfigured"
    )


def test_failed_reconciliation_backs_off(app, monkeypatch):
    for kind in ("jellyfin", "sonarr", "radarr"):
        db.session.add(
            Integration(kind=kind, base_url=f"http://{kind}.local", secret_ref="x")
        )
    db.session.commit()
    calls = []

    def failing_discover():
        calls.append(1)
        raise IntegrationError("sonarr unavailable")

    monkeypatch.setattr("curatarr.commands.check_integration", lambda _kind: "healthy")
    monkeypatch.setattr("curatarr.commands.discover", failing_discover)
    with pytest.raises(IntegrationError):
        worker_cycle()
    assert worker_cycle() == "idle"  # no immediate retry
    assert len(calls) == 1
    earlier = utcnow() - timedelta(seconds=RECONCILE_RETRY_SECONDS + 1)
    set_setting("last_reconcile_attempt_at", earlier.isoformat())
    with pytest.raises(IntegrationError):
        worker_cycle()
    assert len(calls) == 2


def test_demo_worker_reconciles_then_idles(app):
    app.config["DEMO_MODE"] = True
    assert worker_cycle() == "reconciled"
    assert setting("last_reconcile_at") is not None
    assert worker_cycle() == "idle"


def _configured():
    for kind in ("jellyfin", "sonarr", "radarr"):
        db.session.add(
            Integration(kind=kind, base_url=f"http://{kind}.local", secret_ref="x")
        )
    db.session.commit()


def test_discover_button_queues_instead_of_running(app, client, monkeypatch):
    monkeypatch.setattr(
        "curatarr.services.discover",
        lambda: pytest.fail("discovery must not run inside the web request"),
    )
    response = client.post("/discover", follow_redirects=True)
    assert b"Add the Jellyfin, Sonarr, and Radarr API keys" in response.data
    assert setting("reconcile_requested_at") is None
    _configured()
    response = client.post("/discover", follow_redirects=True)
    assert b"Discovery and reconciliation queued" in response.data
    assert setting("reconcile_requested_at") is not None
    assert b"Queued" in client.get("/").data


def test_worker_runs_a_requested_reconciliation_immediately(app, monkeypatch):
    _configured()
    ran = []
    monkeypatch.setattr("curatarr.commands.check_integration", lambda _kind: "healthy")
    monkeypatch.setattr("curatarr.commands.discover", lambda: ran.append("discover"))
    monkeypatch.setattr("curatarr.commands.reconcile_user_state", lambda: None)
    monkeypatch.setattr("curatarr.commands.queue_watch_restoration", lambda: None)
    # Within the hourly interval and a recent failed attempt: normally idle.
    set_setting("last_reconcile_at", utcnow().isoformat())
    set_setting("last_reconcile_attempt_at", utcnow().isoformat())
    assert worker_cycle() == "idle"
    set_setting("reconcile_requested_at", utcnow().isoformat())
    assert worker_cycle() == "reconciled"
    assert ran == ["discover"]
    assert worker_cycle() == "idle"  # the request was consumed


def test_overview_banner_tracks_reconciliation(app, client):
    now = utcnow()
    page = client.get("/").text
    assert "No reconciliation has run yet" in page
    set_setting("reconcile_requested_at", now.isoformat())
    page = client.get("/").text
    assert "Discovery queued" in page and 'http-equiv="refresh"' in page
    set_setting("last_reconcile_attempt_at", (now + timedelta(seconds=5)).isoformat())
    set_setting("worker_heartbeat_at", (now - timedelta(minutes=2)).isoformat())
    page = client.get("/").text
    assert "Discovery and reconciliation running" in page
    assert client.get("/health").get_json()["scheduler"] == "busy"
    set_setting(
        "last_reconcile_error",
        {"at": (now + timedelta(seconds=9)).isoformat(), "message": "sonarr down"},
    )
    page = client.get("/").text
    assert "Last reconciliation failed:</strong> sonarr down" in page
    assert 'http-equiv="refresh"' not in page
    set_setting("last_reconcile_at", (now + timedelta(seconds=60)).isoformat())
    set_setting("last_reconcile_duration_ms", 134743)
    page = client.get("/").text
    assert "Last reconciliation finished" in page and "in 135 s" in page


def test_unfinished_run_is_reported_as_interrupted(app, client):
    started = utcnow() - timedelta(hours=1)
    set_setting("last_reconcile_attempt_at", started.isoformat())
    assert "did not finish" in client.get("/").text
