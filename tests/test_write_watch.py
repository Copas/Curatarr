from curatarr import db, write_watch
from curatarr.services import set_setting


def test_a_long_held_write_lock_is_logged_with_where_it_started(app, monkeypatch):
    clock = iter([100.0, 125.0])
    monkeypatch.setattr(write_watch.time, "monotonic", lambda: next(clock, 125.0))
    said = []
    monkeypatch.setattr(
        write_watch.log, "warning", lambda message, *args: said.append(message % args)
    )
    set_setting("example", 1)
    db.session.commit()
    (message,) = said
    assert "held for 25 s" in message and "services.py" in message
