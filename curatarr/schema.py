"""Check that the running database matches the packaged migration head."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from . import db

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# The stored policy JSON format this code understands; see the
# c4e8a1f07d36 migration. Raised only together with a data migration.
POLICY_SCHEMA_VERSION = 1


def current_schema(app) -> bool:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    head = ScriptDirectory.from_config(config).get_current_head()
    try:
        actual = db.session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()
    except SQLAlchemyError:
        db.session.rollback()
        return False
    if actual != head:
        return False
    return stored_policy_version() <= POLICY_SCHEMA_VERSION


def stored_policy_version() -> int:
    """Version recorded in the database; a newer one means newer settings."""
    try:
        value = db.session.execute(
            text(
                "SELECT value_json FROM app_settings WHERE key = 'policy_schema_version'"
            )
        ).scalar()
    except SQLAlchemyError:
        db.session.rollback()
        return 0
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return POLICY_SCHEMA_VERSION + 1  # unreadable: treat as not understood
