"""Runtime configuration; persistent policy values live in the database."""

import os


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
        CSRF_ENABLED=_bool("CURATARR_CSRF_ENABLED", True),
        DEMO_MODE=_bool("CURATARR_DEMO_MODE", False),
        ALLOW_UNAUTHENTICATED=_bool("CURATARR_ALLOW_UNAUTHENTICATED", True),
    )
    app.config.update(overrides)
    if (
        not app.debug
        and not app.testing
        and app.config["SECRET_KEY"] == "development-only-change-me"
    ):
        app.logger.warning("Set CURATARR_SECRET_KEY before exposing this service")
