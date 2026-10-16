import threading
import time

import pytest

from curatarr import pause, throttle
from curatarr.models import utcnow
from curatarr.services import setting


@pytest.fixture(autouse=True)
def allowed():
    throttle.allow()
    yield
    throttle.allow()


def test_pause_page_controls_and_banner(client, monkeypatch):
    monkeypatch.setattr(pause, "_unload_language_models", lambda: None)
    page = client.get("/chapters").text
    assert "Pause all work" in page and "All work is paused" not in page
    client.post("/pause", data={"hours": "1", "next": "/chapters"})
    paused = setting("work_paused")
    assert paused and paused["until"] > utcnow().isoformat()
    page = client.get("/").text
    assert "All work is paused" in page and "Resume now" in page
    client.post("/resume", data={"next": "/"})
    assert pause.state() is None
    assert client.post("/pause", data={"hours": "99"}).status_code == 400


def test_an_expired_pause_ends_by_itself(app):
    pause.pause(1)
    from curatarr.services import set_setting

    set_setting(
        "work_paused", {**setting("work_paused"), "until": "2000-01-01T00:00:00+00:00"}
    )
    assert pause.state() is None and setting("work_paused") is None


def test_enforce_stops_running_tools_and_unloads_once(app, monkeypatch):
    unloads = []
    monkeypatch.setattr(pause, "_unload_language_models", lambda: unloads.append(1))
    outcome = {}

    def job():
        try:
            throttle.run(["sleep", "30"], timeout=60)
            outcome["result"] = "finished"
        except throttle.Interrupted:
            outcome["result"] = "interrupted"

    worker = threading.Thread(target=job)
    worker.start()
    time.sleep(0.5)
    pause.pause(None)
    assert pause.enforce() is True and pause.enforce() is True
    worker.join(timeout=10)
    assert outcome["result"] == "interrupted" and unloads == [1]
    with pytest.raises(throttle.Interrupted):
        throttle.run(["true"], timeout=5)  # nothing new starts while paused
    pause.resume()
    assert pause.enforce() is False
    assert throttle.run(["true"], timeout=5).returncode == 0


def test_a_stopped_chapter_job_goes_back_in_the_queue(app, monkeypatch):
    from test_enrichment import _movie

    from curatarr import db, enrichment
    from curatarr.models import ChapterState

    _library, _media, part = _movie()
    state = enrichment.request(part, "generate")

    def interrupted(state_id, llm=None):
        raise throttle.Interrupted

    monkeypatch.setattr(enrichment, "run_job", interrupted)
    enrichment._run_one(app, state.id, None)
    db.session.expire_all()
    assert db.session.get(ChapterState, state.id).job_state == "queued"
    assert db.session.get(ChapterState, state.id).chapter_status != "failed"


def test_the_worker_does_nothing_else_while_paused(app, monkeypatch):
    from curatarr import commands, enrichment

    monkeypatch.setattr(pause, "_unload_language_models", lambda: None)
    called = []
    monkeypatch.setattr(enrichment, "dispatch", lambda **kw: called.append("dispatch"))
    pause.pause(4)
    commands._dispatch_chapters()
    assert called == []
