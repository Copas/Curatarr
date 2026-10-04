"""Record the stored policy format version.

Policy values live in JSON (global setting, library policies, title
overrides). Any later change to their format ships as a data migration that
rewrites stored values and raises this version, so a stored value never
silently changes meaning.

Revision ID: c4e8a1f07d36
Revises: a9d3e7c51b20
"""

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "c4e8a1f07d36"
down_revision = "a9d3e7c51b20"
branch_labels = None
depends_on = None

KEY = "policy_schema_version"


def upgrade():
    settings = sa.table(
        "app_settings",
        sa.column("id", sa.String),
        sa.column("key", sa.String),
        sa.column("value_json", sa.JSON),
        sa.column("is_secret", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    bind = op.get_bind()
    if bind.execute(sa.select(settings.c.id).where(settings.c.key == KEY)).first():
        return
    now = datetime.now(UTC)
    op.bulk_insert(
        settings,
        [
            {
                "id": str(uuid4()),
                "key": KEY,
                "value_json": 1,
                "is_secret": False,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )


def downgrade():
    op.execute(sa.text("DELETE FROM app_settings WHERE key = :key").bindparams(key=KEY))
