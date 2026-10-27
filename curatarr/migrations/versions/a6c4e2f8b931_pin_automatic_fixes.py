"""Keep an existing installation's automatic fixes as they were.

New installations start with every automatic fix off (0.2.0rc29): problems are
listed for approval until each is switched on in Settings → Automatic. An
installation that was already running had them on by default, and keeps that:
its unset choices are written down as they behaved.

Revision ID: a6c4e2f8b931
Revises: f3a9d2c7b815
"""

import json
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "a6c4e2f8b931"
down_revision = "f3a9d2c7b815"
branch_labels = None
depends_on = None

# What each meant before, when unset.
ENHANCE = {"dub_replace_automatically": True, "file_replace_automatically": True}
AUTOMATIC = {
    "wrong_show": True,
    "missing": True,
    "misfiled": True,
    # Fixed without asking whenever the check was on.
    "default_audio": True,
    "seek": True,
}


def _load(value):
    if value is None:
        return {}
    return json.loads(value) if isinstance(value, str) else dict(value)


def upgrade():
    bind = op.get_bind()
    running = bind.execute(sa.text("SELECT COUNT(*) FROM libraries")).scalar()
    if not running:
        return  # a new installation: the new defaults apply
    now = datetime.now(UTC)
    for key, before in (("chapter_options", ENHANCE), ("auto_fixes", AUTOMATIC)):
        row = bind.execute(
            sa.text("SELECT id, value_json FROM app_settings WHERE key = :key"),
            {"key": key},
        ).first()
        stored = _load(row[1]) if row else {}
        pinned = before | stored
        if row:
            bind.execute(
                sa.text(
                    "UPDATE app_settings SET value_json = :value, updated_at = :now "
                    "WHERE id = :id"
                ).bindparams(sa.bindparam("value", type_=sa.JSON)),
                {"value": pinned, "now": now, "id": row[0]},
            )
        else:
            bind.execute(
                sa.text(
                    "INSERT INTO app_settings (id, key, value_json, is_secret, "
                    "created_at, updated_at) VALUES (:id, :key, :value, :secret, "
                    ":now, :now)"
                ).bindparams(sa.bindparam("value", type_=sa.JSON)),
                {
                    "id": str(uuid.uuid4()),
                    "key": key,
                    "value": pinned,
                    "secret": False,
                    "now": now,
                },
            )


def downgrade():
    pass
