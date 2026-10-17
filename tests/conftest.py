import pytest

from curatarr import create_app, db
from curatarr.auth import reset_rate_limits
from curatarr.observability import reset


@pytest.fixture
def app(tmp_path, monkeypatch):
    # Keep poster snapshots and other instance data out of the repository.
    monkeypatch.setenv("CURATARR_DATA_DIR", str(tmp_path / "data"))
    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
            "CSRF_ENABLED": False,
            # Route tests run signed out; tests/test_auth.py covers sign-in.
            "ALLOW_UNAUTHENTICATED": True,
            "SECRET_KEY": "test-only",
        }
    )
    with application.app_context():
        db.create_all()
        reset()
        reset_rate_limits()
        yield application
        reset()
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def quiet_machine(monkeypatch):
    """Job dispatch reads the real CPU and GPU; tests get an idle machine and a
    fresh controller so they don't depend on what this computer is doing."""
    from curatarr import adaptive

    monkeypatch.setattr(adaptive, "cpu_use", lambda: 0.05)
    monkeypatch.setattr(adaptive, "gpu_use", lambda: None)
    monkeypatch.setattr(adaptive, "controller", adaptive.Controller(limit=4))


@pytest.fixture(autouse=True)
def no_dub_wait(request, monkeypatch):
    """Movie chapter jobs wait for a dub check, which needs Radarr and Whisper;
    outside the dub tests they go straight ahead."""
    if request.module.__name__.endswith("test_dubs"):
        return
    from curatarr import dub_jobs

    monkeypatch.setattr(dub_jobs, "holds_chapters", lambda state, enabled: False)


@pytest.fixture(autouse=True)
def fresh_caches():
    """Fun facts and Radarr collections are cached between requests."""
    from curatarr import compat_audio, curation, franchises, trivia

    trivia._cache.clear()
    franchises.forget()
    curation.forget()
    compat_audio._sonarr_cache.clear()


def database_free(app) -> bool:
    """Whether another connection (the web app, say) could write right now."""
    import sqlite3

    path = app.config["SQLALCHEMY_DATABASE_URI"].removeprefix("sqlite:///")
    other = sqlite3.connect(path, timeout=0)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        other.close()
