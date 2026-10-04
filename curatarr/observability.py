"""Structured operational logs and durable counters (spec section 94).

Log lines are JSON objects limited to LOG_FIELDS, so credentials or payloads
cannot leak through ad-hoc keyword arguments.

Counters that have no durable source row (duplicate events, external API
failures) are accumulated in memory and written by flush_counters() on a
separate connection after the surrounding work has committed. Recording them
inline would roll back with failed work and can contend for SQLite locks.
"""

import json
import logging
import threading
from collections import Counter

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

LOG_FIELDS = (
    "event_id",
    "action_id",
    "media_identity_id",
    "candidate_id",
    "integration",
    "operation",
    "duration_ms",
    "result",
    "status_code",
)

logger = logging.getLogger("curatarr.ops")
_pending: Counter = Counter()
_lock = threading.Lock()


def log_operation(operation, result, *, level=logging.INFO, **fields):
    record = {"operation": operation, "result": result}
    record.update({key: value for key, value in fields.items() if key in LOG_FIELDS})
    record = {key: value for key, value in record.items() if value is not None}
    logger.log(level, json.dumps(record, sort_keys=True, default=str))


def increment(name, amount=1):
    with _lock:
        _pending[name] += amount


def reset():
    """Discard unflushed counts; used by tests."""
    with _lock:
        _pending.clear()


def flush_counters():
    from . import db
    from .models import MetricCounter

    with _lock:
        batch = dict(_pending)
        _pending.clear()
    if not batch:
        return
    table = MetricCounter.__table__
    try:
        with db.engine.begin() as conn:
            for name, amount in batch.items():
                changed = conn.execute(
                    update(table)
                    .where(table.c.name == name)
                    .values(value=table.c.value + amount)
                ).rowcount
                if not changed:
                    try:
                        with conn.begin_nested():
                            conn.execute(insert(table).values(name=name, value=amount))
                    except IntegrityError:
                        conn.execute(
                            update(table)
                            .where(table.c.name == name)
                            .values(value=table.c.value + amount)
                        )
    except SQLAlchemyError:
        with _lock:
            _pending.update(batch)
        logger.warning("Metric counter flush failed; counts kept for next flush")


def counter_values():
    """Durable counter values plus any not yet flushed by this process."""
    from . import db
    from .models import MetricCounter

    table = MetricCounter.__table__
    values = Counter(
        dict(db.session.execute(select(table.c.name, table.c.value)).all())
    )
    with _lock:
        values.update(_pending)
    return values
