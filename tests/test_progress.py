"""One overall progress bar: items across every kind of work, finishing when
the slowest does."""

from datetime import timedelta

from curatarr import compat_audio, enrichment, progress, seek_repair
from curatarr.models import utcnow


def test_overall_takes_the_slowest_finish(app, monkeypatch):
    now = utcnow()
    monkeypatch.setattr(
        enrichment,
        "job_forecast",
        lambda now=None: {
            "done": 10,
            "remaining": 2,
            "finishes_at": now + timedelta(minutes=10),
        },
    )
    monkeypatch.setattr(
        enrichment,
        "marker_forecast",
        lambda now=None: {
            "done": 30,
            "remaining": 60,
            "finishes_at": now + timedelta(hours=20),
        },
    )
    monkeypatch.setattr(compat_audio, "progress", lambda: None)
    monkeypatch.setattr(seek_repair, "progress", lambda: None)
    result = progress.overall(now)
    assert result["done"] == 40 and result["left"] == 62
    assert result["slowest"] == "Skip-marker checks" and result["time_left"] == "20 h"
    assert not result["approximate"] and result["unknown"] == []


def test_nothing_to_do_means_no_bar(app, monkeypatch):
    monkeypatch.setattr(enrichment, "job_forecast", lambda now=None: None)
    monkeypatch.setattr(enrichment, "marker_forecast", lambda now=None: None)
    monkeypatch.setattr(compat_audio, "progress", lambda: None)
    monkeypatch.setattr(seek_repair, "progress", lambda: None)
    assert progress.overall() is None


def test_rebuilds_without_a_pace_are_named(app, monkeypatch):
    monkeypatch.setattr(enrichment, "job_forecast", lambda now=None: None)
    monkeypatch.setattr(enrichment, "marker_forecast", lambda now=None: None)
    monkeypatch.setattr(
        compat_audio,
        "progress",
        lambda: {
            "on": True,
            "done": 5,
            "films": {"left": 1},
            "episodes": {"left": 4},
        },
    )
    monkeypatch.setattr(seek_repair, "progress", lambda: None)
    result = progress.overall()
    assert result["left"] == 5 and result["unknown"] == ["TV-friendly audio"]
    assert result["time_left"] is None


def test_overview_puts_ongoing_work_at_the_top(app, client, monkeypatch):
    from test_enrichment import _movie

    from curatarr import db

    _movie("/movies/Film (2020)/Film.mkv")
    from curatarr.models import MediaPart

    enrichment.state_for(db.session.query(MediaPart).one()).duration_seconds = 60.0
    db.session.commit()
    monkeypatch.setattr(
        progress,
        "overall",
        lambda now=None: {
            "done": 1,
            "left": 2,
            "percent": 33.3,
            "finishes_at": utcnow() + timedelta(hours=1),
            "time_left": "1 h",
            "slowest": "Skip-marker checks",
            "approximate": False,
            "unknown": [],
            "parts": [],
        },
    )
    page = client.get("/").get_data(as_text=True)
    assert "Everything Curatarr is working through" in page
    assert page.index("Everything Curatarr is working through") < page.index(
        'id="needs-heading"'
    )
