"""Opt-in checks against a migrated PostgreSQL test database."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Lock
from uuid import uuid4

import pytest

from curatarr import create_app, db
from curatarr.leases import acquire, release
from curatarr.lifecycle import execute_action
from curatarr.models import (
    JobLease,
    Library,
    LifecycleAction,
    MediaIdentity,
    WatchStateSnapshot,
    utcnow,
)


@pytest.mark.skipif(
    not os.getenv("CURATARR_TEST_POSTGRES_URL"),
    reason="Set CURATARR_TEST_POSTGRES_URL to a migrated test database",
)
def test_postgres_lease_has_one_owner_under_contention():
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": os.environ["CURATARR_TEST_POSTGRES_URL"],
            "SECRET_KEY": "postgres-test-only",
        }
    )
    scope = f"test-lease:{uuid4()}"

    def contend(barrier):
        with app.app_context():
            barrier.wait(timeout=10)
            return acquire(scope)

    try:
        for expired in (False, True):
            if expired:
                with app.app_context():
                    db.session.add(
                        JobLease(
                            scope=scope,
                            owner="expired-owner",
                            expires_at=utcnow() - timedelta(seconds=1),
                        )
                    )
                    db.session.commit()
            barrier = Barrier(8)
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(contend, barrier) for _ in range(8)]
                owners = [future.result() for future in futures]
            winners = [owner for owner in owners if owner]
            assert len(winners) == 1
            with app.app_context():
                assert db.session.get(JobLease, scope).owner == winners[0]
                release(scope, winners[0])
    finally:
        with app.app_context():
            db.session.query(JobLease).filter_by(scope=scope).delete()
            db.session.commit()


@pytest.mark.skipif(
    not os.getenv("CURATARR_TEST_POSTGRES_URL"),
    reason="Set CURATARR_TEST_POSTGRES_URL to a migrated test database",
)
def test_postgres_action_claim_calls_external_api_once(monkeypatch):
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": os.environ["CURATARR_TEST_POSTGRES_URL"],
            "SECRET_KEY": "postgres-test-only",
        }
    )
    calls = []
    lock = Lock()

    class FakeJellyfin:
        def mark_played(self, user_id, item_id):
            with lock:
                calls.append((user_id, item_id))
            time.sleep(0.05)

    monkeypatch.setattr("curatarr.lifecycle.client", lambda _kind: FakeJellyfin())
    with app.app_context():
        library = Library(
            jellyfin_library_id=f"pg-test-{uuid4()}",
            name="Concurrency test",
            media_type="tv",
        )
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id,
            media_type="series",
            title="Concurrency test",
        )
        db.session.add(media)
        db.session.flush()
        snapshot = WatchStateSnapshot(
            media_identity_id=media.id,
            jellyfin_user_id="test-user",
            provider_key="tvdb:1",
            played=True,
        )
        db.session.add(snapshot)
        db.session.flush()
        action = LifecycleAction(
            idempotency_key=f"pg-action-test:{uuid4()}",
            action_type="jellyfin_restore_played",
            state="PENDING",
            media_identity_id=media.id,
            reason_text="Concurrency test",
            payload_json={
                "user_id": "test-user",
                "item_id": "test-item",
                "snapshot_id": snapshot.id,
            },
        )
        db.session.add(action)
        db.session.commit()
        ids = (library.id, media.id, snapshot.id, action.id)

    def contend(barrier):
        with app.app_context():
            barrier.wait(timeout=10)
            return execute_action(ids[3])

    try:
        barrier = Barrier(8)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(contend, barrier) for _ in range(8)]
            outcomes = [future.result() for future in futures]
        assert outcomes.count("succeeded") == 1
        assert outcomes.count("skipped") == 7
        assert calls == [("test-user", "test-item")]
        with app.app_context():
            assert db.session.get(LifecycleAction, ids[3]).state == "SUCCEEDED"
    finally:
        with app.app_context():
            for model, item_id in zip(
                (LifecycleAction, WatchStateSnapshot, MediaIdentity, Library),
                (ids[3], ids[2], ids[1], ids[0]),
                strict=True,
            ):
                db.session.delete(db.session.get(model, item_id))
            db.session.commit()
