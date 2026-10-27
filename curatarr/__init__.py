"""Curatarr application entry point."""

import os

from flask import Flask, jsonify
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()


def _sqlite_wal(connection, _record):
    import sqlite3
    from contextlib import suppress

    cursor = connection.cursor()
    # A database that is busy right now keeps its current mode until next time.
    with suppress(sqlite3.OperationalError):
        cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def create_app(test_config=None):
    from .config import configure_app

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
        instance_path=os.getenv("CURATARR_DATA_DIR"),
    )
    configure_app(app, test_config or {})
    app.logger.setLevel(os.getenv("CURATARR_LOG_LEVEL", "INFO").upper())

    from .observability import flush_counters

    # Registered before db.init_app so it runs after the request session is
    # removed (teardown functions run in reverse registration order).
    @app.teardown_appcontext
    def write_counters(_exc):
        flush_counters()

    from .schema import MIGRATIONS_DIR

    db.init_app(app)
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        from sqlalchemy import event

        with app.app_context():
            # WAL lets readers and the writer work at the same time (the web
            # app, the worker, and its chapter jobs). The mode is stored in
            # the database file, so this only changes it once.
            event.listen(db.engine, "connect", _sqlite_wal)
    # Migrations ship inside the package so installed copies can upgrade
    # without a source checkout.
    migrate.init_app(app, db, directory=str(MIGRATIONS_DIR))

    from . import models  # noqa: F401
    from .commands import register_commands
    from .routes import bp

    @app.before_request
    def verify_schema():
        if app.testing:
            return
        from .schema import POLICY_SCHEMA_VERSION, current_schema, stored_policy_version

        if not current_schema(app) and stored_policy_version() > POLICY_SCHEMA_VERSION:
            return jsonify(
                {
                    "error": "Settings were saved by a newer Curatarr version",
                    "action": "Install that version or newer; this one will not "
                    "reinterpret settings it does not understand.",
                }
            ), 503
        if not current_schema(app):
            return jsonify(
                {
                    "error": "Database migration required",
                    "command": "flask --app curatarr db upgrade",
                }
            ), 503

    from .secrets import WrongKey

    @app.errorhandler(WrongKey)
    def wrong_key(_exc):
        # Every page that reads a saved password or token would fail with an
        # Internal Server Error; say once, plainly, what's wrong instead.
        return jsonify(
            {
                "error": "Curatarr's secret key doesn't match the one its saved "
                "passwords and tokens were encrypted with",
                "action": "Put back the CURATARR_SECRET_KEY this database was "
                "used with (in curatarr.env, or the .env beside a Docker install), "
                "then restart Curatarr.",
            }
        ), 503

    from . import (
        chapter_routes,  # noqa: F401  (adds the chapter pages to bp)
        flag_routes,  # noqa: F401  (adds the flags API)
        franchise_routes,  # noqa: F401  (adds Library → Collections)
        poster_routes,  # noqa: F401  (adds Library → Posters)
    )

    app.register_blueprint(bp)
    register_commands(app)
    return app
