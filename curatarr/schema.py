"""Check that the running database matches the packaged migration head."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from . import db


def current_schema(app) -> bool:
    directory = Path(app.root_path).parent / "migrations"
    if not directory.is_dir():
        directory = Path.cwd() / "migrations"
    if not directory.is_dir():
        return False
    config = Config()
    config.set_main_option("script_location", str(directory))
    head = ScriptDirectory.from_config(config).get_current_head()
    try:
        actual = db.session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()
    except SQLAlchemyError:
        db.session.rollback()
        return False
    return actual == head
