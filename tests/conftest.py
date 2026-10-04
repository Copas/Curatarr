import pytest

from curatarr import create_app, db
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
            "SECRET_KEY": "test-only",
        }
    )
    with application.app_context():
        db.create_all()
        reset()
        yield application
        reset()
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()
