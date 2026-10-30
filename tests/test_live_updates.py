"""Pages that update themselves do it in place: no page reloads, so
nothing someone opened on a page closes under them."""

from pathlib import Path

from curatarr import db
from curatarr.models import utcnow
from curatarr.services import set_setting

TEMPLATES = Path(__file__).parent.parent / "curatarr" / "templates"
STATIC = Path(__file__).parent.parent / "curatarr" / "static"


def test_no_page_reloads_itself():
    for path in [*TEMPLATES.glob("*.html"), *STATIC.glob("*.js")]:
        text = path.read_text()
        assert 'http-equiv="refresh"' not in text, path.name
        assert "location.reload" not in text, path.name


def test_the_overview_updates_its_status_line_in_place(app, client):
    set_setting("last_reconcile_attempt_at", utcnow().isoformat())
    db.session.commit()
    page = client.get("/").text
    assert 'id="reconcile-status"' in page and 'data-active="1"' in page
    assert "reconcile-status.js" in page and "This line updates on its own" in page
    line = client.get("/overview/reconcile-status").text
    assert line.lstrip().startswith("<div") and "running" in line
    set_setting("last_reconcile_at", utcnow().isoformat())
    db.session.commit()
    finished = client.get("/overview/reconcile-status").text
    assert "data-active" not in finished and "Last reconciliation finished" in finished


def test_refreshed_sections_have_ids_to_stay_open():
    """A part of a page that's redrawn keeps its open sections only if they
    have ids to find them by."""
    for name in (
        "_chapter_activity.html",
        "_artwork_image.html",
        "_artwork_candidate.html",
        "_artwork_job_top.html",
        "_artwork_job_row.html",
    ):
        text = (TEMPLATES / name).read_text()
        for tag in text.split("<details")[1:]:
            assert " id=" in tag.split(">", 1)[0], name
