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


def _app(tmp_path, name):
    return create_app(
        {
            "TESTING": False,
            "ALLOW_UNAUTHENTICATED": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / name}",
            "SECRET_KEY": "test-only",
        }
    )


def test_a_new_installation_starts_with_automatic_fixes_off(tmp_path):
    from flask_migrate import upgrade

    from curatarr import autofix
    from curatarr.enrichment import options

    with _app(tmp_path, "new.db").app_context():
        upgrade()
        assert not any(autofix.enabled().values())
        assert not options()["file_replace_automatically"]
        assert options()["file_check"]  # the checks still run and list what they find


def test_an_existing_installation_keeps_its_automatic_fixes(tmp_path):
    from flask_migrate import upgrade

    from curatarr import autofix, db
    from curatarr.enrichment import options
    from curatarr.models import Library
    from curatarr.services import set_setting

    with _app(tmp_path, "old.db").app_context():
        upgrade(revision="f3a9d2c7b815")
        db.session.add(
            Library(jellyfin_library_id="lib", name="Films", media_type="movies")
        )
        set_setting(
            "chapter_options", {"concurrency": 2, "dub_replace_automatically": False}
        )
        set_setting("auto_fixes", {"missing": False})
        db.session.commit()
        upgrade()
        db.session.expire_all()
        enabled = autofix.enabled()
        assert enabled["files"] and enabled["wrong_show"] and enabled["seek"]
        assert enabled["default_audio"] and enabled["misfiled"]
        assert not enabled["dubs"] and not enabled["missing"]  # chosen before: kept
        assert options()["concurrency"] == 2
