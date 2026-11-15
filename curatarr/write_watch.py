"""Finds what keeps SQLite's write lock too long.

SQLite has one writer at a time: from a transaction's first write until its
commit, every other write waits (up to 30 seconds, then "database is
locked"). A write followed by slow work (a file copy, a model, a web call)
before the commit makes everything else wait. This notes where each
transaction's first write came from and logs it when the lock was held
longer than HELD_TOO_LONG, so the slow path can be found and fixed.
"""

import logging
import os
import sys
import time
import traceback

from sqlalchemy import event

log = logging.getLogger("curatarr.write_watch")
PACKAGE = os.path.dirname(os.path.abspath(__file__))
HELD_TOO_LONG = 10.0  # seconds
WRITES = ("INSERT", "UPDATE", "DELETE", "REPLACE")
DEPTH = 80  # frames kept of where a write came from


def watch(engine) -> None:
    @event.listens_for(engine, "before_cursor_execute")
    def _first_write(conn, _cursor, statement, _params, _context, _many):
        info = conn.info
        if "write_since" in info or "write_from" in info:
            return
        if statement.lstrip()[:7].upper().startswith(WRITES):
            # Deep enough to reach past SQLAlchemy's own frames (a flush is
            # some 25 of them) to the Curatarr code that asked for the write.
            info["write_from"] = traceback.StackSummary.extract(
                traceback.walk_stack(sys._getframe()), limit=DEPTH, lookup_lines=False
            )

    @event.listens_for(engine, "after_cursor_execute")
    def _has_lock(conn, _cursor, _statement, _params, _context, _many):
        # Counted from when the first write went through, which is when this
        # connection got the lock: counted from before it, every writer kept
        # waiting by a long hold was reported as holding the lock itself.
        info = conn.info
        if "write_from" in info and "write_since" not in info:
            info["write_since"] = time.monotonic()

    def _ended(conn):
        since = conn.info.pop("write_since", None)
        where = conn.info.pop("write_from", None)
        if since is None:
            return
        held = time.monotonic() - since
        if held >= HELD_TOO_LONG:
            # Curatarr's own frames only: an installed copy lives under a
            # path that itself contains "curatarr", so the name alone picked
            # out SQLAlchemy's frames and hid where the write came from.
            frames = [
                f"{f.filename[len(PACKAGE) + 1 :]}:{f.lineno} {f.name}"
                for f in reversed(where or [])  # walk_stack gives the newest first
                if f.filename.startswith(PACKAGE + os.sep)
                and not f.filename.endswith("write_watch.py")
            ]
            log.warning(
                "The database write lock was held for %.0f s; first write from %s",
                held,
                " < ".join(reversed(frames[-6:])) or "unknown",
            )

    event.listen(engine, "commit", _ended)
    event.listen(engine, "rollback", _ended)

    @event.listens_for(engine, "checkin")
    def _returned(_dbapi_connection, record):
        # Rolled back by the pool on its way back: nothing is held any more.
        record.info.pop("write_since", None)
        record.info.pop("write_from", None)
