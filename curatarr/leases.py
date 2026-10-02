"""Short database leases for work that must not run concurrently."""

from datetime import timedelta
from uuid import uuid4

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
