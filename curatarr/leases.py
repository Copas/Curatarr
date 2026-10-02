"""Short database leases for work that must not run concurrently."""

from contextlib import contextmanager
from datetime import timedelta
from threading import Event, Thread
from uuid import uuid4

from flask import current_app
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from . import db
from .models import JobLease, utcnow


def acquire(scope: str, seconds=300) -> str | None:
    owner = str(uuid4())
    expires = utcnow() + timedelta(seconds=seconds)
    db.session.add(JobLease(scope=scope, owner=owner, expires_at=expires))
    try:
        db.session.commit()
        return owner
    except IntegrityError:
        db.session.rollback()
    claimed = db.session.execute(
        update(JobLease)
        .where(JobLease.scope == scope, JobLease.expires_at <= utcnow())
        .values(owner=owner, expires_at=expires)
    ).rowcount
    db.session.commit()
    return owner if claimed == 1 else None


def release(scope: str, owner: str) -> None:
    db.session.execute(
        delete(JobLease).where(JobLease.scope == scope, JobLease.owner == owner)
    )
    db.session.commit()


def renew(scope: str, owner: str, seconds=300) -> bool:
    """Extend a lease only while its current owner still holds it."""
    now = utcnow()
    changed = db.session.execute(
        update(JobLease)
        .where(
            JobLease.scope == scope,
            JobLease.owner == owner,
            JobLease.expires_at > now,
        )
        .values(expires_at=now + timedelta(seconds=seconds))
    ).rowcount
    db.session.commit()
    return changed == 1


@contextmanager
def keep_alive(scope: str, owner: str, seconds=300):
    """Renew long work in a separate application context and DB session."""
    app = current_app._get_current_object()
    stop = Event()
    lost = Event()

    def heartbeat():
        with app.app_context():
            while not stop.wait(max(0.1, seconds / 3)):
                try:
                    if not renew(scope, owner, seconds):
                        lost.set()
                        return
                except Exception:
                    db.session.rollback()
                    app.logger.exception("Database lease renewal failed for %s", scope)
                    lost.set()
                    return

    thread = Thread(target=heartbeat, name=f"curatarr-lease:{scope}", daemon=True)
    thread.start()
    try:
        yield lost
    finally:
        stop.set()
        thread.join()
