from sqlalchemy import event

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


def test_a_write_kept_waiting_isnt_reported_as_the_one_holding_the_lock(
    app, monkeypatch
):
    """Timed from before the write, the wait for someone else's lock counted
    as holding it: one long hold was reported by every writer behind it."""
    clock = [100.0]
    monkeypatch.setattr(write_watch.time, "monotonic", lambda: clock[0])
    said = []
    monkeypatch.setattr(
        write_watch.log, "warning", lambda message, *args: said.append(message % args)
    )

    def waits_for_the_lock(_conn, _cursor, statement, *_rest):
        if statement.lstrip().upper().startswith(write_watch.WRITES):
            clock[0] += 25

    event.listen(db.engine, "before_cursor_execute", waits_for_the_lock)
    try:
        set_setting("example", 1)
    finally:
        event.remove(db.engine, "before_cursor_execute", waits_for_the_lock)
    assert said == []


def test_the_report_reaches_past_a_deep_flush_to_the_code_that_wrote(app, monkeypatch):
    clock = iter([100.0, 125.0])
    monkeypatch.setattr(write_watch.time, "monotonic", lambda: next(clock, 125.0))
    said = []
    monkeypatch.setattr(
        write_watch.log, "warning", lambda message, *args: said.append(message % args)
    )

    def deep(levels):
        if levels:
            return deep(levels - 1)
        set_setting("example", 1)

    deep(10)
    (message,) = said
    assert "services.py" in message and "set_setting" in message
