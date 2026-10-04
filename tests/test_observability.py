import json
import logging

import pytest
import requests

from curatarr import db
from curatarr.integrations import IntegrationError, RadarrClient
from curatarr.models import MetricCounter
from curatarr.observability import flush_counters, log_operation
from curatarr.services import ingest_event, metrics_summary, set_setting


def _ops_records(caplog):
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "curatarr.ops"
    ]


def test_log_lines_are_json_limited_to_known_fields(caplog):
    caplog.set_level(logging.INFO, logger="curatarr.ops")
    log_operation("delete_media", "ok", action_id="a1", api_key="leak", payload={})
    assert _ops_records(caplog) == [
        {"action_id": "a1", "operation": "delete_media", "result": "ok"}
    ]


def test_external_failure_is_counted_and_logged_without_secrets(app, caplog):
    class FailingSession:
        def request(self, *_args, **_kwargs):
            response = requests.Response()
            response.status_code = 503
            raise requests.HTTPError("token=secret-key", response=response)

    caplog.set_level(logging.INFO, logger="curatarr.ops")
    with app.app_context():
        radarr = RadarrClient(
            "http://radarr.local:7878", "secret-key", FailingSession()
        )
        with pytest.raises(IntegrationError):
            radarr.request("DELETE", "/api/v3/movie/7", destructive=True)
        assert metrics_summary()["external_api_failures"]["radarr"] == 1
        flush_counters()
        assert db.session.get(MetricCounter, "external_api_failures.radarr").value == 1
        assert metrics_summary()["external_api_failures"] == {
            "jellyfin": 0,
            "sonarr": 0,
            "radarr": 1,
        }
    records = _ops_records(caplog)
    assert records[-1]["integration"] == "radarr"
    assert records[-1]["operation"] == "DELETE /api/v3/movie/7"
    assert records[-1]["result"] == "error"
    assert records[-1]["status_code"] == 503
    assert "duration_ms" in records[-1]
    assert "secret-key" not in caplog.text


def test_duplicate_webhook_is_counted_after_request(app, client):
    with app.app_context():
        set_setting("webhook_token", "test-token")
        db.session.commit()
    payload = {
        "event_id": "dup-metric",
        "event_type": "playback_started",
        "item_external_id": "unknown-item",
        "user_external_id": "viewer",
    }
    for _ in range(3):
        response = client.post(
            "/api/v1/webhook/jellyfin",
            json=payload,
            headers={"X-Curatarr-Token": "test-token"},
        )
        assert response.status_code in {200, 202}
    metrics = client.get("/api/v1/metrics").get_json()
    assert metrics["duplicate_events_ignored"] == 2
    assert metrics["pending_actions"] == 0
    assert metrics["oldest_pending_action_age_seconds"] is None


def _duplicate_twice():
    event = {"event_id": "x", "event_type": "playback_started", "item_external_id": "i"}
    ingest_event(event)
    ingest_event(event)


def test_counters_are_written_when_app_context_ends(app):
    # Each web request and worker cycle runs in its own app context.
    with app.app_context():
        _duplicate_twice()
    assert db.session.get(MetricCounter, "duplicate_events_ignored").value == 1


def test_counter_flush_adds_to_existing_row(app):
    db.session.add(MetricCounter(name="duplicate_events_ignored", value=5))
    db.session.commit()
    _duplicate_twice()
    flush_counters()
    db.session.expire_all()
    assert db.session.get(MetricCounter, "duplicate_events_ignored").value == 6


def test_reconcile_records_duration_and_overview_shows_operations(app):
    app.config["DEMO_MODE"] = True
    result = app.test_cli_runner().invoke(args=["reconcile"])
    assert result.exit_code == 0, result.output
    with app.app_context():
        metrics = metrics_summary()
        assert isinstance(metrics["last_reconcile_duration_ms"], int)
        assert metrics["purge_candidates_created"] > 0
        assert metrics["bytes_proposed"] > 0
    page = app.test_client().get("/")
    assert page.status_code == 200
    assert b"Duplicate events ignored" in page.data
