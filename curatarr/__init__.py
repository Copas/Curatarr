"""Curatarr application entry point."""

import os

from flask import Flask, jsonify
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()


def create_app(test_config=None):
    from .config import configure_app

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
        instance_path=os.getenv("CURATARR_DATA_DIR"),
    )
    configure_app(app, test_config or {})
    if not app.config["ALLOW_UNAUTHENTICATED"]:
        raise RuntimeError(
            "Authentication is not implemented; unauthenticated mode must be explicitly allowed"
        )
    db.init_app(app)
    migrate.init_app(app, db)

    from . import models  # noqa: F401
    from .commands import register_commands
    from .routes import bp

    @app.before_request
    def verify_schema():
        if app.testing:
            return
        from .schema import current_schema

        if not current_schema(app):
            return jsonify(
                {
                    "error": "Database migration required",
                    "command": "flask --app curatarr db upgrade",
                }
            ), 503

    app.register_blueprint(bp)
    register_commands(app)
    return app
