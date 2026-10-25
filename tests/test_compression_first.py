"""Compression first (issue #3): while an external transcoder has work
queued, low space marks nothing Leaving Soon; critical space still does."""

import os
from datetime import timedelta

from test_disk_pressure import PressureServices
from test_space_driven_cleanup import _candidates, _library_with_defaults

from curatarr import compression, db
from curatarr.lifecycle import evaluate_retention
from curatarr.models import LifecycleAction, utcnow
from curatarr.services import set_setting


def test_low_space_waits_for_the_transcoder(app, client, monkeypatch):
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _k: PressureServices(150))
    _library_with_defaults()
    set_setting("webhook_token", "secret")
    db.session.commit()
    compression.save_settings(True, "", 6)
    response = client.post(
        "/api/v1/compression",
        json={"pending": 312, "source": "Tdarr"},
        headers={"X-Curatarr-Token": "secret"},
    )
    assert response.get_json()["holding"] is True
    assert evaluate_retention() == 0  # 15% free, below the 20% low threshold
    held = db.session.query(LifecycleAction).filter_by(
        action_type="cleanup_held_for_compression"
    )
    assert held.count() == 1 and "Tdarr has 312 files" in held.one().reason_text
    # The queue's done: cleanup goes ahead.
    client.post(
        "/api/v1/compression",
        json={"pending": 0, "source": "Tdarr"},
        headers={"X-Curatarr-Token": "secret"},
    )
    assert evaluate_retention() == 1


def test_critical_space_doesnt_wait(app, monkeypatch):
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _k: PressureServices(50))
    _library_with_defaults()
    compression.save_settings(True, "", 6)
    compression.report(40, "Tdarr")
    assert evaluate_retention() >= 1  # 5% free, below the 10% critical threshold
    assert _candidates()


def test_a_signal_goes_stale_and_off_means_off(app, monkeypatch):
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _k: PressureServices(150))
    _library_with_defaults()
    compression.report(5, "Tdarr")
    assert compression.holding() is None  # not switched on
    compression.save_settings(True, "", 6)
    assert compression.holding()["pending"] == 5
    set_setting(
        compression.SIGNAL_KEY,
        {
            "pending": 5,
            "source": "Tdarr",
            "at": (utcnow() - timedelta(hours=7)).isoformat(),
        },
    )
    db.session.commit()
    assert compression.holding() is None  # silent for too long
    assert evaluate_retention() == 1


def test_a_file_can_be_the_signal(app, client, tmp_path):
    busy = tmp_path / "queue-busy"
    compression.save_settings(True, str(busy), 6)
    assert compression.holding() is None
    busy.write_text("17\n")
    assert compression.holding()["pending"] == 17
    old = (utcnow() - timedelta(hours=8)).timestamp()
    os.utime(busy, (old, old))
    assert compression.holding() is None
    page = client.get("/settings").text
    assert "Compression first" in page
    client.post(
        "/settings",
        data={
            "kind": "compression_first",
            "enabled": "on",
            "file": "",
            "stale_hours": "3",
        },
    )
    assert compression.settings() == {"enabled": True, "file": "", "stale_hours": 3}


def test_the_api_needs_the_token(app, client):
    set_setting("webhook_token", "secret")
    db.session.commit()
    assert client.post("/api/v1/compression", json={"pending": 1}).status_code == 403
    response = client.post(
        "/api/v1/compression",
        json={"pending": "lots"},
        headers={"X-Curatarr-Token": "secret"},
    )
    assert response.status_code == 400
    assert client.get("/api/v1/compression").get_json()["pending"] == 0
