"""Track when a show was reset to its always-keep minimum.

Revision ID: d2f6a8c41e93
Revises: a1e5d9c3b7f2
"""

import sqlalchemy as sa
from alembic import op

revision = "d2f6a8c41e93"
down_revision = "a1e5d9c3b7f2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "media_identities", sa.Column("viewing_reset_at", sa.DateTime(timezone=True))
    )


def downgrade():
    op.drop_column("media_identities", "viewing_reset_at")
