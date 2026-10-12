from datetime import UTC, datetime

from curatarr.routes import when


def test_times_render_for_the_browser_with_a_utc_fallback():
    moment = datetime(2026, 10, 12, 10, 40, 58, tzinfo=UTC)
    assert str(when(moment, "seconds")) == (
        '<time datetime="2026-10-12T10:40:58+00:00" data-style="seconds">'
        "10:40:58 UTC</time>"
    )
    assert "2026-10-12</time>" in str(when(moment, "date"))
    # Naive values from SQLite are UTC; ISO strings from JSON work too.
    assert 'datetime="2026-10-12T10:40:58+00:00"' in str(
        when(moment.replace(tzinfo=None))
    )
    assert "10:40 UTC" in str(when("2026-10-12T10:40:58+00:00"))
    assert when(None) == ""


def test_pages_load_the_local_time_script(client):
    assert client.get("/static/local-time.js").status_code == 200
