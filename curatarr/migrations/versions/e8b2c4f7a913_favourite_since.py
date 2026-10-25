"""When each favourite was made, for favourite ribbons.

Revision ID: e8b2c4f7a913
Revises: d4e7a1c9b352
"""

import sqlalchemy as sa
from alembic import op

revision = "e8b2c4f7a913"
down_revision = "d4e7a1c9b352"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("user_media_state", "episode_user_state"):
        op.add_column(table, sa.Column("favorite_since", sa.DateTime(timezone=True)))


def downgrade():
    for table in ("user_media_state", "episode_user_state"):
        op.drop_column(table, "favorite_since")
