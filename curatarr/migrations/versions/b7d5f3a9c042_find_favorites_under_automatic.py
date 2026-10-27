"""Find Favorites moved to Settings → Automatic: carry over a choice made on
Household choices (0.2.0rc29).

Revision ID: b7d5f3a9c042
Revises: a6c4e2f8b931
"""

import json
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "b7d5f3a9c042"
down_revision = "a6c4e2f8b931"
branch_labels = None
depends_on = None


def _load(value):
    if value is None:
        return {}
    return json.loads(value) if isinstance(value, str) else dict(value)


def upgrade():
    bind = op.get_bind()
    curation = bind.execute(
        sa.text("SELECT value_json FROM app_settings WHERE key = 'curation'")
    ).first()
    stored = _load(curation[0]) if curation else {}
    if not stored.pop("find_favourites", False):
        return  # never switched on: Automatic's default (off) applies
    now = datetime.now(UTC)
    json_param = sa.bindparam("value", type_=sa.JSON)
    bind.execute(
        sa.text(
            "UPDATE app_settings SET value_json = :value, updated_at = :now "
            "WHERE key = 'curation'"
        ).bindparams(json_param),
        {"value": stored, "now": now},
    )
    row = bind.execute(
        sa.text("SELECT id, value_json FROM app_settings WHERE key = 'auto_fixes'")
    ).first()
    fixes = (_load(row[1]) if row else {}) | {"favourites": True}
    if row:
        bind.execute(
            sa.text(
                "UPDATE app_settings SET value_json = :value, updated_at = :now "
                "WHERE id = :id"
            ).bindparams(json_param),
            {"value": fixes, "now": now, "id": row[0]},
        )
    else:
        bind.execute(
            sa.text(
                "INSERT INTO app_settings (id, key, value_json, is_secret, created_at, "
                "updated_at) VALUES (:id, 'auto_fixes', :value, :secret, :now, :now)"
            ).bindparams(json_param),
            {"id": str(uuid.uuid4()), "value": fixes, "secret": False, "now": now},
        )


def downgrade():
    pass
