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
