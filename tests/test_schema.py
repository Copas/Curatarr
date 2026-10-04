from curatarr import create_app


def test_unmigrated_app_returns_clear_error(tmp_path):
    app = create_app(
        {
            "TESTING": False,
            "ALLOW_UNAUTHENTICATED": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'fresh.db'}",
            "SECRET_KEY": "test-only",
        }
    )
    client = app.test_client()
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json["error"] == "Database migration required"
    response = client.post("/api/v1/webhook/jellyfin", json={})
    assert response.status_code == 503


def test_settings_from_a_newer_version_are_refused(tmp_path):
    from sqlalchemy import text

    from curatarr import db
    from curatarr.schema import POLICY_SCHEMA_VERSION

    app = create_app(
        {
            "TESTING": False,
            "ALLOW_UNAUTHENTICATED": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'newer.db'}",
            "SECRET_KEY": "test-only",
        }
    )
    with app.app_context():
        from flask_migrate import upgrade

        upgrade()
        assert app.test_client().get("/health").status_code == 200
        db.session.execute(
            text(
                "UPDATE app_settings SET value_json = :v "
                "WHERE key = 'policy_schema_version'"
            ),
            {"v": str(POLICY_SCHEMA_VERSION + 1)},
        )
        db.session.commit()
    response = app.test_client().get("/health")
    assert response.status_code == 503
    assert response.json["error"] == "Settings were saved by a newer Curatarr version"
