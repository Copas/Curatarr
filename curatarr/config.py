"""Runtime configuration; persistent policy values live in the database."""

import os
from datetime import timedelta


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


def configure_app(app, overrides: dict) -> None:
    app.config.update(
        SECRET_KEY=os.getenv("CURATARR_SECRET_KEY", "development-only-change-me"),
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///curatarr.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_bool("CURATARR_SESSION_COOKIE_SECURE", False),
        SESSION_COOKIE_PATH="/",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        CSRF_ENABLED=_bool("CURATARR_CSRF_ENABLED", True),
        DEMO_MODE=_bool("CURATARR_DEMO_MODE", False),
        ALLOW_UNAUTHENTICATED=_bool("CURATARR_ALLOW_UNAUTHENTICATED", False),
    )
    app.config.update(overrides)
    if app.config["SESSION_COOKIE_SECURE"] and "SESSION_COOKIE_NAME" not in overrides:
        # Over HTTPS the cookie's name carries the __Host- prefix: browsers then
        # accept it only from this exact host, sent securely, for the whole
        # site, so another service on the same domain can't set or replace it.
        # (Changing it signs everyone out once.)
        app.config["SESSION_COOKIE_NAME"] = "__Host-curatarr_session"
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        # The worker's background chapter jobs write while discovery holds a
        # long transaction; wait for the lock instead of failing after 5 s.
        app.config.setdefault(
            "SQLALCHEMY_ENGINE_OPTIONS", {"connect_args": {"timeout": 30}}
        )
    if (
        not app.debug
        and not app.testing
        and app.config["SECRET_KEY"] in {"development-only-change-me", "change-me"}
    ):
        raise RuntimeError("Set a stable, random CURATARR_SECRET_KEY before starting")
